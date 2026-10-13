"""Behavioral tests for durable connection targets and their private control API.

The facade tests fake the ``portmap.client`` boundary (the surface the
parent owns); the HTTP tests drive the real catalog and agent handlers over
real sockets and fake the facade boundary. Nothing here asserts forwarding
echoes or internal storage text, only user-visible persistence,
transitions, permissions and errors.
"""
import datetime as dt
import http.client
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from portmap import agent, agent_client, catalog, client, connection_config
from portmap.errors import PortmapError

GENERATED_AT = "2026-10-12T00:00:00+00:00"
CHECKED_AT = "2026-10-12T00:00:01+00:00"
ROW_FIELDS = {"id", "target", "via", "domain", "status", "latency_ms", "last_checked_at", "error", "actual_via", "has_connection"}


@pytest.fixture(autouse=True)
def isolated_access_config(tmp_path: Path, monkeypatch) -> None:
    # Never observe a developer machine's real controller snapshot.
    path = tmp_path / "catalog-access.json"
    path.write_text(
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
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(path))


@pytest.fixture(autouse=True)
def missing_agent_socket(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(tmp_path / "missing-agent.sock"))


@pytest.fixture
def state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "client"
    state_dir.mkdir(mode=0o700)
    return state_dir


def client_row(domain, *, target="192.0.2.10", via="direct", status="configured", **extra):
    row = {
        "domain": domain,
        "target": target,
        "via": via,
        "status": status,
        "address": "192.0.2.10",
        "remote_http_port": 80,
        "scheme": "http",
        "catalog_url": f"http://{domain}/",
        "tcp_address": "192.0.2.10",
        "tcp_ports": [],
        "include_tcp": False,
    }
    row.update(extra)
    return row


class ClientFake:
    """The portmap.client boundary: connection rows plus recorded calls."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.connect_result = None
        self.connect_error = None
        self.disconnect_error = None
        self.connect_calls = []
        self.disconnect_calls = []
        self.list_calls = []

    def install(self, monkeypatch):
        monkeypatch.setattr(client, "connect", self.connect)
        monkeypatch.setattr(client, "disconnect", self.disconnect)
        monkeypatch.setattr(client, "connections", self.connections)
        monkeypatch.setattr(client, "_load", lambda state_dir: {row["domain"]: dict(row) for row in self.rows})
        return self

    def connect(self, state_dir, target, **options):
        self.connect_calls.append({"target": target, **options})
        if self.connect_error is not None:
            raise self.connect_error
        previous = next((row for row in self.rows if row["domain"] == self.connect_result["domain"]), {})
        self.rows = [row for row in self.rows if row["domain"] != self.connect_result["domain"]]
        self.rows.append({**previous, **self.connect_result})
        return dict(self.connect_result)

    def disconnect(self, state_dir, domain, *, use_sudo=True):
        self.disconnect_calls.append({"domain": domain, "use_sudo": use_sudo})
        if self.disconnect_error is not None:
            raise self.disconnect_error
        self.rows = [row for row in self.rows if row.get("domain") != domain]
        return {"disconnected": domain}

    def connections(self, state_dir, *, check=False):
        self.list_calls.append(check)
        return {
            "connections": [dict(row) for row in self.rows],
            "local": None,
            "dns": {"running": False},
            "resolver": {"installed": True},
            "gateway": {"running": False},
        }


@pytest.fixture
def fake(monkeypatch):
    return ClientFake().install(monkeypatch)


def rows_by_target(payload):
    return {row["target"]: row for row in payload["connections"]}


# ---------------------------------------------------------------- facade


def test_save_persists_target_without_connecting(state, fake):
    payload = connection_config.mutate_connection_target(
        state, {"action": "save", "target": "192.0.2.10", "via": "direct"}
    )

    row = payload["connections"][0]
    assert row["target"] == "192.0.2.10"
    assert row["via"] == "direct"
    assert row["status"] == "configured"
    assert row["latency_ms"] is None
    assert row["last_checked_at"] is None
    assert "error" not in row
    assert "actual_via" not in row
    assert fake.connect_calls == []

    # The store is durable and private to the owning user.
    store = state / "connection-targets.json"
    assert store.stat().st_mode & 0o777 == 0o600
    reloaded = connection_config.connection_targets(state)
    assert reloaded["connections"][0]["id"] == row["id"]


def test_save_rejects_invalid_payloads(state, fake):
    connection_config.mutate_connection_target(state, {"action": "save", "target": "192.0.2.10", "via": "direct"})
    cases = [
        {"action": "save", "via": "direct"},
        {"action": "save", "target": "   ", "via": "ssh"},
        {"action": "save", "target": "192.0.2.11"},
        {"action": "save", "target": "192.0.2.11", "via": "auto"},
        {"action": "save", "target": "x" * 256, "via": "direct"},
        {"action": "save", "id": "missing", "target": "192.0.2.11", "via": "ssh"},
        {"action": "save", "target": "192.0.2.10", "via": "direct"},
        {"action": "invent", "id": "x"},
        {"action": "connect"},
        {"action": "disconnect"},
        {"action": "delete"},
        {"action": "connect", "id": "missing"},
    ]
    for payload in cases:
        with pytest.raises(PortmapError):
            connection_config.mutate_connection_target(state, payload)
    # Nothing partial was persisted by the rejected payloads.
    assert len(connection_config.connection_targets(state)["connections"]) == 1


def test_managed_connect_persists_before_attempt_and_keeps_failure(state, fake, monkeypatch):
    seen_during_connect = {}
    clock = {"now": GENERATED_AT}
    monkeypatch.setattr(connection_config, "_now", lambda: clock["now"])

    def connect(state_dir, target, **options):
        seen_during_connect["rows"] = connection_config.connection_targets(state_dir)["connections"]
        clock["now"] = CHECKED_AT
        raise PortmapError("gateway did not answer")

    monkeypatch.setattr(client, "connect", connect)

    with pytest.raises(PortmapError, match="gateway did not answer"):
        connection_config.managed_connect(state, "192.0.2.10", via="direct")

    in_flight = seen_during_connect["rows"][0]
    assert in_flight["status"] == "connecting"
    assert in_flight["target"] == "192.0.2.10"
    assert in_flight["last_checked_at"] is None

    failed = connection_config.connection_targets(state)["connections"][0]
    assert failed["status"] == "error"
    assert failed["error"] == "gateway did not answer"
    assert failed["latency_ms"] is None
    assert failed["last_checked_at"] == CHECKED_AT
    clock["now"] = "2026-10-12T01:00:00+00:00"
    assert connection_config.connection_targets(state)["connections"][0]["last_checked_at"] == CHECKED_AT


def test_ssh_selection_failure_is_preserved_and_retained(state, fake):
    from portmap.client import SSHSelectionRequired

    fake.connect_error = SSHSelectionRequired("direct connection failed; use --via ssh")

    with pytest.raises(SSHSelectionRequired):
        connection_config.managed_connect(state, "box", via="auto")

    row = connection_config.connection_targets(state)["connections"][0]
    assert row["status"] == "error"
    assert row["error"] == "direct connection failed; use --via ssh"


def test_failed_target_can_be_edited_and_retried(state, fake):
    fake.connect_error = PortmapError("no route to host")
    with pytest.raises(PortmapError):
        connection_config.managed_connect(state, "192.0.2.10", via="direct")
    row_id = connection_config.connection_targets(state)["connections"][0]["id"]

    payload = connection_config.mutate_connection_target(
        state, {"action": "save", "id": row_id, "target": "192.0.2.10", "via": "ssh"}
    )
    edited = payload["connections"][0]
    assert edited["via"] == "ssh"
    assert edited["status"] == "configured"
    assert "error" not in edited
    assert edited["last_checked_at"] is None

    fake.connect_error = None
    fake.connect_result = {"domain": "srv.portmap", "target": "192.0.2.10", "via": "ssh"}
    fake.rows = [client_row("srv.portmap", target="192.0.2.10", via="ssh", status="reachable", latency_ms=12.5, last_checked_at=CHECKED_AT)]

    payload = connection_config.mutate_connection_target(state, {"action": "connect", "id": row_id})
    connected = payload["connections"][0]
    assert connected["status"] == "reachable"
    assert connected["actual_via"] == "ssh"
    assert connected["latency_ms"] == 12.5
    assert connected["last_checked_at"] == CHECKED_AT
    assert fake.connect_calls[-1]["use_sudo"] is False


def test_connect_action_never_accepts_a_forwarded_target(state, fake):
    connection_config.mutate_connection_target(state, {"action": "save", "target": "192.0.2.10", "via": "direct"})
    row_id = connection_config.connection_targets(state)["connections"][0]["id"]

    with pytest.raises(PortmapError, match="unexpected field"):
        connection_config.mutate_connection_target(
            state, {"action": "connect", "id": row_id, "target": "198.51.100.99"}
        )
    assert fake.connect_calls == []


def test_disconnect_retains_saved_target_as_disconnected(state, fake):
    fake.connect_result = {"domain": "srv.portmap", "target": "192.0.2.10", "via": "direct"}
    connection_config.managed_connect(state, "192.0.2.10", via="direct")
    fake.rows = [client_row("srv.portmap")]
    row_id = connection_config.connection_targets(state)["connections"][0]["id"]

    # The fake disconnect drops the active row itself.
    result = connection_config.mutate_connection_target(state, {"action": "disconnect", "id": row_id})

    row = rows_by_target(result)["192.0.2.10"]
    assert row["status"] == "disconnected"
    assert row["domain"] == "srv.portmap"
    assert row["id"] == row_id
    assert "error" not in row


def test_managed_disconnect_returns_client_result_and_adopts_cli_profile(state, fake):
    fake.rows = [client_row("box.portmap", target="deploy@box", via="ssh")]

    result = connection_config.managed_disconnect(state, "box.portmap")

    assert result == {"disconnected": "box.portmap"}
    row = connection_config.connection_targets(state)["connections"][0]
    assert row["status"] == "disconnected"
    assert row["target"] == "deploy@box"
    assert row["via"] == "ssh"
    assert row["domain"] == "box.portmap"


def test_delete_disconnects_active_transport_first_and_retains_on_failure(state, fake):
    fake.connect_result = {"domain": "srv.portmap", "target": "192.0.2.10", "via": "direct"}
    connection_config.managed_connect(state, "192.0.2.10", via="direct")
    fake.rows = [client_row("srv.portmap")]
    row_id = connection_config.connection_targets(state)["connections"][0]["id"]

    fake.disconnect_error = PortmapError("cannot stop the tunnel")
    with pytest.raises(PortmapError):
        connection_config.mutate_connection_target(state, {"action": "delete", "id": row_id})
    retained = connection_config.connection_targets(state)["connections"]
    assert [row["id"] for row in retained] == [row_id]

    fake.disconnect_error = None
    payload = connection_config.mutate_connection_target(state, {"action": "delete", "id": row_id})
    assert payload["connections"] == []
    assert fake.disconnect_calls[0]["domain"] == "srv.portmap"


def test_delete_without_active_transport_just_removes_the_row(state, fake):
    connection_config.mutate_connection_target(state, {"action": "save", "target": "192.0.2.10", "via": "direct"})
    row_id = connection_config.connection_targets(state)["connections"][0]["id"]

    payload = connection_config.mutate_connection_target(state, {"action": "delete", "id": row_id})

    assert payload["connections"] == []
    assert fake.disconnect_calls == []


def test_delete_adopted_cli_profile_disconnects_without_resaving(state, fake):
    fake.rows = [client_row("box.portmap", target="deploy@box", via="ssh")]

    payload = connection_config.mutate_connection_target(state, {"action": "delete", "id": "box.portmap"})

    assert payload["connections"] == []
    assert fake.disconnect_calls[0]["domain"] == "box.portmap"


def test_editing_active_entry_requires_disconnect_first(state, fake):
    fake.connect_result = {"domain": "srv.portmap", "target": "192.0.2.10", "via": "direct"}
    connection_config.managed_connect(state, "192.0.2.10", via="direct")
    fake.rows = [client_row("srv.portmap")]
    row_id = connection_config.connection_targets(state)["connections"][0]["id"]

    with pytest.raises(PortmapError, match="disconnect it before editing"):
        connection_config.mutate_connection_target(
            state, {"action": "save", "id": row_id, "target": "192.0.2.11", "via": "ssh"}
        )

    fake.rows = []
    payload = connection_config.mutate_connection_target(
        state, {"action": "save", "id": row_id, "target": "192.0.2.11", "via": "ssh"}
    )
    edited = payload["connections"][0]
    assert edited["target"] == "192.0.2.11"
    assert edited["status"] == "configured"
    assert "domain" not in edited


def test_adopted_cli_profiles_are_listed_with_client_status(state, fake):
    fake.rows = [
        client_row("srv.portmap", status="reachable", latency_ms=8.0, last_checked_at=CHECKED_AT),
        client_row("box.portmap", target="deploy@box", via="ssh", status="unreachable", error="SSH tunnel is down"),
    ]

    payload = connection_config.connection_targets(state)

    by_id = {row["id"]: row for row in payload["connections"]}
    assert by_id["srv.portmap"]["status"] == "reachable"
    assert by_id["srv.portmap"]["actual_via"] == "direct"
    assert by_id["box.portmap"]["status"] == "unreachable"
    assert by_id["box.portmap"]["error"] == "SSH tunnel is down"
    assert by_id["box.portmap"]["latency_ms"] is None


def test_rows_never_expose_transport_details(state, fake):
    fake.rows = [client_row("srv.portmap", tunnel={"socket": "/run/tunnel.sock"}, backend_url="http://127.0.0.1:1234")]
    connection_config.mutate_connection_target(state, {"action": "save", "target": "192.0.2.10", "via": "direct"})

    payload = connection_config.connection_targets(state)

    for row in payload["connections"]:
        assert set(row) <= ROW_FIELDS
        assert "tunnel" not in row
        assert "backend_url" not in row
        assert "127.0.0.1" not in json.dumps(row)
        assert ".sock" not in json.dumps(row)






def test_generated_at_is_iso8601(state, fake):
    payload = connection_config.connection_targets(state)
    parsed = dt.datetime.fromisoformat(payload["generated_at"])
    assert parsed.tzinfo is not None


# ------------------------------------------------------- catalog HTTP API


def request_catalog(
    path: str,
    *,
    method: str = "GET",
    body: str | None = None,
    scope: str = "private",
    host: str | None = None,
    origin: str | None = None,
):
    server = catalog.CatalogServer(("127.0.0.1", 0), scope=scope)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        headers = {}
        if body is not None:
            headers["content-type"] = "application/json"
        if host is not None:
            headers["Host"] = host
        if origin is not None:
            headers["Origin"] = origin
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def deny_agent(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("this request must not reach the host agent")

    monkeypatch.setattr(catalog, "agent_connection_targets", deny)
    monkeypatch.setattr(catalog, "agent_mutate_connection", deny)


def test_machines_capability_follows_server_scope():
    status, _, body = request_catalog("/api/machines")
    assert status == 200
    assert json.loads(body)["can_manage_connections"] is True

    status, _, body = request_catalog("/api/machines", scope="public")
    assert status == 200
    assert json.loads(body)["can_manage_connections"] is False


def test_public_listener_denies_control_before_ipc(monkeypatch):
    deny_agent(monkeypatch)

    status, _, _ = request_catalog("/api/connections", scope="public")
    assert status == 404

    status, _, _ = request_catalog(
        "/api/connections",
        method="POST",
        scope="public",
        body='{"action":"save","target":"192.0.2.10","via":"direct"}',
        origin="http://laptop.portmap",
        host="laptop.portmap",
    )
    assert status == 404


def test_control_endpoints_require_valid_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(tmp_path / "absent.json"))
    deny_agent(monkeypatch)

    status, _, body = request_catalog("/api/connections")
    assert status == 503
    assert json.loads(body)["error"] == "catalog access snapshot is unavailable"

    status, _, body = request_catalog(
        "/api/connections",
        method="POST",
        body='{"action":"save","target":"192.0.2.10","via":"direct"}',
        host="laptop.portmap",
        origin="http://laptop.portmap",
    )
    assert status == 503




@pytest.mark.parametrize("query", ["?check=0", "?check=true", "?check=", "?check=1&check=1"])
def test_private_get_rejects_other_check_values(query, monkeypatch):
    deny_agent(monkeypatch)

    status, _, body = request_catalog(f"/api/connections{query}")

    assert status == 400
    assert json.loads(body)["error"] == "check must be exactly 1"


@pytest.mark.parametrize(
    ("host", "origin", "expected_status"),
    [
        ("laptop.portmap", None, 403),
        ("laptop.portmap", "http://evil.example", 403),
        ("localhost:58082", "http://localhost:58082", 400),
        ("localhost:58082", "http://localhost", 403),
        ("laptop.portmap:80", "http://laptop.portmap", 400),
        ("laptop.portmap", "http://laptop.portmap:0", 403),
        ("laptop.portmap", "http://laptop.portmap:bad", 403),
        ("laptop.portmap", "http://[invalid", 403),
    ],
)
def test_mutations_require_same_origin(monkeypatch, host, origin, expected_status):
    deny_agent(monkeypatch)
    # Only an accepted origin may reach body validation; no agent call is needed.
    status, _, _ = request_catalog(
        "/api/connections", method="POST", body="not json", host=host, origin=origin
    )
    assert status == expected_status


def test_post_rejects_invalid_json_bodies(monkeypatch):
    deny_agent(monkeypatch)
    oversized = '{"action":"save","target":"' + "a" * 20000 + '","via":"direct"}'

    for body in (None, "not json", "[1, 2]", oversized):
        status, _, payload = request_catalog(
            "/api/connections", method="POST", body=body, host="laptop.portmap", origin="http://laptop.portmap"
        )
        assert status in (400, 413)
        assert "error" in json.loads(payload)


def test_agent_failures_map_to_sanitized_errors(monkeypatch):
    def unavailable(*args, **kwargs):
        raise agent_client.AgentUnavailable("agent socket unavailable")

    monkeypatch.setattr(catalog, "agent_connection_targets", unavailable)
    status, _, body = request_catalog("/api/connections")
    assert status == 502
    assert "host agent unavailable" in json.loads(body)["error"]

    def rejected(payload):
        raise agent_client.AgentRequestError(400, "no saved connection 'x'")

    monkeypatch.setattr(catalog, "agent_mutate_connection", rejected)
    status, _, body = request_catalog(
        "/api/connections",
        method="POST",
        body='{"action":"delete","id":"x"}',
        host="laptop.portmap",
        origin="http://laptop.portmap",
    )
    assert status == 400
    assert json.loads(body)["error"] == "no saved connection 'x'"

    def internal(payload):
        raise agent_client.AgentRequestError(500, "failed to update the connection list")

    monkeypatch.setattr(catalog, "agent_mutate_connection", internal)
    status, _, body = request_catalog(
        "/api/connections",
        method="POST",
        body='{"action":"delete","id":"x"}',
        host="laptop.portmap",
        origin="http://laptop.portmap",
    )
    assert status == 502


# ------------------------------------------------------ agent Unix socket


@contextmanager
def agent_serving(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(agent.AgentHandler, "log_message", lambda self, *args: None)
    socket_path = tmp_path / "agent.sock"
    server = agent.ThreadingUnixHTTPServer(str(socket_path), agent.AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield socket_path
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()




def test_agent_validates_check_flag(tmp_path, monkeypatch):

    with agent_serving(tmp_path, monkeypatch) as socket_path:
        status, body = agent_client.request_json_status("GET", "/access/connections?check=0", socket_path=socket_path)

    assert status == 400
    assert body["error"] == "check must be exactly 1"






def test_agent_rejects_invalid_json_body(tmp_path, monkeypatch):

    with agent_serving(tmp_path, monkeypatch) as socket_path:
        connection = agent_client.UnixHTTPConnection(str(socket_path))
        connection.request("POST", "/access/connections", body=b"not json", headers={"content-type": "application/json"})
        response = connection.getresponse()
        body = response.read()
        connection.close()

    assert response.status == 400
    assert b"error" in body


def test_agent_keeps_unknown_paths_not_found(tmp_path, monkeypatch):
    with agent_serving(tmp_path, monkeypatch) as socket_path:
        status, _ = agent_client.request_json_status("GET", "/access/other", socket_path=socket_path)

    assert status == 404
