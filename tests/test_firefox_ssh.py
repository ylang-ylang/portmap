"""Focused SSH ABI/installer checks; Docker integration uses only owned fixtures."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests/firefox_ssh.mjs"


def test_handshake_pinning_fixed_installer_and_eagain_cancellation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for focused SSH JS tests")
    result = subprocess.run([node, str(HARNESS), "stub"], check=True, capture_output=True,
                            text=True, timeout=30)
    evidence = json.loads(result.stdout)
    assert evidence["count"] == 6


FIXTURE = r'''
import base64, hashlib, json, os, secrets, subprocess, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
os.umask(0o077)
state = Path('/state')
state.mkdir(exist_ok=True)
Path('/run/sshd').mkdir(parents=True, exist_ok=True)
def command(*args, data=None):
    subprocess.run(args, input=data, text=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for name, kind, size in [('host', 'ecdsa', '256'), ('existing', 'rsa', '3072'), ('candidate', 'rsa', '3072')]:
    command('ssh-keygen', '-q', '-t', kind, '-b', size, '-m', 'PEM', '-N', '', '-f', str(state / name))
password = secrets.token_urlsafe(32)
command('chpasswd', data='probe:' + password + '\n')
ssh = Path('/home/probe/.ssh')
ssh.mkdir(mode=0o700, exist_ok=True)
ssh.chmod(0o700)
os.chown(ssh, 10000, 10000)
original = b'# Preserve every original byte and option.\nrestrict ' + (state / 'existing.pub').read_bytes().strip() + b'\n# no final LF'
(state / 'original').write_bytes(original)
authorized = ssh / 'authorized_keys'
authorized.write_bytes(original)
authorized.chmod(0o600)
os.chown(authorized, 10000, 10000)
host = (state / 'host.pub').read_text().split()[1]
(state / 'client.json').write_text(json.dumps({'username': 'probe', 'password': password,
    'hostKeySha256Hex': hashlib.sha256(base64.b64decode(host)).hexdigest(),
    'candidate': {'privateKey': (state / 'candidate').read_text(),
                  'publicKey': (state / 'candidate.pub').read_text().strip()}}))
config = ''' + '"""' + '''Port 2222
ListenAddress 0.0.0.0
HostKey /state/host
PidFile /run/sshd/fixture.pid
AuthorizedKeysFile .ssh/authorized_keys
PasswordAuthentication yes
PubkeyAuthentication yes
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PermitRootLogin no
UsePAM no
AllowUsers probe
AllowTcpForwarding local
PermitOpen 127.0.0.1:8080
PermitListen none
GatewayPorts no
PermitTunnel no
AllowAgentForwarding no
X11Forwarding no
MaxSessions 4
MaxAuthTries 3
LoginGraceTime 20
UseDNS no
PrintMotd no
LogLevel VERBOSE
''' + '"""' + '''
(state / 'sshd_config').write_text(config)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'owned SSH fixture')
threading.Thread(target=HTTPServer(('127.0.0.1', 8080), Handler).serve_forever, daemon=True).start()
server = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', '/state/sshd_config'])
raise SystemExit(server.wait())
'''

RESET = r'''
import os, shutil, sys
from pathlib import Path
ssh = Path('/home/probe/.ssh')
if ssh.is_symlink(): ssh.unlink()
elif ssh.exists(): shutil.rmtree(ssh)
ssh.mkdir(mode=0o700)
os.chown(ssh, 10000, 10000)
authorized = ssh / 'authorized_keys'
original = Path('/state/original').read_bytes()
authorized.write_bytes(original)
authorized.chmod(0o600)
os.chown(authorized, 10000, 10000)
case = sys.argv[1]
if case in ('symlink', 'hardlink'):
    other = Path('/home/probe/unrelated')
    if other.exists(): other.unlink()
    other.write_bytes(original)
    other.chmod(0o600); os.chown(other, 10000, 10000)
    authorized.unlink()
    if case == 'symlink': authorized.symlink_to(other)
    else: os.link(other, authorized)
elif case == 'directory':
    authorized.unlink(); authorized.mkdir(); os.chown(authorized, 10000, 10000)
elif case == 'foreign-owner':
    os.chown(authorized, 0, 0)
elif case == 'writable':
    authorized.chmod(0o620)
elif case == 'ssh-symlink':
    other = Path('/home/probe/real-ssh')
    if other.exists(): shutil.rmtree(other)
    ssh.rename(other); ssh.symlink_to(other)
elif case == 'restricted-duplicate':
    candidate = Path('/state/candidate.pub').read_bytes().strip()
    authorized.write_bytes(original + b'\nfrom="192.0.2.1",restrict ' + candidate + b' Bob\'s "laptop\n')
'''

AUDIT = r'''
import base64, hashlib, json, os
from pathlib import Path
ssh = Path('/home/probe/.ssh')
authorized = ssh / 'authorized_keys'
original = Path('/state/original').read_bytes()
candidate = Path('/state/candidate.pub').read_text().split()[1]
data = authorized.read_bytes()
print(json.dumps({'prefixPreserved': data.startswith(original + b'\n'),
    'candidateCount': sum(candidate.encode() in line and not line.lstrip().startswith(b'#') for line in data.splitlines()),
    'fileMode': authorized.stat().st_mode & 0o777, 'directoryMode': ssh.stat().st_mode & 0o777,
    'sha256': hashlib.sha256(data).hexdigest(),
    'unrelatedPreserved': not Path('/home/probe/unrelated').exists() or Path('/home/probe/unrelated').read_bytes() == original}))
'''


@pytest.mark.integration
def test_actual_sshd_preserving_install_fresh_key_auth_and_unsafe_paths():
    image = os.environ.get("PORTMAP_SSH_FIXTURE_IMAGE")
    if not image:
        pytest.skip("Set PORTMAP_SSH_FIXTURE_IMAGE to the built tests/browser/Dockerfile target image")
    runtime = os.environ.get("PORTMAP_SSH_RUNTIME_IMAGE", "emscripten/emsdk:3.1.74")
    name = "portmap-ssh-installer-" + secrets.token_hex(5)
    fixture = name + "-target"

    def docker(*args, input=None, check=True):
        __tracebackhide__ = True
        result = subprocess.run(["docker", *args], input=input, text=True, check=False,
                                capture_output=True, timeout=90)
        if check and result.returncode:
            pytest.fail("Owned SSH fixture command failed: " + result.stderr.strip(), pytrace=False)
        return result

    def remote(script, *args):
        return docker("exec", fixture, "python3", "-c", script, *args).stdout

    def run(client, **extra):
        __tracebackhide__ = True
        result = docker("run", "--rm", "--network", name, "--user", "1000:1000", "--cap-drop=ALL",
                        "--security-opt=no-new-privileges", "-v", str(ROOT) + ":/app:ro", "-w", "/app",
                        "-i", runtime, "node", "tests/firefox_ssh.mjs", "real",
                        input=json.dumps({**client, **extra}))
        return json.loads(result.stdout)

    docker("network", "create", "--internal", name)
    try:
        docker("run", "-d", "--name", fixture, "--network", name, "--network-alias", "ssh-target",
               image, "python3", "-u", "-c", FIXTURE)
        # Readiness is a bounded real SSH listen check inside the owned target.
        remote("import socket,time\nfor i in range(200):\n try:\n  s=socket.create_connection(('127.0.0.1',2222),.2);s.close();break\n except OSError: time.sleep(.05)\nelse: raise SystemExit('sshd did not become ready')")
        client = json.loads(remote("from pathlib import Path; print(Path('/state/client.json').read_text())"))
        result = run(client)
        assert result["count"] == 2
        audit = json.loads(remote(AUDIT))
        assert audit["prefixPreserved"] and audit["candidateCount"] == 1
        assert audit["fileMode"] == 0o600 and audit["directoryMode"] == 0o700
        remote(RESET, "restricted-duplicate")
        before = json.loads(remote(AUDIT))["sha256"]
        run(client, restrictedDuplicate=True)
        assert json.loads(remote(AUDIT))["sha256"] == before
        for case, expected in [("symlink", "symlink, hard link"), ("hardlink", "symlink, hard link"),
                               ("directory", "symlink, hard link"), ("foreign-owner", "symlink, hard link"),
                               ("writable", "symlink, hard link"), ("ssh-symlink", "remote .ssh directory")]:
            remote(RESET, case)
            run(client, expected=expected, case=case)
            assert remote("from pathlib import Path; p=Path('/home/probe/unrelated'); print(not p.exists() or p.read_bytes()==Path('/state/original').read_bytes())").strip() == "True"
    finally:
        docker("rm", "-f", fixture, check=False)
        docker("network", "rm", name, check=False)


def test_packaged_ssh_artifact_and_license_hashes_match_provenance():
    extension = ROOT / "extension/firefox"
    provenance = json.loads((extension / "ssh/PROVENANCE.json").read_text())
    for section in ("integrity", "licenseIntegrity"):
        for name, expected in provenance[section].items():
            assert hashlib.sha256((extension / name).read_bytes()).hexdigest() == expected, name
