"""Behavioral tests for the container ingress backend and backend selection."""
import copy
import json
import os
import re
from pathlib import Path

import pytest

from portmap import ingress, runtime_ingress
from portmap.container_runtime import OWNER_LABEL, STATE_LABEL, UID_LABEL, ContainerRuntime
from portmap.errors import PortAllocationError, PortmapError
from portmap.infrastructure_config import IMAGES, resource_labels
from tests.test_ingress import SYSTEMCTL, FakeSystemd

GATEWAY = "172.19.0.250"
NETWORK = "portmap-gateway"


def infra_state(**overrides):
    state = {
        "version": 1,
        "uid": os.getuid(),
        "state_dir": "/state",
        "owner": "0" * 32,
        "runtime": {"backend": "docker", "socket_path": "/fake/engine.sock", "rootless": False},
        "network": {"name": NETWORK, "id": "netid1"},
        "addresses": {"gateway": GATEWAY},
    }
    state.update(overrides)
    return state


class FakeEngine:
    """Emulates the exact container engine API surface ingress is allowed to use."""

    def __init__(self, *, rootless=False):
        self.rootless = rootless
        self.containers = {}
        self.by_name = {}
        self.next_id = 1
        self.calls = []
        self.create_payloads = []
        self.serving = set()  # (ip, port) reachable after start
        self.busy_ports = set()  # (ip, port) owned by unrelated listeners
        self.start_errors = {}  # (ip, port) -> engine failure message
        self.exit_codes = {}  # (ip, port) -> container exit during startup

    # -- helpers -------------------------------------------------------------

    def container(self, name):
        cid = self.by_name.get(name)
        return copy.deepcopy(self.containers[cid]) if cid else None

    def running(self, name):
        container = self.container(name)
        return bool(container and container["State"].get("Running"))

    def published(self, name):
        container = self.container(name)
        if not container:
            return None
        return container["NetworkSettings"]["Ports"]

    def mutations(self):
        return [call for call in self.calls if call[0] != "GET"]

    # -- request dispatch ------------------------------------------------------

    def request(self, method, path, payload=None, *, statuses=(200,), timeout=15):
        self.calls.append((method, path))
        if method == "POST" and path.startswith("/containers/create"):
            return self._create(path, payload)
        if method == "POST" and re.fullmatch(r"/containers/[^/]+/start", path):
            return self._start(path)
        if method == "GET" and re.fullmatch(r"/containers/[^/]+/json", path):
            return self._inspect(path)
        if method == "DELETE" and path.startswith("/containers/"):
            return self._delete(path)
        raise AssertionError(f"unexpected engine request: {method} {path}")

    def _create(self, path, payload):
        name = path.partition("name=")[2]
        if not name or name in self.by_name:
            raise PortmapError(f"docker API POST {path} returned 409: Conflict. The container name {name!r} is already in use")
        host = payload["HostConfig"]
        (binding,) = host["PortBindings"][f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp"]
        cid = f"cid{self.next_id:04d}"
        self.next_id += 1
        self.create_payloads.append(copy.deepcopy(payload))
        network = next(iter((payload.get("NetworkingConfig") or {}).get("EndpointsConfig") or {}))
        self.containers[cid] = {
            "Id": cid,
            "Name": f"/{name}",
            "Config": {
                "Labels": dict(payload["Labels"]),
                "Image": payload["Image"],
                "User": payload["User"],
                "Cmd": list(payload["Cmd"]),
            },
            "HostConfig": copy.deepcopy(host),
            "State": {"Status": "created", "Running": False, "ExitCode": None, "Error": ""},
            "NetworkSettings": {
                "Ports": {},
                "Networks": {network: {"IPAddress": "172.19.0.9"}},
            },
            "_publish": (binding["HostIp"], int(binding["HostPort"])),
        }
        self.by_name[name] = cid
        return {"Id": cid, "Warnings": []}

    def _start(self, path):
        cid = path.split("/")[2]
        container = self.containers.get(cid)
        if container is None:
            return {"message": "No such container"}
        bind, port = container["_publish"]
        if (bind, port) in self.busy_ports:
            raise PortmapError(
                f"docker API POST {path} returned 500: driver failed programming external connectivity: "
                f"Bind for {bind}:{port} failed: port is already allocated"
            )
        if (bind, port) in self.start_errors:
            raise PortmapError(f"docker API POST {path} returned 500: {self.start_errors[(bind, port)]}")
        if (bind, port) in self.exit_codes:
            container["State"] = {
                "Status": "exited", "Running": False, "ExitCode": self.exit_codes[(bind, port)], "Error": "haproxy bad config",
            }
            return None
        container["State"] = {"Status": "running", "Running": True, "ExitCode": 0, "Error": ""}
        container["NetworkSettings"]["Ports"] = {
            f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp": [{"HostIp": bind, "HostPort": str(port)}]
        }
        self.serving.add((bind, port))
        return None

    def _inspect(self, path):
        identity = path.split("/")[2]
        cid = identity if identity in self.containers else self.by_name.get(identity)
        container = self.containers.get(cid) if cid else None
        if container is None:
            return {"message": "No such container"}
        return copy.deepcopy(container)

    def _delete(self, path):
        cid = path.split("/")[2].split("?")[0]
        container = self.containers.pop(cid, None)
        if container is not None:
            name = container["Name"].lstrip("/")
            self.by_name.pop(name, None)
            bind, port = container["_publish"]
            self.serving.discard((bind, port))
        return None


class FakeRuntime(ContainerRuntime):
    def __init__(self, engine):
        super().__init__(backend="docker", socket_path="/fake/engine.sock", rootless=engine.rootless)
        self.engine = engine

    def request(self, method, path, payload=None, *, statuses=(200,), timeout=15):
        return self.engine.request(method, path, payload, statuses=statuses, timeout=timeout)


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(ContainerRuntime, "from_record", classmethod(lambda cls, record: FakeRuntime(fake)))
    monkeypatch.setattr(runtime_ingress, "_connectable", lambda bind, port: (bind, int(port)) in fake.serving)
    return fake


@pytest.fixture
def fake_systemd(tmp_path, monkeypatch):
    unit_dir = tmp_path / "units"
    run_dir = tmp_path / "run"
    unit_dir.mkdir()
    run_dir.mkdir()
    proxyd = tmp_path / "systemd-socket-proxyd"
    proxyd.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    proxyd.chmod(0o755)
    fake = FakeSystemd(unit_dir, proxyd)
    monkeypatch.setattr(ingress, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(ingress, "SYSTEMD_RUN_DIR", run_dir)
    monkeypatch.setattr(ingress, "PROXYD_CANDIDATES", (proxyd,))
    monkeypatch.setattr(ingress, "ROOT_UID", os.getuid())
    monkeypatch.setattr(ingress, "_systemctl_path", lambda: SYSTEMCTL)
    monkeypatch.setattr(ingress.subprocess, "run", fake.run)
    return fake


DEFAULT_INFRA = object()


def ensure(tmp_path, *, role="private", bind="127.0.0.1", port=18081, target_port=8080,
           backend="container", state=DEFAULT_INFRA, **kwargs):
    infrastructure = None if state is None else (infra_state() if state is DEFAULT_INFRA else state)
    return ingress.ensure_ingress(
        tmp_path, name=role, bind_address=bind, port=port, target_port=target_port,
        backend=backend, infrastructure=infrastructure, **kwargs,
    )


def record_of(tmp_path, role="private"):
    return json.loads((tmp_path / "ingress" / f"{role}.json").read_text(encoding="utf-8"))


def expected_labels(tmp_path, role="private"):
    return runtime_ingress.ingress_labels(tmp_path, role)


# --- container creation and configuration ---------------------------------------------


def test_container_ingress_creates_independently_owned_haproxy(tmp_path, engine):
    result = ensure(tmp_path)

    assert result["backend"] == "container"
    assert result["running"] is True
    assert result["owned"] is True
    assert result["changed"] is True
    assert result["bind_address"] == "127.0.0.1"
    assert result["port"] == 18081
    assert result["target_port"] == 8080

    name = runtime_ingress.container_name(tmp_path, "private")
    payload = engine.create_payloads[0]
    assert payload["Image"] == IMAGES["relay"]
    assert payload["Labels"] == expected_labels(tmp_path)
    assert not (set(payload["Labels"]) & {OWNER_LABEL, STATE_LABEL, UID_LABEL})
    assert all(label.startswith("portmap.ingress") for label in payload["Labels"])
    host = payload["HostConfig"]
    assert host["PortBindings"] == {
        f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]
    }
    assert host.get("NetworkMode") != "host"
    assert host["CapDrop"] == ["ALL"] and "CapAdd" not in host
    assert host["ReadonlyRootfs"] is True
    assert payload["NetworkingConfig"]["EndpointsConfig"] == {NETWORK: {}}
    assert payload["User"] == f"{os.getuid()}:{os.getgid()}"
    (mount,) = host["Binds"]
    assert mount.endswith(f":{runtime_ingress.CONTAINER_CONFIG_PATH}:ro")

    config = (tmp_path / "ingress" / "private.cfg").read_text(encoding="utf-8")
    assert f"server gateway {GATEWAY}:80" in config
    assert f"bind *:{runtime_ingress.CONTAINER_LISTEN_PORT}" in config

    saved = record_of(tmp_path)
    assert saved["backend"] == "container"
    assert saved["container"] == name
    assert saved["container_id"] == engine.container(name)["Id"]
    assert saved["labels"] == expected_labels(tmp_path)
    assert saved["gateway_address"] == GATEWAY
    assert saved["gateway_port"] == 80
    assert saved["uid"] == os.getuid()
    assert saved["state"] == str(tmp_path.resolve())


def test_ingress_containers_never_match_the_infrastructure_inventory(tmp_path, engine):
    ensure(tmp_path)
    labels = infra_state()["owner"], str(tmp_path), str(os.getuid())
    infra_labels = resource_labels(infra_state())
    matched = [
        container for container in engine.containers.values()
        if all((container["Config"]["Labels"] or {}).get(key) == value for key, value in infra_labels.items())
    ]
    assert matched == []
    assert labels[0] != engine.container(runtime_ingress.container_name(tmp_path, "private"))["Id"]


def test_public_role_targets_gateway_web_entrypoint_with_distinct_container(tmp_path, engine):
    ensure(tmp_path, role="public", bind="192.0.2.10", port=80, target_port=18082)
    ensure(tmp_path)

    private_name = runtime_ingress.container_name(tmp_path, "private")
    public_name = runtime_ingress.container_name(tmp_path, "public")
    assert public_name != private_name
    assert engine.published(public_name) == {
        f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp": [{"HostIp": "192.0.2.10", "HostPort": "80"}]
    }
    assert engine.published(private_name) == {
        f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]
    }
    public_config = (tmp_path / "ingress" / "public.cfg").read_text(encoding="utf-8")
    assert f"server gateway {GATEWAY}:8080" in public_config


def test_rootless_runtime_maps_config_to_the_invoking_user(tmp_path, engine):
    engine.rootless = True
    ensure(tmp_path, state=infra_state(runtime={"backend": "podman", "socket_path": "/fake/engine.sock", "rootless": True}))
    assert engine.create_payloads[0]["User"] == "0:0"


def test_explicit_container_backend_never_uses_sudo_or_systemctl(tmp_path, engine, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the container backend must not touch systemd or sudo")

    monkeypatch.setattr(ingress, "_require_systemd", forbidden)
    monkeypatch.setattr(ingress, "_privileged", forbidden)
    monkeypatch.setattr(ingress, "_run", forbidden)
    monkeypatch.setattr(ingress, "_systemctl_path", forbidden)
    assert "subprocess" not in vars(runtime_ingress)

    result = ensure(tmp_path)
    assert result["running"] is True


# --- conflict and failure behavior -----------------------------------------------------


def test_bind_conflict_is_authoritative_and_never_falls_back(tmp_path, engine, monkeypatch):
    engine.busy_ports.add(("127.0.0.1", 18081))
    monkeypatch.setattr(ingress, "_require_systemd", lambda *a, **k: (_ for _ in ()).throw(AssertionError("systemd")))

    with pytest.raises(PortAllocationError, match="never stops unrelated services"):
        ensure(tmp_path)

    assert len(engine.create_payloads) == 1
    binding = engine.create_payloads[0]["HostConfig"]["PortBindings"][f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp"][0]
    assert binding["HostPort"] == "18081"
    assert not (tmp_path / "ingress" / "private.json").exists()
    assert not (tmp_path / "ingress" / "private.cfg").exists()


def test_container_exit_during_startup_reports_and_cleans_up(tmp_path, engine):
    engine.exit_codes[("127.0.0.1", 18081)] = 1

    with pytest.raises(PortmapError, match="exited during startup"):
        ensure(tmp_path)

    assert engine.containers == {}
    assert not (tmp_path / "ingress" / "private.json").exists()


def test_failed_replacement_restores_the_previous_container(tmp_path, engine):
    ensure(tmp_path)
    old_id = engine.container(runtime_ingress.container_name(tmp_path, "private"))["Id"]

    engine.busy_ports.add(("127.0.0.1", 18082))
    with pytest.raises(PortAllocationError, match="never stops unrelated services"):
        ensure(tmp_path, port=18082)

    container = engine.container(runtime_ingress.container_name(tmp_path, "private"))
    assert container is not None and container["Id"] != old_id
    assert engine.running(runtime_ingress.container_name(tmp_path, "private"))
    assert engine.published(runtime_ingress.container_name(tmp_path, "private")) == {
        f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]
    }
    assert record_of(tmp_path)["port"] == 18081
    assert record_of(tmp_path)["container_id"] == container["Id"]


def test_address_conflict_on_a_new_port_retains_the_old_owned_record(tmp_path, engine):
    ensure(tmp_path)
    before = record_of(tmp_path)
    engine.serving.add(("127.0.0.1", 18082))  # a foreign listener already owns the new port

    with pytest.raises(PortAllocationError, match="never stops unrelated services"):
        ensure(tmp_path, port=18082)

    assert record_of(tmp_path) == before
    name = runtime_ingress.container_name(tmp_path, "private")
    assert engine.running(name)
    assert engine.published(name) == {
        f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]
    }


def test_parameter_change_recreates_the_container(tmp_path, engine):
    ensure(tmp_path)
    first_id = engine.container(runtime_ingress.container_name(tmp_path, "private"))["Id"]

    result = ensure(tmp_path, port=18083)

    container = engine.container(runtime_ingress.container_name(tmp_path, "private"))
    assert container["Id"] != first_id
    assert result["changed"] is True
    assert result["port"] == 18083
    assert engine.published(runtime_ingress.container_name(tmp_path, "private")) == {
        f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18083"}]
    }


def test_idempotent_ensure_does_not_mutate_the_engine(tmp_path, engine):
    ensure(tmp_path)
    before = list(engine.mutations())

    result = ensure(tmp_path)

    assert result["changed"] is False
    assert result["running"] is True
    assert engine.mutations() == before


def test_external_config_edit_triggers_recreation(tmp_path, engine):
    ensure(tmp_path)
    first_id = engine.container(runtime_ingress.container_name(tmp_path, "private"))["Id"]
    config = tmp_path / "ingress" / "private.cfg"
    config.write_text("global\n    sabotage\n", encoding="utf-8")

    result = ensure(tmp_path)

    assert result["changed"] is True
    assert engine.container(runtime_ingress.container_name(tmp_path, "private"))["Id"] != first_id
    assert "server gateway" in config.read_text(encoding="utf-8")


# --- ownership enforcement -------------------------------------------------------------


def test_foreign_container_with_the_ingress_name_is_refused(tmp_path, engine):
    name = runtime_ingress.container_name(tmp_path, "private")
    engine.containers["cid9999"] = {
        "Id": "cid9999", "Name": f"/{name}",
        "Config": {"Labels": {"someone.else": "true"}, "Image": "nginx", "User": "", "Cmd": []},
        "HostConfig": {}, "State": {"Status": "running", "Running": True, "ExitCode": 0, "Error": ""},
        "NetworkSettings": {"Ports": {}, "Networks": {}}, "_publish": ("127.0.0.1", 18081),
    }
    engine.by_name[name] = "cid9999"

    with pytest.raises(PortmapError, match="refusing to change container"):
        ensure(tmp_path)

    assert "cid9999" in engine.containers


def test_tampered_record_is_refused(tmp_path, engine):
    ensure(tmp_path)
    record = record_of(tmp_path)
    record["uid"] = record["uid"] + 1
    (tmp_path / "ingress" / "private.json").write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(PortmapError, match="refusing to use the recorded ingress"):
        ensure(tmp_path, port=18084)
    assert ingress.ingress_status(tmp_path, name="private")["error"] is not None
    with pytest.raises(PortmapError, match="refusing to use the recorded ingress"):
        ingress.stop_ingress(tmp_path, name="private")


def test_record_from_another_state_directory_is_refused(tmp_path, tmp_path_factory, engine):
    ensure(tmp_path)
    other = tmp_path_factory.mktemp("other-state")
    (other / "ingress").mkdir(parents=True)
    (other / "ingress" / "private.json").write_text(json.dumps(record_of(tmp_path)), encoding="utf-8")
    (other / "ingress" / "private.json").chmod(0o600)

    with pytest.raises(PortmapError, match="refusing to use the recorded ingress"):
        ingress.ensure_ingress(
            other, name="private", bind_address="127.0.0.1", port=18081, target_port=8080,
            backend="container", infrastructure=infra_state(),
        )


def test_missing_infrastructure_for_explicit_container_is_an_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_ingress, "load_infrastructure_state", lambda state_dir: None)
    with pytest.raises(PortmapError, match="requires the shared container infrastructure"):
        ingress.ensure_ingress(
            tmp_path, name="private", bind_address="127.0.0.1", port=18081, target_port=8080, backend="container"
        )


# --- status and stop --------------------------------------------------------------------


def test_status_reports_missing_and_stopped_containers(tmp_path, engine):
    ensure(tmp_path)
    engine.containers.clear()
    engine.by_name.clear()

    status = ingress.ingress_status(tmp_path, name="private")
    assert status["backend"] == "container"
    assert status["installed"] is False
    assert status["running"] is False
    assert status["port"] == 18081
    assert "missing" in status["error"]


def test_status_running_requires_the_published_port_mapping(tmp_path, engine):
    ensure(tmp_path)
    name = runtime_ingress.container_name(tmp_path, "private")
    assert ingress.ingress_status(tmp_path, name="private")["running"] is True

    engine.containers[engine.by_name[name]]["State"]["Running"] = False
    assert ingress.ingress_status(tmp_path, name="private")["running"] is False


def test_status_preserves_keys_consumed_by_the_server_and_legacy_views(tmp_path, engine):
    ensure(tmp_path)
    status = ingress.ingress_status(tmp_path, name="private")
    for key in ("owned", "running", "bind_address", "port", "target_port", "installed", "error"):
        assert key in status


def test_stop_removes_only_the_owned_container_and_config(tmp_path, engine):
    ensure(tmp_path)
    name = runtime_ingress.container_name(tmp_path, "private")

    result = ingress.stop_ingress(tmp_path, name="private")

    assert result["backend"] == "container"
    assert result["stopped"] is True and result["removed"] is True
    assert name not in engine.by_name
    assert not (tmp_path / "ingress" / "private.json").exists()
    assert not (tmp_path / "ingress" / "private.cfg").exists()

    again = ingress.stop_ingress(tmp_path, name="private")
    assert again["removed"] is False and again["stopped"] is True


def test_stop_refuses_a_container_whose_labels_changed(tmp_path, engine):
    ensure(tmp_path)
    name = runtime_ingress.container_name(tmp_path, "private")
    engine.containers[engine.by_name[name]]["Config"]["Labels"] = {"someone.else": "true"}

    with pytest.raises(PortmapError, match="refusing to change container"):
        ingress.stop_ingress(tmp_path, name="private")

    assert engine.by_name[name] is not None
    assert record_of(tmp_path)["backend"] == "container"


# --- role independence -------------------------------------------------------------------


def test_public_lan_changes_leave_the_private_ingress_untouched(tmp_path, engine):
    ensure(tmp_path)
    ensure(tmp_path, role="public", bind="192.0.2.10", port=8080, target_port=18082)
    private_name = runtime_ingress.container_name(tmp_path, "private")
    private_id = engine.container(private_name)["Id"]

    ensure(tmp_path, role="public", bind="198.51.100.7", port=8081, target_port=18082)

    assert engine.container(private_name)["Id"] == private_id
    assert engine.running(private_name)
    assert ingress.ingress_status(tmp_path, name="private")["running"] is True
    assert engine.published(runtime_ingress.container_name(tmp_path, "public")) == {
        f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp": [{"HostIp": "198.51.100.7", "HostPort": "8081"}]
    }


# --- automatic backend selection ----------------------------------------------------------


def test_auto_follows_the_infrastructure_recorded_selection(tmp_path, engine, monkeypatch):
    loaded = []

    def fake_load(state_dir):
        loaded.append(Path(state_dir))
        return infra_state(ingress_backend="container")

    monkeypatch.setattr(runtime_ingress, "load_infrastructure_state", fake_load)

    result = ensure(tmp_path, backend="auto", state=None)

    assert result["backend"] == "container"
    assert loaded == [Path(tmp_path)]
    assert record_of(tmp_path)["backend"] == "container"


def test_auto_reuses_a_saved_systemd_record_without_a_backend_field(tmp_path, engine, fake_systemd):
    ingress_dir = tmp_path / "ingress"
    ingress_dir.mkdir(parents=True)
    (ingress_dir / "private.json").write_text(json.dumps({
        "version": 1, "name": "private", "bind_address": "127.0.0.1", "port": 18085,
        "target_port": 18080, "socket_unit": "portmap-ingress-private.socket",
        "service_unit": "portmap-ingress-private.service", "proxyd": "/usr/lib/systemd/systemd-socket-proxyd",
        "uid": os.getuid(), "state": str(tmp_path.resolve()),
    }), encoding="utf-8")
    (ingress_dir / "private.json").chmod(0o600)

    result = ingress.ensure_ingress(
        tmp_path, name="private", bind_address="127.0.0.1", port=18085, target_port=18080,
        backend="auto", infrastructure=infra_state(),
    )

    assert result["backend"] == "systemd"
    assert result["running"] is True
    assert engine.calls == []
    socket_unit, _ = ingress.unit_names(tmp_path, "private")
    assert (fake_systemd.unit_dir / socket_unit).exists()


def test_auto_selects_the_container_backend_on_a_rootless_engine(tmp_path, engine):
    engine.rootless = True
    result = ensure(
        tmp_path, backend="auto", port=8080,
        state=infra_state(runtime={"backend": "podman", "socket_path": "/fake/engine.sock", "rootless": True}),
    )
    assert result["backend"] == "container"
    assert result["running"] is True


def test_auto_selects_container_for_privileged_ports_on_a_rootful_engine(tmp_path, engine):
    result = ensure(tmp_path, backend="auto", port=80)
    assert result["backend"] == "container"
    assert result["port"] == 80
    assert result["running"] is True


def test_auto_publishes_the_exact_privileged_port_on_a_rootless_engine(tmp_path, engine):
    engine.rootless = True
    result = ensure(
        tmp_path, backend="auto", port=80,
        state=infra_state(runtime={"backend": "podman", "socket_path": "/fake/engine.sock", "rootless": True}),
    )
    assert result["backend"] == "container"
    assert result["port"] == 80
    assert result["running"] is True
    assert ("127.0.0.1", 80) in engine.serving
    binding = engine.create_payloads[0]["HostConfig"]["PortBindings"][f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp"][0]
    assert binding == {"HostIp": "127.0.0.1", "HostPort": "80"}


def test_auto_stays_on_the_container_backend_even_when_systemd_and_sudo_are_available(tmp_path, engine, fake_systemd):
    engine.rootless = True
    result = ensure(
        tmp_path, backend="auto", port=80,
        state=infra_state(runtime={"backend": "podman", "socket_path": "/fake/engine.sock", "rootless": True}),
    )
    assert result["backend"] == "container"
    assert result["running"] is True
    assert fake_systemd.commands == []
    socket_unit, _ = ingress.unit_names(tmp_path, "private")
    assert not (fake_systemd.unit_dir / socket_unit).exists()


def test_auto_container_engine_permission_failure_is_authoritative_even_with_systemd(tmp_path, engine, fake_systemd):
    engine.rootless = True
    engine.start_errors[("127.0.0.1", 80)] = "permission denied while publishing port: operation not permitted"

    with pytest.raises(PortmapError, match="operation not permitted"):
        ensure(
            tmp_path, backend="auto", port=80,
            state=infra_state(runtime={"backend": "podman", "socket_path": "/fake/engine.sock", "rootless": True}),
        )

    assert len(engine.create_payloads) == 1
    assert fake_systemd.commands == []
    socket_unit, _ = ingress.unit_names(tmp_path, "private")
    assert not (fake_systemd.unit_dir / socket_unit).exists()
    assert not (tmp_path / "ingress" / "private.json").exists()
    assert not (tmp_path / "ingress" / "private.cfg").exists()


def test_auto_without_infrastructure_state_is_a_setup_error_not_a_systemd_fallback(tmp_path, fake_systemd, monkeypatch):
    monkeypatch.setattr(runtime_ingress, "load_infrastructure_state", lambda state_dir: None)

    with pytest.raises(PortmapError, match="requires the shared container infrastructure"):
        ingress.ensure_ingress(tmp_path, name="private", bind_address="127.0.0.1", port=18081, target_port=8080)

    assert fake_systemd.commands == []
    socket_unit, _ = ingress.unit_names(tmp_path, "private")
    assert not (fake_systemd.unit_dir / socket_unit).exists()


# --- switching implementations -----------------------------------------------------------


def test_explicit_switch_to_systemd_releases_the_container_port_first(tmp_path, engine, fake_systemd):
    ensure(tmp_path)

    result = ensure(tmp_path, backend="systemd")

    assert result["backend"] == "systemd"
    assert result["running"] is True
    assert runtime_ingress.container_name(tmp_path, "private") not in engine.by_name
    socket_unit, service_unit = ingress.unit_names(tmp_path, "private")
    assert (fake_systemd.unit_dir / socket_unit).exists()
    assert (fake_systemd.unit_dir / service_unit).exists()
    assert ingress.ingress_status(tmp_path, name="private")["backend"] == "systemd"


def test_failed_switch_restores_the_previous_container_ingress(tmp_path, engine, fake_systemd, monkeypatch):
    monkeypatch.setattr(runtime_ingress, "load_infrastructure_state", lambda state_dir: infra_state())
    ensure(tmp_path)
    fake_systemd.fail_install_content = "[Socket]"

    with pytest.raises(PortmapError, match="installing system unit"):
        ensure(tmp_path, backend="systemd")

    name = runtime_ingress.container_name(tmp_path, "private")
    assert engine.running(name)
    assert engine.published(name) == {
        f"{runtime_ingress.CONTAINER_LISTEN_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]
    }
    assert record_of(tmp_path)["backend"] == "container"
    assert ingress.ingress_status(tmp_path, name="private")["backend"] == "container"
