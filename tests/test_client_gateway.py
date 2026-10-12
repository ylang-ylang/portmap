import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import client_gateway
from portmap.errors import PortmapError


def request(port, host, path="/", *, method="GET", body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers={"Host": host})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def save_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def ssh_profile(domain, backend_port, **changes):
    return {
        "domain": domain, "target": "configured-alias", "via": "ssh",
        "backend_url": f"http://127.0.0.1:{backend_port}", "remote_http_port": backend_port,
        "address": "127.0.0.1", "http_port": 18080, "scheme": "http", "tcp_address": "127.77.0.1",
        "tcp_ports": [], "include_tcp": False, **changes,
    }


def local_profile(domain, backend_port, public_port):
    return {
        "domain": domain, "via": "local", "target": "local",
        "backend_url": f"http://127.0.0.1:{backend_port}", "remote_http_port": public_port,
        "http_port": public_port, "address": "127.0.0.1", "scheme": "http",
        "tcp_address": "127.0.0.1", "tcp_ports": [], "include_tcp": False,
    }


@pytest.fixture
def native_traefik():
    if shutil.which("traefik") is None:
        pytest.skip("Native Traefik is required for gateway routing regression tests")


@pytest.fixture
def backend():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/registry.json" and self.headers.get("Host", "").split(":")[0] in {"remote.portmap", "local.portmap", "127.0.0.1"}:
                payload = {"dns_domain": self.server.dns_domain, "http_port": self.server.server_port, "services": [], "worktrees": []}
            else:
                payload = {"host": self.headers.get("Host"), "path": self.path, "method": "GET"}
            self.send_json(payload)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
            self.send_json({"host": self.headers.get("Host"), "path": self.path, "method": "POST", "body": body})

        def send_json(self, value):
            body = json.dumps(value).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.dns_domain = "remote.portmap"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def gateway(tmp_path, backend, native_traefik):
    profile = ssh_profile("remote.portmap", backend.server_port)
    profiles = {profile["domain"]: profile}
    save_json(tmp_path / "connections.json", {"version": 1, "connections": profiles})
    try:
        state = client_gateway.sync_gateway(tmp_path, profiles, http_port=free_port())
        yield SimpleNamespace(state_dir=tmp_path, state=state, profiles=profiles)
    finally:
        client_gateway.stop_gateway(tmp_path)


@pytest.fixture
def local_gateway(tmp_path, backend, native_traefik):
    backend.dns_domain = "local.portmap"
    public = free_port()
    profile = local_profile("local.portmap", backend.server_port, public)
    try:
        state = client_gateway.sync_gateway(tmp_path, {profile["domain"]: profile}, http_port=public)
        yield SimpleNamespace(state_dir=tmp_path, state=state, profiles={profile["domain"]: profile})
    finally:
        client_gateway.stop_gateway(tmp_path)


def test_native_routing_preserves_hosts_and_actions_without_intercepting_app_registry(gateway):
    port = gateway.state["http_port"]
    status, body = request(port, f"remote.portmap:{port}", "/registry.json")
    assert status == 200
    catalog = json.loads(body)
    assert catalog["http_port"] == port
    assert catalog["client_access"]["transport"] == "ssh"

    app_host = f"web.dev.sample.remote.portmap:{port}"
    status, body = request(port, app_host, "/registry.json")
    assert status == 200
    assert json.loads(body) == {"host": app_host, "path": "/registry.json", "method": "GET"}

    action_host = f"remote.portmap:{port}"
    status, body = request(port, action_host, "/actions/start?branch=dev", method="POST", body='{"repo":"sample"}')
    assert status == 200
    assert json.loads(body) == {
        "host": action_host, "path": "/actions/start?branch=dev", "method": "POST", "body": '{"repo":"sample"}',
    }
    for host in ("notremote.portmap", "web.remote.portmap.attacker.invalid", "unknown.portmap"):
        assert request(port, host, "/registry.json")[0] == 404
    assert request(port, "remote.portmap", "/api/rawdata")[0] == 404


def test_local_role_proxies_catalog_actions_and_bare_ip_straight_to_backend(local_gateway):
    port = local_gateway.state["http_port"]
    # The local backend owns its whole domain: the apex catalog is its own,
    # never a read-only client view.
    status, body = request(port, f"local.portmap:{port}", "/registry.json")
    assert status == 200
    assert json.loads(body)["dns_domain"] == "local.portmap"
    status, body = request(port, f"local.portmap:{port}", "/actions/compose-up", method="POST", body="{}")
    assert status == 200
    assert json.loads(body)["method"] == "POST"
    # Bare-IP and localhost requests fall back to the local backend privately.
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        status, body = request(port, host, "/")
        assert status == 200
        assert json.loads(body)["path"] == "/"
    # Unrelated hosts stay unrouted, and app subdomains keep their Host.
    assert request(port, "unknown.portmap", "/")[0] == 404
    app_host = f"web.local.portmap:{port}"
    status, body = request(port, app_host, "/some/app/path")
    assert status == 200
    assert json.loads(body) == {"host": app_host, "path": "/some/app/path", "method": "GET"}


def test_descriptor_separates_public_port_from_private_listener(gateway):
    state = client_gateway._load(gateway.state_dir)
    assert state["version"] == 2
    assert state["http_port"] == gateway.state["http_port"]
    assert state["listen_port"] != state["view_port"] != state["management_port"]
    assert state["listen_port"] >= 1024
    assert state["ingress"] is None
    status = client_gateway.gateway_status(gateway.state_dir)
    assert status["running"] is True
    assert status["listen_port"] == state["listen_port"]


def test_hot_reload_removes_old_routes(local_gateway, backend, native_traefik):
    initial = local_gateway.state
    replacement = {"other.portmap": {**ssh_profile("other.portmap", backend.server_port)}}
    updated = client_gateway.sync_gateway(local_gateway.state_dir, replacement)
    assert updated["http_port"] == initial["http_port"]
    assert updated["traefik"]["pid"] == initial["traefik"]["pid"]
    assert request(updated["http_port"], "web.local.portmap")[0] == 404
    assert request(updated["http_port"], "web.other.portmap")[0] == 200
    # With no SSH/local profile left, the owned gateway stops entirely; the
    # descriptor is removed and every listener is closed.
    stopped = client_gateway.sync_gateway(local_gateway.state_dir, {})
    assert stopped["running"] is False
    assert stopped["http_port"] is None
    assert client_gateway.gateway_status(local_gateway.state_dir)["running"] is False
    assert not (local_gateway.state_dir / "gateway.json").exists()
    for port in (initial["http_port"], initial["listen_port"], initial["view_port"], initial["management_port"]):
        with pytest.raises(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass


def test_direct_only_profiles_need_no_gateway_or_traefik(tmp_path, monkeypatch):
    def unreachable(*_args, **_kwargs):
        pytest.fail("direct-only connections must never require the native Traefik binary")

    monkeypatch.setattr(client_gateway, "find_native_binary", unreachable)
    status = client_gateway.sync_gateway(tmp_path, {"remote.portmap": {"domain": "remote.portmap", "via": "direct", "backend_url": "http://192.0.2.10:8080"}})
    assert status == {"running": False, "address": "127.0.0.1", "http_port": None, "listen_port": None, "view_port": None, "management_port": None, "ingress": None}
    assert not (tmp_path / "gateway.json").exists()
    assert not (tmp_path / "gateway").exists()


def test_preferred_public_port_roundtrip(tmp_path):
    assert client_gateway.preferred_http_port(tmp_path) is None
    client_gateway.save_public_http_port(tmp_path, 9090)
    assert client_gateway.preferred_http_port(tmp_path) == 9090
    assert (tmp_path / "settings.json").stat().st_mode & 0o077 == 0
    save_json(tmp_path / "settings.json", {"version": 1, "public_http_port": 70000})
    with pytest.raises(PortmapError):
        client_gateway.preferred_http_port(tmp_path)


def test_gateway_survives_launcher_exit_and_can_be_stopped_by_another_process(tmp_path, native_traefik, backend):
    environment = {**os.environ, "PYTHONPATH": str(Path(client_gateway.__file__).resolve().parent.parent)}
    program = (
        "import sys; from pathlib import Path; from portmap.client_gateway import sync_gateway; "
        "sync_gateway(Path(sys.argv[1]), "
        "{'remote.portmap': {'domain': 'remote.portmap', 'via': 'ssh', 'backend_url': 'http://127.0.0.1:' + sys.argv[3]}}, "
        "http_port=int(sys.argv[2]))"
    )
    try:
        subprocess.run(
            [sys.executable, "-c", program, str(tmp_path), str(free_port()), str(backend.server_port)],
            cwd=tmp_path, env=environment, capture_output=True, text=True, check=True, timeout=30,
        )
        state = client_gateway.gateway_status(tmp_path)
        assert state["running"] is True
        client_gateway.stop_gateway(tmp_path)
        assert client_gateway.gateway_status(tmp_path)["running"] is False
        for port in (state["http_port"], state["view_port"], state["management_port"]):
            with pytest.raises(OSError):
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass
    finally:
        client_gateway.stop_gateway(tmp_path)


def test_live_port_change_is_rejected_without_disturbing_existing_gateway(gateway):
    with pytest.raises(PortmapError):
        client_gateway.sync_gateway(gateway.state_dir, {}, http_port=free_port())
    status, body = request(gateway.state["http_port"], "app.remote.portmap")
    assert status == 200
    assert json.loads(body)["host"] == "app.remote.portmap"


def test_publication_failure_rolls_back_routes_already_loaded_by_traefik(gateway, monkeypatch):
    original_save = client_gateway._save
    failed = False

    def fail_commit(state_dir, state, **kwargs):
        nonlocal failed
        if state["revision"] != gateway.state["revision"] and not failed:
            failed = True
            raise OSError("simulated descriptor write failure")
        return original_save(state_dir, state, **kwargs)

    monkeypatch.setattr(client_gateway, "_save", fail_commit)
    replacement = {"other.portmap": {**gateway.profiles["remote.portmap"], "domain": "other.portmap"}}
    with pytest.raises(PortmapError):
        client_gateway.sync_gateway(gateway.state_dir, replacement)
    assert failed
    status = client_gateway.gateway_status(gateway.state_dir)
    assert status["running"] is True
    assert status["revision"] == gateway.state["revision"]
    assert request(status["http_port"], "web.remote.portmap")[0] == 200
    assert request(status["http_port"], "web.other.portmap")[0] == 404


def test_stop_refuses_tampered_ownership_record(gateway):
    descriptor_path = gateway.state_dir / "gateway.json"
    original = json.loads(descriptor_path.read_text())
    changed = deepcopy(original)
    changed["traefik"]["pid"] = os.getpid()
    save_json(descriptor_path, changed)
    try:
        assert client_gateway.gateway_status(gateway.state_dir)["running"] is False
        with pytest.raises(PortmapError):
            client_gateway.stop_gateway(gateway.state_dir)
        assert request(original["http_port"], "web.remote.portmap")[0] == 200
    finally:
        save_json(descriptor_path, original)


def test_stop_does_not_signal_foreign_pid_even_with_copied_start_identity(gateway):
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    descriptor_path = gateway.state_dir / "gateway.json"
    original = json.loads(descriptor_path.read_text())
    marker_path = Path(original["instance_dir"]) / "owner.json"
    original_marker = json.loads(marker_path.read_text())
    changed = deepcopy(original)
    identity = client_gateway._process_identity(unrelated.pid)
    assert identity is not None
    changed["traefik"] = {"pid": unrelated.pid, "start_time": identity[1], "command": original["traefik"]["command"]}
    save_json(descriptor_path, changed)
    save_json(marker_path, {key: value for key, value in changed.items() if key != "revision"})
    try:
        client_gateway.stop_gateway(gateway.state_dir)
        assert unrelated.poll() is None
        assert client_gateway._process_identity(original["traefik"]["pid"]) is not None
    finally:
        save_json(descriptor_path, original)
        save_json(marker_path, original_marker)
        unrelated.terminate()
        unrelated.wait(timeout=5)


def craft_descriptor(state_dir, *, version=1, ingress=None, traefik_pid=999999):
    owner = "a" * 32
    runtime = state_dir / "gateway" / owner
    (runtime / "dynamic").mkdir(parents=True, mode=0o700)
    runtime.parent.chmod(0o700)
    runtime.chmod(0o700)
    (runtime / "dynamic").chmod(0o700)
    state = {
        "version": version, "owner": owner, "uid": os.getuid(), "state_dir": str(state_dir),
        "address": "127.0.0.1", "http_port": 18080,
        "view_port": 38081, "management_port": 38082,
        "instance_dir": str(runtime), "config_path": str(runtime / "traefik.yaml"),
        "dynamic_path": str(runtime / "dynamic" / "routes.yaml"),
        "traefik": {"pid": traefik_pid, "start_time": "0", "command": ["/usr/bin/traefik", f"--configFile={runtime / 'traefik.yaml'}"]},
        "view": {"pid": 999999 - 1, "start_time": "0", "command": [sys.executable, "-m", "portmap.client_view", "--state-dir", str(state_dir), "--port", "38081", "--owner", owner]},
        "revision": "b" * 64,
    }
    if version == 2:
        state["listen_port"] = 38083
        state["ingress"] = ingress
    save_json(runtime / "owner.json", {key: value for key, value in state.items() if key != "revision"})
    save_json(runtime / "dynamic" / "routes.yaml", {"http": {"routers": {}, "services": {}}})
    save_json(state_dir / "gateway.json", state)
    return state


def test_legacy_descriptor_is_accepted_then_stopped_without_signaling_anything(tmp_path):
    # The removed standalone layout must remain loadable so migration can stop
    # only verified owned workers; here the recorded identity is stale, so no
    # process is signaled and the descriptor is simply discarded.
    craft_descriptor(tmp_path, version=1)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    descriptor = json.loads((tmp_path / "gateway.json").read_text())
    marker_path = Path(descriptor["instance_dir"]) / "owner.json"
    descriptor["traefik"]["pid"] = unrelated.pid
    save_json(tmp_path / "gateway.json", descriptor)
    save_json(marker_path, {key: value for key, value in descriptor.items() if key != "revision"})
    try:
        client_gateway.stop_gateway(tmp_path)
        assert unrelated.poll() is None
        assert not (tmp_path / "gateway.json").exists()
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_descriptor_rejects_mismatched_private_ingress_record(tmp_path):
    craft_descriptor(tmp_path, version=2, ingress={"name": "private", "port": 18080, "target_port": 9999})
    with pytest.raises(PortmapError, match="ingress"):
        client_gateway.stop_gateway(tmp_path)


def test_valid_version_two_descriptor_without_processes_reports_stopped(tmp_path):
    craft_descriptor(tmp_path, version=2, ingress=None)
    status = client_gateway.gateway_status(tmp_path)
    assert status["running"] is False
    assert status["http_port"] == 18080
    assert status["listen_port"] == 38083
    client_gateway.stop_gateway(tmp_path)
    assert not (tmp_path / "gateway.json").exists()


@pytest.mark.parametrize(
    "domain,backend_url",
    [
        ("remote.portmap`) || PathPrefix(`/", "http://127.0.0.1:8080"),
        ("remote.portmap", "http://remote.example:8080"),
        ("remote.portmap", "http://user@127.0.0.1:8080"),
        ("remote.portmap", "http://127.0.0.1:8080/registry.json?command=bad"),
        ("remote.portmap", "http://[::1]:65536"),
        ("remote.portmap", "http://0.0.0.0:8080"),
        ("local.portmap", "http://192.0.2.10:8080"),
    ],
)
def test_untrusted_routing_values_fail_before_creating_state(tmp_path, domain, backend_url):
    state_dir = tmp_path / "not-created"
    via = "local" if domain == "local.portmap" else "ssh"
    with pytest.raises(PortmapError):
        client_gateway.sync_gateway(state_dir, {domain: {"domain": domain, "via": via, "backend_url": backend_url}})
    assert not state_dir.exists()


def test_missing_native_binary_reports_install_command_without_starting_view(tmp_path, monkeypatch):
    monkeypatch.setattr(client_gateway, "find_native_binary", lambda _name: None)
    with pytest.raises(PortmapError, match="brew install traefik"):
        client_gateway.sync_gateway(tmp_path, {"remote.portmap": ssh_profile("remote.portmap", 8080)})
    assert not (tmp_path / "gateway.json").exists()
    assert not (tmp_path / "gateway").exists()


def test_explicit_port_collision_leaves_existing_listener_untouched(tmp_path, backend, monkeypatch):
    # No process is launched: fail at the bind even when an executable is found.
    monkeypatch.setattr(client_gateway, "find_native_binary", lambda _name: Path(sys.executable))
    with pytest.raises(PortmapError):
        client_gateway.sync_gateway(tmp_path, {"remote.portmap": ssh_profile("remote.portmap", backend.server_port)}, http_port=backend.server_port)
    assert request(backend.server_port, "existing-listener")[0] == 200
    assert not (tmp_path / "gateway.json").exists()
    assert not (tmp_path / "gateway").exists()


def test_native_start_failure_reaps_both_children_and_retains_private_diagnostics(tmp_path, native_traefik, monkeypatch):
    original_config = client_gateway._static_config
    original_launch = client_gateway._launch
    children = []

    def invalid_config(state):
        config = original_config(state)
        config["entryPoints"]["web"]["address"] = "invalid-listen-address"
        return config

    def record_launch(state, kind, executable, started):
        try:
            return original_launch(state, kind, executable, started)
        finally:
            children[:] = started

    monkeypatch.setattr(client_gateway, "_static_config", invalid_config)
    monkeypatch.setattr(client_gateway, "_launch", record_launch)
    try:
        with pytest.raises(PortmapError):
            client_gateway.sync_gateway(tmp_path, {"remote.portmap": ssh_profile("remote.portmap", 8080)}, http_port=free_port())
        assert len(children) == 2
        assert all(child.poll() is not None for child in children)
        assert not (tmp_path / "gateway.json").exists()
        logs = list((tmp_path / "gateway").glob("*/traefik.log"))
        assert len(logs) == 1
        assert logs[0].stat().st_mode & 0o077 == 0
        assert "invalid-listen-address" in logs[0].read_text()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_privileged_public_port_is_owned_by_private_ingress(tmp_path, backend, native_traefik, monkeypatch):
    """A privileged public port binds Traefik unprivileged and forwards through
    the private ingress socket; the recorded ingress ties port to listener."""
    ensured = {}

    def fake_ensure(state_dir, *, name, bind_address, port, target_port, use_sudo=True):
        ensured.update(name=name, bind_address=bind_address, port=port, target_port=target_port, use_sudo=use_sudo)
        return {"name": name, "port": port, "target_port": target_port, "bind_address": bind_address}

    stopped = {}

    def fake_stop(state_dir, *, name, use_sudo=True):
        stopped.update(name=name, called=True)

    monkeypatch.setattr(client_gateway, "_ensure_ingress", fake_ensure)
    monkeypatch.setattr(client_gateway, "_stop_ingress", fake_stop)
    monkeypatch.setattr(client_gateway, "_START_TIMEOUT", 0.5)
    try:
        with pytest.raises(PortmapError):
            client_gateway.sync_gateway(
                tmp_path, {"remote.portmap": ssh_profile("remote.portmap", backend.server_port)},
                http_port=80, use_sudo=False,
            )

        # The ingress was asked for the public port and the unprivileged
        # private listener; no real socket on 80 exists in this environment,
        # so the public readiness probe fails and everything rolls back.
        assert (ensured["name"], ensured["bind_address"], ensured["port"], ensured["use_sudo"]) == ("private", "127.0.0.1", 80, False)
        assert ensured["target_port"] >= 1024 and ensured["target_port"] != 80
        assert stopped.get("name") == "private"
        assert not (tmp_path / "gateway.json").exists()
    finally:
        client_gateway.stop_gateway(tmp_path)


def test_ingress_port_conflict_is_an_error_without_ingress_or_children(tmp_path, backend, native_traefik, monkeypatch):
    def refused(state_dir, *, name, bind_address, port, target_port, use_sudo=True):
        raise PortmapError(f"port {port} is already in use by another service")

    def must_not_stop(*_args, **_kwargs):
        pytest.fail("an ingress that was never installed must not be stopped")

    monkeypatch.setattr(client_gateway, "_ensure_ingress", refused)
    monkeypatch.setattr(client_gateway, "_stop_ingress", must_not_stop)
    try:
        with pytest.raises(PortmapError, match="already in use"):
            client_gateway.sync_gateway(tmp_path, {"remote.portmap": ssh_profile("remote.portmap", backend.server_port)}, http_port=80)
        assert not (tmp_path / "gateway.json").exists()

    finally:
        client_gateway.stop_gateway(tmp_path)


def test_ownership_verifies_recorded_worker_command_not_current_mode(gateway, monkeypatch):
    state = client_gateway._load(gateway.state_dir)
    assert state["view"]["command"][1:3] == ["-m", "portmap.client_view"]
    # A frozen portmap executable must still recognize a source-mode gateway,
    # and vice versa: only the recorded argv may be compared against the process.
    monkeypatch.setattr(client_gateway, "frozen", lambda: True)
    monkeypatch.setattr(
        client_gateway, "serve_catalog_command",
        lambda **kwargs: [sys.executable, "_serve-catalog", "--state-dir", kwargs["state_dir"], "--port", str(kwargs["port"]), "--owner", kwargs["owner"]],
    )
    assert client_gateway._owned_process(client_gateway._load(gateway.state_dir), "view") is True
    assert client_gateway.gateway_status(gateway.state_dir)["running"] is True


def test_descriptor_accepts_frozen_worker_command_shape(gateway):
    descriptor_path = gateway.state_dir / "gateway.json"
    original = json.loads(descriptor_path.read_text())
    marker_path = Path(original["instance_dir"]) / "owner.json"
    original_marker = json.loads(marker_path.read_text())
    frozen_form = [sys.executable, "_serve-catalog", "--state-dir", original["state_dir"], "--port", str(original["view_port"]), "--owner", original["owner"]]
    changed = deepcopy(original)
    changed["view"] = {**original["view"], "command": frozen_form}
    save_json(descriptor_path, changed)
    save_json(marker_path, {key: value for key, value in changed.items() if key != "revision"})
    try:
        # _load validates the whole descriptor against the marker: accepting it
        # proves the frozen argv is a valid recorded worker command, while the
        # live source-mode child (different argv) is correctly reported as not owned.
        assert client_gateway._load(gateway.state_dir)["view"]["command"] == frozen_form
        assert client_gateway._valid_recorded_command(changed, "view", frozen_form) is True
        assert client_gateway._owned_process(changed, "view") is False
        assert client_gateway.gateway_status(gateway.state_dir)["running"] is False
    finally:
        save_json(descriptor_path, original)
        save_json(marker_path, original_marker)


def test_reservation_reuses_a_closed_listener_after_server_active_close():
    # Leave a real TCP connection in TIME_WAIT on the server's port.
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as peer:
            accepted, _ = server.accept()
            accepted.shutdown(socket.SHUT_WR)
            assert peer.recv(1) == b""
        accepted.close()
    with client_gateway._reserve(port) as reserved:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            connection, _ = reserved.accept()
            connection.close()
