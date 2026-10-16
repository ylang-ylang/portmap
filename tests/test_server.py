"""Behavioral tests for the shared-infrastructure local publishing role."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import client, server
from portmap.errors import PortmapError
from portmap.settings import load_portmap_settings

BACKEND_PORT = 45555


def make_settings(tmp_path: Path, **env: str):
    values = {
        "PORTMAP_ROOT": str(tmp_path),
        "PORTMAP_STATE_DIR": str(tmp_path / "state"),
        "PORTMAP_AGENT_RUNTIME_DIR": str(tmp_path / "agent"),
        "PORTMAP_HOST_IP": "192.0.2.10",
    }
    values.update(env)
    return load_portmap_settings(environ=values)


class FakeAgentStatus:
    def __init__(self, running: bool = True):
        self.running = running

    def as_dict(self):
        return {"running": self.running}


class Harness:
    """Records every client, infrastructure and ingress call server.py makes."""

    def __init__(self, tmp_path: Path):
        self.access_dir = tmp_path / "client-state"
        self.events: list[tuple] = []
        self.errors: dict[str, Exception] = {}
        self.infra_state = None
        self.infra_report = {"running": True, "gateway": {"running": True, "public_backend_port": BACKEND_PORT}}
        self.sync_result = {"running": True, "gateway": {"running": True, "public_backend_port": BACKEND_PORT}}
        self.agent_status = FakeAgentStatus(True)
        self.ingress_report = {"owned": True, "running": True, "bind_address": None, "port": None}
        self.profiles: dict = {}
        self.saved_local = None

    def calls(self, name: str) -> list[tuple]:
        return [event for event in self.events if event[0] == name]

    def args(self, name: str, index: int = 0):
        return self.calls(name)[index][1]

    def kwargs(self, name: str, index: int = 0):
        return self.calls(name)[index][2]

    def stub(self, name: str, result=None):
        def call(*arguments, **keywords):
            self.events.append((name, arguments, keywords))
            error = self.errors.get(name)
            if error is not None:
                raise error
            return result() if callable(result) else result
        return call


@pytest.fixture
def host(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(harness.access_dir))
    monkeypatch.setattr(server, "setup_client", harness.stub("setup_client", {"dns": {}, "resolver": {}, "gateway": {}}))
    monkeypatch.setattr(server, "set_local_server", harness.stub("set_local_server", lambda: harness.sync_result))
    monkeypatch.setattr(server, "_load_client_local", lambda directory: harness.saved_local)
    monkeypatch.setattr(server, "probe_gateway", harness.stub("probe_gateway", SimpleNamespace(domain="x", http_port=80, address="127.0.0.1", scheme="http", catalog={}, catalog_url="")))
    monkeypatch.setattr(server, "infrastructure_status", harness.stub("infrastructure_status", lambda: harness.infra_report))
    monkeypatch.setattr(server, "load_infrastructure", harness.stub("load_infrastructure", lambda: harness.infra_state))
    monkeypatch.setattr(server, "agent_status", harness.stub("agent_status", lambda: harness.agent_status))
    monkeypatch.setattr(server, "ensure_ingress", harness.stub("ensure_ingress", {"name": "public"}))
    monkeypatch.setattr(server, "stop_ingress", harness.stub("stop_ingress", {"stopped": True}))
    monkeypatch.setattr(server, "ingress_status", harness.stub("ingress_status", lambda: dict(harness.ingress_report)))
    monkeypatch.setattr(server, "load_legacy_record", lambda settings: None)
    monkeypatch.setattr(server, "load_migration", lambda state_dir: None)
    return harness


def record_settings(settings, **overrides):
    values = server._settings_record(settings)
    values.update(overrides)
    return values


def save_record(settings, record):
    directory = settings.state_dir / "server"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "instance.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def v2_record(settings, host, *, lan=None):
    return {
        "version": 2, "uid": os.getuid(), "state_dir": str(settings.state_dir.resolve()),
        "client_state_dir": str(host.access_dir), "http_port": settings.http_port,
        "lan": lan, "settings": record_settings(settings),
    }


def v1_record(settings, host, *, root=None, backend_port=47123):
    return {
        "version": 1, "uid": os.getuid(), "state_dir": str(settings.state_dir.resolve()),
        "backend_port": backend_port, "client_state_dir": str(host.access_dir),
        "settings": record_settings(settings, root=str(root)) if root else record_settings(settings),
    }


def load_saved(settings):
    return json.loads((settings.state_dir / "server" / "instance.json").read_text(encoding="utf-8"))


def local_calls(host):
    return [call[1] for call in host.calls("set_local_server")]


# --- up ----------------------------------------------------------------------------------


def test_up_rejects_non_up_compose_arguments(tmp_path, host):
    with pytest.raises(PortmapError, match="up accepts gateway compose 'up' arguments"):
        server.start_server(make_settings(tmp_path), ["ps"])
    assert host.calls("setup_client") == []


def test_up_requires_a_local_container_runtime(tmp_path, host):
    settings = make_settings(tmp_path)
    object.__setattr__(settings, "runtime_socket", "ssh://user@remote")
    with pytest.raises(PortmapError, match="needs a local Docker/Podman socket"):
        server.start_server(settings, ["up", "-d"])
    assert host.calls("setup_client") == []






def test_up_installs_only_an_explicit_lan_ingress(tmp_path, host):
    settings = make_settings(tmp_path, PORTMAP_HTTP_BIND="192.0.2.10")
    server.start_server(settings, ["up", "-d"])

    assert len(host.calls("ensure_ingress")) == 1
    assert host.kwargs("ensure_ingress") == {"name": "public", "bind_address": "192.0.2.10", "port": 80, "target_port": BACKEND_PORT, "use_sudo": True}
    assert load_saved(settings)["lan"] == {"bind_address": "192.0.2.10", "port": 80}


def test_up_with_loopback_bind_publishes_nothing(tmp_path, host):
    settings = make_settings(tmp_path, PORTMAP_HTTP_BIND="127.0.0.1")
    server.start_server(settings, ["up", "-d"])
    assert host.calls("ensure_ingress") == []
    assert load_saved(settings)["lan"] is None


def test_up_keeps_a_preserved_lan_entry_on_unchanged_configuration(tmp_path, host):
    settings = make_settings(tmp_path)
    lan = {"bind_address": "203.0.113.5", "port": 80}
    save_record(settings, v2_record(settings, host, lan=lan))
    server.start_server(settings, ["up", "-d"])

    assert len(host.calls("ensure_ingress")) == 1
    assert host.kwargs("ensure_ingress")["bind_address"] == "203.0.113.5"
    assert host.kwargs("ensure_ingress")["port"] == 80
    assert load_saved(settings)["lan"] == lan
    assert host.calls("stop_ingress") == []


def test_up_with_explicit_loopback_withdraws_a_preserved_lan_entry(tmp_path, host):
    settings = make_settings(tmp_path, PORTMAP_HTTP_BIND="127.0.0.1")
    save_record(settings, v2_record(settings, host, lan={"bind_address": "203.0.113.5", "port": 80}))
    server.start_server(settings, ["up", "-d"])

    assert host.calls("ensure_ingress") == []
    assert len(host.calls("stop_ingress")) == 1
    assert host.kwargs("stop_ingress")["name"] == "public"
    assert load_saved(settings)["lan"] is None


def test_up_refuses_changed_publishing_configuration(tmp_path, host):
    settings = make_settings(tmp_path)
    save_record(settings, v2_record(settings, host, lan=None) | {"settings": record_settings(settings, dns_domain="other.portmap")})
    with pytest.raises(PortmapError, match="local publishing configuration changed"):
        server.start_server(settings, ["up", "-d"])
    assert load_saved(settings)["settings"]["dns_domain"] == "other.portmap"


def test_up_rolls_back_a_fresh_role_when_the_lan_ingress_fails(tmp_path, host):
    settings = make_settings(tmp_path, PORTMAP_HTTP_BIND="192.0.2.10")
    host.errors["ensure_ingress"] = PortmapError("ingress boom")
    with pytest.raises(PortmapError, match="local publishing failed: ingress boom"):
        server.start_server(settings, ["up", "-d"])
    assert local_calls(host)[-1] == (host.access_dir, None)
    assert not (settings.state_dir / "server" / "instance.json").exists()


@pytest.mark.parametrize("operation", ["up", "down"])
def test_failed_publication_change_preserves_the_saved_http_address(tmp_path, host, monkeypatch, operation):
    settings = make_settings(tmp_path, PORTMAP_HTTP_BIND="192.0.2.10")
    lan = {"bind_address": "192.0.2.10", "port": 80}
    save_record(settings, v2_record(settings, host, lan=lan))
    monkeypatch.setattr(client, "_publish", lambda *args, **kwargs: host.sync_result)
    monkeypatch.setattr(server, "set_local_server", client.set_local_server)
    monkeypatch.setattr(server, "_load_client_local", client._load_local)
    client.set_local_server(host.access_dir, {**server.local_role(settings), "http_address": lan["bind_address"]})
    original = client._load_local(host.access_dir)
    host.infra_state = {"public_backend_port": BACKEND_PORT}

    if operation == "up":
        changed = make_settings(tmp_path, PORTMAP_HTTP_BIND="192.0.2.11")
        host.errors["ensure_ingress"] = PortmapError("requested address is unavailable")
        with pytest.raises(PortmapError):
            server.start_server(changed, ["up", "-d"])
    else:
        unlink = Path.unlink

        def fail_record_removal(path, *args, **kwargs):
            if path == server._record_path(settings):
                raise PortmapError("cannot commit removal")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_record_removal)
        with pytest.raises(PortmapError):
            server.stop_server(settings, ["down"])

    assert client._load_local(host.access_dir) == original
    assert load_saved(settings)["lan"] == lan


def test_up_failure_before_the_role_leaves_no_partial_state(tmp_path, host):
    settings = make_settings(tmp_path)
    host.errors["set_local_server"] = PortmapError("sync boom")
    with pytest.raises(PortmapError, match="local publishing failed: sync boom"):
        server.start_server(settings, ["up", "-d"])
    assert not (settings.state_dir / "server" / "instance.json").exists()


# --- down --------------------------------------------------------------------------------


def test_down_removes_the_role_but_keeps_the_shared_infrastructure(tmp_path, host):
    settings = make_settings(tmp_path)
    save_record(settings, v2_record(settings, host, lan={"bind_address": "192.0.2.10", "port": 80}))
    host.infra_state = {"public_backend_port": BACKEND_PORT}
    result = server.stop_server(settings, ["down"])

    assert not (settings.state_dir / "server" / "instance.json").exists()
    assert result == {"configured": False, "running": False, "state_dir": str(settings.state_dir)}




def test_down_failure_restores_the_role_and_ingress(tmp_path, host, monkeypatch):
    settings = make_settings(tmp_path)
    lan = {"bind_address": "192.0.2.10", "port": 80}
    save_record(settings, v2_record(settings, host, lan=lan))
    host.infra_state = {"public_backend_port": BACKEND_PORT}
    unlink = Path.unlink

    def refuse_record_removal(path, *args, **kwargs):
        if path == server._record_path(settings):
            raise PortmapError("unpublish boom")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_record_removal)
    with pytest.raises(PortmapError, match="could not be stopped: unpublish boom"):
        server.stop_server(settings, ["down"])

    assert len(host.calls("ensure_ingress")) == 1
    assert host.kwargs("ensure_ingress")["target_port"] == BACKEND_PORT
    assert host.kwargs("ensure_ingress")["bind_address"] == "192.0.2.10"
    assert load_saved(settings)["lan"] == lan


def test_down_without_any_state_is_a_no_op(tmp_path, host):
    settings = make_settings(tmp_path)
    result = server.stop_server(settings, ["down"])
    assert result == {"configured": False, "running": False, "state_dir": str(settings.state_dir)}
    assert host.calls("set_local_server") == []


def test_down_with_only_legacy_state_directs_to_the_cutover(tmp_path, host, monkeypatch):
    settings = make_settings(tmp_path)
    record = v1_record(settings, host)
    monkeypatch.setattr(server, "load_legacy_record", lambda value: record)
    with pytest.raises(PortmapError, match="legacy server stack"):
        server.stop_server(settings, ["down"])


# --- gateway queries ---------------------------------------------------------------------


class FakeRuntime:
    def __init__(self):
        self.composed = []

    def compose(self, path, arguments, *, capture=False):
        self.composed.append((path, list(arguments)))
        return subprocess.CompletedProcess(["docker", "compose"], 0, "", "")


@pytest.fixture
def ready_infra(tmp_path, monkeypatch):
    state = {"phase": "ready", "runtime": {"backend": "docker"}, "instance_dir": str(tmp_path / "instance")}
    fake = FakeRuntime()
    monkeypatch.setattr(server, "load_infrastructure", lambda state_dir: state)
    monkeypatch.setattr(server, "ContainerRuntime", SimpleNamespace(from_record=lambda record: fake))
    return fake, state




@pytest.mark.parametrize("arguments", [["down"], ["up", "-d"], ["restart"], ["kill", "gateway"], ["rm", "-f"], ["stop", "dns"]])
def test_gateway_refuses_lifecycle_commands(tmp_path, ready_infra, arguments):
    with pytest.raises(PortmapError):
        server.run_gateway_compose(make_settings(tmp_path), arguments)


@pytest.mark.parametrize("arguments", [["ps", "-f", "/etc/passwd"], ["logs", "--file=/tmp/other.yml"], ["config", "-p", "evil"], ["--project-name=evil", "ps"]])
def test_gateway_refuses_project_or_file_overrides(tmp_path, ready_infra, arguments):
    with pytest.raises(PortmapError, match="cannot override the generated compose project or files"):
        server.run_gateway_compose(make_settings(tmp_path), arguments)


def test_gateway_requires_configured_infrastructure(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "load_infrastructure", lambda state_dir: None)
    monkeypatch.setattr(server, "load_legacy_record", lambda value: None)
    with pytest.raises(PortmapError, match="not configured"):
        server.run_gateway_compose(make_settings(tmp_path), ["ps"])


def test_gateway_with_legacy_state_directs_to_the_cutover(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "load_infrastructure", lambda state_dir: None)
    monkeypatch.setattr(server, "load_legacy_record", lambda value: {"version": 1})
    with pytest.raises(PortmapError, match="legacy server stack still owns local publishing"):
        server.run_gateway_compose(make_settings(tmp_path), ["ps"])


# --- status ------------------------------------------------------------------------------


def test_status_without_state_reports_unconfigured(tmp_path, host):
    result = server.server_status(make_settings(tmp_path))
    assert result == {"configured": False, "running": False, "state_dir": str(make_settings(tmp_path).state_dir)}


def test_status_reports_a_pending_legacy_migration(tmp_path, host, monkeypatch):
    settings = make_settings(tmp_path)
    record = v1_record(settings, host)
    monkeypatch.setattr(server, "load_legacy_record", lambda value: record)
    result = server.server_status(settings)
    assert result["configured"] is True
    assert result["migration_required"] is True
    assert result["running"] is None
    assert result["legacy"] == {"version": 1, "domain": settings.dns_domain, "http_port": settings.http_port}


def test_status_reports_the_shared_role_and_lan_ingress(tmp_path, host):
    settings = make_settings(tmp_path)
    save_record(settings, v2_record(settings, host, lan={"bind_address": "192.0.2.10", "port": 80}))
    host.ingress_report = {"owned": True, "running": True, "bind_address": "192.0.2.10", "port": 80}
    result = server.server_status(settings, check=True)
    assert result["version"] == 2 and result["configured"] is True and result["running"] is True
    assert result["public_address"] == "192.0.2.10"
    assert result["lan_ingress"]["running"] is True
    assert result["infrastructure"]["gateway"]["public_backend_port"] == BACKEND_PORT


def test_status_running_is_false_when_the_lan_ingress_is_down(tmp_path, host):
    settings = make_settings(tmp_path)
    save_record(settings, v2_record(settings, host, lan={"bind_address": "192.0.2.10", "port": 80}))
    host.ingress_report = {"owned": True, "running": False, "bind_address": "192.0.2.10", "port": 80}
    result = server.server_status(settings, check=True)
    assert result["running"] is False


def test_status_running_is_false_when_the_agent_is_down(tmp_path, host):
    settings = make_settings(tmp_path)
    save_record(settings, v2_record(settings, host, lan=None))
    host.agent_status = FakeAgentStatus(False)
    result = server.server_status(settings, check=True)
    assert result["running"] is False
