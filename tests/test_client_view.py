import json

import pytest

from portmap.client_view import project_catalog, request_host
from portmap.errors import PortmapError


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


def local_profile(**changes) -> dict:
    return connection_profile(
        via="local",
        target="local",
        backend_url="http://127.0.0.1:39080",
        remote_http_port=80,
        http_port=80,
        tcp_address="127.0.0.1",
        tunnel=None,
        **changes,
    )


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


def test_ssh_projection_uses_local_listener_and_preserves_remote_context() -> None:
    source = remote_catalog([{
        "kind": "http",
        "host": "web.dev.sample.remote.portmap",
        "host_port": 8080,
        "url": "https://web.dev.sample.remote.portmap:8443/path%2Fpart?q=one#two",
        "container_port": 3000,
        "preserve_host": "true",
    }])
    source_json = json.dumps(source, sort_keys=True)
    result = project_catalog(source, connection_profile(via="ssh"), 18080)
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
    assert result["client_access"]["transport"] == "ssh"
    assert result["client_access"]["dns_setup_command"] == "portmap setup"
    assert result["client_access"]["dns_unset_command"] == "portmap teardown"
    assert "private-tunnel-token" not in json.dumps(result)
    assert "/private/controller/socket" not in json.dumps(result)
    assert json.dumps(source, sort_keys=True) == source_json
    default_port = project_catalog(source, connection_profile(via="ssh"), 80)
    assert default_port["services"][0]["endpoints"][0]["url"] == "http://web.dev.sample.remote.portmap/path%2Fpart?q=one#two"


def test_direct_projection_keeps_remote_urls_and_ports() -> None:
    source = remote_catalog([{
        "kind": "http",
        "host": "web.dev.sample.remote.portmap",
        "host_port": 8080,
        "url": "http://web.dev.sample.remote.portmap:8080/path%2Fpart?q=one#two",
    }])
    result = project_catalog(source, connection_profile(), 8080)
    endpoint = result["services"][0]["endpoints"][0]
    # DNS maps the domain to the verified remote IP, so the remote catalog URL
    # is already reachable exactly as advertised; nothing is rewritten.
    assert endpoint["url"] == "http://web.dev.sample.remote.portmap:8080/path%2Fpart?q=one#two"
    assert endpoint["host_port"] == 8080
    assert endpoint["transport_supported"] is True
    assert result["http_port"] == 8080
    assert result["client_access"]["transport"] == "direct"


def test_direct_apex_endpoint_is_reachable_without_local_interception() -> None:
    result = project_catalog(
        remote_catalog([{"kind": "http", "host": "remote.portmap", "url": "http://remote.portmap:8080/"}]),
        connection_profile(),
        8080,
    )
    assert result["services"][0]["endpoints"][0]["transport_supported"] is True


def test_ssh_apex_application_is_not_advertised_over_the_local_catalog():
    result = project_catalog(
        remote_catalog([{"kind": "http", "host": "remote.portmap", "url": "http://remote.portmap:8080/"}]),
        connection_profile(via="ssh"),
        18080,
    )
    assert result["services"][0]["endpoints"][0]["transport_supported"] is False


def test_local_projection_uses_public_port_and_local_raw_addresses() -> None:
    endpoints = [
        {"kind": "http", "host": "web.dev.sample.remote.portmap", "host_port": 8080, "url": "https://web.dev.sample.remote.portmap:8443/"},
        {"kind": "http", "host": "remote.portmap", "url": "http://remote.portmap:8080/"},
        {"kind": "tcp", "host_port": 15432},
        {"kind": "udp", "host_port": 15353},
        {"kind": "range", "protocol": "tcp", "host_port": 16000, "range_start": 16000, "range_end": 16010},
    ]
    result = project_catalog(remote_catalog(endpoints), local_profile(), 80)
    http_endpoint, apex, tcp, udp, port_range = result["services"][0]["endpoints"]
    assert http_endpoint["url"] == "http://web.dev.sample.remote.portmap/"
    assert http_endpoint["host_port"] == 80
    assert http_endpoint["transport_supported"] is True
    # The local backend owns its whole domain, including the apex.
    assert apex["transport_supported"] is True
    assert apex["url"] == "http://remote.portmap/"
    for endpoint in (tcp, udp, port_range):
        assert endpoint["host"] == "127.0.0.1"
        assert endpoint["transport_supported"] is True
    assert (tcp["host_port"], udp["host_port"]) == (15432, 15353)
    assert result["client_access"]["transport"] == "local"


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


def test_unsupported_transport_is_rejected() -> None:
    with pytest.raises(PortmapError, match="invalid local gateway state"):
        project_catalog(remote_catalog([]), connection_profile(via="weird"), 80)



def test_projection_uses_controller_dns_when_provided() -> None:
    result = project_catalog(
        remote_catalog([]),
        connection_profile(via="ssh"),
        18080,
        dns_address="169.254.254.53",
        dns_port=1053,
    )
    assert result["dns_server"] == "169.254.254.53"
    assert result["dns_port"] == 1053


def test_request_host_accepts_authority_forms_only() -> None:
    assert request_host("server.portmap") == "server.portmap"
    assert request_host("Server.Portmap:8080") == "server.portmap"
    assert request_host("server.portmap:notaport") is None
    assert request_host("server.portmap:0") is None
    assert request_host("192.168.201.16") == "192.168.201.16"
    assert request_host("user@host") is None
    assert request_host("") is None
