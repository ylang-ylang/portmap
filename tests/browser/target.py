"""Owned OpenSSH + HTTP target fixture; never a browser-side SSH helper."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import threading

import fixture
from fixture import DOMAIN, serve
from managed_fixture import ManagedAccounts

os.umask(0o077)
state, meta = Path('/state'), Path('/meta')
for directory in (state, meta):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(directory, 0, 0)
    os.chmod(directory, 0o700)
uid, gid = int(os.environ['BROWSER_UID']), int(os.environ['BROWSER_GID'])
if uid < 0 or gid < 0:
    raise ValueError('Invalid fixture metadata owner')
os.chown(meta, uid, gid)


def command(*args, stdin=None):
    subprocess.run(args, input=stdin, text=True, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def export(path, value):
    with path.open('w') as output:
        json.dump(value, output)
    os.chown(path, 0, 0)
    os.chmod(path, 0o600)
    os.chown(path, uid, gid)


host, key = state / 'host_ecdsa', state / 'user_rsa'
if not host.exists():
    command('ssh-keygen', '-q', '-t', 'ecdsa', '-b', '256', '-N', '', '-f', str(host))
if not key.exists():
    command('ssh-keygen', '-q', '-t', 'rsa', '-b', '3072', '-m', 'PEM', '-N', '', '-f', str(key))
password = secrets.token_urlsafe(32)
command('chpasswd', stdin='probe:' + password + '\n')
sshdir = Path('/home/probe/.ssh')
sshdir.mkdir(mode=0o700, parents=True, exist_ok=True)
authorized = sshdir / 'authorized_keys'
authorized.write_text(key.with_suffix('.pub').read_text())
for path, mode in ((sshdir, 0o700), (authorized, 0o600)):
    os.chown(path, 0, 0)
    os.chmod(path, mode)
    os.chown(path, 10000, 10000)
public_host = host.with_suffix('.pub').read_text().strip()
fingerprint = hashlib.sha256(base64.b64decode(public_host.split()[1])).hexdigest()
managed = ManagedAccounts(key.with_suffix('.pub').read_text())
fixture.MANAGED_CONTROL = managed.control
managed_config, managed_users = managed.configuration()
export(meta / 'client.json', {
    'domain': DOMAIN, 'sshPort': 2222, 'httpPort': 8080, 'httpsPort': 8443,
    'username': 'probe', 'password': password, 'hostKeySha256Hex': fingerprint,
    'publicKey': key.with_suffix('.pub').read_text(), 'privateKey': key.read_text(),
    'passphrase': '',
    'managedAccounts': managed.credentials,
})
config = state / 'sshd_config'
config.write_text('''Port 2222
ListenAddress 0.0.0.0
HostKey /state/host_ecdsa
PidFile /run/sshd/fixture.pid
AuthorizedKeysFile .ssh/authorized_keys
PasswordAuthentication yes
PubkeyAuthentication yes
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PermitRootLogin no
UsePAM no
AllowUsers probe ''' + managed_users + '''
AllowTcpForwarding local
PermitOpen 127.0.0.1:8080 127.0.0.1:8443
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
LogLevel DEBUG1
''' + managed_config)
threading.Thread(target=serve, daemon=True).start()
server = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(config)],
                          start_new_session=True, stderr=subprocess.PIPE)
threading.Thread(target=managed.observe_logs, args=(server.stderr,), daemon=True).start()


def shutdown(_signum, _frame):
    try:
        os.killpg(server.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
raise SystemExit(server.wait())
