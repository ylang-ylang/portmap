import http.client
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from portmap.catalog import read_static_asset
from portmap.client_view import ClientViewServer, project_catalog


def connection_profile(**changes) -> dict:
    return {
        "domain": "remote.portmap",
        "target": "configured-ssh-alias",
        "via": "direct",
        "backend_url": "http://192.0.2.10:8080",
        "remote_http_port": 8080,
        "address": "127.0.0.1",
        "http_port": 18080,
        "scheme": "http",
        "tcp_address": "192.0.2.10",
        "tcp_ports": [],
        "include_tcp": False,
        "tunnel": {"control_path": "/private/controller/socket", "owner_token": "private-tunnel-token"},
        **changes,
    }


def remote_catalog(endpoints: list[dict]) -> dict:
    return {
        "generated_at": "2026-09-23T00:00:00Z",
        "dns_domain": "remote.portmap",
        "dns_server": "172.19.0.2",
        "http_port": 8080,
        "agent": {"available": True, "message": "ready", "generated_at": "2026-09-23T00:00:00Z"},
        "services": [{
            "repo_id": "sample-id",
            "repo_name": "sample",
            "worktree": "/work/sample@dev",
            "worktree_root": "/work/sample@wt",
            "branch": "dev",
            "compose_project": "sample_dev",
            "endpoints": endpoints,
        }],
        "worktrees": [{
            "repo_id": "sample-id",
            "worktree": "/work/sample@dev",
            "worktree_status": "running",
            "compose_project": "sample_dev",
            "branch_tip_epoch": 1234,
            "submodule_parent": "/work/parent",
        }],
    }


def test_http_projection_uses_local_listener_and_preserves_remote_context() -> None:
    source = remote_catalog([{
        "kind": "http",
        "host": "web.dev.sample.remote.portmap",
        "host_port": 8080,
        "url": "https://web.dev.sample.remote.portmap:8443/path%2Fpart?q=one#two",
        "container_port": 3000,
        "preserve_host": "true",
    }])
    source_json = json.dumps(source, sort_keys=True)
    result = project_catalog(source, connection_profile(), 18080)
    endpoint = result["services"][0]["endpoints"][0]
    assert endpoint["url"] == "http://web.dev.sample.remote.portmap:18080/path%2Fpart?q=one#two"
    assert endpoint["host"] == "web.dev.sample.remote.portmap"
    assert endpoint["host_port"] == 18080
    assert endpoint["transport_supported"] is True
    assert endpoint["container_port"] == 3000
    assert result["http_port"] == 18080
    assert result["agent"] == source["agent"]
    assert result["worktrees"] == source["worktrees"]
    assert {key: value for key, value in result["services"][0].items() if key != "endpoints"} == {
        key: value for key, value in source["services"][0].items() if key != "endpoints"
    }
    assert result["client_access"]["dns_setup_command"] == "portmap client setup"
    assert result["client_access"]["dns_unset_command"] == "portmap client teardown"
    assert "private-tunnel-token" not in json.dumps(result)
    assert "/private/controller/socket" not in json.dumps(result)
    assert json.dumps(source, sort_keys=True) == source_json
    default_port = project_catalog(source, connection_profile(), 80)
    assert default_port["services"][0]["endpoints"][0]["url"] == "http://web.dev.sample.remote.portmap/path%2Fpart?q=one#two"


def test_custom_or_malformed_http_hosts_are_not_advertised_as_local_routes() -> None:
    endpoints = [
        {"kind": "http", "host": "custom.example", "url": "http://custom.example:8080/path"},
        {"kind": "http", "url": "http://notremote.portmap:8080/"},
        {"kind": "http", "url": "http://web.remote.portmap.example:8080/"},
        {"kind": "http", "url": "http://web.remote.portmap:invalid/"},
        {"kind": "http", "host": "custom.example", "url": "http://web.remote.portmap:8080/"},
    ]
    result = project_catalog(remote_catalog(endpoints), connection_profile(), 18080)
    for original, projected in zip(endpoints, result["services"][0]["endpoints"], strict=True):
        assert projected["url"] == original["url"]
        assert projected.get("host") == original.get("host")
        assert projected["transport_supported"] is False
        assert "managed DNS zone" in projected["reason"]


def test_direct_raw_endpoints_use_remote_address_not_http_loopback() -> None:
    endpoints = [
        {"kind": "tcp", "host": "172.19.0.2", "host_port": 18000},
        {"kind": "udp", "host": "172.19.0.2", "host_port": 19000},
        {"kind": "range", "protocol": "udp", "host_port": 19001, "range_start": 20000, "range_end": 20010},
        {"kind": "tcp", "container_port": 5432},
    ]
    result = project_catalog(remote_catalog(endpoints), connection_profile(), 18080)
    tcp, udp, port_range, unpublished = result["services"][0]["endpoints"]
    for endpoint in (tcp, udp, port_range):
        assert endpoint["host"] == "192.0.2.10"
        assert endpoint["transport_supported"] is True
    assert tcp["host_port"] == 18000
    assert udp["host_port"] == 19000
    assert (port_range["host_port"], port_range["range_start"], port_range["range_end"]) == (19001, 20000, 20010)
    assert unpublished["transport_supported"] is False
    assert "published port" in unpublished["reason"]


def test_ssh_only_advertises_individually_forwarded_tcp_ports() -> None:
    endpoints = [
        {"kind": "tcp", "host": "192.0.2.10", "host_port": 18000},
        {"kind": "tcp", "host": "192.0.2.10", "host_port": 18001},
        {"kind": "udp", "host": "192.0.2.10", "host_port": 18000},
        {"kind": "range", "protocol": "tcp", "host_port": 18000, "range_start": 20000, "range_end": 20010},
        {"kind": "range", "protocol": "udp", "host_port": 18000, "range_start": 21000, "range_end": 21010},
    ]
    profile = connection_profile(via="ssh", tcp_address="127.77.0.2", tcp_ports=[18000], include_tcp=True)
    result = project_catalog(remote_catalog(endpoints), profile, 80)
    tcp, unforwarded, udp, tcp_range, udp_range = result["services"][0]["endpoints"]
    assert tcp["host"] == "127.77.0.2"
    assert tcp["host_port"] == 18000
    assert tcp["transport_supported"] is True
    for endpoint in (unforwarded, udp, tcp_range, udp_range):
        assert endpoint["transport_supported"] is False
        assert "not forwarded" in endpoint["reason"]
    assert unforwarded["host"] == "192.0.2.10"
    assert result["client_access"]["transport"] == "ssh"


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def request(server, path: str, *, host: str = "remote.portmap", method: str = "GET", duplicate_host: str | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host)
        if duplicate_host is not None:
            connection.putheader("Host", duplicate_host)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


def replace_json(path: Path, value: dict) -> None:
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(value), encoding="utf-8")
    staging.replace(path)


@pytest.fixture
def local_view(tmp_path):
    catalog = remote_catalog([{"kind": "http", "url": "http://app.remote.portmap:8080/"}])

    class RemoteHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/registry.json":
                self.send_error(404)
                return
            body = json.dumps(catalog).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with serving(ThreadingHTTPServer(("127.0.0.1", 0), RemoteHandler)) as remote:
        profile = connection_profile(backend_url=f"http://127.0.0.1:{remote.server_port}")
        replace_json(tmp_path / "connections.json", {"version": 1, "connections": {"remote.portmap": profile}})
        replace_json(tmp_path / "gateway.json", {"http_port": 18080, "owner": "private-gateway-owner"})
        with serving(ClientViewServer(tmp_path, 0, "view-owner")) as server:
            yield server, catalog, tmp_path


def test_registry_fetches_real_remote_catalog_and_reloads_atomic_state(local_view) -> None:
    server, catalog, state_dir = local_view
    status, _, body = request(server, "/registry.json", host="remote.portmap:18080")
    assert status == 200
    projected = json.loads(body)
    assert projected["services"][0]["endpoints"][0]["url"] == "http://app.remote.portmap:18080/"
    assert projected["worktrees"] == catalog["worktrees"]
    for private in (b"private-gateway-owner", b"private-tunnel-token", b"/private/controller/socket", b"view-owner"):
        assert private not in body

    replace_json(state_dir / "gateway.json", {"http_port": 80})
    status, _, body = request(server, "/registry.json")
    assert status == 200
    assert json.loads(body)["services"][0]["endpoints"][0]["url"] == "http://app.remote.portmap/"

    replace_json(state_dir / "connections.json", {"version": 1, "connections": {}})
    assert request(server, "/registry.json")[0] == 404
    assert request(server, "/index.html")[0] == 404


def test_host_scope_is_exact_and_only_valid_numeric_ports_are_stripped(local_view) -> None:
    server, _, _ = local_view
    assert request(server, "/registry.json", host="REMOTE.PORTMAP:80")[0] == 200
    for host in (
        "app.remote.portmap",
        "remote.portmap.example",
        "remote.portmap:",
        "remote.portmap:abc",
        "remote.portmap:0",
        "remote.portmap:65536",
        "remote.portmap:80:80",
        "remote.portmap@other.example",
        "[remote.portmap]:80",
    ):
        assert request(server, "/registry.json", host=host)[0] == 404
        assert request(server, "/favicon.svg", host=host)[0] == 404
    assert request(server, "/registry.json", duplicate_host="other.portmap")[0] == 404
    assert request(server, "/healthz", host="other.portmap")[0] == 404


def test_static_files_reuse_catalog_serving_and_reject_traversal(local_view) -> None:
    server, _, _ = local_view
    for path, asset in (
        ("/", "index.html"),
        ("/index.html", "index.html"),
        ("/assets/dns-check.svg", "assets/dns-check.svg"),
        ("/favicon.svg", "favicon.svg"),
    ):
        status, headers, body = request(server, path)
        expected, content_type = read_static_asset(asset)
        assert status == 200
        assert body == expected
        assert headers["Content-Type"] == content_type
    assert request(server, "/assets/../../client_view.py")[0] == 404
    status, headers, body = request(server, "/favicon.svg", method="HEAD")
    assert status == 200
    assert body == b""
    assert int(headers["Content-Length"]) == len(read_static_asset("favicon.svg")[0])


def test_local_post_actions_are_rejected_without_docker_or_agent_access(local_view, monkeypatch) -> None:
    server, _, _ = local_view

    def forbidden(*args, **kwargs):
        pytest.fail("local catalog must not query or mutate local containers")

    monkeypatch.setattr("portmap.catalog.docker_request", forbidden)
    monkeypatch.setattr("portmap.catalog.agent_compose_up_worktree", forbidden)
    for path in ("/actions/compose-up", "/actions/compose-down", "/actions/compose-restart"):
        status, headers, body = request(server, path, method="POST")
        assert status == 405
        assert headers["Allow"] == "GET, HEAD"
        assert body == b""
    assert request(server, "/actions/compose-up", host="other.portmap", method="POST")[0] == 404
    assert request(server, "/registry.json")[0] == 200


def test_mismatched_remote_domain_does_not_project_another_catalog(local_view) -> None:
    server, catalog, _ = local_view
    catalog["dns_domain"] = "unexpected.portmap"
    status, _, body = request(server, "/registry.json")
    assert status == 502
    assert json.loads(body) == {"error": "remote catalog unavailable; check portmap connections"}


def test_health_is_available_before_connections_exist(tmp_path) -> None:
    with serving(ClientViewServer(tmp_path, 0, "readiness-owner")) as server:
        status, _, body = request(server, "/healthz", host=f"127.0.0.1:{server.server_port}")
        assert status == 200
        assert json.loads(body) == {"ok": True, "owner": "readiness-owner"}
        status, _, body = request(server, "/healthz", host="portmap-gateway.invalid")
        assert status == 200
        assert json.loads(body) == {"ok": True, "owner": "readiness-owner"}
        assert request(server, "/registry.json", host="portmap-gateway.invalid")[0] == 404
        assert request(server, "/registry.json", host="127.0.0.1")[0] == 404
        assert request(server, "/healthz", host="unregistered.portmap")[0] == 404


def test_apex_application_is_not_advertised_over_the_local_catalog():
    result = project_catalog(
        remote_catalog([{"kind": "http", "host": "remote.portmap", "url": "http://remote.portmap:8080/"}]),
        connection_profile(),
        18080,
    )
    assert result["services"][0]["endpoints"][0]["transport_supported"] is False
