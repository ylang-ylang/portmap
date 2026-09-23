import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import client_tunnel
from portmap.errors import PortmapError


@pytest.fixture
def ssh_binary():
    executable = shutil.which("ssh")
    if executable is None:
        pytest.skip("OpenSSH is required for control-protocol regression tests")
    return executable


@pytest.fixture
def replace_master(monkeypatch):
    """Replace only the connecting master with a real, owned local child."""
    real_popen = subprocess.Popen
    started = []

    def install(program, inspect=None):
        def popen(command, **kwargs):
            if command[0] != "ssh" or "-M" not in command or "-G" in command:
                return real_popen(command, **kwargs)
            controlpath = Path(command[command.index("-S") + 1])
            if inspect is not None:
                inspect(command)
            child = real_popen([sys.executable, "-c", program], **kwargs)
            started.append(SimpleNamespace(child=child, runtime=controlpath.parent))
            return child

        monkeypatch.setattr(client_tunnel.subprocess, "Popen", popen)
        return started

    yield install
    for master in started:
        if master.child.poll() is None:
            master.child.kill()
        master.child.wait(timeout=5)
        if master.runtime.exists():
            for name in ("control", "owner.json"):
                (master.runtime / name).unlink(missing_ok=True)
            master.runtime.rmdir()


@pytest.fixture
def owned_tunnel(tmp_path):
    runtime = Path(tempfile.mkdtemp(prefix=f"portmap-ssh-{os.getuid()}-", dir=Path("/tmp").resolve()))
    runtime.chmod(0o700)
    log_file = tmp_path / "ssh.log"
    log_file.write_text("retained diagnostics\n", encoding="utf-8")
    config_file = tmp_path / "ssh_config"
    config_file.write_text("Host *\n", encoding="utf-8")
    descriptor = {
        "version": 1,
        "target": "configured-alias",
        "key": "test-client",
        "controlpath": str(runtime / "control"),
        "config_file": str(config_file),
        "log_file": str(log_file),
        "owner_token": "123456789abcdef0123456789abcdef0",
    }
    ownership = runtime / "owner.json"
    ownership.write_text(json.dumps(descriptor), encoding="utf-8")
    ownership.chmod(0o600)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(descriptor["controlpath"])
    try:
        yield descriptor, listener
    finally:
        listener.close()
        if runtime.exists():
            for name in ("control", "owner.json"):
                (runtime / name).unlink(missing_ok=True)
            runtime.rmdir()


def test_failed_authentication_reports_bounded_stderr_and_reaps_child(tmp_path, replace_master):
    started = replace_master("import sys; sys.stderr.write('x' * 12000 + '\\nPermission denied (publickey).\\n'); sys.exit(255)")
    with pytest.raises(PortmapError) as error:
        client_tunnel.start_tunnel("user@configured-alias", state_dir=tmp_path, key="auth-failure")
    assert "Permission denied (publickey)" in str(error.value)
    assert len(str(error.value)) < 5000
    assert started[0].child.poll() == 255
    assert not started[0].runtime.exists()


def test_startup_timeout_reaps_only_its_owned_child(tmp_path, replace_master):
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        started = replace_master("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
        before = time.monotonic()
        with pytest.raises(PortmapError):
            client_tunnel.start_tunnel("configured-alias", state_dir=tmp_path, key="timeout", timeout=0.2)
        assert time.monotonic() - before < 5
        assert started[0].child.poll() is not None
        assert unrelated.poll() is None
        assert not started[0].runtime.exists()
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_master_config_preserves_auth_and_proxy_without_inherited_side_effects(tmp_path, replace_master, ssh_binary):
    config = tmp_path / "ssh_config"
    config.write_text(
        "Host configured-alias\n"
        " HostName 127.0.0.1\n"
        " User configured-user\n"
        " IdentityFile /explicit/user-key\n"
        " ProxyCommand exec printf proxy\n"
        " LocalForward 0.0.0.0:12345 127.0.0.1:80\n"
        " RemoteForward 12346 127.0.0.1:81\n"
        " DynamicForward 12347\n"
        " PermitLocalCommand yes\n"
        " LocalCommand touch /never-run-portmap-local-command\n"
        " RemoteCommand touch /never-run-portmap-remote-command\n"
        " RequestTTY force\n"
        " ControlMaster auto\n"
        " ControlPath /tmp/shared-user-master\n"
        " ControlPersist yes\n"
        " ForkAfterAuthentication yes\n"
        " StrictHostKeyChecking no\n",
        encoding="utf-8",
    )
    effective = {}

    def inspect(command):
        result = subprocess.run([ssh_binary, "-G", *command[1:]], capture_output=True, text=True, check=True, timeout=5)
        for line in result.stdout.splitlines():
            name, _, value = line.partition(" ")
            effective.setdefault(name, []).append(value)

    started = replace_master("raise SystemExit(255)", inspect=inspect)
    with pytest.raises(PortmapError):
        client_tunnel.start_tunnel("configured-alias", state_dir=tmp_path, key="config", config_file=config)
    assert effective["user"] == ["configured-user"]
    assert effective["identityfile"] == ["/explicit/user-key"]
    assert effective["proxycommand"] == ["exec printf proxy"]
    assert effective["controlpath"] == [str(started[0].runtime / "control")]
    assert "localforward" not in effective
    assert "remoteforward" not in effective
    assert "dynamicforward" not in effective
    for option in ("permitlocalcommand", "controlpersist", "forkafterauthentication", "requesttty"):
        assert effective[option][0] in ("no", "false")
    assert effective.get("remotecommand", ["none"]) == ["none"]
    assert effective["stricthostkeychecking"][0] in ("no", "false")


@pytest.mark.parametrize("target", ["-oProxyCommand=bad", "user@host;command", "user@-option"])
def test_target_rejects_option_and_shell_injection_before_start(tmp_path, target):
    with pytest.raises(PortmapError):
        client_tunnel.start_tunnel(target, state_dir=tmp_path / "state", key="invalid")
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("timeout", [0, float("inf"), float("nan"), True])
def test_start_rejects_unbounded_or_nonpositive_timeout(tmp_path, timeout):
    with pytest.raises(PortmapError):
        client_tunnel.start_tunnel("configured-alias", state_dir=tmp_path / "state", key="invalid", timeout=timeout)
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        {"local_address": "0.0.0.0"},
        {"local_address": "::"},
        {"local_address": "localhost"},
        {"local_port": True},
        {"local_port": 0},
        {"remote_port": 65536},
        {"remote_address": "host:80:injected"},
    ],
)
def test_forward_rejects_unsafe_endpoint_before_contacting_master(owned_tunnel, arguments):
    descriptor, listener = owned_tunnel
    listener.listen(1)
    listener.settimeout(0.02)
    endpoint = {"local_address": "127.0.0.1", "local_port": 12345, "remote_port": 8080}
    endpoint.update(arguments)
    with pytest.raises(PortmapError):
        client_tunnel.add_forward(descriptor, **endpoint)
    with pytest.raises(TimeoutError):
        listener.accept()


def test_tampered_descriptor_cannot_disconnect_master(owned_tunnel):
    descriptor, listener = owned_tunnel
    listener.listen(1)
    listener.settimeout(0.02)
    tampered = {**descriptor, "owner_token": "0" * 32}
    with pytest.raises(PortmapError):
        client_tunnel.stop_tunnel(tampered)
    with pytest.raises(TimeoutError):
        listener.accept()
    assert Path(descriptor["controlpath"]).exists()


def test_symlinked_control_path_cannot_disconnect_another_master(owned_tunnel):
    descriptor, listener = owned_tunnel
    controlpath = Path(descriptor["controlpath"])
    with tempfile.TemporaryDirectory(prefix="pm-test-", dir="/tmp") as directory:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as shared:
            shared.bind(str(Path(directory) / "shared"))
            shared.listen(1)
            shared.settimeout(0.02)
            controlpath.unlink()
            controlpath.symlink_to(Path(directory) / "shared")
            with pytest.raises(PortmapError):
                client_tunnel.stop_tunnel(descriptor)
            with pytest.raises(TimeoutError):
                shared.accept()
            assert controlpath.is_symlink()


def test_serialized_stale_tunnel_is_cleaned_idempotently(owned_tunnel, ssh_binary):
    descriptor, listener = owned_tunnel
    listener.close()
    restored = json.loads(json.dumps(descriptor))
    client_tunnel.stop_tunnel(restored)
    assert not Path(restored["controlpath"]).parent.exists()
    assert Path(restored["log_file"]).read_text(encoding="utf-8") == "retained diagnostics\n"
    assert not client_tunnel.tunnel_alive(restored)
    client_tunnel.stop_tunnel(restored)


def test_unresponsive_master_retains_ownership_and_control_socket(owned_tunnel, ssh_binary):
    descriptor, listener = owned_tunnel
    listener.listen(4)
    assert not client_tunnel.tunnel_alive(descriptor, timeout=0.1)
    with pytest.raises(PortmapError):
        client_tunnel.stop_tunnel(descriptor, timeout=0.1)
    assert Path(descriptor["controlpath"]).is_socket()
    assert (Path(descriptor["controlpath"]).parent / "owner.json").is_file()


def _receive_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        block = connection.recv(size - len(data))
        if not block:
            raise EOFError("control client closed an incomplete packet")
        data.extend(block)
    return bytes(data)


def _receive_packet(connection):
    size = struct.unpack(">I", _receive_exact(connection, 4))[0]
    if size > 65536:
        raise ValueError("oversized control packet")
    return _receive_exact(connection, size)


def _send_packet(connection, packet):
    connection.sendall(struct.pack(">I", len(packet)) + packet)


def _reply_to_mux(listener, *, error=None, remove_on_exit=None):
    """Speak only OpenSSH's documented mux v4 handshake/replies, not SSH.

    Real ssh parses arguments and formats requests, so IPv6 splitting and
    cancellation's zero exit code on a refused request are exercised here.
    No proxy or remote service is simulated.
    """
    listener.settimeout(3)
    connection, _ = listener.accept()
    with connection:
        connection.settimeout(3)
        hello = _receive_packet(connection)
        assert hello[:8] == struct.pack(">II", 1, 4)
        _send_packet(connection, struct.pack(">II", 1, 4))
        request = _receive_packet(connection)
        operation, request_id = struct.unpack(">II", request[:8])
        if error is not None:
            encoded = error.encode("utf-8")
            reply = struct.pack(">III", 0x80000003, request_id, len(encoded)) + encoded
        elif operation == 0x10000004:
            reply = struct.pack(">III", 0x80000005, request_id, os.getpid())
        else:
            reply = struct.pack(">II", 0x80000001, request_id)
        _send_packet(connection, reply)
        if remove_on_exit is not None:
            assert operation == 0x10000005
            listener.close()
            Path(remove_on_exit).unlink()
        return request


def test_serialized_live_tunnel_can_be_checked_and_stopped(owned_tunnel, ssh_binary):
    descriptor, listener = owned_tunnel
    listener.listen(4)
    restored = json.loads(json.dumps(descriptor))
    with ThreadPoolExecutor(max_workers=1) as executor:
        check = executor.submit(_reply_to_mux, listener)
        assert client_tunnel.tunnel_alive(restored)
        assert struct.unpack(">I", check.result(timeout=5)[:4])[0] == 0x10000004
        stop = executor.submit(_reply_to_mux, listener, remove_on_exit=restored["controlpath"])
        client_tunnel.stop_tunnel(restored)
        assert struct.unpack(">I", stop.result(timeout=5)[:4])[0] == 0x10000005
    assert not Path(restored["controlpath"]).parent.exists()


@pytest.mark.parametrize(
    ("operation", "request_type"),
    [(client_tunnel.add_forward, 0x10000006), (client_tunnel.cancel_forward, 0x10000007)],
)
def test_ipv6_forward_refusals_surface_without_rereading_config(owned_tunnel, ssh_binary, tmp_path, operation, request_type):
    descriptor, listener = owned_tunnel
    listener.listen(4)
    unwanted_command = tmp_path / "config-was-executed"
    Path(descriptor["config_file"]).write_text(
        f'Match exec "touch {unwanted_command}"\n User ignored\n', encoding="utf-8"
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        refused = executor.submit(_reply_to_mux, listener, error="x" * 800 + " fixture refused endpoint")
        with pytest.raises(PortmapError) as error:
            operation(
                json.loads(json.dumps(descriptor)),
                local_address="::1",
                local_port=12345,
                remote_address="2001:db8::1",
                remote_port=8080,
            )
        packet = io.BytesIO(refused.result(timeout=5))
    assert "fixture refused endpoint" in str(error.value)
    assert len(str(error.value)) < 5000
    assert not unwanted_command.exists()
    kind, _, forwarding_kind = struct.unpack(">III", packet.read(12))
    assert kind == request_type
    assert forwarding_kind == 1
    local_size = struct.unpack(">I", packet.read(4))[0]
    local_address = packet.read(local_size).decode()
    local_port = struct.unpack(">I", packet.read(4))[0]
    remote_size = struct.unpack(">I", packet.read(4))[0]
    remote_address = packet.read(remote_size).decode()
    remote_port = struct.unpack(">I", packet.read(4))[0]
    assert (local_address, local_port, remote_address, remote_port) == ("::1", 12345, "2001:db8::1", 8080)
