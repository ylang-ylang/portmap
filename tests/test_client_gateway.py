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


@pytest.fixture
def native_traefik():
    if shutil.which("traefik") is None:
        pytest.skip("Native Traefik is required for gateway routing regression tests")


@pytest.fixture
def backend():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/registry.json" and self.headers.get("Host", "").split(":")[0] in {"remote.portmap", "127.0.0.1"}:
                payload = {"dns_domain": "remote.portmap", "http_port": self.server.server_port, "services": [], "worktrees": []}
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
    profile = {
        "domain": "remote.portmap", "target": "127.0.0.1", "via": "direct",
        "backend_url": f"http://127.0.0.1:{backend.server_port}", "remote_http_port": backend.server_port,
        "address": "127.0.0.1", "scheme": "http", "tcp_address": "127.0.0.1",
        "tcp_ports": [], "include_tcp": False,
    }
    profiles = {profile["domain"]: profile}
    save_json(tmp_path / "connections.json", {"version": 1, "connections": profiles})
    try:
        state = client_gateway.sync_gateway(tmp_path, profiles, http_port=free_port())
        yield SimpleNamespace(state_dir=tmp_path, state=state, profiles=profiles)
    finally:
        client_gateway.stop_gateway(tmp_path)


def test_native_routing_preserves_hosts_and_actions_without_intercepting_app_registry(gateway):
    port = gateway.state["http_port"]
    status, body = request(port, f"remote.portmap:{port}", "/registry.json")
    assert status == 200
    catalog = json.loads(body)
    assert catalog["http_port"] == port
    assert catalog["client_access"]["transport"] == "direct"

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


def test_hot_reload_removes_old_routes_and_keeps_empty_gateway_owned(gateway):
    initial = gateway.state
    replacement = {"other.portmap": {**gateway.profiles["remote.portmap"], "domain": "other.portmap"}}
    updated = client_gateway.sync_gateway(gateway.state_dir, replacement)
    assert updated["http_port"] == initial["http_port"]
    assert updated["traefik"]["pid"] == initial["traefik"]["pid"]
    assert request(updated["http_port"], "web.remote.portmap")[0] == 404
    assert request(updated["http_port"], "web.other.portmap")[0] == 200
    empty = client_gateway.sync_gateway(gateway.state_dir, {})
    assert empty["running"] is True
    assert request(empty["http_port"], "web.other.portmap")[0] == 404
    assert client_gateway.gateway_status(gateway.state_dir)["running"] is True


def test_gateway_survives_launcher_exit_and_can_be_stopped_by_another_process(tmp_path, native_traefik):
    environment = {**os.environ, "PYTHONPATH": str(Path(client_gateway.__file__).resolve().parent.parent)}
    program = (
        "import sys; from pathlib import Path; from portmap.client_gateway import sync_gateway; "
        "sync_gateway(Path(sys.argv[1]), {}, http_port=int(sys.argv[2]))"
    )
    try:
        subprocess.run(
            [sys.executable, "-c", program, str(tmp_path), str(free_port())],
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
    changed["traefik"] = {"pid": unrelated.pid, "start_time": identity[1], "executable": sys.executable}
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


@pytest.mark.parametrize(
    "domain,backend_url",
    [
        ("remote.portmap`) || PathPrefix(`/", "http://127.0.0.1:8080"),
        ("remote.portmap", "http://remote.example:8080"),
        ("remote.portmap", "http://user@127.0.0.1:8080"),
        ("remote.portmap", "http://127.0.0.1:8080/registry.json?command=bad"),
        ("remote.portmap", "http://[::1]:65536"),
        ("remote.portmap", "http://0.0.0.0:8080"),
    ],
)
def test_untrusted_routing_values_fail_before_creating_state(tmp_path, domain, backend_url):
    state_dir = tmp_path / "not-created"
    with pytest.raises(PortmapError):
        client_gateway.sync_gateway(state_dir, {domain: {"domain": domain, "backend_url": backend_url}})
    assert not state_dir.exists()


def test_missing_native_binary_reports_install_command_without_starting_view(tmp_path, monkeypatch):
    monkeypatch.setattr(client_gateway.shutil, "which", lambda _name: None)
    with pytest.raises(PortmapError, match="brew install traefik"):
        client_gateway.sync_gateway(tmp_path, {})
    assert not (tmp_path / "gateway.json").exists()
    assert not (tmp_path / "gateway").exists()


def test_explicit_port_collision_leaves_existing_listener_untouched(tmp_path, backend, monkeypatch):
    # No process is launched: fail at the bind even when an executable is found.
    monkeypatch.setattr(client_gateway.shutil, "which", lambda _name: sys.executable)
    with pytest.raises(PortmapError):
        client_gateway.sync_gateway(tmp_path, {}, http_port=backend.server_port)
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
            client_gateway.sync_gateway(tmp_path, {}, http_port=free_port())
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
