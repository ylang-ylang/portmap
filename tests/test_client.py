from pathlib import Path

import pytest

from portmap import client
from portmap.client_discovery import GatewayInfo
from portmap.errors import PortmapError


def gateway(address="192.0.2.10", port=8080):
    return GatewayInfo(
        domain="remote.portmap",
        http_port=port,
        address=address,
        scheme="http",
        catalog={"http_port": port, "dns_domain": "remote.portmap", "services": []},
        catalog_url=f"http://{address}:{port}/registry.json",
    )


def dns_ready(monkeypatch):
    monkeypatch.setattr(client, "resolver_status", lambda path: {"installed": True})
    monkeypatch.setattr(client, "dns_status", lambda path: {"running": True})
    monkeypatch.setattr(client, "gateway_status", lambda path: {"running": True, "http_port": 18080})
    monkeypatch.setattr(client, "sync_gateway", lambda path, profiles, **kwargs: {"running": True, "http_port": 18080})


def test_separate_catalog_is_not_gateway_readiness(tmp_path: Path, monkeypatch):
    dns_ready(monkeypatch)
    published = {}
    monkeypatch.setattr(client, "sync_dns", lambda path, records: published.update(records))

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


def test_duplicate_hostname_does_not_replace_existing_route(tmp_path: Path, monkeypatch):
    dns_ready(monkeypatch)
    existing = {"remote.portmap": {"domain": "remote.portmap", "target": "192.0.2.10:8080", "ssh_target": None, "via": "direct", "address": "127.0.0.1", "http_port": 18080, "scheme": "http", "backend_url": "http://192.0.2.10:8080"}}
    client._save(tmp_path, existing)
    published = {"remote.portmap": "127.0.0.1"}

    def publish(path, records):
        published.clear()
        published.update(records)

    monkeypatch.setattr(client, "sync_dns", publish)
    monkeypatch.setattr(client, "probe_gateway", lambda *args, **kwargs: gateway("192.0.2.11"))
    with pytest.raises(PortmapError, match="already connected"):
        client.connect(tmp_path, "192.0.2.11:8080", via="direct")
    assert client._load(tmp_path) == existing
    assert published == {"remote.portmap": "127.0.0.1"}


def test_failed_dns_publication_closes_new_ssh_connection(tmp_path: Path, monkeypatch):
    dns_ready(monkeypatch)
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
    monkeypatch.setattr(client, "sync_dns", publish)
    with pytest.raises(PortmapError, match="DNS refused"):
        client.connect(tmp_path, "configured-ssh-host", via="ssh")
    assert live_tunnels == set()
    assert client._load(tmp_path) == {}


def test_failed_disconnect_preserves_connection_for_retry(tmp_path: Path, monkeypatch):
    dns_ready(monkeypatch)
    existing = {"remote.portmap": {"domain": "remote.portmap", "address": "127.0.0.1", "backend_url": "http://127.0.0.1:30001", "tunnel": {"target": "host"}}}
    client._save(tmp_path, existing)
    published = {"remote.portmap": "127.0.0.1"}

    def publish(path, records):
        published.clear()
        published.update(records)

    def stop(tunnel):
        raise PortmapError("SSH master did not close")

    monkeypatch.setattr(client, "sync_dns", publish)
    monkeypatch.setattr(client, "stop_tunnel", stop)
    with pytest.raises(PortmapError, match="did not close"):
        client.disconnect(tmp_path, "remote")
    assert client._load(tmp_path) == existing
    assert published == {"remote.portmap": "127.0.0.1"}


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


def test_teardown_reaps_dns_even_when_gateway_cleanup_fails(tmp_path: Path, monkeypatch):
    active = {"resolver": True, "dns": True}
    monkeypatch.setattr(client, "teardown_resolver", lambda *args, **kwargs: active.update(resolver=False))
    monkeypatch.setattr(client, "stop_dns", lambda *args: active.update(dns=False))

    def failed_gateway(*args):
        raise PortmapError("gateway ownership check failed")

    monkeypatch.setattr(client, "stop_gateway", failed_gateway)
    with pytest.raises(PortmapError):
        client.teardown_client(tmp_path)
    assert active == {"resolver": False, "dns": False}


def test_saved_connections_prevent_offline_shared_port_change(tmp_path: Path, monkeypatch):
    profiles = {"remote.portmap": {"domain": "remote.portmap", "http_port": 18080}}
    client._save(tmp_path, profiles)

    def must_not_change_system(*args, **kwargs):
        pytest.fail("port validation must precede DNS setup")

    monkeypatch.setattr(client, "setup_resolver", must_not_change_system)
    with pytest.raises(PortmapError):
        client.setup_client(tmp_path, http_port=28080)
    assert client._load(tmp_path) == profiles
