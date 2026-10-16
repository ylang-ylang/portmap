"""Update control: capability pairing, signed remote updates, durable batches."""
import http.client
import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from portmap import update_control
from portmap.catalog_access import MachineAccess
from portmap.errors import PortmapError


class FakeEngine:
    """Contract-shaped stand-in for the target updater module."""

    def __init__(self):
        self.starts: list[tuple[str, str]] = []
        self.launches: list[tuple[str, str]] = []
        self.jobs: dict[str, dict] = {}
        self.latest_version = "0.16.0"
        self.fail_start_for: set[str] = set()

    def local_status(self, *, state_dir, check=False):
        return {
            "current_version": "0.15.0",
            "installed_version": "0.15.0",
            "build_id": "build-abc123",
            "latest_version": self.latest_version if check else None,
            "install_method": "brew",
            "update_available": bool(self.latest_version),
            "can_update": True,
            "reason": None,
            "active_job": None,
            "last_job": None,
        }

    def start_update(self, version, request_id, *, state_dir):
        machine = request_id.rsplit(":", 1)[-1]
        if machine in self.fail_start_for:
            raise PortmapError("an update is already running")
        if request_id in self.jobs:
            if self.jobs[request_id]["target_version"] != version:
                raise PortmapError("request id already belongs to another release")
            return self.jobs[request_id]
        job = {
            "id": f"job-{len(self.jobs) + 1}",
            "request_id": request_id,
            "state": "succeeded",
            "version_before": "0.15.0",
            "target_version": version,
            "version_after": version,
            "created_at": "2026-10-17T00:00:00+00:00",
            "updated_at": "2026-10-17T00:00:01+00:00",
            "message": "installed",
            "log_tail": "install log line",
            "error": None,
        }
        self.jobs[request_id] = job
        self.starts.append((version, request_id))
        return job

    def get_job(self, job_id, *, state_dir):
        for job in self.jobs.values():
            if job["id"] == job_id:
                return job
        raise PortmapError("unknown job")

    def launch_durable_worker(self, kind, identifier, *, state_dir):
        self.launches.append((kind, identifier))

@pytest.fixture(autouse=True)
def missing_agent_socket(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(tmp_path / "missing-agent.sock"))
    monkeypatch.setenv("PORTMAP_AGENT_RUNTIME_DIR", str(tmp_path / "missing-runtime"))


@pytest.fixture
def engine(monkeypatch, state_dir):
    from portmap import agent

    fake = FakeEngine()
    monkeypatch.setattr(update_control, "_engine_module", lambda: fake)
    runtime = state_dir.parent / "agent-runtime"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "agent.sock"
    monkeypatch.setenv("PORTMAP_AGENT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(state_dir))
    server = agent.ThreadingUnixHTTPServer(str(socket_path), agent.AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        socket_path.unlink(missing_ok=True)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "client"
    directory.mkdir(mode=0o700)
    return directory


def machine(machine_id: str, backend_url: str | None, *, via: str = "direct") -> MachineAccess:
    return MachineAccess(
        id=machine_id,
        domain=machine_id,
        via=via,
        target=f"user@{machine_id}" if via == "ssh" else None,
        address="127.0.0.1" if backend_url else None,
        http_port=80,
        remote_http_port=80,
        backend_url=backend_url,
        tcp_address=None,
        tcp_ports=(),
        include_tcp=False,
        available=backend_url is not None,
    )


def use_snapshot(monkeypatch, machines: dict[str, MachineAccess], domain: str = "laptop.portmap"):
    snapshot = SimpleNamespace(
        local=SimpleNamespace(domain=domain, http_port=80, enabled=True),
        machines=machines,
    )
    monkeypatch.setattr(update_control, "_host_snapshot_or_local_only", lambda state_dir: snapshot)
    return snapshot


class RemoteTargetHandler(BaseHTTPRequestHandler):
    """A real remote target: the update API backed by update_control itself."""

    server_version = "portmap-catalog/0.16.0"
    scope = "private"
    state_dir: Path = None  # type: ignore[assignment]
    tamper_response_signature = False
    strip_response_signature = False

    def _relay(self, method: str) -> None:
        length = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        status, body, headers = update_control.handle_remote_request(
            method=method,
            path=self.path,
            body=raw,
            scope=self.scope,
            signature=self.headers.get(update_control.SIGNATURE_HEADER),
            timestamp=self.headers.get(update_control.TIMESTAMP_HEADER),
            nonce=self.headers.get(update_control.NONCE_HEADER),
            state_dir=self.state_dir,
        )
        if self.strip_response_signature:
            headers = {key: value for key, value in headers.items() if key != update_control.RESPONSE_SIGNATURE_HEADER}
        if self.tamper_response_signature and update_control.RESPONSE_SIGNATURE_HEADER in headers:
            headers[update_control.RESPONSE_SIGNATURE_HEADER] = "0" * 64
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._relay("GET")

    def do_POST(self) -> None:
        self._relay("POST")

    def log_message(self, fmt, *args) -> None:
        pass


class LegacyTargetHandler(BaseHTTPRequestHandler):
    """A pre-update-API remote: only its build identity is observable."""

    server_version = "portmap-catalog/0.14.2"

    def _missing(self) -> None:
        body = b'{"error": "not found"}\n'
        self.send_response(404)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._missing()

    def do_POST(self) -> None:
        self._missing()

    def log_message(self, fmt, *args) -> None:
        pass


class VersionOnlyTargetHandler(BaseHTTPRequestHandler):
    """A current remote without a stored capability: minimal version only."""

    server_version = "portmap-catalog/0.16.0"

    def do_GET(self) -> None:
        if self.path == "/api/version":
            body = json.dumps({"version": "0.16.0", "build_id": "build-def456", "update_api": True}).encode()
            self.send_response(200)
        else:
            body = b'{"error": "authentication required"}\n'
            self.send_response(401)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        body = b'{"error": "authentication required"}\n'
        self.send_response(401)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:
        pass


class serving_http:
    def __init__(self, handler_class) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "serving_http":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def remote_target(state_dir: Path, *, scope: str = "private") -> serving_http:
    handler = type(
        "BoundTargetHandler",
        (RemoteTargetHandler,),
        {"scope": scope, "state_dir": state_dir},
    )
    service = serving_http(handler)
    service.state_dir = state_dir
    return service


def dead_worker_pid() -> int:
    """A real finished pid: guaranteed dead and inside the pid namespace."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait(timeout=10)
    return child.pid


# ---------------------------------------------------------------------------
# Capability pairing


def test_local_capability_is_created_once_and_kept_private(state_dir: Path):
    first = update_control.local_capability(state_dir=state_dir)
    second = update_control.local_capability(state_dir=state_dir)
    assert first == second
    assert first.startswith("pm1.") and len(first) == len("pm1.") + 64
    capability_file = state_dir / "updates" / "capability.json"
    assert capability_file.stat().st_mode & 0o777 == 0o600
    assert json.loads(capability_file.read_text())["token"] == first


def test_reveal_keeps_local_key_and_cannot_grant_reciprocal_access(state_dir):
    original = update_control.local_capability(state_dir=state_dir)
    result = update_control.reveal_local_capability({}, state_dir=state_dir)
    assert result["token"] == original
    with pytest.raises(PortmapError):
        update_control.reveal_local_capability({"machine": "desk.portmap"}, state_dir=state_dir)
    assert update_control._load_credentials(state_dir) == {}


def test_pairing_is_refused_outside_the_private_tunnel(state_dir: Path):
    status, body, _headers = update_control.handle_remote_request(
        method="POST",
        path="/api/update-agent/pair",
        body=b"{}",
        scope="public",
        state_dir=state_dir,
    )
    assert status == 403


def test_pairing_cannot_replace_an_existing_remote_credential(state_dir: Path):
    original = update_control._new_capability()
    update_control._store_credential("laptop.portmap", original, state_dir=state_dir)
    for payload in (
        {"token": "not-a-token", "manager": "laptop.portmap"},
        {"token": update_control._new_capability(), "manager": "evil machine"},
        {"token": update_control._new_capability()},
    ):
        status, body, _headers = update_control.handle_remote_request(
            method="POST",
            path="/api/update-agent/pair",
            body=json.dumps(payload).encode(),
            scope="private",
            state_dir=state_dir,
        )
        assert status == 400, body
        assert update_control.credential_for("laptop.portmap", state_dir=state_dir) == original


def test_store_remote_credential_verifies_possession_before_storing(state_dir, monkeypatch):
    remote_state = state_dir / "pair-remote"
    remote_state.mkdir(mode=0o700)
    with remote_target(remote_state) as remote:
        use_snapshot(monkeypatch, {"desk.portmap": machine("desk.portmap", remote.url)})
        good = update_control.local_capability(state_dir=remote.state_dir)
        result = update_control.store_remote_credential(
            {"machine": "desk.portmap", "token": good}, state_dir=state_dir
        )
        assert result["ok"] is True
        assert update_control.credential_for("desk.portmap", state_dir=state_dir) == good
        with pytest.raises(PortmapError):
            update_control.store_remote_credential(
                {"machine": "desk.portmap", "token": update_control._new_capability()},
                state_dir=state_dir,
            )


def test_store_remote_credential_rejects_unknown_machine(state_dir, monkeypatch):
    use_snapshot(monkeypatch, {})
    with pytest.raises(PortmapError):
        update_control.store_remote_credential(
            {"machine": "ghost.portmap", "token": update_control._new_capability()},
            state_dir=state_dir,
        )


# ---------------------------------------------------------------------------
# Signed request/response authentication


def signed_headers(
    capability: str, method: str, path: str, body: bytes, *, nonce: str | None = None, timestamp: str | None = None
):
    nonce = nonce or uuid.uuid4().hex
    timestamp = timestamp or str(int(time.time()))
    signature = update_control.request_mac(capability, method, path, body, timestamp, nonce)
    return {
        update_control.SIGNATURE_HEADER: signature,
        update_control.TIMESTAMP_HEADER: timestamp,
        update_control.NONCE_HEADER: nonce,
    }, timestamp, signature


def test_signed_request_roundtrip_and_replay_rejection(state_dir: Path):
    capability = update_control.local_capability(state_dir=state_dir)
    headers, _timestamp, _signature = signed_headers(capability, "GET", "/api/update-agent/version", b"")
    update_control.verify_signed_request(
        method="GET",
        path="/api/update-agent/version",
        body=b"",
        timestamp=headers[update_control.TIMESTAMP_HEADER],
        nonce=headers[update_control.NONCE_HEADER],
        signature=headers[update_control.SIGNATURE_HEADER],
        capability=capability,
        state_dir=state_dir,
    )
    with pytest.raises(update_control.RequestAuthError) as replayed:
        update_control.verify_signed_request(
            method="GET",
            path="/api/update-agent/version",
            body=b"",
            timestamp=headers[update_control.TIMESTAMP_HEADER],
            nonce=headers[update_control.NONCE_HEADER],
            signature=headers[update_control.SIGNATURE_HEADER],
            capability=capability,
            state_dir=state_dir,
        )
    assert replayed.value.status == 403


def test_signed_request_rejects_forged_stale_and_malformed(state_dir: Path):
    capability = update_control.local_capability(state_dir=state_dir)
    headers, timestamp, signature = signed_headers(capability, "POST", "/api/update-agent/start", b"{}")
    forged = "0" * 64
    checks = [
        {**headers, update_control.SIGNATURE_HEADER: forged},
        {**headers, update_control.TIMESTAMP_HEADER: str(int(time.time()) - 3600)},
        {update_control.SIGNATURE_HEADER: "xyz", update_control.TIMESTAMP_HEADER: timestamp, update_control.NONCE_HEADER: headers[update_control.NONCE_HEADER]},
    ]
    for candidate in checks:
        with pytest.raises(update_control.RequestAuthError):
            update_control.verify_signed_request(
                method="POST",
                path="/api/update-agent/start",
                body=b"{}",
                timestamp=candidate[update_control.TIMESTAMP_HEADER],
                nonce=candidate[update_control.NONCE_HEADER],
                signature=candidate[update_control.SIGNATURE_HEADER],
                capability=capability,
                state_dir=state_dir,
            )
    assert signature != forged


def test_remote_request_rejects_forged_and_missing_response_signatures(state_dir, monkeypatch):
    capability = update_control.local_capability(state_dir=state_dir)
    with remote_target(state_dir) as remote:
        use_snapshot(monkeypatch, {"desk.portmap": machine("desk.portmap", remote.url)})
        status, body, _raw, _server = update_control.remote_request(
            remote.url, "/api/update-agent/version", capability=capability
        )
        assert status == 200 and body["update_api"] is True

    for tamper in ("tamper", "strip"):
        handler = type(
            "TamperedTarget",
            (RemoteTargetHandler,),
            {
                "scope": "private",
                "state_dir": state_dir,
                "tamper_response_signature": tamper == "tamper",
                "strip_response_signature": tamper == "strip",
            },
        )
        with serving_http(handler) as remote:
            with pytest.raises(update_control.RemoteUpdateError):
                update_control.remote_request(remote.url, "/api/update-agent/version", capability=capability)


# ---------------------------------------------------------------------------
# Remote update API operations


def test_remote_operations_require_authentication(engine, state_dir: Path):
    for method, path in (
        ("GET", "/api/update-agent/version"),
        ("GET", "/api/update-agent/status"),
        ("POST", "/api/update-agent/start"),
        ("GET", "/api/update-agent/jobs/job-1"),
    ):
        status, body, _headers = update_control.handle_remote_request(
            method=method, path=path, body=b"", scope="public", state_dir=state_dir
        )
        assert status == 401, (path, body)


def test_remote_version_and_status_shapes(engine, state_dir: Path):
    capability = update_control.local_capability(state_dir=state_dir)
    headers, timestamp, signature = signed_headers(capability, "GET", "/api/update-agent/version", b"")
    status, body, out_headers = update_control.handle_remote_request(
        method="GET",
        path="/api/update-agent/version",
        body=b"",
        scope="public",
        signature=signature,
        timestamp=timestamp,
        nonce=headers[update_control.NONCE_HEADER],
        state_dir=state_dir,
    )
    payload = json.loads(body)
    assert status == 200
    assert payload["current_version"] == "0.15.0"
    assert payload["update_api"] is True
    assert "active_job" not in payload and "last_job" not in payload
    assert out_headers[update_control.RESPONSE_SIGNATURE_HEADER] == update_control.response_mac(
        capability, headers[update_control.NONCE_HEADER], status, body
    )

    headers, timestamp, signature = signed_headers(capability, "GET", "/api/update-agent/status", b"")
    status, body, _out = update_control.handle_remote_request(
        method="GET",
        path="/api/update-agent/status",
        body=b"",
        scope="public",
        signature=signature,
        timestamp=timestamp,
        nonce=headers[update_control.NONCE_HEADER],
        state_dir=state_dir,
    )
    payload = json.loads(body)
    assert status == 200
    assert payload["latest_version"] is None
    headers, timestamp, signature = signed_headers(capability, "GET", "/api/update-agent/status?check=1", b"")
    status, body, _out = update_control.handle_remote_request(
        method="GET",
        path="/api/update-agent/status?check=1",
        body=b"",
        scope="public",
        signature=signature,
        timestamp=timestamp,
        nonce=headers[update_control.NONCE_HEADER],
        state_dir=state_dir,
    )
    assert json.loads(body)["latest_version"] == "0.16.0"


def test_remote_start_resolves_latest_and_rejects_bad_input(engine, state_dir: Path):
    capability = update_control.local_capability(state_dir=state_dir)
    headers, timestamp, signature = signed_headers(
        capability, "POST", "/api/update-agent/start", json.dumps({"version": "latest", "request_id": "req-1"}).encode()
    )
    status, body, _out = update_control.handle_remote_request(
        method="POST",
        path="/api/update-agent/start",
        body=json.dumps({"version": "latest", "request_id": "req-1"}).encode(),
        scope="public",
        signature=signature,
        timestamp=timestamp,
        nonce=headers[update_control.NONCE_HEADER],
        state_dir=state_dir,
    )
    assert status == 202
    assert json.loads(body)["job"]["target_version"] == "0.16.0"
    assert engine.starts == [("0.16.0", "req-1")]

    for version in ("0.16.0; rm -rf /", "https://evil.example/release", "latest ", "0.16"):
        payload = json.dumps({"version": version, "request_id": "req-2"}).encode()
        headers, timestamp, signature = signed_headers(capability, "POST", "/api/update-agent/start", payload)
        status, body, _out = update_control.handle_remote_request(
            method="POST",
            path="/api/update-agent/start",
            body=payload,
            scope="public",
            signature=signature,
            timestamp=timestamp,
            nonce=headers[update_control.NONCE_HEADER],
            state_dir=state_dir,
        )
        assert status == 409, (version, body)

    payload = json.dumps({"version": "latest", "request_id": "../escape"}).encode()
    headers, timestamp, signature = signed_headers(capability, "POST", "/api/update-agent/start", payload)
    status, _body, _out = update_control.handle_remote_request(
        method="POST",
        path="/api/update-agent/start",
        body=payload,
        scope="public",
        signature=signature,
        timestamp=timestamp,
        nonce=headers[update_control.NONCE_HEADER],
        state_dir=state_dir,
    )
    assert status == 409


def test_remote_job_lookup_rejects_traversal(engine, state_dir: Path):
    capability = update_control.local_capability(state_dir=state_dir)
    engine.starts.clear()
    engine.start_update("0.16.0", "req-1", state_dir=state_dir)
    job_id = engine.jobs["req-1"]["id"]
    for candidate in (job_id, "../capability.json", "job-1/../../etc"):
        path = f"/api/update-agent/jobs/{candidate}"
        headers, timestamp, signature = signed_headers(capability, "GET", path, b"")
        status, body, _out = update_control.handle_remote_request(
            method="GET",
            path=path,
            body=b"",
            scope="public",
            signature=signature,
            timestamp=timestamp,
            nonce=headers[update_control.NONCE_HEADER],
            state_dir=state_dir,
        )
        assert status == (200 if candidate == job_id else 404), candidate


# ---------------------------------------------------------------------------
# Machines listing


def test_updates_status_local_only_without_a_portal(engine, state_dir: Path):
    payload = update_control.updates_status(state_dir=state_dir)
    assert payload["update_api"] is True
    assert [item["id"] for item in payload["machines"]] == ["local"]
    local = payload["machines"][0]
    assert local["authorized"] is True and local["supported"] is True
    assert local["current_version"] == "0.15.0"
    assert local["install_method"] == "brew"
    assert payload["batches"] == []


def test_updates_status_reports_paired_current_legacy_and_offline_machines(engine, state_dir, monkeypatch):
    local_key = update_control.local_capability(state_dir=state_dir)
    paired_state = state_dir / "remote-state"
    paired_state.mkdir(mode=0o700)
    with remote_target(paired_state) as paired, serving_http(VersionOnlyTargetHandler) as unpaired, serving_http(
        LegacyTargetHandler
    ) as legacy:
        machines = {
            "desk.portmap": machine("desk.portmap", paired.url),
            "box.portmap": machine("box.portmap", unpaired.url, via="direct"),
            "old.portmap": machine("old.portmap", legacy.url),
            "down.portmap": machine("down.portmap", "http://127.0.0.1:9"),
        }
        use_snapshot(monkeypatch, machines)
        update_control._store_credential(
            "desk.portmap", update_control.local_capability(state_dir=paired_state), state_dir=state_dir
        )
        payload = update_control.updates_status(state_dir=state_dir)

    by_id = {item["id"]: item for item in payload["machines"]}
    assert by_id["local"]["current_version"] == "0.15.0"

    desk = by_id["desk.portmap"]
    assert desk["authorized"] is True and desk["supported"] is True
    assert desk["current_version"] == "0.15.0"

    box = by_id["box.portmap"]
    assert box["authorized"] is False and box["supported"] is True
    assert box["current_version"] == "0.16.0"
    assert box["build_id"] == "build-def456"

    old = by_id["old.portmap"]
    assert old["authorized"] is False and old["supported"] is False
    assert old["current_version"] == "0.14.2"
    assert old["installed_version"] is None and old["build_id"] is None

    down = by_id["down.portmap"]
    assert down["supported"] is False and down["current_version"] is None

    assert local_key not in json.dumps(payload)


def test_updates_status_does_not_fall_back_to_local_versions(engine, state_dir, monkeypatch):
    with serving_http(LegacyTargetHandler) as legacy:
        use_snapshot(monkeypatch, {"old.portmap": machine("old.portmap", legacy.url)})
        payload = update_control.updates_status(state_dir=state_dir)
    old = next(item for item in payload["machines"] if item["id"] == "old.portmap")
    assert old["current_version"] == "0.14.2"
    assert old["current_version"] != payload["machines"][0]["current_version"]


# ---------------------------------------------------------------------------
# Durable batches


def test_start_batch_validates_the_whole_target_list(engine, state_dir, monkeypatch):
    use_snapshot(monkeypatch, {"desk.portmap": machine("desk.portmap", None)})
    for payload in (
        {"machines": []},
        {"machines": "desk.portmap"},
        {"machines": ["ghost.portmap"]},
        {"machines": ["local"], "version": "0.16.0; evil"},
        {"machines": ["local"], "version": "https://evil.example"},
        {"machines": ["local"], "extra": 1},
        {"machines": ["local"] * 40},
    ):
        with pytest.raises(PortmapError):
            update_control.start_batch(payload, state_dir=state_dir)
    assert engine.launches == []


def test_run_batch_updates_remotes_serially_and_local_last(engine, state_dir, monkeypatch):
    ok_state = state_dir / "ok-remote"
    ok_state.mkdir(mode=0o700)
    with remote_target(ok_state) as ok_remote, serving_http(LegacyTargetHandler) as legacy:
        machines = {
            "desk.portmap": machine("desk.portmap", ok_remote.url),
            "old.portmap": machine("old.portmap", legacy.url),
        }
        use_snapshot(monkeypatch, machines)
        update_control._store_credential(
            "desk.portmap", update_control.local_capability(state_dir=ok_state), state_dir=state_dir
        )
        # The legacy target is paired, so its failure is the missing remote
        # update agent rather than a missing authorization.
        update_control._store_credential(
            "old.portmap", update_control._new_capability(), state_dir=state_dir
        )
        batch = update_control.start_batch(
            {"machines": ["local", "old.portmap", "desk.portmap"], "version": "0.16.0"}, state_dir=state_dir
        )
        update_control.run_batch(batch["id"], state_dir=state_dir)
        finished = update_control.get_batch(batch["id"], state_dir=state_dir)

    assert engine.launches == [("batch", batch["id"])]
    order = [request_id.rsplit(":", 1)[-1] for _version, request_id in engine.starts]
    assert "old.portmap" not in order  # legacy target is never started
    assert order.index("local") > order.index("desk.portmap")
    assert order[-1] == "local"

    by_machine = {target["machine_id"]: target for target in finished["targets"]}
    assert by_machine["desk.portmap"]["state"] == "succeeded"
    assert by_machine["desk.portmap"]["target_version"] == "0.16.0"
    assert by_machine["desk.portmap"]["job_id"].startswith("job-")
    assert by_machine["local"]["state"] == "succeeded"
    assert by_machine["old.portmap"]["state"] == "failed"
    assert "no update agent" in by_machine["old.portmap"]["error"]
    assert finished["state"] == "partial"
    assert finished["worker_pid"] is None


def test_run_batch_is_idempotent_after_completion(engine, state_dir, monkeypatch):
    use_snapshot(monkeypatch, {})
    batch = update_control.start_batch({"machines": ["local"], "version": "latest"}, state_dir=state_dir)
    update_control.run_batch(batch["id"], state_dir=state_dir)
    starts = list(engine.starts)
    update_control.run_batch(batch["id"], state_dir=state_dir)
    assert engine.starts == starts
    assert update_control.get_batch(batch["id"], state_dir=state_dir)["state"] == "succeeded"


def test_run_batch_survives_a_dead_worker_and_resumes(engine, state_dir, monkeypatch):
    ok_state = state_dir / "ok-remote"
    ok_state.mkdir(mode=0o700)
    with remote_target(ok_state) as ok_remote:
        use_snapshot(monkeypatch, {"desk.portmap": machine("desk.portmap", ok_remote.url)})
        update_control._store_credential(
            "desk.portmap", update_control.local_capability(state_dir=ok_state), state_dir=state_dir
        )
        batch = update_control.start_batch({"machines": ["desk.portmap", "local"], "version": "0.16.0"}, state_dir=state_dir)
        # Simulate a worker that died mid-batch: one machine done, pid gone.
        dead_pid = dead_worker_pid()
        update_control._mutate_batch(
            state_dir,
            batch["id"],
            lambda record: (
                record.__setitem__("state", "running"),
                record.__setitem__("worker_pid", dead_pid),
                next(target for target in record["targets"] if target["machine_id"] == "desk.portmap").__setitem__(
                    "state", "succeeded"
                ),
            ),
        )
        healed = update_control.get_batch(batch["id"], state_dir=state_dir)
        assert healed["state"] == "running"
        assert ("batch", batch["id"]) in engine.launches
        update_control.run_batch(batch["id"], state_dir=state_dir)
        finished = update_control.get_batch(batch["id"], state_dir=state_dir)
    assert finished["state"] == "succeeded"
    assert all(target["state"] == "succeeded" for target in finished["targets"])
    assert engine.starts[-1][1].endswith(":local")


def test_run_batch_isolates_unreachable_and_unauthorized_targets(engine, state_dir, monkeypatch):
    ok_state = state_dir / "ok-remote"
    ok_state.mkdir(mode=0o700)
    with remote_target(ok_state) as ok_remote:
        machines = {
            "desk.portmap": machine("desk.portmap", ok_remote.url),
            "down.portmap": machine("down.portmap", "http://127.0.0.1:9"),
            "fresh.portmap": machine("fresh.portmap", None),
        }
        use_snapshot(monkeypatch, machines)
        update_control._store_credential(
            "desk.portmap", update_control.local_capability(state_dir=ok_state), state_dir=state_dir
        )
        update_control._store_credential("down.portmap", update_control._new_capability(), state_dir=state_dir)
        batch = update_control.start_batch(
            {"machines": ["fresh.portmap", "down.portmap", "desk.portmap"], "version": "0.16.0"}, state_dir=state_dir
        )
        update_control.run_batch(batch["id"], state_dir=state_dir)
        finished = update_control.get_batch(batch["id"], state_dir=state_dir)
    by_machine = {target["machine_id"]: target for target in finished["targets"]}
    assert by_machine["desk.portmap"]["state"] == "succeeded"
    assert by_machine["down.portmap"]["state"] == "failed"
    assert by_machine["fresh.portmap"]["state"] == "failed"
    assert finished["state"] == "partial"


def test_batch_records_survive_reader_restart(engine, state_dir, monkeypatch):
    use_snapshot(monkeypatch, {})
    batch = update_control.start_batch({"machines": ["local"], "version": "0.16.0"}, state_dir=state_dir)
    update_control.run_batch(batch["id"], state_dir=state_dir)
    listed = update_control.updates_status(state_dir=state_dir)
    assert listed["batches"][0]["id"] == batch["id"]
    stored = update_control.get_batch(batch["id"], state_dir=state_dir)
    assert stored["state"] == "succeeded"
    assert stored["targets"][0]["machine_id"] == "local"


def test_get_batch_rejects_unknown_and_traversal_ids(engine, state_dir: Path):
    for bad in ("../updates", "b123", "b" + "a" * 12 + "!", ""):
        with pytest.raises(PortmapError):
            update_control.get_batch(bad, state_dir=state_dir)
    assert update_control.get_batch("b000000000000", state_dir=state_dir) is None


def test_worker_launch_failure_fails_the_batch_explicitly(engine, state_dir, monkeypatch):
    use_snapshot(monkeypatch, {})

    def broken_launch(kind, identifier, *, state_dir):
        raise PortmapError("snapshot failed")

    original = engine.launch_durable_worker
    engine.launch_durable_worker = broken_launch
    try:
        with pytest.raises(PortmapError):
            update_control.start_batch({"machines": ["local"], "version": "0.16.0"}, state_dir=state_dir)
    finally:
        engine.launch_durable_worker = original
    batch = update_control.recent_batches(state_dir=state_dir)[0]
    assert batch["state"] == "failed"
    assert batch["targets"][0]["error"]


def test_second_batch_conflicts_while_one_is_active(engine, state_dir, monkeypatch):
    use_snapshot(monkeypatch, {})
    first = update_control.start_batch({"machines": ["local"], "version": "0.16.0"}, state_dir=state_dir)
    with pytest.raises(update_control.BatchConflictError) as conflict:
        update_control.start_batch({"machines": ["local"], "version": "0.16.0"}, state_dir=state_dir)
    assert conflict.value.batch["id"] == first["id"]
    update_control.run_batch(first["id"], state_dir=state_dir)
    second = update_control.start_batch({"machines": ["local"], "version": "0.16.0"}, state_dir=state_dir)
    assert second["id"] != first["id"]


@pytest.fixture
def ssh_remote(state_dir, monkeypatch, engine):
    import hashlib
    import shutil
    import socketserver
    from portmap import client, client_tunnel

    target = machine("socket-target.portmap", "http://192.0.2.1:9", via="ssh")
    remote_state = state_dir / "ssh-target"
    remote_state.mkdir(mode=0o700)
    directory = client_tunnel.http_socket_dir(state_dir)
    tunnel = {"owner_token": "fixture-owned-master", "controlpath": str(state_dir / "master")}
    name = hashlib.sha256(f"{target.id}:{tunnel['owner_token']}".encode()).hexdigest()[:24] + ".sock"
    socket_path = directory / name
    profile = {
        "domain": target.id, "target": target.target, "via": "ssh",
        "remote_http_port": 80, "http_socket": str(socket_path), "tunnel": tunnel,
    }
    update_control._write_json(socket_path.with_suffix(".json"), client_tunnel._http_identity(tunnel, 80))
    monkeypatch.setattr(client, "_load", lambda state_dir: {target.id: profile})
    monkeypatch.setattr(client_tunnel, "tunnel_alive", lambda tunnel: True)
    handler = type("UnixTargetHandler", (RemoteTargetHandler,), {"state_dir": remote_state, "scope": "private"})
    server = socketserver.ThreadingUnixStreamServer(str(socket_path), handler)
    server.daemon_threads = True
    socket_path.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    use_snapshot(monkeypatch, {target.id: target})
    try:
        yield target, remote_state, socket_path, profile
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        shutil.rmtree(directory)


@pytest.mark.parametrize("stale_credential", [False, True])
def test_ssh_updates_use_owned_socket_not_container_relay_and_grant_no_reverse_access(state_dir, ssh_remote, stale_credential):
    target, remote_state, _socket_path, _profile = ssh_remote
    old_key = update_control._new_capability() if stale_credential else None
    if old_key:
        update_control._store_credential(target.id, old_key, state_dir=state_dir)
    status = update_control.updates_status(state_dir=state_dir, check=True)
    row = next(item for item in status["machines"] if item["id"] == target.id)
    assert row["can_update"] is True
    assert row["authorized"] is False
    assert update_control.credential_for(target.id, state_dir=state_dir) == old_key
    batch = update_control.start_batch({"machines": [target.id], "version": "0.16.0"}, state_dir=state_dir)
    update_control.run_batch(batch["id"], state_dir=state_dir)
    result = update_control.get_batch(batch["id"], state_dir=state_dir)
    assert result["state"] == "succeeded"
    assert update_control.credential_for(target.id, state_dir=state_dir) == update_control.local_capability(state_dir=remote_state)
    assert update_control._load_credentials(remote_state) == {}


@pytest.mark.parametrize("fault", ["record", "mode", "target"])
def test_ssh_update_transport_rejects_changed_ownership_without_tcp_fallback(state_dir, ssh_remote, fault):
    target, _remote_state, socket_path, profile = ssh_remote
    if fault == "record":
        update_control._write_json(socket_path.with_suffix(".json"), {"owner_token": "someone-else"})
    elif fault == "mode":
        socket_path.chmod(0o666)
    else:
        profile["target"] = "another-server"
    with pytest.raises(update_control.RemoteUpdateError, match="reconnect"):
        update_control._request_machine(target, "/api/update-agent/version", state_dir=state_dir, timeout=0.1)


def test_capability_symlink_is_rejected_without_touching_target(state_dir, tmp_path):
    target = tmp_path / "foreign.json"
    original = json.dumps({"version": 1, "token": update_control._new_capability()})
    target.write_text(original)
    target.chmod(0o600)
    path = update_control._updates_dir(state_dir) / update_control.CAPABILITY_FILE_NAME
    path.symlink_to(target)
    with pytest.raises(PortmapError):
        update_control.local_capability(state_dir=state_dir)
    assert path.is_symlink()
    assert target.read_text() == original


@pytest.mark.parametrize("invalid", [None, {}, {"version": 1, "token": "damaged-record"}])
def test_invalid_capability_is_not_silently_rotated(state_dir, invalid):
    path = update_control._updates_dir(state_dir) / update_control.CAPABILITY_FILE_NAME
    update_control._write_json(path, invalid)
    with pytest.raises(PortmapError):
        update_control.local_capability(state_dir=state_dir)
    assert json.loads(path.read_text()) == invalid


def test_running_local_update_keeps_live_phase_until_completion(engine, state_dir, monkeypatch):
    use_snapshot(monkeypatch, {})
    original_start = engine.start_update
    original_get = engine.get_job
    polling = threading.Event()
    finish = threading.Event()

    def start(version, request_id, *, state_dir):
        job = original_start(version, request_id, state_dir=state_dir)
        job.update(state="installing", version_after=None)
        return job

    def get(job_id, *, state_dir):
        polling.set()
        if not finish.wait(10):
            raise RuntimeError("test did not release the update")
        job = original_get(job_id, state_dir=state_dir)
        job.update(state="succeeded", version_after=job["target_version"])
        return job

    monkeypatch.setattr(engine, "start_update", start)
    monkeypatch.setattr(engine, "get_job", get)
    batch = update_control.start_batch({"machines": ["local"], "version": "0.16.0"}, state_dir=state_dir)
    worker = threading.Thread(target=update_control.run_batch, args=(batch["id"],), kwargs={"state_dir": state_dir})
    worker.start()
    try:
        assert polling.wait(5)
        running = update_control.get_batch(batch["id"], state_dir=state_dir, heal=False)
        assert running["state"] == "running"
        assert running["targets"][0]["state"] == "installing"
        assert running["targets"][0]["current_version"] == "0.15.0"
    finally:
        finish.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert update_control.get_batch(batch["id"], state_dir=state_dir)["state"] == "succeeded"


def test_local_batch_does_not_update_from_snapshot_when_host_agent_is_missing(engine, state_dir, monkeypatch):
    use_snapshot(monkeypatch, {})
    monkeypatch.setenv("PORTMAP_AGENT_RUNTIME_DIR", str(state_dir / "absent-agent"))
    batch = update_control.start_batch({"machines": ["local"], "version": "0.16.0"}, state_dir=state_dir)
    update_control.run_batch(batch["id"], state_dir=state_dir)
    assert update_control.get_batch(batch["id"], state_dir=state_dir)["state"] == "failed"
    assert engine.starts == []


def test_local_start_requires_the_matching_agent_state(engine, state_dir):
    from portmap.agent_client import request_json_status

    payload = {"version": "0.16.0", "request_id": "test-local-scope", "expected_state": str(state_dir / "other")}
    socket_path = state_dir.parent / "agent-runtime" / "agent.sock"
    _status, body = request_json_status("POST", "/updates/local/start", socket_path=socket_path, payload=payload)
    assert "job" not in body
    assert engine.starts == []
    payload["expected_state"] = str(state_dir.resolve())
    status, body = request_json_status("POST", "/updates/local/start", socket_path=socket_path, payload=payload)
    assert status == 202
    assert body["job"]["request_id"] == "test-local-scope"
    assert engine.starts == [("0.16.0", "test-local-scope")]


def test_full_nonce_window_rejects_new_requests_without_allowing_old_replays(engine, state_dir, monkeypatch):
    monkeypatch.setattr(update_control, "MAX_NONCE_ENTRIES", 2)
    capability = update_control.local_capability(state_dir=state_dir)
    requests = []
    for _ in range(3):
        headers, timestamp, signature = signed_headers(capability, "GET", "/api/update-agent/version", b"")
        requests.append({
            "method": "GET", "path": "/api/update-agent/version", "body": b"", "scope": "public",
            "signature": signature, "timestamp": timestamp, "nonce": headers[update_control.NONCE_HEADER],
            "state_dir": state_dir,
        })
    assert [update_control.handle_remote_request(**request)[0] for request in requests] == [200, 200, 429]
    assert update_control.handle_remote_request(**requests[0])[0] == 403


def test_batch_resume_follows_existing_local_job_when_latest_changes(engine, state_dir, monkeypatch):
    use_snapshot(monkeypatch, {})
    batch = update_control.start_batch({"machines": ["local"], "version": "latest"}, state_dir=state_dir)
    request_id = f"{batch['id']}:local"
    job = engine.start_update("0.16.0", request_id, state_dir=state_dir)
    dead_pid = dead_worker_pid()

    def checkpoint(record):
        record.update(state="running", worker_pid=dead_pid, worker_start="old-worker")
        record["targets"][0].update(state="installing", job_id=job["id"], target_version="0.16.0")

    update_control._mutate_batch(state_dir, batch["id"], checkpoint)
    engine.latest_version = "0.17.0"
    update_control.run_batch(batch["id"], state_dir=state_dir)
    result = update_control.get_batch(batch["id"], state_dir=state_dir)
    assert result["state"] == "succeeded"
    assert result["targets"][0]["target_version"] == "0.16.0"
    assert engine.starts == [("0.16.0", request_id)]


def test_reused_pid_does_not_block_batch_recovery_or_kill_unrelated_process(engine, state_dir, monkeypatch):
    use_snapshot(monkeypatch, {})
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()", "portmap-update-fixture"],
        stdin=subprocess.PIPE,
    )
    try:
        batch = update_control.start_batch({"machines": ["local"], "version": "0.16.0"}, state_dir=state_dir)
        update_control._mutate_batch(
            state_dir, batch["id"],
            lambda record: record.update(state="running", worker_pid=child.pid, worker_start="previous-incarnation"),
        )
        update_control.run_batch(batch["id"], state_dir=state_dir)
        assert update_control.get_batch(batch["id"], state_dir=state_dir)["state"] == "succeeded"
        assert child.poll() is None
    finally:
        child.stdin.close()
        child.wait(timeout=5)
