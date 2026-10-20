import json
import shutil
import socket
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import client, infrastructure
from portmap.client_discovery import GatewayInfo
from portmap.errors import PortmapError


@pytest.fixture(autouse=True)
def isolated_legacy_state(tmp_path, monkeypatch):
    """Keep the standalone legacy state directory out of every test."""
    monkeypatch.delenv("PORTMAP_CLIENT_STATE_DIR", raising=False)
    monkeypatch.delenv("PORTMAP_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "legacy-home"))


@contextmanager
def exposed_state_root():
    """A shared-root layout with no private owned ancestor above the state."""
    root = Path(tempfile.mkdtemp(prefix="portmap-shared-", dir="/tmp"))
    root.chmod(0o755)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def gateway(address="192.0.2.10", port=8080, domain="remote.portmap"):
    return GatewayInfo(
        domain=domain,
        http_port=port,
        address=address,
        scheme="http",
        catalog={"http_port": port, "dns_domain": domain, "services": []},
        catalog_url=f"http://{address}:{port}/registry.json",
    )


def direct_profile(domain="remote.portmap", address="192.0.2.10", port=8080):
    return {
        "domain": domain,
        "target": address,
        "ssh_target": None,
        "via": "direct",
        "address": address,
        "remote_http_port": port,
        "scheme": "http",
        "backend_url": f"http://{address}:{port}",
        "catalog_url": f"http://{domain}:{port}/",
        "tcp_address": address,
        "tcp_ports": [],
        "include_tcp": False,
    }


class Infrastructure:
    """Recorder for the shared controller side effects of a client mutation."""

    def __init__(self, monkeypatch, *, running=False, http_port=None, migration=None, migration_error=None):
        self.synced = []
        self.stopped = []
        self.agents = []
        self.migrations = []
        self._migration = migration
        self._migration_error = migration_error
        self.state = {
            "configured": running,
            "running": running,
            "gateway": {"running": running, "http_port": http_port},
            "dns": {"running": running},
            "resolver": {"installed": True},
        }
        monkeypatch.setattr(client, "sync_infrastructure", self._sync)
        monkeypatch.setattr(client, "stop_infrastructure", self._stop)
        monkeypatch.setattr(client, "infrastructure_status", self._status)
        monkeypatch.setattr(client, "ensure_agent_started", self._agent)
        monkeypatch.setattr(client, "load_portmap_settings", lambda *args, **kwargs: None)
        monkeypatch.setattr(client, "_migrate_legacy_publishing", self._migrate)

    def _sync(self, path, profiles, local, **kwargs):
        self.synced.append((json.loads(json.dumps(profiles)), json.loads(json.dumps(local)) if local else None, kwargs))
        return json.loads(json.dumps(self.state))

    def _stop(self, path, **kwargs):
        self.stopped.append(path)

    def _status(self, path, **kwargs):
        return json.loads(json.dumps(self.state))

    def _migrate(self, settings, *, use_sudo=True):
        self.migrations.append(settings)
        if self._migration_error is not None:
            raise self._migration_error
        return self._migration
    def _agent(self, settings, *, use_sudo=True, access_state_dir=None):
        self.agents.append(access_state_dir)
        return SimpleNamespace(running=True, message="agent ready")


def ssh_transport(monkeypatch, *, fail_start_for=(), domain="remote.portmap"):
    """Mock the SSH transport machinery; a faithful remote serves the saved domain."""
    calls = {"started": [], "stopped": [], "configs": [], "http_forwards": [], "cancelled_http": [], "tcp_forwards": []}

    def start(target, **kwargs):
        if target in fail_start_for:
            raise PortmapError(f"ssh: no route to {target}")
        calls["started"].append(target)
        calls["configs"].append(kwargs.get("config_file"))
        # Mirror the real descriptor shape: JSON-safe, config as a string.
        return {"target": target, "config_file": str(kwargs["config_file"]) if kwargs.get("config_file") else None}

    def add_http(tunnel, *, state_dir, domain, remote_port, **kwargs):
        calls["http_forwards"].append((tunnel["target"], domain, remote_port))
        return f"/tmp/portmap-http-{len(calls['http_forwards']):024x}.sock"

    monkeypatch.setattr(client, "start_tunnel", start)
    monkeypatch.setattr(client, "stop_tunnel", lambda tunnel, **kwargs: calls["stopped"].append(tunnel))
    monkeypatch.setattr(client, "add_forward", lambda tunnel, **kwargs: calls["tcp_forwards"].append(kwargs))
    monkeypatch.setattr(client, "cancel_forward", lambda tunnel, **kwargs: None)
    monkeypatch.setattr(client, "add_http_forward", add_http)
    monkeypatch.setattr(client, "cancel_http_forward", lambda tunnel, **kwargs: calls["cancelled_http"].append(kwargs))
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: False)
    monkeypatch.setattr(client, "probe_gateway", lambda target, **kwargs: gateway(domain=kwargs.get("expected_domain") or domain))
    monkeypatch.setattr(client, "probe_gateway_socket", lambda path, **kwargs: gateway(domain=kwargs.get("expected_domain") or domain))
    return calls


def faithful_probe(monkeypatch, domain="remote.portmap"):
    monkeypatch.setattr(client, "probe_gateway", lambda target, **kwargs: gateway(domain=kwargs.get("expected_domain") or domain))


@pytest.mark.parametrize("parent_mode", [0o700, 0o2700])
def test_private_client_state_preserves_group_inheritance(tmp_path, parent_mode):
    parent = tmp_path / "shared-volume"
    parent.mkdir()
    parent.chmod(parent_mode)
    directory = parent / "client"
    profile = direct_profile()
    with client.client_lock(directory):
        client._save(directory, {profile["domain"]: profile})
    assert client._load(directory)[profile["domain"]] == profile
    assert directory.stat().st_mode & 0o777 == 0o700
    assert directory.stat().st_mode & 0o2000 == parent_mode & 0o2000


@pytest.mark.parametrize("mode", [0o750, 0o770, 0o2750])
def test_group_access_without_private_ancestor_remains_forbidden(mode):
    # Recovery needs a provably private owned ancestor; a shared root has
    # none, so the gate stays closed and the state is left untouched.
    with exposed_state_root() as root:
        directory = root / "client"
        directory.mkdir()
        directory.chmod(mode)
        saved = directory / "connections.json"
        saved.write_text("existing private data\n")
        with pytest.raises(PortmapError, match="owned private directory"):
            with client.client_lock(directory):
                pytest.fail("group-readable state must not be opened")
        assert saved.read_text() == "existing private data\n"
        assert directory.stat().st_mode & 0o7777 == mode
        assert not (directory / "client.lock").exists()


def test_foreground_exit_retires_dead_transport_without_forgetting_identity(tmp_path, monkeypatch):
    infra = Infrastructure(monkeypatch, running=True, http_port=80)
    dead = {
        "domain": "dead.portmap", "via": "ssh", "target": "configured-dead",
        "ssh_target": "configured-dead", "remote_http_port": 8080, "http_port": 80,
        "tcp_address": "127.77.1.2", "tcp_ports": [18123], "include_tcp": True,
        "http_socket": "/tmp/dead.sock",
        "tunnel": {"target": "configured-dead", "config_file": str(tmp_path / "custom-ssh.conf")},
    }
    live = {
        **dead, "domain": "live.portmap", "target": "configured-live",
        "ssh_target": "configured-live", "http_socket": "/tmp/live.sock",
        "tunnel": {"target": "configured-live", "config_file": None},
    }
    direct = direct_profile()
    with client.client_lock(tmp_path):
        client._save(tmp_path, {"dead.portmap": dead, "live.portmap": live, direct["domain"]: direct})
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: tunnel["target"] == "configured-live")

    assert client.retire_stopped_transports(tmp_path, use_sudo=False) == []
    saved = client._load(tmp_path)
    dormant = saved["dead.portmap"]
    assert "tunnel" not in dormant and "http_socket" not in dormant
    assert dormant["ssh_target"] == "configured-dead"
    assert dormant["ssh_config"] == str(tmp_path / "custom-ssh.conf")
    assert dormant["remote_http_port"] == 8080
    assert dormant["tcp_ports"] == [18123]
    assert dormant["include_tcp"] is True
    assert saved["live.portmap"] == live
    assert saved[direct["domain"]] == direct
    assert infra.stopped == []

# Direct connections


def test_direct_connect_publishes_verified_remote_origin(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch)
    faithful_probe(monkeypatch)
    result = client.connect(tmp_path, "192.0.2.10", via="direct")
    assert result["via"] == "direct"
    assert result["address"] == "192.0.2.10"
    assert result["already_connected"] is False
    assert result["dns_ready"] is True
    published, local, _ = infra.synced[-1]
    # The controller receives the verified numeric remote origin; no local
    # role or SSH route is requested for a direct host.
    assert published["remote.portmap"]["address"] == "192.0.2.10"
    assert published["remote.portmap"]["via"] == "direct"
    assert local is None
    profile = client._load(tmp_path)["remote.portmap"]
    assert profile["target"] == "192.0.2.10"
    assert profile["remote_http_port"] == 8080
    assert profile["backend_url"] == "http://192.0.2.10:8080"
    assert profile["catalog_url"] == "http://remote.portmap:8080/"
    assert "http_port" not in profile
    assert "tunnel" not in profile
    assert "http_socket" not in profile


def test_equivalent_direct_targets_are_idempotent(tmp_path: Path, monkeypatch):
    Infrastructure(monkeypatch)
    faithful_probe(monkeypatch)
    assert client.connect(tmp_path, "192.0.2.10", via="direct")["already_connected"] is False
    for equivalent in ("192.0.2.10:80", "http://192.0.2.10", "http://192.0.2.10:80"):
        result = client.connect(tmp_path, equivalent, via="direct")
        assert result["already_connected"] is True
    assert len(client._load(tmp_path)) == 1


def test_direct_target_hostname_and_port_variants_stay_distinct(tmp_path: Path, monkeypatch):
    Infrastructure(monkeypatch)
    faithful_probe(monkeypatch)
    client.connect(tmp_path, "192.0.2.10", via="direct")
    # A different port is a different gateway, and a hostname spelling must
    # keep its own identity: both hit the domain collision guard.
    with pytest.raises(PortmapError, match="already connected"):
        client.connect(tmp_path, "192.0.2.10:8080", via="direct")
    with pytest.raises(PortmapError, match="already connected"):
        client.connect(tmp_path, "gw.example.com:8080", via="direct")
    assert len(client._load(tmp_path)) == 1


def test_separate_catalog_is_not_gateway_readiness(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch)

    def probe(target, **kwargs):
        if target == "192.0.2.10:80":
            info = gateway()
            return GatewayInfo(info.domain, info.http_port, info.address, info.scheme, info.catalog, "http://192.0.2.10:80/registry.json")
        raise PortmapError("gateway listener is down")

    monkeypatch.setattr(client, "probe_gateway", probe)
    with pytest.raises(PortmapError, match="gateway listener"):
        client.connect(tmp_path, "192.0.2.10:80", via="direct")
    assert client._load(tmp_path) == {}
    assert infra.synced == []


# ---------------------------------------------------------------------------
# SSH connections


def test_first_ssh_connection_routes_through_a_unix_socket_forward(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    result = client.connect(tmp_path, "configured-ssh-host", via="ssh", use_sudo=False)
    assert result["via"] == "ssh"
    assert result["http_port"] == 80
    assert result["already_connected"] is False
    assert result["dns_ready"] is True
    # The persistent upstream is the owned Unix-socket forward; the only TCP
    # forward is the short bootstrap listener on loopback.
    assert calls["http_forwards"] == [("configured-ssh-host", "remote.portmap", 8080)]
    assert calls["tcp_forwards"], "the short TCP bootstrap must run"
    assert all(forward["local_address"] == "127.0.0.1" for forward in calls["tcp_forwards"])
    profile = client._load(tmp_path)["remote.portmap"]
    assert profile["tunnel"]["target"] == "configured-ssh-host"
    assert profile["http_socket"].startswith("/tmp/portmap-http-")
    assert profile["http_port"] == 80
    assert "backend_url" not in profile
    published, _local, _ = infra.synced[-1]
    assert published["remote.portmap"]["http_socket"] == profile["http_socket"]


def test_failed_publication_closes_new_ssh_connection(tmp_path: Path, monkeypatch):
    Infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)

    def refused(path, profiles, local, **kwargs):
        if profiles:
            raise PortmapError("controller refused the new configuration")

    monkeypatch.setattr(client, "sync_infrastructure", refused)
    with pytest.raises(PortmapError, match="controller refused"):
        client.connect(tmp_path, "configured-ssh-host", via="ssh")
    assert calls["stopped"], "the master must be closed again"
    assert calls["cancelled_http"], "the socket forward must be retired"
    assert client._load(tmp_path) == {}


def test_failed_disconnect_preserves_connection_for_retry(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch)
    existing = {"remote.portmap": {
        "domain": "remote.portmap", "via": "ssh", "address": "127.0.0.1", "target": "host",
        "tunnel": {"target": "host"}, "http_socket": "/tmp/portmap-http-old.sock", "remote_http_port": 8080,
    }}
    client._save(tmp_path, existing)
    cancelled = []
    monkeypatch.setattr(client, "cancel_http_forward", lambda tunnel, **kwargs: cancelled.append(kwargs))
    failure = PortmapError("SSH master did not close")

    def stop(tunnel, **kwargs):
        raise failure

    monkeypatch.setattr(client, "stop_tunnel", stop)
    with pytest.raises(PortmapError) as caught:
        client.disconnect(tmp_path, "remote")
    assert caught.value is failure
    assert client._load(tmp_path) == existing
    assert cancelled == [{"socket_path": "/tmp/portmap-http-old.sock", "remote_port": 8080}]
    assert "remote.portmap" in infra.synced[-1][0]


def test_disconnect_keeps_direct_records_and_retires_ssh_transport(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch)
    profiles = {
        "direct.portmap": direct_profile("direct.portmap"),
        "ssh.portmap": {
            "domain": "ssh.portmap", "via": "ssh", "address": "127.0.0.1", "target": "host",
            "tunnel": {"target": "host"}, "http_socket": "/tmp/portmap-http-a.sock", "remote_http_port": 8080,
        },
    }
    client._save(tmp_path, profiles)
    stopped, cancelled = [], []
    monkeypatch.setattr(client, "stop_tunnel", lambda tunnel, **kwargs: stopped.append(tunnel))
    monkeypatch.setattr(client, "cancel_http_forward", lambda tunnel, **kwargs: cancelled.append(kwargs))
    client.disconnect(tmp_path, "ssh.portmap")
    assert stopped == [{"target": "host"}]
    assert cancelled == [{"socket_path": "/tmp/portmap-http-a.sock", "remote_port": 8080}]
    assert set(infra.synced[-1][0]) == {"direct.portmap"}
    assert client._load(tmp_path) == {"direct.portmap": profiles["direct.portmap"]}


# ---------------------------------------------------------------------------
# Local server role


def test_set_local_server_publishes_role_without_touching_remote_connections(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch)
    remote = {"remote.portmap": direct_profile("remote.portmap")}
    client._save(tmp_path, remote)
    result = client.set_local_server(tmp_path, {"domain": "Local.Portmap"})
    assert result["local"]["via"] == "local"
    assert result["local"]["target"] == "local"
    assert result["local"]["http_port"] == 80
    assert set(result) >= {"dns", "resolver", "gateway"}
    assert client._load(tmp_path) == remote
    assert json.loads((tmp_path / "local-server.json").read_text())["local"]["domain"] == "local.portmap"
    published, local, _ = infra.synced[-1]
    assert "remote.portmap" in published
    assert local["domain"] == "local.portmap"
    # The local role needs no native backend: the shared infrastructure routes
    # the domain to its own web entry.
    assert "backend_url" not in local
    assert "backend_port" not in local


def test_set_local_server_honors_saved_public_port_and_rejects_collisions(tmp_path: Path, monkeypatch):
    from portmap import infrastructure

    Infrastructure(monkeypatch)
    infrastructure.save_public_http_port(tmp_path, 9090)
    result = client.set_local_server(tmp_path, {"domain": "local.portmap"})
    assert result["local"]["http_port"] == 9090
    result = client.set_local_server(tmp_path, {"domain": "other.portmap", "http_port": 80})
    assert result["local"]["http_port"] == 80

    client._save(tmp_path, {"other.portmap": direct_profile("other.portmap")})
    with pytest.raises(PortmapError, match="used by a remote connection"):
        client.set_local_server(tmp_path, {"domain": "other.portmap"})
    with pytest.raises(PortmapError):
        client.set_local_server(tmp_path, {"domain": "not-portmap.example"})


def test_set_local_server_removal_stops_the_role_only(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch)
    remote = {"remote.portmap": direct_profile("remote.portmap")}
    client._save(tmp_path, remote)
    client.set_local_server(tmp_path, {"domain": "local.portmap"})
    result = client.set_local_server(tmp_path, None)
    assert result["local"] is None
    published, local, _ = infra.synced[-1]
    assert local is None
    assert "remote.portmap" in published
    assert client._load(tmp_path) == remote


def test_connect_refuses_domain_owned_by_local_role(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch)
    monkeypatch.setattr(client, "probe_gateway", lambda *args, **kwargs: gateway(domain="local.portmap"))
    client.set_local_server(tmp_path, {"domain": "local.portmap"})
    with pytest.raises(PortmapError, match="already connected"):
        client.connect(tmp_path, "192.0.2.10", via="direct")
    assert "local.portmap" not in client._load(tmp_path)
    assert infra.synced[-1][1] is not None


def test_teardown_keeps_the_local_role_but_removes_remote_connections(tmp_path: Path, monkeypatch):
    Infrastructure(monkeypatch)
    client.set_local_server(tmp_path, {"domain": "local.portmap"})
    local = client._load_local(tmp_path)
    client._save(tmp_path, {"remote.portmap": direct_profile()})

    client.teardown_client(tmp_path)

    assert client._load_local(tmp_path) == local
    assert client._load(tmp_path) == {}


# ---------------------------------------------------------------------------
# Setup and teardown


def test_insecure_state_fails_before_controller_or_agent_changes(monkeypatch):
    effects = []
    monkeypatch.setattr(client, "ensure_agent_started", lambda *args, **kwargs: effects.append("agent"))
    monkeypatch.setattr(client, "sync_infrastructure", lambda *args, **kwargs: effects.append("sync"))
    with exposed_state_root() as root:
        state = root / "shared-state"
        state.mkdir()
        state.chmod(0o775)
        with pytest.raises(PortmapError):
            client.setup_client(state)
        assert effects == []
        assert state.stat().st_mode & 0o777 == 0o775
        assert not (state / "client.lock").exists()


@pytest.mark.parametrize("running", [False, True])
def test_setup_leaves_transactional_controller_cleanup_to_its_owner(tmp_path: Path, monkeypatch, running):
    infra = Infrastructure(monkeypatch, running=running)

    def failed(*args, **kwargs):
        raise PortmapError("gateway cutover rolled back")

    monkeypatch.setattr(client, "sync_infrastructure", failed)
    with pytest.raises(PortmapError, match="gateway cutover rolled back"):
        client.setup_client(tmp_path)
    # A blanket stop after controller rollback could retire the still-serving
    # native route; after a committed cutover it could destroy the replacement.
    assert infra.stopped == []




def test_setup_migrates_an_already_publishing_legacy_server(tmp_path: Path, monkeypatch):
    migration = {
        "migrated": True,
        "lan": {"bind_address": "0.0.0.0", "port": 8080},
        "dns_publish": {"address": "192.0.2.10", "port": 53},
        "retired": ["portmap-gateway"],
    }
    infra = Infrastructure(monkeypatch, migration=migration)
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: False)
    result = client.setup_client(tmp_path)
    # The server cutover runs after the client lock is released; its result is
    # surfaced instead of leaving a second stack until the next `up`.
    assert len(infra.migrations) == 1
    assert result["legacy_server_migration"] == migration


def test_setup_without_legacy_server_record_adds_no_migration_key(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch, migration=None)
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: False)
    result = client.setup_client(tmp_path)
    assert infra.migrations == [None]  # the exact effective settings object
    assert "legacy_server_migration" not in result


def test_setup_fails_when_legacy_server_cutover_fails(tmp_path: Path, monkeypatch):
    Infrastructure(monkeypatch, migration_error=PortmapError("legacy cutover rolled back"))
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: False)
    with pytest.raises(PortmapError, match="legacy cutover rolled back"):
        client.setup_client(tmp_path)


def test_setup_saves_preferred_public_port_and_refreshes_stale_endpoints(tmp_path: Path, monkeypatch):
    from portmap import infrastructure

    infra = Infrastructure(monkeypatch)
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: True)
    profiles = {
        "direct.portmap": {**direct_profile("direct.portmap"), "endpoints": [{"url": "http://app.direct.portmap:8080/"}]},
        "ssh.portmap": {
            "domain": "ssh.portmap", "via": "ssh", "address": "127.0.0.1", "target": "host",
            "http_port": 18080, "remote_http_port": 8080, "tunnel": {"target": "host"},
            "http_socket": "/tmp/portmap-http-1.sock", "catalog_url": "http://ssh.portmap:18080/",
            "endpoints": [{"url": "http://app.ssh.portmap:18080/"}],
        },
    }
    client._save(tmp_path, profiles)
    client.setup_client(tmp_path, http_port=9090)
    assert infrastructure.preferred_http_port(tmp_path) == 9090
    assert infra.synced[0][2]["http_port"] == 9090
    saved = client._load(tmp_path)
    # Direct endpoints keep the remote port; SSH endpoint data embedded the old
    # public port and is dropped until that connection is refreshed.
    assert saved["direct.portmap"]["endpoints"] == [{"url": "http://app.direct.portmap:8080/"}]
    assert "endpoints" not in saved["ssh.portmap"]
    assert saved["ssh.portmap"]["http_port"] == 9090
    assert saved["ssh.portmap"]["catalog_url"] == "http://ssh.portmap:9090/"


def test_teardown_removes_direct_connection_and_shared_infrastructure(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch)
    faithful_probe(monkeypatch)
    client.connect(tmp_path, "192.0.2.10", via="direct")
    result = client.teardown_client(tmp_path, use_sudo=False)
    assert result["stopped"] is True
    assert client._load(tmp_path) == {}
    assert infra.stopped == [tmp_path]


def test_teardown_reports_incomplete_when_controller_stop_fails(tmp_path: Path, monkeypatch):
    def failed(*args, **kwargs):
        raise PortmapError("ownership check failed")

    monkeypatch.setattr(client, "stop_infrastructure", failed)
    with pytest.raises(PortmapError, match="client teardown incomplete"):
        client.teardown_client(tmp_path)
    assert client._load(tmp_path) == {}


# ---------------------------------------------------------------------------
# Legacy standalone state import


def _legacy_layout(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy" / "portmap-client"
    legacy.mkdir(parents=True, mode=0o700)
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
            "backend_url": "http://127.0.0.1:30001", "tunnel": {"target": "ssh-host", "config_file": "/saved/ssh/config"},
            "tcp_address": "127.77.0.2", "tcp_ports": [5432], "include_tcp": True,
        },
    }
    client._save(legacy, profiles)
    return legacy, profiles


def _setup_mocks(monkeypatch, tmp_path):
    infra = Infrastructure(monkeypatch)
    # Restoration gets one clean attempt against a mocked unreachable host.
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: False)
    monkeypatch.setattr(client, "start_tunnel", lambda target, **kwargs: (_ for _ in ()).throw(PortmapError("ssh: no route to host")))
    monkeypatch.setattr(client, "stop_tunnel", lambda tunnel, **kwargs: None)
    monkeypatch.setattr(client, "cancel_http_forward", lambda tunnel, **kwargs: None)
    return infra


class LiveLegacyTransport(Infrastructure):
    """A live master with independently observable old and replacement routes."""

    def __init__(self, monkeypatch, profile, *, running=False, http_port=18081):
        self.profile = profile
        self.master = profile["tunnel"]
        self.domain = profile["domain"]
        self.http_port = http_port
        self.socket_path = "/tmp/portmap-http-legacy.sock"
        self.socket_live = False
        self.old_http = True
        self.native = True
        self.published = False
        self.socket_error = None
        self.route_error = None
        self.retirement_error = False
        self.attachments = 0
        self.retirements = 0
        super().__init__(monkeypatch, running=running, http_port=http_port)
        self.calls = ssh_transport(monkeypatch, domain=self.domain)
        monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: True)
        monkeypatch.setattr(client, "add_http_forward", self._attach)
        monkeypatch.setattr(client, "cancel_http_forward", self._cancel_http)
        monkeypatch.setattr(client, "cancel_forward", self._cancel_old)
        monkeypatch.setattr(client, "probe_gateway_socket", self._probe_socket)
        monkeypatch.setattr(client, "probe_gateway", self.request)
        monkeypatch.setattr(client.socket, "create_connection", self._old_listener)
        monkeypatch.setattr(infrastructure, "probe_gateway", self.request)
        monkeypatch.setattr(infrastructure, "verify_resolution", lambda *args: None)

    def _attach(self, tunnel, *, remote_port, **kwargs):
        assert tunnel == self.master
        assert remote_port == self.profile["remote_http_port"]
        assert self.old_http  # attachment never replaces the serving listener
        self.attachments += 1
        self.socket_live = True
        return self.socket_path

    def _cancel_http(self, tunnel, *, socket_path, remote_port, **kwargs):
        assert tunnel == self.master
        assert socket_path == self.socket_path
        assert remote_port == self.profile["remote_http_port"]
        self.socket_live = False

    def _probe_socket(self, path, *, expected_domain, **kwargs):
        if path != self.socket_path or not self.socket_live:
            raise PortmapError("remote socket is unavailable")
        if self.socket_error is not None:
            raise self.socket_error
        assert expected_domain == self.domain
        return gateway(domain=self.domain, port=self.profile["remote_http_port"])

    def request(self, target, *, expected_domain=None, http_host=None, **kwargs):
        if target == self.profile["backend_url"] and self.old_http:
            return gateway(domain=self.domain, port=self.profile["remote_http_port"])
        new_ingress = target == f"http://127.0.0.1:{self.http_port}" and http_host == self.domain
        public_name = target == client._url(self.domain, self.http_port)
        if self.published and self.socket_live and (new_ingress or public_name):
            if self.route_error is not None:
                raise self.route_error
            assert expected_domain == self.domain
            return gateway(domain=self.domain, port=self.profile["remote_http_port"])
        raise PortmapError("requested gateway is unavailable")

    def _sync(self, path, profiles, local, **kwargs):
        status = super()._sync(path, profiles, local, **kwargs)
        previous = self.published
        self.published = True
        views = {
            domain: {**profile, "available": profile.get("http_socket") == self.socket_path and self.socket_live}
            for domain, profile in profiles.items()
        }
        try:
            # Exercise the real cutover proof, not an unconditional successful
            # sync mock: the socket alone cannot satisfy the new public route.
            infrastructure._verify_host_view(
                {"domain": "local.portmap", "http_port": self.http_port, "host_dns_enabled": True}, views,
            )
        except BaseException:
            self.published = previous
            raise
        self.native = False
        return status

    def _cancel_old(self, tunnel, **forward):
        assert tunnel == self.master
        assert forward == {
            "local_address": "127.0.0.1", "local_port": 30001,
            "remote_port": self.profile["remote_http_port"],
        }
        assert self.published and not self.native
        self.retirements += 1
        if self.retirement_error or not self.old_http:
            raise PortmapError("old HTTP forward could not be cancelled")
        self.old_http = False

    def _old_listener(self, address, **kwargs):
        assert address == ("127.0.0.1", 30001)
        if not self.old_http:
            raise ConnectionRefusedError("old listener is gone")
        return nullcontext()


@pytest.mark.parametrize("override", [None, "PORTMAP_STATE_DIR", "PORTMAP_CLIENT_STATE_DIR"])
def test_setup_adopts_same_state_live_ssh_before_retiring_native_route(tmp_path: Path, monkeypatch, override):
    legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    source_bytes = (legacy / "connections.json").read_bytes()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    if override:
        monkeypatch.setenv(override, str(state))
    profiles["old.portmap"] = direct_profile("old.portmap")
    profiles["tun.portmap"]["tunnel"].update({
        "controlpath": "/tmp/owned-legacy-master/control",
        "owner_token": "7754a2d588bc75b3c832550b5d235be5",
    })
    profiles["modern.portmap"] = {
        "domain": "modern.portmap", "via": "ssh", "target": "already-migrated",
        "tunnel": {"target": "already-migrated"}, "http_socket": "/tmp/already-migrated.sock",
        "http_port": 18081, "remote_http_port": 80,
    }
    client._save(state, profiles)
    transport = LiveLegacyTransport(monkeypatch, profiles["tun.portmap"])

    client.setup_client(state)

    saved = client._load(state)
    live = saved["tun.portmap"]
    for field in ("target", "ssh_target", "tunnel", "remote_http_port", "tcp_address", "tcp_ports", "include_tcp"):
        assert live[field] == profiles["tun.portmap"][field]
    assert live["ssh_config"] == "/saved/ssh/config"
    assert live["http_socket"] == transport.socket_path
    assert live["catalog_url"] == "http://tun.portmap:18081/"
    assert "backend_url" not in live
    assert "legacy_http_forward" not in live
    assert saved["old.portmap"] == profiles["old.portmap"]
    assert saved["modern.portmap"] == profiles["modern.portmap"]
    assert (legacy / "connections.json").read_bytes() == source_bytes
    assert not transport.native and not transport.old_http
    assert transport.request("http://tun.portmap:18081", expected_domain="tun.portmap").domain == "tun.portmap"
    rows = {row["domain"]: row for row in client.connections(state, check=True)["connections"]}
    assert rows["tun.portmap"]["status"] == "reachable"
    assert transport.calls["started"] == transport.calls["stopped"] == transport.calls["tcp_forwards"] == []


def test_matching_connect_adopts_live_native_ssh_without_reauthentication(tmp_path: Path, monkeypatch):
    _legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    client._save(tmp_path, profiles)
    transport = LiveLegacyTransport(monkeypatch, profiles["tun.portmap"], running=True)

    result = client.connect(tmp_path, "ssh-host", via="ssh", include_tcp=True)

    assert result["already_connected"] and result["restored"] and result["dns_ready"]
    assert result["tcp_address"] == "127.77.0.2"
    assert result["tcp_ports"] == [5432]
    rows = {row["domain"]: row for row in client.connections(tmp_path, check=True)["connections"]}
    assert rows["tun.portmap"]["status"] == "reachable"
    assert not transport.old_http and transport.socket_live
    assert transport.calls["started"] == transport.calls["stopped"] == []


@pytest.mark.parametrize("standalone", [False, True])
def test_legacy_socket_failure_preserves_prior_route_and_cleans_only_new_socket(tmp_path: Path, monkeypatch, standalone):
    legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    source_bytes = (legacy / "connections.json").read_bytes()
    state = tmp_path / "state"
    if not standalone:
        state.mkdir(mode=0o700)
        client._save(state, profiles)
    transport = LiveLegacyTransport(monkeypatch, profiles["tun.portmap"])
    transport.socket_error = PortmapError("gateway domain 'wrong.portmap' does not match expected domain 'tun.portmap'")

    with pytest.raises(PortmapError, match="does not match expected domain"):
        client.setup_client(state)

    saved = client._load(state)["tun.portmap"]
    for field in ("backend_url", "tunnel", "remote_http_port", "tcp_address", "tcp_ports"):
        assert saved[field] == profiles["tun.portmap"][field]
    assert "http_socket" not in saved
    assert transport.native and transport.old_http and not transport.socket_live
    assert transport.request(saved["backend_url"]).domain == "tun.portmap"
    assert transport.synced == []
    assert transport.calls["started"] == transport.calls["stopped"] == []
    assert (legacy / "connections.json").read_bytes() == source_bytes


@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize("error_type", [PortmapError, KeyboardInterrupt])
def test_failed_or_interrupted_cutover_retries_same_master_and_source(tmp_path: Path, monkeypatch, standalone, error_type):
    legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    source_bytes = (legacy / "connections.json").read_bytes()
    state = tmp_path / "state"
    if not standalone:
        state.mkdir(mode=0o700)
        client._save(state, profiles)
    transport = LiveLegacyTransport(monkeypatch, profiles["tun.portmap"])
    transport.route_error = error_type("new shared route is unavailable")

    with pytest.raises(error_type, match="new shared route is unavailable"):
        client.setup_client(state)

    pending = client._load(state)["tun.portmap"]
    assert pending["tunnel"] == profiles["tun.portmap"]["tunnel"]
    assert pending["http_socket"] == transport.socket_path
    assert pending["backend_url"] == profiles["tun.portmap"]["backend_url"]
    assert pending["legacy_http_forward"]["local_port"] == 30001
    assert transport.request(profiles["tun.portmap"]["backend_url"]).domain == "tun.portmap"
    assert transport.old_http and transport.native
    assert transport.retirements == 0
    assert transport.stopped == []

    transport.route_error = None
    client.setup_client(state)

    assert not transport.old_http and not transport.native
    assert "legacy_http_forward" not in client._load(state)["tun.portmap"]
    assert transport.attachments == 1
    assert transport.request("http://tun.portmap:18081", expected_domain="tun.portmap").domain == "tun.portmap"
    if standalone:
        assert all(kwargs["legacy_state_dir"] == legacy for _, _, kwargs in transport.synced)
    assert (legacy / "connections.json").read_bytes() == source_bytes
    assert transport.calls["started"] == transport.calls["stopped"] == []


def test_retirement_retry_requires_replacement_route_readiness_again(tmp_path: Path, monkeypatch):
    _legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    client._save(tmp_path, profiles)
    transport = LiveLegacyTransport(monkeypatch, profiles["tun.portmap"], running=True)
    transport.retirement_error = True

    result = client.setup_client(tmp_path)

    assert "tun.portmap" in result["legacy_forward_failures"][0]
    assert transport.old_http and transport.published
    assert "legacy_http_forward" in client._load(tmp_path)["tun.portmap"]
    transport.retirement_error = False
    transport.route_error = PortmapError("replacement route regressed")
    with pytest.raises(PortmapError, match="replacement route regressed"):
        client.setup_client(tmp_path)
    assert transport.old_http and transport.retirements == 1

    transport.route_error = None
    client.setup_client(tmp_path)

    assert not transport.old_http and transport.retirements == 2
    assert transport.attachments == 1
    assert "legacy_http_forward" not in client._load(tmp_path)["tun.portmap"]
    assert transport.calls["started"] == transport.calls["stopped"] == []


def test_interrupted_retirement_recognizes_already_removed_exact_listener(tmp_path: Path, monkeypatch):
    _legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    client._save(tmp_path, profiles)
    transport = LiveLegacyTransport(monkeypatch, profiles["tun.portmap"])
    save = client._save
    interrupt = True

    def interrupted(path, proposed, **kwargs):
        nonlocal interrupt
        if interrupt and not transport.old_http and "legacy_http_forward" not in proposed["tun.portmap"]:
            interrupt = False
            raise KeyboardInterrupt("after cancellation, before checkpoint")
        save(path, proposed, **kwargs)

    monkeypatch.setattr(client, "_save", interrupted)
    with pytest.raises(KeyboardInterrupt, match="before checkpoint"):
        client.setup_client(tmp_path)
    assert not transport.old_http and transport.published
    assert "legacy_http_forward" in client._load(tmp_path)["tun.portmap"]

    client.setup_client(tmp_path)

    assert "legacy_http_forward" not in client._load(tmp_path)["tun.portmap"]
    assert transport.request("http://tun.portmap:18081", expected_domain="tun.portmap").domain == "tun.portmap"
    assert transport.attachments == 1
    assert transport.calls["started"] == transport.calls["stopped"] == []


def test_http_forward_shared_with_saved_raw_tcp_is_not_obsolete(tmp_path: Path, monkeypatch):
    _legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    profiles["tun.portmap"].update(
        remote_http_port=30001, tcp_address="127.0.0.1", tcp_ports=[30001],
    )
    client._save(tmp_path, profiles)
    transport = LiveLegacyTransport(monkeypatch, profiles["tun.portmap"])

    client.setup_client(tmp_path)

    saved = client._load(tmp_path)["tun.portmap"]
    assert saved["tcp_address"] == "127.0.0.1"
    assert saved["tcp_ports"] == [30001]
    assert transport.retirements == 0 and transport.old_http
    assert transport.request(profiles["tun.portmap"]["backend_url"]).domain == "tun.portmap"
    assert transport.request("http://tun.portmap:18081", expected_domain="tun.portmap").domain == "tun.portmap"
    assert "legacy_http_forward" not in saved


def test_pending_legacy_route_cannot_pass_readiness_as_an_unavailable_profile(monkeypatch):
    monkeypatch.setattr(infrastructure, "verify_resolution", lambda *args: None)
    with pytest.raises(PortmapError, match="legacy SSH replacement for tun.portmap is not available"):
        infrastructure._verify_host_view(
            {"domain": "local.portmap", "http_port": 18081, "host_dns_enabled": True},
            {"tun.portmap": {"via": "ssh", "available": False, "legacy_http_forward": {}}},
        )


def test_already_migrated_offline_ssh_does_not_gain_a_new_readiness_requirement(monkeypatch):
    monkeypatch.setattr(infrastructure, "verify_resolution", lambda *args: None)

    def must_not_probe(*args, **kwargs):
        pytest.fail("ordinary migrated profiles keep their existing offline semantics")

    monkeypatch.setattr(infrastructure, "probe_gateway", must_not_probe)
    infrastructure._verify_host_view(
        {"domain": "local.portmap", "http_port": 18081, "host_dns_enabled": True},
        {"modern.portmap": {"via": "ssh", "available": False, "http_socket": "/tmp/existing.sock"}},
    )


def test_setup_imports_legacy_state_and_keeps_old_chain_until_cutover(tmp_path: Path, monkeypatch):
    legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    infra = _setup_mocks(monkeypatch, tmp_path)
    stopped = []
    monkeypatch.setattr(client, "stop_tunnel", lambda tunnel, **kwargs: stopped.append(tunnel))
    # The legacy SSH master is alive: it must be kept and gain a socket forward.
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: True)
    attached, cancelled_http, cancelled_tcp = [], [], []
    monkeypatch.setattr(
        client, "add_http_forward",
        lambda tunnel, *, state_dir, domain, remote_port, **kwargs: (
            attached.append((domain, remote_port, state_dir)), "/tmp/portmap-http-abc.sock")[1],
    )
    monkeypatch.setattr(client, "cancel_http_forward", lambda tunnel, **kwargs: cancelled_http.append(kwargs))
    monkeypatch.setattr(client, "cancel_forward", lambda tunnel, **kwargs: cancelled_tcp.append(kwargs))
    monkeypatch.setattr(client, "probe_gateway_socket", lambda path, **kwargs: gateway(domain=kwargs.get("expected_domain") or "tun.portmap"))
    client.setup_client(tmp_path / "state")
    # The controller receives the legacy source path for its own staging.
    assert infra.synced[0][2]["legacy_state_dir"] == legacy
    # Nothing from the old chain was stopped by the client import.
    assert stopped == []
    assert cancelled_http == []
    # The live master gained the Unix-socket forward before cutover...
    assert attached == [("tun.portmap", 8080, (tmp_path / "state"))]
    # ...and the obsolete loopback HTTP forward was retired only after the
    # shared infrastructure accepted the publication.
    assert cancelled_tcp == [{"local_address": "127.0.0.1", "local_port": 30001, "remote_port": 8080}]
    assert client._load(legacy) == profiles
    migrated = client._load(tmp_path / "state")
    assert migrated["old.portmap"]["via"] == "direct"
    assert migrated["old.portmap"]["address"] == "192.0.2.10"
    assert migrated["old.portmap"]["catalog_url"] == "http://old.portmap:8080/"
    live = migrated["tun.portmap"]
    assert live["tunnel"] == {"target": "ssh-host", "config_file": "/saved/ssh/config"}
    assert live["http_socket"] == "/tmp/portmap-http-abc.sock"
    assert live["http_port"] == 80
    assert live["tcp_ports"] == [5432]
    assert "backend_url" not in live
    # The exact saved SSH config identity is preserved for restoration.
    assert live["ssh_config"] == "/saved/ssh/config"


def test_setup_does_not_reimport_disconnected_legacy_profiles(tmp_path: Path, monkeypatch):
    legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    infra = _setup_mocks(monkeypatch, tmp_path)
    state = tmp_path / "state"
    client.setup_client(state)
    for domain in profiles:
        client.disconnect(state, domain)

    client.setup_client(state)

    assert client._load(state) == {}
    assert client._load(legacy) == profiles


def test_legacy_import_failure_keeps_everything_intact(tmp_path: Path, monkeypatch):
    legacy, _profiles = _legacy_layout(monkeypatch, tmp_path)
    Infrastructure(monkeypatch)
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: True)

    def refused(tunnel, *, state_dir, domain, remote_port, **kwargs):
        raise PortmapError("socket directory is not owned")

    monkeypatch.setattr(client, "add_http_forward", refused)

    def must_not_touch(*args, **kwargs):
        pytest.fail("a failed legacy import must not change the new infrastructure")

    monkeypatch.setattr(client, "ensure_agent_started", must_not_touch)
    monkeypatch.setattr(client, "sync_infrastructure", must_not_touch)
    with pytest.raises(PortmapError, match="could not be attached to the shared infrastructure"):
        client.setup_client(tmp_path / "state")
    saved = client._load(tmp_path / "state")["tun.portmap"]
    assert saved["tunnel"] == _profiles["tun.portmap"]["tunnel"]
    assert saved["backend_url"] == _profiles["tun.portmap"]["backend_url"]
    assert "http_socket" not in saved
    assert client._load(legacy) == _profiles


def test_legacy_import_skips_used_state_and_isolated_override(tmp_path: Path, monkeypatch):
    legacy, _profiles = _legacy_layout(monkeypatch, tmp_path)
    infra = _setup_mocks(monkeypatch, tmp_path)
    attached = []
    monkeypatch.setattr(client, "add_http_forward", lambda tunnel, **kwargs: attached.append(kwargs) or "/tmp/portmap-http-abc.sock")

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    client._save(state, {"new.portmap": direct_profile("new.portmap", address="192.0.2.99", port=8080)})
    client.setup_client(state)
    assert all(kwargs["legacy_state_dir"] is None for _, _, kwargs in infra.synced)

    monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(tmp_path / "isolated"))
    isolated = tmp_path / "isolated"
    isolated.mkdir(mode=0o700)
    client.setup_client(isolated)
    assert attached == []
    assert client._load(isolated) == {}


def test_shared_state_override_does_not_touch_default_legacy_access(tmp_path: Path, monkeypatch):
    legacy, profiles = _legacy_layout(monkeypatch, tmp_path)
    _setup_mocks(monkeypatch, tmp_path)
    root = tmp_path / "isolated"
    monkeypatch.setenv("PORTMAP_STATE_DIR", str(root))

    def foreign(tunnel, **kwargs):
        pytest.fail("isolated setup must not touch the default legacy SSH masters")

    monkeypatch.setattr(client, "add_http_forward", foreign)
    client.setup_client(root / "client")

    assert client._load(root / "client") == {}
    assert client._load(legacy) == profiles


# ---------------------------------------------------------------------------
# Saved SSH restoration


def test_connect_revives_dormant_saved_ssh_without_forced_disconnect(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch, running=True, http_port=80)
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
    assert calls["http_forwards"] == [("saved-host", "remote.portmap", 8080)]
    profile = client._load(tmp_path)["remote.portmap"]
    assert profile["tunnel"]["target"] == "saved-host"
    assert profile["http_port"] == 80
    assert profile["http_socket"].startswith("/tmp/portmap-http-")
    assert "backend_url" not in profile
    assert infra.synced[-1][0]["remote.portmap"]["http_socket"] == profile["http_socket"]


def test_connect_revives_dead_tunnel_record_after_stopping_it(tmp_path: Path, monkeypatch):
    Infrastructure(monkeypatch, running=True, http_port=80)
    calls = ssh_transport(monkeypatch)
    stale = {"target": "saved-host", "alive": False}
    client._save(tmp_path, {"remote.portmap": {
        "domain": "remote.portmap", "target": "saved-host", "ssh_target": "saved-host", "via": "ssh",
        "address": "127.0.0.1", "http_port": 80, "backend_url": "http://127.0.0.1:30001", "tunnel": stale,
        "http_socket": "/tmp/portmap-http-old.sock", "remote_http_port": 8080,
        "tcp_address": "127.77.0.5", "tcp_ports": [], "include_tcp": False,
    }})
    result = client.connect(tmp_path, "saved-host", via="ssh")
    assert result["restored"] is True
    assert calls["stopped"] == [stale]
    assert calls["cancelled_http"] == [{"socket_path": "/tmp/portmap-http-old.sock", "remote_port": 8080}]
    profile = client._load(tmp_path)["remote.portmap"]
    assert profile["tunnel"]["target"] == "saved-host"
    assert "backend_url" not in profile


def test_dormant_ssh_is_published_without_a_live_route(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch)
    client._save(tmp_path, {"remote.portmap": {
        "domain": "remote.portmap", "target": "saved-host", "ssh_target": "saved-host", "via": "ssh",
        "address": "127.0.0.1", "tcp_address": "127.77.0.5", "include_tcp": False,
    }})
    client.set_local_server(tmp_path, {"domain": "local.portmap"})
    published, local, _ = infra.synced[-1]
    # The dormant profile reaches the controller without any live transport,
    # so it is listed for management but never routed to loopback.
    assert set(published) == {"remote.portmap"}
    assert "tunnel" not in published["remote.portmap"]
    assert "http_socket" not in published["remote.portmap"]
    assert local["domain"] == "local.portmap"


def test_failed_restore_keeps_saved_profile_and_reports_reconnect(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch, running=True, http_port=80)
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
    assert "http_socket" not in kept
    assert infra.synced == []


def test_restore_refuses_remote_now_serving_a_different_domain(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch, running=True, http_port=80)
    calls = ssh_transport(monkeypatch)
    monkeypatch.setattr(client, "probe_gateway", lambda target, **kwargs: gateway(domain="other.portmap"))
    client._save(tmp_path, {"remote.portmap": {
        "domain": "remote.portmap", "target": "saved-host", "ssh_target": "saved-host", "via": "ssh",
        "address": "127.0.0.1", "tcp_address": "127.77.0.5", "include_tcp": False,
    }})
    with pytest.raises(PortmapError, match="now serves 'other.portmap', not the saved 'remote.portmap'"):
        client.connect(tmp_path, "saved-host", via="ssh")
    kept = client._load(tmp_path)["remote.portmap"]
    assert "tunnel" not in kept
    assert calls["stopped"] == [{"target": "saved-host", "config_file": None}]
    # A relocated remote never gains a socket forward.
    assert calls["http_forwards"] == []
    assert infra.synced == []


def test_setup_restores_each_dormant_ssh_once_and_reports_failures(tmp_path: Path, monkeypatch):
    infra = Infrastructure(monkeypatch, running=True, http_port=80)
    calls = ssh_transport(monkeypatch, fail_start_for=("bad-host",))

    def faithful(target, **kwargs):
        # The good remote serves its own saved domain on unqualified probes.
        return gateway(domain=kwargs.get("expected_domain") or "good.portmap")

    monkeypatch.setattr(client, "probe_gateway", faithful)
    client._save(tmp_path, {
        "good.portmap": {"domain": "good.portmap", "target": "good-host", "ssh_target": "good-host", "via": "ssh", "address": "127.0.0.1", "tcp_address": "127.77.0.1"},
        "bad.portmap": {"domain": "bad.portmap", "target": "bad-host", "ssh_target": "bad-host", "via": "ssh", "address": "127.0.0.1", "tcp_address": "127.77.0.2"},
        "direct.portmap": direct_profile("direct.portmap"),
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
    published = infra.synced[-1][0]
    assert published["good.portmap"]["http_socket"] == saved["good.portmap"]["http_socket"]
    assert "tunnel" not in published["bad.portmap"]


class MaintenanceClock:
    """An interruptible worker clock without wall-clock retries or sleeps."""

    def __init__(self, monkeypatch, *, until=10, on_wait=None):
        self.now = 0
        self.until = until
        self.stopped = False
        self.waits = []
        self.on_wait = on_wait
        monkeypatch.setattr(client.time, "monotonic", lambda: self.now)

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.on_wait is not None:
            self.on_wait(self)
        if not self.stopped:
            self.now += timeout
            if self.now >= self.until:
                self.set()
        return self.stopped


def saved_ssh_profile(domain="remote.portmap", target="saved-host", *, live=False):
    profile = {
        "domain": domain, "target": target, "ssh_target": target, "via": "ssh",
        "address": "127.0.0.1", "http_port": 80, "remote_http_port": 8080,
        "ssh_config": "/saved/ssh/config", "tcp_address": "127.77.0.5",
        "include_tcp": False, "tcp_ports": [],
    }
    if live:
        profile.update(tunnel={"target": target, "dead": True}, http_socket="/tmp/old-http.sock")
    return profile


def maintenance_infrastructure(monkeypatch, *, actual_sockets=False):
    infra = Infrastructure(monkeypatch, running=True, http_port=80)
    monkeypatch.setattr(client, "sync_connection_routes", infra._sync)
    monkeypatch.setattr(client, "load_infrastructure", lambda path: {
        "phase": "ready" if infra.state["running"] else "stopped", "http_port": 80,
    })
    if not actual_sockets:
        # ssh_transport models forwards without creating host Unix sockets.
        monkeypatch.setattr(client, "_ssh_socket_present", lambda path: isinstance(path, str) and bool(path))

    def forbidden(*args, **kwargs):
        pytest.fail("runtime maintenance cannot setup infrastructure or host DNS")

    monkeypatch.setattr(client, "sync_infrastructure", forbidden)
    monkeypatch.setattr(client, "setup_client", forbidden)
    return infra


def test_maintenance_outage_withdraws_before_retry_and_recovers_without_touching_peers(tmp_path, monkeypatch):
    infra = maintenance_infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    dead = saved_ssh_profile(live=True)
    healthy = saved_ssh_profile("healthy.portmap", "healthy-host", live=True)
    healthy["tunnel"]["dead"] = False
    direct = direct_profile("direct.portmap")
    client._save(tmp_path, {row["domain"]: row for row in (dead, healthy, direct)})
    clock = MaintenanceClock(monkeypatch)
    attempts = []
    start = client.start_tunnel

    def reconnect(target, **kwargs):
        attempts.append((clock.now, target, kwargs))
        # Unavailable publication has committed before authentication starts.
        assert "tunnel" not in infra.synced[-1][0]["remote.portmap"]
        if clock.now < 5:
            raise PortmapError("network is still down")
        return start(target, **kwargs)

    monkeypatch.setattr(client, "start_tunnel", reconnect)
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: not tunnel.get("dead", False))
    client.run_connection_maintenance(tmp_path, clock)

    assert [attempt[0] for attempt in attempts] == [0, 5]
    assert all(attempt[1] == "saved-host" for attempt in attempts)
    assert all(attempt[2]["use_sudo"] is False for attempt in attempts)
    assert all(attempt[2]["stop_event"] is clock for attempt in attempts)
    assert all(attempt[2]["timeout"] >= 30 for attempt in attempts)
    assert calls["configs"] == [Path("/saved/ssh/config")]
    assert calls["stopped"] == [dead["tunnel"]]
    saved = client._load(tmp_path)
    assert saved["healthy.portmap"] == healthy
    assert saved["direct.portmap"] == direct
    rows = {row["domain"]: row for row in client.connections(tmp_path, check=True)["connections"]}
    assert rows["remote.portmap"]["status"] == "reachable"
    assert "http_socket" not in infra.synced[0][0]["remote.portmap"]
    assert infra.synced[-1][0]["remote.portmap"]["tunnel"]["target"] == "saved-host"
    assert clock.waits == [5, 5]


def test_maintenance_backoff_is_per_profile_bounded_and_ignores_disconnected_targets(tmp_path, monkeypatch):
    infra = maintenance_infrastructure(monkeypatch)
    ssh_transport(monkeypatch)
    clock = MaintenanceClock(monkeypatch, until=200)
    client._save(tmp_path, {
        "bad.portmap": saved_ssh_profile("bad.portmap", "bad-host"),
        "good.portmap": saved_ssh_profile("good.portmap", "good-host"),
    })
    (tmp_path / "connection-targets.json").write_text(json.dumps({
        "version": 1, "targets": [{"id": "disconnected", "target": "never-host", "via": "ssh"}],
    }))
    attempts = []
    start = client.start_tunnel

    def reconnect(target, **kwargs):
        attempts.append((clock.now, target))
        if target == "bad-host":
            raise PortmapError("unreachable peer")
        return start(target, **kwargs)

    monkeypatch.setattr(client, "start_tunnel", reconnect)
    monkeypatch.setattr(client, "tunnel_alive", lambda *args, **kwargs: True)
    monkeypatch.setattr(client, "probe_gateway", lambda target, **kwargs: gateway(domain=kwargs.get("expected_domain") or "good.portmap"))
    client.run_connection_maintenance(tmp_path, clock)

    assert [stamp for stamp, target in attempts if target == "bad-host"] == [0, 5, 15, 35, 75, 135, 195]
    assert [stamp for stamp, target in attempts if target == "good-host"] == [0]
    assert all(target != "never-host" for _, target in attempts)
    assert "tunnel" in client._load(tmp_path)["good.portmap"]
    assert "tunnel" not in client._load(tmp_path)["bad.portmap"]
    assert all(wait == 5 for wait in clock.waits)
    assert all("good.portmap" in entry[0] for entry in infra.synced)


@pytest.mark.parametrize("action", ["disconnect", "delete", "edit"])
def test_maintenance_rereads_after_accepted_removal_between_sweep_and_attempt(tmp_path, monkeypatch, action):
    from portmap import connection_config

    maintenance_infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    client._save(tmp_path, {"remote.portmap": saved_ssh_profile()})
    clock = MaintenanceClock(monkeypatch, until=5)
    lock = client._maintenance_lock
    entered = 0

    @contextmanager
    def interleaved(path, event):
        nonlocal entered
        with lock(path, event):
            entered += 1
            yield
        if entered == 1:
            connection_config.managed_disconnect(path, "remote.portmap", use_sudo=False)
            row = connection_config.connection_targets(path)["connections"][0]
            if action == "delete":
                connection_config.mutate_connection_target(path, {"action": "delete", "id": row["id"]})
            elif action == "edit":
                connection_config.mutate_connection_target(path, {
                    "action": "save", "id": row["id"], "target": "edited-host", "via": "ssh",
                })

    # User-requested mutations still use their ordinary publication path.
    monkeypatch.setattr(client, "sync_infrastructure", lambda *args, **kwargs: {})
    monkeypatch.setattr(client, "_maintenance_lock", interleaved)
    client.run_connection_maintenance(tmp_path, clock)

    assert client._load(tmp_path) == {}
    assert calls["started"] == []
    assert entered == 2


@pytest.mark.parametrize("cancel_at", ["start", "socket", "publish"])
def test_maintenance_stop_cleans_inflight_tunnel_and_never_leaves_reachable_route(tmp_path, monkeypatch, cancel_at):
    infra = maintenance_infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    client._save(tmp_path, {"remote.portmap": saved_ssh_profile()})
    clock = MaintenanceClock(monkeypatch)
    start = client.start_tunnel
    socket_probe = client.probe_gateway_socket

    def reconnect(target, **kwargs):
        tunnel = start(target, **kwargs)
        if cancel_at == "start":
            clock.set()
        return tunnel

    def probe(path, **kwargs):
        if cancel_at == "socket":
            clock.set()
        return socket_probe(path, **kwargs)

    def publish(path, profiles, local, **kwargs):
        result = infra._sync(path, profiles, local, **kwargs)
        if cancel_at == "publish" and profiles["remote.portmap"].get("tunnel"):
            clock.set()
        return result

    monkeypatch.setattr(client, "start_tunnel", reconnect)
    monkeypatch.setattr(client, "probe_gateway_socket", probe)
    monkeypatch.setattr(client, "sync_connection_routes", publish)
    client.run_connection_maintenance(tmp_path, clock)

    assert len(calls["started"]) == len(calls["stopped"]) == 1
    assert "tunnel" not in client._load(tmp_path)["remote.portmap"]
    assert "http_socket" not in infra.synced[-1][0]["remote.portmap"]
    if cancel_at != "publish":
        assert all("tunnel" not in item[0]["remote.portmap"] for item in infra.synced)
    assert clock.now == 0


def test_maintenance_preserves_saved_port_tcp_and_expected_domain(tmp_path, monkeypatch):
    infra = maintenance_infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    profile = saved_ssh_profile()
    profile.update(include_tcp=True, remote_http_port=18080)
    client._save(tmp_path, {"remote.portmap": profile})
    clock = MaintenanceClock(monkeypatch, until=5)
    bootstrap = []

    def probe(target, **kwargs):
        bootstrap.append((target, kwargs))
        return gateway(port=18080, domain=kwargs.get("expected_domain") or "remote.portmap")

    monkeypatch.setattr(client, "probe_gateway", probe)

    def socket_probe(path, **kwargs):
        info = gateway(port=18080, domain="remote.portmap")
        info.catalog["services"] = [{"endpoints": [{"kind": "tcp", "host_port": 5432}]}]
        return info

    monkeypatch.setattr(client, "probe_gateway_socket", socket_probe)
    client.run_connection_maintenance(tmp_path, clock)

    assert calls["tcp_forwards"][0]["remote_port"] == 18080
    assert calls["configs"] == [Path(profile["ssh_config"])]
    assert calls["http_forwards"] == [("saved-host", "remote.portmap", 18080)]
    saved = client._load(tmp_path)["remote.portmap"]
    assert saved["tcp_ports"] == [5432]
    assert calls["tcp_forwards"][-1]["local_address"] == profile["tcp_address"]
    assert calls["tcp_forwards"][-1]["remote_port"] == 5432
    for key in ("target", "ssh_target", "ssh_config", "include_tcp", "tcp_address", "remote_http_port"):
        assert saved[key] == profile[key]
    assert bootstrap[-1][1]["expected_domain"] == "remote.portmap"
    assert infra.synced[-1][0]["remote.portmap"]["tunnel"]


def test_maintenance_domain_change_keeps_saved_mapping_unavailable(tmp_path, monkeypatch):
    infra = maintenance_infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    profile = saved_ssh_profile()
    client._save(tmp_path, {"remote.portmap": profile})
    clock = MaintenanceClock(monkeypatch, until=5)
    monkeypatch.setattr(client, "probe_gateway", lambda *args, **kwargs: gateway(domain="other.portmap"))

    client.run_connection_maintenance(tmp_path, clock)

    assert client._load(tmp_path)["remote.portmap"] == profile
    assert len(calls["stopped"]) == 1
    assert calls["http_forwards"] == []
    assert all("tunnel" not in item[0]["remote.portmap"] for item in infra.synced)


def test_maintenance_waits_when_owned_infrastructure_is_not_ready(tmp_path, monkeypatch):
    infra = maintenance_infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    client._save(tmp_path, {"remote.portmap": saved_ssh_profile(live=True)})
    infra.state["running"] = False
    clock = MaintenanceClock(monkeypatch)

    client.run_connection_maintenance(tmp_path, clock)

    assert infra.synced == []
    assert calls["started"] == calls["stopped"] == []
    assert clock.waits == [5, 5]


def test_maintenance_preserves_live_ssh_and_tcp_when_remote_http_is_unavailable(tmp_path, monkeypatch):
    infra = maintenance_infrastructure(monkeypatch, actual_sockets=True)
    calls = ssh_transport(monkeypatch)
    profile = saved_ssh_profile(live=True)
    profile.update(include_tcp=True, tcp_ports=[5432], http_socket=str(tmp_path / "http.sock"))
    client._save(tmp_path, {"remote.portmap": profile})
    clock = MaintenanceClock(monkeypatch)
    http_probes = []

    def unavailable(*args, **kwargs):
        http_probes.append(args)
        raise PortmapError("remote HTTP timed out")

    monkeypatch.setattr(client, "tunnel_alive", lambda *args, **kwargs: True)
    monkeypatch.setattr(client, "probe_gateway_socket", unavailable)
    monkeypatch.setattr(client, "probe_gateway", unavailable)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(profile["http_socket"])
        client.run_connection_maintenance(tmp_path, clock)
        assert http_probes == []
        assert client._load(tmp_path)["remote.portmap"] == profile
        assert calls["started"] == calls["stopped"] == calls["cancelled_http"] == []
        assert infra.synced == []
        # Explicit HTTP health remains meaningful without retiring SSH/TCP.
        row = client.connections(tmp_path, check=True)["connections"][0]
        assert row["status"] == "unreachable"
        assert calls["stopped"] == []


@pytest.mark.parametrize("socket_kind", ["missing", "regular_file", "symlink"])
def test_maintenance_recovers_structurally_missing_or_invalid_socket_with_live_master(tmp_path, monkeypatch, socket_kind):
    infra = maintenance_infrastructure(monkeypatch, actual_sockets=True)
    calls = ssh_transport(monkeypatch)
    profile = saved_ssh_profile(live=True)
    path = tmp_path / "http.sock"
    profile["http_socket"] = str(path)
    if socket_kind == "regular_file":
        path.write_text("not a socket")
    elif socket_kind == "symlink":
        target = tmp_path / "other.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(target))
        path.symlink_to(target)
    client._save(tmp_path, {"remote.portmap": profile})
    clock = MaintenanceClock(monkeypatch, until=5)
    monkeypatch.setattr(client, "tunnel_alive", lambda *args, **kwargs: True)

    client.run_connection_maintenance(tmp_path, clock)

    assert calls["stopped"] == [profile["tunnel"]]
    assert calls["started"] == ["saved-host"]
    assert "http_socket" not in infra.synced[0][0]["remote.portmap"]
    assert client._load(tmp_path)["remote.portmap"]["http_socket"] != profile["http_socket"]


def test_maintenance_failed_retirement_withdraws_route_but_retains_cleanup_ownership(tmp_path, monkeypatch):
    infra = maintenance_infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    profile = saved_ssh_profile(live=True)
    client._save(tmp_path, {"remote.portmap": profile})
    clock = MaintenanceClock(monkeypatch, until=5)

    def refused(*args, **kwargs):
        raise PortmapError("master refused cancellation")

    monkeypatch.setattr(client, "cancel_http_forward", refused)
    client.run_connection_maintenance(tmp_path, clock)

    pending = client._load(tmp_path)["remote.portmap"]
    assert pending["tunnel"] == profile["tunnel"]
    assert pending["retiring_http_socket"] == profile["http_socket"]
    assert "http_socket" not in pending
    assert "http_socket" not in infra.synced[-1][0]["remote.portmap"]
    assert calls["started"] == []
    row = client.connections(tmp_path, check=True)["connections"][0]
    assert row["status"] == "unreachable"
    assert "retiring_http_socket" not in row

    # Setup/adoption must not revive a transport waiting for exact cleanup.
    monkeypatch.setattr(client, "sync_infrastructure", infra._sync)
    monkeypatch.setattr(client, "tunnel_alive", lambda *args, **kwargs: True)
    client.set_local_server(tmp_path, None)
    assert calls["http_forwards"] == []
    monkeypatch.setattr(client, "cancel_http_forward", lambda tunnel, **kwargs: calls["cancelled_http"].append(kwargs))
    client.disconnect(tmp_path, "remote.portmap", use_sudo=False)
    assert client._load(tmp_path) == {}
    assert calls["cancelled_http"][-1]["socket_path"] == profile["http_socket"]
    assert calls["stopped"] == [profile["tunnel"]]


def test_maintenance_stop_during_retirement_commits_unavailable_view(tmp_path, monkeypatch):
    infra = maintenance_infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    profile = saved_ssh_profile(live=True)
    client._save(tmp_path, {"remote.portmap": profile})
    clock = MaintenanceClock(monkeypatch)

    def cancel(*args, **kwargs):
        clock.set()

    monkeypatch.setattr(client, "cancel_http_forward", cancel)
    client.run_connection_maintenance(tmp_path, clock)

    assert calls["started"] == []
    assert calls["stopped"] == [profile["tunnel"]]
    assert "tunnel" not in client._load(tmp_path)["remote.portmap"]
    assert "http_socket" not in infra.synced[-1][0]["remote.portmap"]
    assert clock.now == 0


def test_maintenance_startup_cleanup_failure_keeps_new_transport_owned_and_unrouted(tmp_path, monkeypatch):
    infra = maintenance_infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    client._save(tmp_path, {"remote.portmap": saved_ssh_profile()})
    clock = MaintenanceClock(monkeypatch, until=5)

    def unavailable(*args, **kwargs):
        raise PortmapError("catalog unavailable")

    def refused(*args, **kwargs):
        raise PortmapError("master exit refused")

    monkeypatch.setattr(client, "probe_gateway", unavailable)
    monkeypatch.setattr(client, "stop_tunnel", refused)
    client.run_connection_maintenance(tmp_path, clock)

    pending = client._load(tmp_path)["remote.portmap"]
    assert calls["started"] == ["saved-host"]
    assert pending["tunnel"]["target"] == "saved-host"
    assert "retiring_http_socket" in pending
    assert "http_socket" not in pending
    assert "http_socket" not in infra.synced[-1][0]["remote.portmap"]


def test_maintenance_stop_interrupts_waiting_for_user_mutation_lock(tmp_path, monkeypatch):
    infra = maintenance_infrastructure(monkeypatch)
    calls = ssh_transport(monkeypatch)
    client._save(tmp_path, {"remote.portmap": saved_ssh_profile()})
    waiting = threading.Event()

    class ObservedStop(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    stopped = ObservedStop()
    path = tmp_path / "connection-operations.lock"
    path.touch(mode=0o600)
    with path.open("a+") as operations:
        client.fcntl.flock(operations.fileno(), client.fcntl.LOCK_EX)
        worker = threading.Thread(target=client.run_connection_maintenance, args=(tmp_path, stopped))
        worker.start()
        try:
            assert waiting.wait(2)
            stopped.set()
            worker.join(2)
            assert not worker.is_alive()
        finally:
            stopped.set()
            client.fcntl.flock(operations.fileno(), client.fcntl.LOCK_UN)
            worker.join(2)
    assert calls["started"] == []
    assert infra.synced == []


# ---------------------------------------------------------------------------
# Connection rows


def test_connections_check_measures_remote_http_latency(tmp_path: Path, monkeypatch):
    Infrastructure(monkeypatch)
    probes = []

    def slow_direct(target, **kwargs):
        probes.append(target)
        time.sleep(0.01)
        return gateway(domain=kwargs.get("expected_domain") or "remote.portmap")

    def slow_socket(path, **kwargs):
        probes.append(path)
        time.sleep(0.01)
        return gateway(domain=kwargs.get("expected_domain") or "ssh.portmap")

    monkeypatch.setattr(client, "probe_gateway", slow_direct)
    monkeypatch.setattr(client, "probe_gateway_socket", slow_socket)
    monkeypatch.setattr(client, "tunnel_alive", lambda tunnel, **kwargs: True)
    client._save(tmp_path, {
        "remote.portmap": direct_profile("remote.portmap"),
        "ssh.portmap": {
            "domain": "ssh.portmap", "via": "ssh", "address": "127.0.0.1", "target": "host",
            "tunnel": {"target": "host"}, "http_socket": "/tmp/portmap-http-1.sock",
            "http_port": 80, "remote_http_port": 8080,
        },
    })
    unchecked = client.connections(tmp_path)
    assert all(row["status"] == "configured" for row in unchecked["connections"])
    assert all("latency_ms" not in row and "last_checked_at" not in row for row in unchecked["connections"])
    assert set(unchecked) >= {"connections", "local", "dns", "resolver", "gateway"}

    checked = client.connections(tmp_path, check=True)
    rows = {row["domain"]: row for row in checked["connections"]}
    # The measurement is a real HTTP request: direct profiles hit the numeric
    # remote origin, SSH profiles the owned Unix-socket forward.
    assert "http://192.0.2.10:8080" in probes
    assert "/tmp/portmap-http-1.sock" in probes
    for row in rows.values():
        assert row["status"] == "reachable"
        assert row["latency_ms"] >= 10
        datetime.fromisoformat(row["last_checked_at"])


def test_connections_check_failure_has_no_latency_number(tmp_path: Path, monkeypatch):
    Infrastructure(monkeypatch)

    def down(target, **kwargs):
        raise PortmapError("no route to host")

    monkeypatch.setattr(client, "probe_gateway", down)
    client._save(tmp_path, {"remote.portmap": direct_profile("remote.portmap")})
    row = client.connections(tmp_path, check=True)["connections"][0]
    assert row["status"] == "unreachable"
    assert "latency_ms" not in row
    assert "no route to host" in row["error"]
    datetime.fromisoformat(row["last_checked_at"])


def test_dormant_ssh_check_reports_reconnect_never_reachable(tmp_path: Path, monkeypatch):
    Infrastructure(monkeypatch)
    client._save(tmp_path, {"remote.portmap": {
        "domain": "remote.portmap", "target": "saved-host", "ssh_target": "saved-host", "via": "ssh",
        "address": "127.0.0.1", "tcp_address": "127.77.0.5", "include_tcp": False,
    }})
    row = client.connections(tmp_path, check=True)["connections"][0]
    assert row["status"] == "unreachable"
    assert "dormant" in row["error"]
    assert "saved-host" in row["error"]
    assert "latency_ms" not in row


def test_public_rows_strip_private_transport_details(tmp_path: Path, monkeypatch):
    Infrastructure(monkeypatch)
    client._save(tmp_path, {"remote.portmap": {
        "domain": "remote.portmap", "via": "ssh", "target": "host", "ssh_target": "host",
        "address": "127.0.0.1", "http_port": 80, "remote_http_port": 8080, "scheme": "http",
        "backend_url": "http://127.0.0.1:30001", "tunnel": {"target": "host"},
        "http_socket": "/tmp/portmap-http-1.sock", "ssh_config": "/saved/ssh/config",
        "catalog_url": "http://remote.portmap/", "tcp_address": "127.77.0.1", "tcp_ports": [], "include_tcp": False,
    }})
    row = client.connections(tmp_path)["connections"][0]
    for key in ("tunnel", "backend_url", "http_socket", "ssh_config"):
        assert key not in row
    assert row["status"] == "configured"
    assert row["domain"] == "remote.portmap"
