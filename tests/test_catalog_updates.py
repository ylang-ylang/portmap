"""Catalog update API surface and agent update IPC routes."""
import http.client
import json
import threading
from pathlib import Path

import pytest

from portmap import __version__, agent, update_control
from portmap.agent_client import UnixHTTPConnection
from portmap.catalog import CatalogServer
from portmap.errors import PortmapError


@pytest.fixture(autouse=True)
def missing_agent_socket(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(tmp_path / "missing-agent.sock"))


@pytest.fixture(autouse=True)
def isolated_access_config(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "catalog-access.json"
    staging = tmp_path / "catalog-access.json.tmp"
    staging.write_text(
        json.dumps(
            {
                "version": 1,
                "owner": "catalog-owner",
                "dns": {"address": "169.254.254.53", "port": 1053},
                "local": {"domain": "laptop.portmap", "http_port": 80, "enabled": False},
                "machines": {},
            }
        ),
        encoding="utf-8",
    )
    staging.replace(path)
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(path))


class FakeEngine:
    def __init__(self):
        self.started = []

    def local_status(self, *, state_dir, check=False):
        return {
            "current_version": "0.15.0",
            "installed_version": "0.15.0",
            "build_id": "build-abc123",
            "latest_version": "0.16.0" if check else None,
            "install_method": "brew",
            "update_available": True,
            "can_update": True,
            "reason": None,
            "active_job": None,
            "last_job": None,
        }

    def start_update(self, version, request_id, *, state_dir):
        self.started.append((version, request_id))
        return {
            "id": "job-1",
            "request_id": request_id,
            "state": "queued",
            "version_before": "0.15.0",
            "target_version": version,
            "version_after": None,
            "created_at": "2026-10-17T00:00:00+00:00",
            "updated_at": "2026-10-17T00:00:00+00:00",
            "message": "queued",
            "log_tail": None,
            "error": None,
        }

    def get_job(self, job_id, *, state_dir):
        raise PortmapError("unknown job")

    def launch_durable_worker(self, kind, identifier, *, state_dir):
        pass


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(update_control, "_engine_module", lambda: fake)
    return fake


def request_catalog(
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    scope: str = "private",
    origin: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, http.client.HTTPMessage, bytes]:
    server = CatalogServer(("127.0.0.1", 0), scope=scope)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        request_headers: dict[str, str] = {"Host": "localhost"}
        if body is not None:
            request_headers["content-type"] = "application/json"
        if origin is not None:
            request_headers["Origin"] = origin
        if headers:
            request_headers.update(headers)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def same_origin(port: int) -> str:
    return f"http://localhost:{port}"


# ---------------------------------------------------------------------------
# Public/private scope


def test_public_listener_has_no_manager_update_surface():
    for method, path, body in (
        ("GET", "/api/updates", None),
        ("POST", "/api/updates", b'{"machines": ["local"]}'),
        ("GET", "/api/updates/jobs/b000000000000", None),
        ("POST", "/api/updates/token", b"{}"),
        ("POST", "/api/updates/credentials", b"{}"),
    ):
        status, _headers, _body = request_catalog(path, method=method, body=body, scope="public")
        assert status == 404, (method, path)


def test_public_pairing_is_rejected_before_forwarding(monkeypatch):
    called = []
    monkeypatch.setattr("portmap.catalog.agent_update_agent_dispatch", lambda payload: called.append(payload) or (200, {}))
    status, _headers, body = request_catalog(
        "/api/update-agent/pair",
        method="POST",
        body=b"{}",
        scope="public",
    )
    assert status == 403
    assert called == []


def test_private_updates_listing_requires_the_agent():
    status, _headers, body = request_catalog("/api/updates")
    assert status == 502
    assert b"host agent unavailable" in body


def test_private_updates_listing_rejects_malformed_check_flag():
    status, _headers, body = request_catalog("/api/updates?check=2")
    assert status == 400
    assert b"check must be exactly 1" in body


def test_updates_job_lookup_rejects_traversal_shaped_ids():
    status, _headers, _body = request_catalog("/api/updates/jobs/..%2Fupdates")
    assert status == 404


# ---------------------------------------------------------------------------
# Same-origin mutation protection


def test_updates_post_requires_same_origin(monkeypatch):
    sent = []
    monkeypatch.setattr("portmap.catalog.agent_update_start", lambda payload: sent.append(payload) or (202, {"job": {"id": "ok"}}))
    status, _headers, body = request_catalog(
        "/api/updates", method="POST", body=b'{"machines": ["local"]}', origin="http://evil.example"
    )
    assert status == 403
    assert sent == []

    status, _headers, body = request_catalog("/api/updates", method="POST", body=b'{"machines": ["local"]}')
    assert status == 403
    assert sent == []

    server_port = 0

    class Capture:
        def __init__(self):
            self.payload = None

    capture = Capture()

    def fake_start(payload):
        capture.payload = payload
        return (202, {"job": {"id": "b000000000000"}})

    monkeypatch.setattr("portmap.catalog.agent_update_start", fake_start)
    status, _headers, body = request_catalog(
        "/api/updates", method="POST", body=b'{"machines": ["local"], "version": "latest"}', origin=None
    )
    # Without a single Host+Origin pair the mutation is still foreign.
    assert status == 403
    assert capture.payload is None


def test_token_and_credentials_posts_require_private_same_origin(monkeypatch):
    monkeypatch.setattr(
        "portmap.catalog.agent_update_token", lambda payload: (200, {"token": "pm1." + "a" * 64})
    )
    status, _headers, _body = request_catalog("/api/updates/token", method="POST", body=b"{}", origin="http://other.host:1")
    assert status == 403


def test_updates_post_rejects_oversized_and_broken_bodies(monkeypatch):
    monkeypatch.setattr("portmap.catalog.agent_update_start", lambda payload: (202, {"job": {}}))
    status, _headers, _body = request_catalog("/api/updates", method="POST", body=b"{" * 4000, origin="http://localhost")
    assert status in (400, 413)
    status, _headers, _body = request_catalog("/api/updates", method="POST", body=b"not json", origin="http://localhost")
    assert status == 400


# ---------------------------------------------------------------------------
# /api/version


def test_version_distinguishes_running_catalog_from_agent(engine, tmp_path, monkeypatch):
    status_method = engine.local_status
    monkeypatch.setattr(engine, "local_status", lambda **kwargs: {**status_method(**kwargs), "current_version": "0.0.1"})
    with serving_agent(tmp_path) as service:
        monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(service.socket_path))
        for scope in ("private", "public"):
            status, headers, body = request_catalog("/api/version", scope=scope)
            payload = json.loads(body)
            assert status == 200
            assert payload["version"] == "0.0.1"
            assert headers["Server"].split()[0] == f"portmap-catalog/{payload['catalog_version']}"
            assert payload["catalog_version"] != payload["version"]
            assert set(payload) == {"version", "catalog_version", "build_id", "update_api"}


def test_version_falls_back_to_the_running_catalog_without_an_agent():
    for scope in ("private", "public"):
        status, _headers, body = request_catalog("/api/version", scope=scope)
        payload = json.loads(body)
        assert status == 200
        assert payload["version"] == __version__
        assert payload["build_id"] is None
        assert payload["update_api"] is True


# ---------------------------------------------------------------------------
# /api/update-agent relay


def test_public_update_request_and_response_are_authenticated_through_real_agent(engine, tmp_path, monkeypatch):
    import time
    import uuid

    with serving_agent(tmp_path) as service:
        monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(service.socket_path))
        capability = update_control.local_capability(state_dir=service.state_dir)
        path = "/api/update-agent/start"
        raw = b'{ "request_id" : "real-signed", "version" : "0.16.0" }'

        def headers_for(body):
            nonce = uuid.uuid4().hex
            timestamp = str(int(time.time()))
            return nonce, {
                update_control.NONCE_HEADER: nonce,
                update_control.TIMESTAMP_HEADER: timestamp,
                update_control.SIGNATURE_HEADER: update_control.request_mac(capability, "POST", path, body, timestamp, nonce),
            }

        nonce, auth = headers_for(raw)
        status, headers, body = request_catalog(path, method="POST", body=raw, scope="public", headers=auth)
        assert status == 202
        assert json.loads(body)["job"]["target_version"] == "0.16.0"
        assert headers[update_control.RESPONSE_SIGNATURE_HEADER] == update_control.response_mac(capability, nonce, status, body)
        _nonce, auth = headers_for(raw)
        status, _headers, _body = request_catalog(
            path, method="POST", body=raw.replace(b"0.16.0", b"0.99.0"), scope="public", headers=auth,
        )
        assert "job" not in json.loads(_body)
        assert engine.started == [("0.16.0", "real-signed")]
        status, _headers, _body = request_catalog(path, method="POST", body=raw, scope="public")
        assert status == 401


def test_update_agent_relay_rejects_oversized_bodies():
    status, _headers, _body = request_catalog(
        "/api/update-agent/start", method="POST", body=b"x" * 20000, scope="public"
    )
    assert status == 413


@pytest.mark.parametrize(("origin", "content_type", "expected"), [
    (None, "application/json", 403),
    ("http://untrusted-project.portmap", "application/json", 403),
    ("http://localhost", "text/plain", 415),
])
def test_private_pairing_rejects_cross_origin_and_simple_posts(monkeypatch, origin, content_type, expected):
    def forbidden_dispatch(payload):
        pytest.fail("an unauthorized pairing request reached the host agent")

    monkeypatch.setattr("portmap.catalog.agent_update_agent_dispatch", forbidden_dispatch)
    status, _headers, _body = request_catalog(
        "/api/update-agent/pair", method="POST", body=b"{}",
        scope="private", origin=origin, headers={"content-type": content_type},
    )
    assert status == expected


def test_private_same_origin_pairing_reveals_only_local_key(tmp_path, monkeypatch):
    with serving_agent(tmp_path) as service:
        monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(service.socket_path))
        original = update_control.local_capability(state_dir=service.state_dir)
        status, _headers, body = request_catalog(
            "/api/update-agent/pair", method="POST", body=b"{}",
            scope="private", origin="http://localhost",
        )
        assert status == 200
        assert json.loads(body)["token"] == original
        assert update_control._load_credentials(service.state_dir) == {}


# ---------------------------------------------------------------------------
# Agent IPC routes over the real Unix socket


class serving_agent:
    def __init__(self, tmp_path: Path) -> None:
        self.state_dir = tmp_path / "client"
        self.state_dir.mkdir(mode=0o700)
        import os

        self.previous_client_state = os.environ.get("PORTMAP_CLIENT_STATE_DIR")
        os.environ["PORTMAP_CLIENT_STATE_DIR"] = str(self.state_dir)
        socket_dir = tmp_path / "agent-runtime"
        socket_dir.mkdir(mode=0o700)
        self.socket_path = socket_dir / "agent.sock"
        self.server = agent.ThreadingUnixHTTPServer(str(self.socket_path), agent.AgentHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "serving_agent":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        import os

        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.socket_path.unlink(missing_ok=True)
        if self.previous_client_state is None:
            os.environ.pop("PORTMAP_CLIENT_STATE_DIR", None)
        else:
            os.environ["PORTMAP_CLIENT_STATE_DIR"] = self.previous_client_state

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode() if payload is not None else b""
        connection = UnixHTTPConnection(str(self.socket_path), timeout=10)
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={"content-type": "application/json", "content-length": str(len(body))},
            )
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            parsed = {}
        return response.status, parsed




def test_agent_serves_updates_status_with_local_machine(engine, tmp_path):
    with serving_agent(tmp_path) as service:
        status, body = service.request("GET", "/updates/status")
    assert status == 200
    assert body["update_api"] is True
    assert body["machines"][0]["id"] == "local"
    assert body["machines"][0]["current_version"] == "0.15.0"
    assert "token" not in json.dumps(body)


def test_agent_start_validates_input_and_returns_202(engine, tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(
        update_control, "_engine_module", lambda: engine, raising=True
    )
    with serving_agent(tmp_path) as service:
        status, body = service.request("POST", "/updates/start", {"machines": ["local"], "version": "latest"})
        assert status == 202
        assert body["job"]["state"] == "queued"
        assert body["job"]["targets"][0]["machine_id"] == "local"

        status, body = service.request("POST", "/updates/start", {"machines": []})
        assert status == 400
        assert "error" in body

        status, body = service.request("POST", "/updates/start", {"machines": ["local"], "version": "0.16; rm -rf /"})
        assert status == 400


def test_agent_batch_conflict_maps_to_409_with_live_job(engine, tmp_path):
    with serving_agent(tmp_path) as service:
        _status, first = service.request("POST", "/updates/start", {"machines": ["local"], "version": "latest"})
        batch_id = first["job"]["id"]
        status, body = service.request("POST", "/updates/start", {"machines": ["local"], "version": "latest"})
        assert status == 409
        assert body["job"]["id"] == batch_id

        status, body = service.request("GET", f"/updates/jobs/{batch_id}")
        assert status == 200
        assert body["job"]["id"] == batch_id

        status, _body = service.request("GET", "/updates/joes/../secret")
        assert status == 404


def test_agent_dispatch_verifies_signature_and_rejects_replay(engine, tmp_path):
    with serving_agent(tmp_path) as service:
        capability = update_control.local_capability(state_dir=service.state_dir)
        timestamp = str(int(update_control.dt.datetime.now().timestamp()))
        nonce = "aa" * 16
        signature = update_control.request_mac(capability, "GET", "/api/update-agent/version", b"", timestamp, nonce)
        dispatch = {
            "method": "GET",
            "path": "/api/update-agent/version",
            "body": "",
            "scope": "public",
            "signature": signature,
            "timestamp": timestamp,
            "nonce": nonce,
        }
        status, envelope = service.request("POST", "/update-agent", dispatch)
        assert status == 200
        assert envelope["status"] == 200
        assert json.loads(envelope["body"])["current_version"] == "0.15.0"
        assert (
            envelope["headers"][update_control.RESPONSE_SIGNATURE_HEADER]
            == update_control.response_mac(capability, nonce, 200, envelope["body"].encode())
        )

        status, envelope = service.request("POST", "/update-agent", dispatch)
        assert status == 200
        assert envelope["status"] == 403

        unsigned = {"method": "GET", "path": "/api/update-agent/version", "body": "", "scope": "public"}
        status, envelope = service.request("POST", "/update-agent", unsigned)
        assert envelope["status"] == 401

        enroll = {
            "method": "POST",
            "path": "/api/update-agent/pair",
            "body": "{}",
            "scope": "public",
        }
        status, envelope = service.request("POST", "/update-agent", enroll)
        assert envelope["status"] == 403

        enroll["scope"] = "private"
        status, envelope = service.request("POST", "/update-agent", enroll)
        assert envelope["status"] == 200
        assert json.loads(envelope["body"])["ok"] is True
