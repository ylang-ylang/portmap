import json
from pathlib import Path

import pytest

from portmap import client
from portmap.client_discovery import GatewayInfo
from portmap.errors import PortmapError


def gateway(address="192.0.2.10", port=8080, domain="remote.portmap"):
    return GatewayInfo(
        domain=domain,
        http_port=port,
        address=address,
        scheme="http",
        catalog={"http_port": port, "dns_domain": domain, "services": []},
        catalog_url=f"http://{address}:{port}/registry.json",
    )


def resolver_installed(monkeypatch):
    monkeypatch.setattr(client, "resolver_status", lambda path: {"installed": True})


class Transport:
    """Recorder for the DNS/gateway/verify side effects of a client mutation."""

    def __init__(self, monkeypatch, *, gateway_status=None):
        self.records = {}
        self.gateway_profiles = []
        self.gateway_kwargs = []
        self.verified = []
        self.probes = []
        self.gateway_state = gateway_status or {"running": False, "http_port": None}
        monkeypatch.setattr(client, "sync_dns", self._sync_dns)
        monkeypatch.setattr(client, "sync_gateway", self._sync_gateway)
        monkeypatch.setattr(client, "verify_resolution", self._verify)
        monkeypatch.setattr(client, "probe_gateway", self._probe)
        monkeypatch.setattr(client, "dns_status", lambda path: {"running": bool(self.records)})
        monkeypatch.setattr(client, "gateway_status", lambda path: dict(self.gateway_state))

    def _sync_dns(self, path, records):
        self.records = dict(records)
    def _sync_gateway(self, path, profiles, **kwargs):
        self.gateway_profiles.append(dict(profiles))
        self.gateway_kwargs.append(kwargs)
        return dict(self.gateway_state)

    def _verify(self, domain, address):
        self.verified.append((domain, address))

    def _probe(self, target, **kwargs):
        self.probes.append(target)
        # A faithful remote answers with the domain it is probed for.
        return gateway(domain=kwargs.get("expected_domain") or "remote.portmap")


def test_direct_connect_publishes_remote_dns_without_gateway(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    transport = Transport(monkeypatch)
    result = client.connect(tmp_path, "192.0.2.10", via="direct")
    assert result["via"] == "direct"
    assert result["address"] == "192.0.2.10"
    assert result["already_connected"] is False
    assert result["dns_ready"] is True
    assert transport.records == {"remote.portmap": "192.0.2.10"}
    assert transport.verified == [("remote.portmap", "192.0.2.10")]
    # No SSH/local profile exists: the gateway is only asked to manage nothing
    # and no local port is needed for a direct route.
    assert transport.gateway_profiles == [{}]
    assert "http://remote.portmap:8080" in transport.probes
    profile = client._load(tmp_path)["remote.portmap"]
    assert profile["target"] == "192.0.2.10"
    assert profile["remote_http_port"] == 8080
    assert profile["backend_url"] == "http://192.0.2.10:8080"
    assert profile["catalog_url"] == "http://remote.portmap:8080/"
    assert "http_port" not in profile
    assert "tunnel" not in profile


def test_equivalent_direct_targets_are_idempotent(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    transport = Transport(monkeypatch)
    assert client.connect(tmp_path, "192.0.2.10", via="direct")["already_connected"] is False
    for equivalent in ("192.0.2.10:80", "http://192.0.2.10", "http://192.0.2.10:80"):
        result = client.connect(tmp_path, equivalent, via="direct")
        assert result["already_connected"] is True
    assert len(client._load(tmp_path)) == 1
    assert transport.records == {"remote.portmap": "192.0.2.10"}


def test_direct_target_hostname_and_port_variants_stay_distinct(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    Transport(monkeypatch)
    client.connect(tmp_path, "192.0.2.10", via="direct")
    # A different port is a different gateway, and a hostname spelling must
    # keep its own identity: both hit the domain collision guard.
    with pytest.raises(PortmapError, match="already connected"):
        client.connect(tmp_path, "192.0.2.10:8080", via="direct")
    with pytest.raises(PortmapError, match="already connected"):
        client.connect(tmp_path, "gw.example.com:8080", via="direct")
    assert len(client._load(tmp_path)) == 1


def test_separate_catalog_is_not_gateway_readiness(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    published = {}
    monkeypatch.setattr(client, "sync_dns", lambda path, records: published.update(records))
    monkeypatch.setattr(client, "sync_gateway", lambda path, profiles, **kwargs: {"running": False, "http_port": None})

    def probe(target, **kwargs):
        if target == "192.0.2.10:80":
            info = gateway()
            return GatewayInfo(info.domain, info.http_port, info.address, info.scheme, info.catalog, "http://192.0.2.10:80/registry.json")
        raise PortmapError("gateway listener is down")

    monkeypatch.setattr(client, "probe_gateway", probe)
    with pytest.raises(PortmapError, match="gateway listener"):
        client.connect(tmp_path, "192.0.2.10:80", via="direct")
    assert client._load(tmp_path) == {}
    assert published == {}


def test_first_ssh_connection_publishes_without_an_existing_gateway(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    live_tunnels = set()

    def start(target, **kwargs):
        live_tunnels.add(target)
        return {"target": target}

    def stop(tunnel, **kwargs):
        live_tunnels.discard(tunnel["target"])

    transport = Transport(monkeypatch)
    monkeypatch.setattr(client, "start_tunnel", start)
    monkeypatch.setattr(client, "stop_tunnel", stop)
    monkeypatch.setattr(client, "add_forward", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "cancel_forward", lambda *args, **kwargs: None)
    result = client.connect(tmp_path, "configured-ssh-host", via="ssh", use_sudo=False)
    assert result["via"] == "ssh"
    assert result["http_port"] == 80
    assert result["address"] == "127.0.0.1"
    assert transport.records == {"remote.portmap": "127.0.0.1"}
    assert "http://remote.portmap" in transport.probes
    profile = client._load(tmp_path)["remote.portmap"]
    assert profile["tunnel"] == {"target": "configured-ssh-host"}
    assert profile["http_port"] == 80


def test_failed_dns_publication_closes_new_ssh_connection(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    live_tunnels = set()

    def start(target, **kwargs):
        live_tunnels.add(target)
        return {"target": target}

    def stop(tunnel, **kwargs):
        live_tunnels.discard(tunnel["target"])

    def publish(path, records):
        if records:
            raise PortmapError("DNS refused the new configuration")

    monkeypatch.setattr(client, "start_tunnel", start)
    monkeypatch.setattr(client, "stop_tunnel", stop)
    monkeypatch.setattr(client, "add_forward", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "cancel_forward", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "probe_gateway", lambda *args, **kwargs: gateway("127.0.0.1"))
    monkeypatch.setattr(client, "sync_gateway", lambda path, profiles, **kwargs: {"running": True, "http_port": 80})
    monkeypatch.setattr(client, "verify_resolution", lambda *args: None)
    monkeypatch.setattr(client, "sync_dns", publish)
    with pytest.raises(PortmapError, match="DNS refused"):
        client.connect(tmp_path, "configured-ssh-host", via="ssh")
    assert live_tunnels == set()
    assert client._load(tmp_path) == {}


def test_failed_disconnect_preserves_connection_for_retry(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    existing = {"remote.portmap": {"domain": "remote.portmap", "address": "127.0.0.1", "backend_url": "http://127.0.0.1:30001", "tunnel": {"target": "host"}}}
    client._save(tmp_path, existing)
    published = {"remote.portmap": "127.0.0.1"}

    def publish(path, records):
        published.clear()
        published.update(records)

    def stop(tunnel):
        raise PortmapError("SSH master did not close")

    monkeypatch.setattr(client, "sync_dns", publish)
    monkeypatch.setattr(client, "sync_gateway", lambda path, profiles, **kwargs: None)
    monkeypatch.setattr(client, "stop_tunnel", stop)
    with pytest.raises(PortmapError, match="did not close"):
        client.disconnect(tmp_path, "remote")
    assert client._load(tmp_path) == existing
    assert published == {"remote.portmap": "127.0.0.1"}


def test_disconnect_keeps_direct_dns_records_and_gates_ssh_only_routes(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    profiles = {
        "direct.portmap": {"domain": "direct.portmap", "via": "direct", "address": "192.0.2.10", "target": "192.0.2.10"},
        "ssh.portmap": {"domain": "ssh.portmap", "via": "ssh", "address": "127.0.0.1", "target": "host", "tunnel": {"target": "host"}},
    }
    client._save(tmp_path, profiles)
    transport = Transport(monkeypatch)
    stopped = []
    monkeypatch.setattr(client, "stop_tunnel", lambda tunnel: stopped.append(tunnel))
    client.disconnect(tmp_path, "ssh.portmap")
    assert stopped == [{"target": "host"}]
    assert transport.records == {"direct.portmap": "192.0.2.10"}
    # The remaining connection is direct: no gateway route is requested.
    assert transport.gateway_profiles == [{}]


def test_teardown_refuses_while_local_role_is_active(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    effects = []
    monkeypatch.setattr(client, "teardown_resolver", lambda *args, **kwargs: effects.append("resolver"))
    monkeypatch.setattr(client, "stop_dns", lambda *args: effects.append("dns"))
    monkeypatch.setattr(client, "stop_gateway", lambda *args, **kwargs: effects.append("gateway"))
    monkeypatch.setattr(client, "sync_gateway", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "sync_dns", lambda *args, **kwargs: None)
    client.set_local_server(tmp_path, {"domain": "local.portmap", "backend_port": 39080})
    effects.clear()
    with pytest.raises(PortmapError, match="local publishing is active; run portmap down before teardown"):
        client.teardown_client(tmp_path)
    assert effects == []


def test_set_local_server_publishes_role_without_touching_remote_connections(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    remote = {"remote.portmap": {"domain": "remote.portmap", "via": "direct", "address": "192.0.2.10", "target": "192.0.2.10", "backend_url": "http://192.0.2.10:8080"}}
    client._save(tmp_path, remote)
    transport = Transport(monkeypatch)
    result = client.set_local_server(tmp_path, {"domain": "Local.Portmap", "backend_port": 39080})
    assert result["local"]["via"] == "local"
    assert result["local"]["remote_http_port"] == 80
    assert result["local"]["address"] == "127.0.0.1"
    assert result["local"]["target"] == "local"
    assert client._load(tmp_path) == remote
    assert json.loads((tmp_path / "local-server.json").read_text())["local"]["domain"] == "local.portmap"
    # DNS carries both records; only the local role is routed through the gateway.
    assert transport.records == {"remote.portmap": "192.0.2.10", "local.portmap": "127.0.0.1"}


def test_set_local_server_honors_saved_public_port_and_rejects_collisions(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    from portmap import client_gateway

    transport = Transport(monkeypatch)
    client_gateway.save_public_http_port(tmp_path, 9090)
    result = client.set_local_server(tmp_path, {"domain": "local.portmap", "backend_url": "http://127.0.0.1:39080"})
    assert result["local"]["remote_http_port"] == 9090
    result = client.set_local_server(tmp_path, {"domain": "other.portmap", "backend_port": 39081, "http_port": 80})
    assert result["local"]["remote_http_port"] == 80

    client._save(tmp_path, {"other.portmap": {"domain": "other.portmap", "via": "direct", "address": "192.0.2.10", "target": "x"}})
    with pytest.raises(PortmapError, match="used by a remote connection"):
        client.set_local_server(tmp_path, {"domain": "other.portmap", "backend_port": 39080})
    with pytest.raises(PortmapError):
        client.set_local_server(tmp_path, {"domain": "not-portmap.example", "backend_port": 39080})


def test_set_local_server_removal_stops_the_role_only(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    remote = {"remote.portmap": {"domain": "remote.portmap", "via": "direct", "address": "192.0.2.10", "target": "192.0.2.10", "backend_url": "http://192.0.2.10:8080"}}
    client._save(tmp_path, remote)
    transport = Transport(monkeypatch)
    client.set_local_server(tmp_path, {"domain": "local.portmap", "backend_port": 39080})
    result = client.set_local_server(tmp_path, None)
    assert result["local"] is None
    assert transport.records == {"remote.portmap": "192.0.2.10"}
    assert transport.gateway_profiles[-1] == {}
    assert client._load(tmp_path) == remote


def test_connect_refuses_domain_owned_by_local_role(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    transport = Transport(monkeypatch)
    monkeypatch.setattr(client, "probe_gateway", lambda *args, **kwargs: gateway(domain="local.portmap"))
    client.set_local_server(tmp_path, {"domain": "local.portmap", "backend_port": 39080})
    with pytest.raises(PortmapError, match="already connected"):
        client.connect(tmp_path, "192.0.2.10", via="direct")
    assert "local.portmap" not in client._load(tmp_path)
    assert transport.records == {"local.portmap": "127.0.0.1"}


def test_insecure_state_fails_before_resolver_or_daemon_changes(tmp_path: Path, monkeypatch):
    state = tmp_path / "shared-state"
    state.mkdir()
    state.chmod(0o775)
    effects = []
    monkeypatch.setattr(client, "setup_resolver", lambda *args, **kwargs: effects.append("resolver"))
    monkeypatch.setattr(client, "sync_dns", lambda *args, **kwargs: effects.append("dns"))
    with pytest.raises(PortmapError):
        client.setup_client(state)
    assert effects == []
    assert state.stat().st_mode & 0o777 == 0o775
    assert not (state / "client.lock").exists()


def test_setup_cleanup_continues_after_gateway_cleanup_error(tmp_path: Path, monkeypatch):
    active = {"resolver": False, "dns": False}
    monkeypatch.setattr(client, "resolver_status", lambda path: {"installed": False})
    monkeypatch.setattr(client, "dns_status", lambda path: {"running": False})
    monkeypatch.setattr(client, "gateway_status", lambda path: {"running": False})
    monkeypatch.setattr(client, "setup_resolver", lambda *args, **kwargs: active.update(resolver=True))
    monkeypatch.setattr(client, "sync_dns", lambda *args, **kwargs: active.update(dns=True))
    monkeypatch.setattr(client, "teardown_resolver", lambda *args, **kwargs: active.update(resolver=False))
    monkeypatch.setattr(client, "stop_dns", lambda *args: active.update(dns=False))

    def failed_gateway(*args, **kwargs):
        raise PortmapError("gateway unavailable")

    monkeypatch.setattr(client, "sync_gateway", failed_gateway)
    monkeypatch.setattr(client, "stop_gateway", failed_gateway)
    with pytest.raises(PortmapError):
        client.setup_client(tmp_path)
    assert active == {"resolver": False, "dns": False}


def test_teardown_removes_direct_connection_without_an_http_gateway(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    Transport(monkeypatch)
    client.connect(tmp_path, "192.0.2.10", via="direct")
    # Keep the OS resolver isolated; daemon cleanup uses its real empty-state paths.
    monkeypatch.setattr(client, "teardown_resolver", lambda *args, **kwargs: None)

    result = client.teardown_client(tmp_path, use_sudo=False)

    assert result["stopped"] is True
    assert client._load(tmp_path) == {}


def test_teardown_reaps_dns_even_when_gateway_cleanup_fails(tmp_path: Path, monkeypatch):
    active = {"resolver": True, "dns": True}
    monkeypatch.setattr(client, "teardown_resolver", lambda *args, **kwargs: active.update(resolver=False))
    monkeypatch.setattr(client, "stop_dns", lambda *args: active.update(dns=False))

    def failed_gateway(*args, **kwargs):
        raise PortmapError("gateway ownership check failed")

    monkeypatch.setattr(client, "stop_gateway", failed_gateway)
    with pytest.raises(PortmapError):
        client.teardown_client(tmp_path)
    assert active == {"resolver": False, "dns": False}


def test_setup_direct_only_starts_dns_without_any_gateway(tmp_path: Path, monkeypatch):
    from portmap import client_gateway

    monkeypatch.setattr(client, "resolver_status", lambda path: {"installed": True})
    monkeypatch.setattr(client, "setup_resolver", lambda *args, **kwargs: {"installed": True})
    monkeypatch.setattr(client, "dns_status", lambda path: {"running": True})
    monkeypatch.setattr(client, "gateway_status", lambda path: {"running": False})
    monkeypatch.setattr(client, "stop_gateway", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "teardown_resolver", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "stop_dns", lambda *args: None)
    # A missing Traefik binary must not matter for direct-only setups: the real
    # sync_gateway returns a stopped state without ever resolving a binary.
    monkeypatch.setattr(client_gateway, "find_native_binary", lambda _name: pytest.fail("direct-only setup must not need Traefik"))
    client._save(tmp_path, {"remote.portmap": {"domain": "remote.portmap", "via": "direct", "address": "192.0.2.10", "target": "192.0.2.10", "backend_url": "http://192.0.2.10:8080"}})
    records = {}
    monkeypatch.setattr(client, "sync_dns", lambda path, value: records.update(value))
    result = client.setup_client(tmp_path)
    assert result["gateway"]["running"] is False
    assert records == {"remote.portmap": "192.0.2.10"}


def test_setup_saves_preferred_public_port_and_refreshes_stale_endpoints(tmp_path: Path, monkeypatch):
    from portmap import client_gateway

    monkeypatch.setattr(client, "resolver_status", lambda path: {"installed": True})
    monkeypatch.setattr(client, "setup_resolver", lambda *args, **kwargs: {"installed": True})
    monkeypatch.setattr(client, "dns_status", lambda path: {"running": True})
    monkeypatch.setattr(client, "gateway_status", lambda path: {"running": False})
    monkeypatch.setattr(client, "sync_dns", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "sync_gateway", lambda *args, **kwargs: {"running": False, "http_port": None})
    # The saved SSH tunnel is alive, so setup must not attempt a restoration.
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel: True)
    profiles = {
        "direct.portmap": {"domain": "direct.portmap", "via": "direct", "address": "192.0.2.10", "target": "192.0.2.10", "endpoints": [{"url": "http://app.direct.portmap:8080/"}]},
        "ssh.portmap": {"domain": "ssh.portmap", "via": "ssh", "address": "127.0.0.1", "target": "host", "http_port": 18080, "endpoints": [{"url": "http://app.ssh.portmap:18080/"}], "tunnel": {"target": "host"}},
    }
    client._save(tmp_path, profiles)
    client.setup_client(tmp_path, http_port=9090)
    assert client_gateway.preferred_http_port(tmp_path) == 9090
    saved = client._load(tmp_path)
    # Direct endpoints keep the remote port; SSH endpoint data embedded the old
    # public port and is dropped until that connection is refreshed.
    assert saved["direct.portmap"]["endpoints"] == [{"url": "http://app.direct.portmap:8080/"}]
    assert "endpoints" not in saved["ssh.portmap"]
    assert saved["ssh.portmap"]["http_port"] == 9090


def _legacy_layout(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy" / "portmap-client"
    legacy.mkdir(parents=True, mode=0o700)
    monkeypatch.delenv("PORTMAP_CLIENT_STATE_DIR", raising=False)
    monkeypatch.delenv("PORTMAP_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "legacy"))
    profiles = {
        "old.portmap": {
            "domain": "old.portmap", "target": "192.0.2.10:8080", "ssh_target": None, "via": "direct",
            "address": "127.0.0.1", "http_port": 18080, "remote_http_port": 8080, "scheme": "http",
            "backend_url": "http://192.0.2.10:8080", "tcp_address": "192.0.2.10", "tcp_ports": [], "include_tcp": False,
        },
        "tun.portmap": {
            "domain": "tun.portmap", "target": "ssh-host", "ssh_target": "ssh-host", "via": "ssh",
            "address": "127.0.0.1", "http_port": 18080, "remote_http_port": 8080, "scheme": "http",
            "backend_url": "http://127.0.0.1:30001", "tunnel": {"target": "ssh-host", "config_file": "/saved/ssh/config"}, "tcp_address": "127.77.0.2", "include_tcp": True,
        },
    }
    client._save(legacy, profiles)
    return legacy, profiles


def _setup_mocks(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "resolver_status", lambda path: {"installed": True})
    monkeypatch.setattr(client, "setup_resolver", lambda *args, **kwargs: {"installed": True})
    monkeypatch.setattr(client, "dns_status", lambda path: {"running": True})
    monkeypatch.setattr(client, "gateway_status", lambda path: {"running": False})
    monkeypatch.setattr(client, "sync_dns", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "sync_gateway", lambda *args, **kwargs: {"running": False, "http_port": None})
    # Restoration gets one clean attempt against a mocked unreachable host.
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel: False)
    monkeypatch.setattr(client, "start_tunnel", lambda target, **kwargs: (_ for _ in ()).throw(PortmapError("ssh: no route to host")))
    monkeypatch.setattr(client, "stop_tunnel", lambda tunnel: None)


def test_setup_imports_legacy_state_and_stops_only_owned_resources(tmp_path: Path, monkeypatch):
    legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    _setup_mocks(monkeypatch, tmp_path)
    stopped = []
    monkeypatch.setattr(client, "stop_gateway", lambda path, **kwargs: stopped.append(("gateway", path)))
    monkeypatch.setattr(client, "stop_dns", lambda path: stopped.append(("dns", path)))
    monkeypatch.setattr(client, "stop_tunnel", lambda tunnel: stopped.append(("tunnel", tunnel)))
    monkeypatch.setattr(client, "teardown_resolver", lambda path, **kwargs: stopped.append(("resolver", path)))
    client.setup_client(tmp_path / "state")
    assert ("gateway", legacy) in stopped
    assert ("dns", legacy) in stopped
    assert ("tunnel", {"target": "ssh-host", "config_file": "/saved/ssh/config"}) in stopped
    assert ("resolver", legacy) in stopped
    # The legacy directory and its saved targets are preserved on disk.
    assert client._load(legacy) == profiles
    migrated = client._load(tmp_path / "state")
    assert migrated["old.portmap"]["via"] == "direct"
    assert migrated["old.portmap"]["address"] == "192.0.2.10"
    assert migrated["old.portmap"]["catalog_url"] == "http://old.portmap:8080/"
    assert migrated["tun.portmap"]["via"] == "ssh"
    assert "tunnel" not in migrated["tun.portmap"]
    assert "backend_url" not in migrated["tun.portmap"]
    assert migrated["tun.portmap"]["target"] == "ssh-host"
    # The exact saved SSH config identity is preserved for restoration.
    assert migrated["tun.portmap"]["ssh_config"] == "/saved/ssh/config"


def test_setup_does_not_reimport_disconnected_legacy_profiles(tmp_path: Path, monkeypatch):
    legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    _setup_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "stop_gateway", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "stop_dns", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "teardown_resolver", lambda *args, **kwargs: None)
    state = tmp_path / "state"
    client.setup_client(state)
    for domain in profiles:
        client.disconnect(state, domain)

    client.setup_client(state)

    assert client._load(state) == {}
    assert client._load(legacy) == profiles


def test_legacy_import_failure_keeps_everything_intact(tmp_path: Path, monkeypatch):
    legacy, _profiles = _legacy_layout(monkeypatch, tmp_path)
    _setup_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "stop_gateway", lambda path, **kwargs: (_ for _ in ()).throw(PortmapError("unverifiable ownership")))

    def must_not_touch(*args, **kwargs):
        pytest.fail("a failed legacy import must not change the new resolver or DNS")

    monkeypatch.setattr(client, "setup_resolver", must_not_touch)
    with pytest.raises(PortmapError, match="legacy portmap-client gateway"):
        client.setup_client(tmp_path / "state")
    assert client._load(tmp_path / "state") == {}


def test_legacy_import_skips_used_state_and_isolated_override(tmp_path: Path, monkeypatch):
    legacy, _profiles = _legacy_layout(monkeypatch, tmp_path)
    _setup_mocks(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(client, "stop_gateway", lambda path, **kwargs: calls.append(path))
    monkeypatch.setattr(client, "stop_dns", lambda path: calls.append(path))
    monkeypatch.setattr(client, "teardown_resolver", lambda path, **kwargs: calls.append(path))

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    client._save(state, {"new.portmap": {"domain": "new.portmap", "via": "direct", "address": "192.0.2.99", "target": "192.0.2.99", "backend_url": "http://192.0.2.99:8080"}})
    client.setup_client(state)
    assert calls == []

    monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(tmp_path / "isolated"))
    isolated = tmp_path / "isolated"
    isolated.mkdir(mode=0o700)
    client.setup_client(isolated)
    assert calls == []
    assert client._load(isolated) == {}


def test_shared_state_override_does_not_touch_default_legacy_access(tmp_path: Path, monkeypatch):
    legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    _setup_mocks(monkeypatch, tmp_path)
    root = tmp_path / "isolated"
    monkeypatch.setenv("PORTMAP_STATE_DIR", str(root))

    def foreign_gateway(*args, **kwargs):
        pytest.fail("isolated setup must not stop the default legacy gateway")

    monkeypatch.setattr(client, "stop_gateway", foreign_gateway)
    client.setup_client(root / "client")

    assert client._load(root / "client") == {}
    assert client._load(legacy) == profiles


def ssh_transport(monkeypatch, *, fail_start_for=()):
    """Mock the SSH transport machinery; a faithful remote serves the saved domain."""
    calls = {"started": [], "stopped": [], "configs": []}

    def start(target, **kwargs):
        if target in fail_start_for:
            raise PortmapError(f"ssh: no route to {target}")
        calls["started"].append(target)
        calls["configs"].append(kwargs.get("config_file"))
        # Mirror the real descriptor shape: JSON-safe, config as a string.
        return {"target": target, "config_file": str(kwargs["config_file"]) if kwargs.get("config_file") else None}

    monkeypatch.setattr(client, "start_tunnel", start)
    monkeypatch.setattr(client, "stop_tunnel", lambda tunnel: calls["stopped"].append(tunnel))
    monkeypatch.setattr(client, "add_forward", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "cancel_forward", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel: False)
    return calls


def test_connect_revives_dormant_saved_ssh_without_forced_disconnect(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    transport = Transport(monkeypatch, gateway_status={"running": True, "http_port": 80})
    calls = ssh_transport(monkeypatch)
    client._save(tmp_path, {"remote.portmap": {
        "domain": "remote.portmap", "target": "saved-host", "ssh_target": "saved-host", "via": "ssh",
        "address": "127.0.0.1", "scheme": "http", "tcp_address": "127.77.0.5", "tcp_ports": [],
        "include_tcp": False, "ssh_config": "/saved/ssh/config",
    }})
    result = client.connect(tmp_path, "saved-host", via="ssh")
    assert result["already_connected"] is True
    assert result["restored"] is True
    assert result["dns_ready"] is True
    # The exact saved identity and config were reused, never an alias.
    assert calls["started"] == ["saved-host"]
    assert calls["configs"] == [Path("/saved/ssh/config")]
    profile = client._load(tmp_path)["remote.portmap"]
    assert profile["tunnel"]["target"] == "saved-host"
    assert profile["http_port"] == 80
    assert transport.records == {"remote.portmap": "127.0.0.1"}
    assert transport.verified == [("remote.portmap", "127.0.0.1")]
    assert "http://remote.portmap" in transport.probes


def test_connect_revives_dead_tunnel_record_after_stopping_it(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    transport = Transport(monkeypatch, gateway_status={"running": True, "http_port": 80})
    calls = ssh_transport(monkeypatch)
    stale = {"target": "saved-host", "alive": False}
    client._save(tmp_path, {"remote.portmap": {
        "domain": "remote.portmap", "target": "saved-host", "ssh_target": "saved-host", "via": "ssh",
        "address": "127.0.0.1", "http_port": 80, "backend_url": "http://127.0.0.1:30001", "tunnel": stale,
        "tcp_address": "127.77.0.5", "tcp_ports": [], "include_tcp": False,
    }})
    result = client.connect(tmp_path, "saved-host", via="ssh")
    assert result["restored"] is True
    assert calls["stopped"] == [stale]
    assert client._load(tmp_path)["remote.portmap"]["tunnel"]["target"] == "saved-host"
    assert transport.records == {"remote.portmap": "127.0.0.1"}


def test_dormant_ssh_domain_never_resolves_to_unrouted_loopback(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    transport = Transport(monkeypatch)
    client._save(tmp_path, {"remote.portmap": {
        "domain": "remote.portmap", "target": "saved-host", "ssh_target": "saved-host", "via": "ssh",
        "address": "127.0.0.1", "tcp_address": "127.77.0.5", "include_tcp": False,
    }})
    assert client._records(client._load(tmp_path)) == {}
    client.set_local_server(tmp_path, {"domain": "local.portmap", "backend_port": 39080})
    # Only genuinely routed domains are published; the dormant one is absent
    # rather than pointing at loopback where no owned listener exists.
    assert transport.records == {"local.portmap": "127.0.0.1"}


def test_failed_restore_keeps_saved_profile_and_reports_reconnect(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    transport = Transport(monkeypatch, gateway_status={"running": True, "http_port": 80})
    ssh_transport(monkeypatch, fail_start_for=("saved-host",))
    client._save(tmp_path, {"remote.portmap": {
        "domain": "remote.portmap", "target": "saved-host", "ssh_target": "saved-host", "via": "ssh",
        "address": "127.0.0.1", "tcp_address": "127.77.0.5", "include_tcp": True,
    }})
    with pytest.raises(PortmapError, match="could not restore saved SSH connection remote.portmap through saved-host"):
        client.connect(tmp_path, "saved-host", via="ssh")
    kept = client._load(tmp_path)["remote.portmap"]
    assert kept["target"] == "saved-host"
    assert kept["include_tcp"] is True
    assert "tunnel" not in kept
    assert transport.records == {}


def test_restore_refuses_remote_now_serving_a_different_domain(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    transport = Transport(monkeypatch, gateway_status={"running": True, "http_port": 80})
    calls = ssh_transport(monkeypatch)

    def relocated(target, **kwargs):
        return gateway(domain=kwargs.get("expected_domain") or "other.portmap")

    monkeypatch.setattr(client, "probe_gateway", relocated)
    client._save(tmp_path, {"remote.portmap": {
        "domain": "remote.portmap", "target": "saved-host", "ssh_target": "saved-host", "via": "ssh",
        "address": "127.0.0.1", "tcp_address": "127.77.0.5", "include_tcp": False,
    }})
    with pytest.raises(PortmapError, match="now serves 'other.portmap', not the saved 'remote.portmap'"):
        client.connect(tmp_path, "saved-host", via="ssh")
    kept = client._load(tmp_path)["remote.portmap"]
    assert "tunnel" not in kept
    assert calls["stopped"] == [{"target": "saved-host", "config_file": None}]
    assert transport.records == {}


def test_setup_restores_each_dormant_ssh_once_and_reports_failures(tmp_path: Path, monkeypatch):
    resolver_installed(monkeypatch)
    transport = Transport(monkeypatch, gateway_status={"running": True, "http_port": 80})
    calls = ssh_transport(monkeypatch, fail_start_for=("bad-host",))

    def faithful(target, **kwargs):
        # The good remote serves its own saved domain on unqualified probes.
        return gateway(domain=kwargs.get("expected_domain") or "good.portmap")

    monkeypatch.setattr(client, "probe_gateway", faithful)
    monkeypatch.setattr(client, "setup_resolver", lambda *args, **kwargs: {"installed": True})
    monkeypatch.setattr(client, "dns_status", lambda path: {"running": True})
    monkeypatch.setattr(client, "teardown_resolver", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "stop_dns", lambda *args: None)
    monkeypatch.setattr(client, "stop_gateway", lambda *args, **kwargs: None)
    client._save(tmp_path, {
        "good.portmap": {"domain": "good.portmap", "target": "good-host", "ssh_target": "good-host", "via": "ssh", "address": "127.0.0.1", "tcp_address": "127.77.0.1"},
        "bad.portmap": {"domain": "bad.portmap", "target": "bad-host", "ssh_target": "bad-host", "via": "ssh", "address": "127.0.0.1", "tcp_address": "127.77.0.2"},
        "direct.portmap": {"domain": "direct.portmap", "target": "192.0.2.10", "via": "direct", "address": "192.0.2.10", "backend_url": "http://192.0.2.10:8080"},
    })
    result = client.setup_client(tmp_path)
    assert [row["domain"] for row in result["restored_connections"]] == ["good.portmap"]
    assert [row["domain"] for row in result["restore_failures"]] == ["bad.portmap"]
    assert "could not restore" in result["restore_failures"][0]["error"]
    # Exactly one attempt per dormant connection, using the saved identity.
    assert calls["started"] == ["good-host"]
    saved = client._load(tmp_path)
    assert saved["good.portmap"]["tunnel"]["target"] == "good-host"
    assert "tunnel" not in saved["bad.portmap"]
    assert calls["stopped"] == []
    assert transport.records == {"good.portmap": "127.0.0.1", "direct.portmap": "192.0.2.10"}
