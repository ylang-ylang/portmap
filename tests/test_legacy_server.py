"""Behavioral tests for the legacy server capture/cutover/retire machinery."""
import json
import os
import socket
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import infrastructure, legacy_server, server
from portmap.errors import PortmapError
from portmap.settings import load_portmap_settings

BACKEND_PORT = 45555
LEGACY_BACKEND_PORT = 47123
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"

COREFILE = """{$PORTMAP_DNS_DOMAIN}:53 {
  template IN A {
    match ^(.+\\.)?{$PORTMAP_DNS_DOMAIN}\\.$
    answer "{{ .Name }} 60 IN A {$PORTMAP_DNS_TARGET_IP}"
  }
  template IN AAAA {
    match ^(.+\\.)?{$PORTMAP_DNS_DOMAIN}\\.$
    rcode NOERROR
  }
  errors
  log
}

.:53 {
  forward . {$PORTMAP_DNS_FORWARD}
  cache 30
  errors
  log
}
"""

COREFILE_WITH_HOSTS = """{$PORTMAP_DNS_DOMAIN}:53 {
  hosts {
    192.0.2.99 box.{$PORTMAP_DNS_DOMAIN}
    fallthrough
  }
  forward . {$PORTMAP_DNS_FORWARD}
  errors
}
"""


def make_settings(tmp_path: Path, **env: str):
    values = {
        "PORTMAP_ROOT": str(tmp_path),
        "PORTMAP_STATE_DIR": str(tmp_path / "state"),
        "PORTMAP_AGENT_RUNTIME_DIR": str(tmp_path / "agent"),
        "PORTMAP_HOST_IP": "192.0.2.10",
    }
    values.update(env)
    return load_portmap_settings(environ=values)


class FakeLegacyRuntime:
    """Docker/Podman API stand-in carrying Compose-labelled containers."""

    def __init__(self, root: Path, network: str = "portmap_gateway"):
        self.root = root
        self.network = network
        self.containers: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self._sequence = 0

    def add(self, service, *, running=True, labels=None, address=None, publish=None, name=None):
        self._sequence += 1
        identity = f"id{self._sequence:04d}{service}"
        base = {
            PROJECT_LABEL: "portmap",
            SERVICE_LABEL: service,
            WORKING_DIR_LABEL: str(self.root),
            CONFIG_FILES_LABEL: str(self.root / "docker-compose.yml"),
        }
        if service == "catalog":
            base["portmap.gateway"] = "true"
        merged = {**base, **(labels or {})}
        container = {
            "Id": identity,
            "name": name or f"portmap-{service}",
            "Labels": dict(merged),
            "Config": {"Labels": dict(merged)},
            "State": {"Running": running},
            "NetworkSettings": {
                "Networks": {self.network: {"IPAddress": address or f"172.20.0.{10 + self._sequence}"}},
                "Ports": dict(publish or {}),
            },
        }
        self.containers[identity] = container
        return container

    def by_name(self, name):
        return next(container for container in self.containers.values() if container["name"] == name)

    def owned_containers(self, labels):
        found = []
        for container in self.containers.values():
            if all(container["Labels"].get(key) == value for key, value in labels.items()):
                found.append({"Id": container["Id"], "Names": ["/" + container["name"]],
                              "Labels": dict(container["Labels"]), "State": dict(container["State"])})
        return found

    def inspect_container(self, identity):
        container = self.containers.get(identity)
        return json.loads(json.dumps(container)) if container else None

    def request(self, method, path, payload=None, *, statuses=(200,), timeout=15):
        self.requests.append((method, path))
        identity = path.split("/")[2].split("?")[0] if "/containers/" in path else None
        container = self.containers.get(identity) if identity else None
        if container is None:
            raise PortmapError(f"unknown container {identity}")
        if method == "POST" and path.endswith("/stop"):
            container["State"]["Running"] = False
        elif method == "POST" and path.endswith("/start"):
            container["State"]["Running"] = True
        elif method == "DELETE":
            self.containers.pop(identity, None)
        return None

    def verbs(self):
        return [(method, "stop" if path.endswith("/stop") else "start" if path.endswith("/start") else "delete")
                for method, path in self.requests]


def write_corefile(root: Path, content: str = COREFILE):
    path = root / "gateway" / "Corefile"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o644)
    return path


def save_legacy_record(settings, record):
    directory = settings.state_dir / "server"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "instance.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def legacy_record(settings, root, client_dir, *, backend_port=LEGACY_BACKEND_PORT, overrides=None):
    values = server._settings_record(settings)
    values["root"] = str(root)
    values.update(overrides or {})
    return {"version": 1, "uid": os.getuid(), "state_dir": str(settings.state_dir.resolve()),
            "backend_port": backend_port, "client_state_dir": str(client_dir), "settings": values}


def dns_publish(address="192.0.2.10", port="53"):
    return {"53/udp": [{"HostIp": address, "HostPort": port}], "53/tcp": [{"HostIp": address, "HostPort": port}]}


# --- record loading ----------------------------------------------------------------------


def test_load_legacy_record_validates_ownership(tmp_path):
    settings = make_settings(tmp_path)
    root = tmp_path / "legacy-root"
    record = legacy_record(settings, root, tmp_path / "client")
    save_legacy_record(settings, record)
    assert legacy_server.load_legacy_record(settings) == record


def test_load_legacy_record_absent_returns_none(tmp_path):
    assert legacy_server.load_legacy_record(make_settings(tmp_path)) is None


def test_load_legacy_record_ignores_version2_records(tmp_path):
    settings = make_settings(tmp_path)
    record = legacy_record(settings, tmp_path, tmp_path / "client") | {"version": 2}
    save_legacy_record(settings, record)
    assert legacy_server.load_legacy_record(settings) is None


@pytest.mark.parametrize("mutate", [
    lambda record: record.update(uid=os.getuid() + 1),
    lambda record: record.update(state_dir="/somewhere/else"),
    lambda record: record.update(backend_port=80),
    lambda record: record.update(client_state_dir="relative/path"),
    lambda record: record["settings"].update(root="relative"),
])
def test_load_legacy_record_rejects_invalid_ownership(tmp_path, mutate):
    settings = make_settings(tmp_path)
    record = legacy_record(settings, tmp_path, tmp_path / "client")
    mutate(record)
    save_legacy_record(settings, record)
    with pytest.raises(PortmapError, match="invalid legacy server state"):
        legacy_server.load_legacy_record(settings)


def test_load_legacy_record_rejects_world_writable_state(tmp_path):
    settings = make_settings(tmp_path)
    path = save_legacy_record(settings, legacy_record(settings, tmp_path, tmp_path / "client"))
    path.chmod(0o644)
    with pytest.raises(PortmapError, match="unsafe legacy server state file"):
        legacy_server.load_legacy_record(settings)


# --- capture -----------------------------------------------------------------------------


@pytest.fixture
def legacy_stack(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    root = tmp_path / "legacy-root"
    root.mkdir()
    (root / "docker-compose.yml").write_text("name: portmap\n", encoding="utf-8")
    write_corefile(root)
    runtime = FakeLegacyRuntime(root)
    runtime.add("catalog")
    runtime.add("traefik")
    runtime.add("dns", address="172.20.0.99", publish=dns_publish())
    monkeypatch.setattr(legacy_server, "ingress_status", lambda state_dir, *, name: {
        "owned": True, "running": True, "bind_address": "192.0.2.10", "port": 80, "target_port": LEGACY_BACKEND_PORT})
    record = legacy_record(settings, root, tmp_path / "client")
    save_legacy_record(settings, record)
    return SimpleNamespace(settings=settings, root=root, runtime=runtime, record=record)


def test_capture_records_exact_compose_ownership(legacy_stack):
    capture = legacy_server.capture_legacy_server(legacy_stack.settings, legacy_stack.record, legacy_stack.runtime)
    assert capture["version"] == 1 and capture["uid"] == os.getuid()
    assert capture["root"] == str(legacy_stack.root)
    assert capture["backend_port"] == LEGACY_BACKEND_PORT
    for service in ("catalog", "traefik", "dns"):
        entry = capture["containers"][service]
        assert entry["name"] == f"portmap-{service}"
        assert entry["labels"][PROJECT_LABEL] == "portmap"
        assert entry["labels"][SERVICE_LABEL] == service
        assert entry["labels"][WORKING_DIR_LABEL] == str(legacy_stack.root)
    assert capture["containers"]["catalog"]["labels"]["portmap.gateway"] == "true"
    assert capture["containers"]["dns"]["network"]["address"] == "172.20.0.99"
    assert capture["ingress"] == {"bind_address": "192.0.2.10", "port": 80, "target_port": LEGACY_BACKEND_PORT}
    assert capture["zone"]["wildcard"] == "192.0.2.10"
    assert capture["zone"]["forward"] == "/etc/resolv.conf"
    assert legacy_stack.runtime.requests == []


def test_frozen_compose_migration_stops_only_owned_containers(legacy_stack):
    files = ",".join(str(legacy_stack.root / name) for name in ("docker-compose.yml", "docker-compose.frozen.yml"))
    for container in legacy_stack.runtime.containers.values():
        container["Labels"][CONFIG_FILES_LABEL] = files
        container["Config"]["Labels"][CONFIG_FILES_LABEL] = files
    foreign = legacy_stack.runtime.add("catalog", name="foreign-catalog", labels={
        WORKING_DIR_LABEL: str(legacy_stack.root.parent / "foreign"),
    })
    capture = legacy_server.capture_legacy_server(legacy_stack.settings, legacy_stack.record, legacy_stack.runtime)
    legacy_server.stop_legacy_stack(capture, legacy_stack.runtime)
    assert foreign["State"]["Running"] is True
    assert all(not legacy_stack.runtime.containers[entry["id"]]["State"]["Running"] for entry in capture["containers"].values())


@pytest.mark.parametrize("kind", ["outside", "prefix-sibling", "traversal", "symlink"])
def test_capture_refuses_compose_override_outside_owned_root(legacy_stack, kind):
    root = legacy_stack.root
    if kind == "outside":
        override = root.parent / "foreign.yml"
    elif kind == "prefix-sibling":
        override = root.with_name(root.name + "-foreign") / "override.yml"
    elif kind == "traversal":
        override = root / ".." / "foreign.yml"
    else:
        override = root / "override.yml"
        override.symlink_to(root.parent / "foreign.yml")
    files = str(root / "docker-compose.yml") + "," + str(override)
    container = legacy_stack.runtime.by_name("portmap-catalog")
    container["Labels"][CONFIG_FILES_LABEL] = files
    container["Config"]["Labels"][CONFIG_FILES_LABEL] = files
    with pytest.raises(PortmapError):
        legacy_server.capture_legacy_server(legacy_stack.settings, legacy_stack.record, legacy_stack.runtime)
    assert legacy_stack.runtime.requests == []


@pytest.mark.parametrize("tamper", [
    {CONFIG_FILES_LABEL: "/etc/compose.yml"},
    {"portmap.gateway": "false"},
])
def test_capture_refuses_tampered_ownership_labels(tmp_path, tamper):
    settings = make_settings(tmp_path)
    root = tmp_path / "legacy-root"
    runtime = FakeLegacyRuntime(root)
    runtime.add("catalog", labels=tamper)
    record = legacy_record(settings, root, tmp_path / "client")
    with pytest.raises(PortmapError):
        legacy_server.capture_legacy_server(settings, record, runtime)


def test_capture_refuses_a_familiar_name_without_matching_labels(tmp_path):
    settings = make_settings(tmp_path)
    root = tmp_path / "legacy-root"
    runtime = FakeLegacyRuntime(root)
    runtime.add("traefik", name="portmap-dns")
    record = legacy_record(settings, root, tmp_path / "client")
    with pytest.raises(PortmapError, match="its name is"):
        legacy_server.capture_legacy_server(settings, record, runtime)


def test_capture_tolerates_absent_services_and_foreign_project_members(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    root = tmp_path / "legacy-root"
    runtime = FakeLegacyRuntime(root)
    runtime.add("dns", address="172.20.0.99", publish=dns_publish())
    runtime.add("catalog", labels={WORKING_DIR_LABEL: "/other/root"})
    monkeypatch.setattr(legacy_server, "ingress_status", lambda state_dir, *, name: {"owned": False})
    record = legacy_record(settings, root, tmp_path / "client")
    capture = legacy_server.capture_legacy_server(settings, record, runtime)
    assert capture["containers"]["catalog"] is None and capture["containers"]["traefik"] is None
    assert capture["containers"]["dns"] is not None
    assert capture["foreign_services"] == ["portmap-catalog"]
    assert capture["ingress"] is None


def test_capture_rejects_a_changed_publishing_configuration(tmp_path):
    settings = make_settings(tmp_path)
    root = tmp_path / "legacy-root"
    runtime = FakeLegacyRuntime(root)
    record = legacy_record(settings, root, tmp_path / "client", overrides={"dns_domain": "other.portmap"})
    with pytest.raises(PortmapError, match="was written for dns_domain='other.portmap'"):
        legacy_server.capture_legacy_server(settings, record, runtime)


def test_capture_parses_literal_hosts_entries(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    root = tmp_path / "legacy-root"
    write_corefile(root, COREFILE_WITH_HOSTS)
    runtime = FakeLegacyRuntime(root)
    monkeypatch.setattr(legacy_server, "ingress_status", lambda state_dir, *, name: {"owned": False})
    record = legacy_record(settings, root, tmp_path / "client")
    capture = legacy_server.capture_legacy_server(settings, record, runtime)
    assert capture["zone"]["records"] == {f"box.{settings.dns_domain}": "192.0.2.99"}
    assert capture["zone"]["wildcard"] is None


# --- bindings payload --------------------------------------------------------------------


def test_public_dns_bindings_preserve_the_exact_legacy_binding(legacy_stack):
    saved = server._saved_settings(legacy_stack.settings, legacy_stack.record)
    capture = legacy_server.capture_legacy_server(legacy_stack.settings, legacy_stack.record, legacy_stack.runtime)
    assert legacy_server.public_dns_bindings(saved, capture) == {
        "version": 1, "zone": legacy_stack.settings.dns_domain, "wildcard": "192.0.2.10",
        "records": {}, "forward": "/etc/resolv.conf",
        "address": "172.20.0.99", "listen_port": 1054,
        "publish": {"address": "192.0.2.10", "port": 53},
    }


def test_public_dns_bindings_return_none_without_anything_to_preserve(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    root = tmp_path / "legacy-root"
    runtime = FakeLegacyRuntime(root)
    runtime.add("catalog")
    monkeypatch.setattr(legacy_server, "ingress_status", lambda state_dir, *, name: {"owned": False})
    record = legacy_record(settings, root, tmp_path / "client")
    capture = legacy_server.capture_legacy_server(settings, record, runtime)
    assert legacy_server.public_dns_bindings(settings, capture) is None


def test_public_dns_bindings_reject_ambiguous_publishes(legacy_stack):
    capture = legacy_server.capture_legacy_server(legacy_stack.settings, legacy_stack.record, legacy_stack.runtime)
    capture["containers"]["dns"]["publish"]["53/udp"] = [{"address": "192.0.2.10", "port": "54"}]
    with pytest.raises(PortmapError, match="several host bindings"):
        legacy_server.public_dns_bindings(legacy_stack.settings, capture)


# --- stop / restore / retire -------------------------------------------------------------


def test_stop_legacy_stack_stops_only_running_owned_containers(legacy_stack):
    capture = legacy_server.capture_legacy_server(legacy_stack.settings, legacy_stack.record, legacy_stack.runtime)
    # Someone stopped the legacy traefik out-of-band after the capture.
    legacy_stack.runtime.request("POST", f"/containers/{legacy_stack.runtime.by_name('portmap-traefik')['Id']}/stop", None)
    stopped = legacy_server.stop_legacy_stack(capture, legacy_stack.runtime)
    assert stopped == ["catalog", "dns"]
    assert [verb for _, verb in legacy_stack.runtime.verbs()] == ["stop", "stop", "stop"]
    assert all(container["State"]["Running"] is False for container in legacy_stack.runtime.containers.values())


def test_stop_legacy_stack_refuses_changed_labels_after_capture(legacy_stack):
    capture = legacy_server.capture_legacy_server(legacy_stack.settings, legacy_stack.record, legacy_stack.runtime)
    legacy_stack.runtime.by_name("portmap-dns")["Config"]["Labels"][SERVICE_LABEL] = "somethingelse"
    with pytest.raises(PortmapError, match="ownership labels changed"):
        legacy_server.stop_legacy_stack(capture, legacy_stack.runtime)
    assert legacy_stack.runtime.requests == []


def test_restore_legacy_stack_starts_only_previously_running_containers(legacy_stack):
    capture = legacy_server.capture_legacy_server(legacy_stack.settings, legacy_stack.record, legacy_stack.runtime)
    legacy_server.stop_legacy_stack(capture, legacy_stack.runtime)
    catalog = legacy_stack.runtime.by_name("portmap-catalog")
    capture["containers"]["catalog"]["running"] = False
    started = legacy_server.restore_legacy_stack(capture, legacy_stack.runtime)
    assert started == ["dns", "traefik"]
    assert catalog["State"]["Running"] is False
    assert legacy_stack.runtime.by_name("portmap-traefik")["State"]["Running"] is True
    assert legacy_stack.runtime.by_name("portmap-dns")["State"]["Running"] is True


def test_retire_removes_only_captured_containers_and_the_record(legacy_stack):
    capture = legacy_server.capture_legacy_server(legacy_stack.settings, legacy_stack.record, legacy_stack.runtime)
    foreign = legacy_stack.runtime.add("catalog", labels={WORKING_DIR_LABEL: "/other/root"})
    legacy_server.retire_legacy_server(legacy_stack.settings, legacy_stack.record, capture, legacy_stack.runtime)
    assert set(legacy_stack.runtime.containers) == {foreign["Id"]}
    assert not (legacy_stack.settings.state_dir / "server" / "instance.json").exists()
    assert [method for method, _ in legacy_stack.runtime.requests] == ["DELETE", "DELETE", "DELETE"]


def test_retire_is_idempotent_for_absent_resources(legacy_stack):
    capture = legacy_server.capture_legacy_server(legacy_stack.settings, legacy_stack.record, legacy_stack.runtime)
    legacy_stack.runtime.containers.clear()
    (legacy_stack.settings.state_dir / "server" / "instance.json").unlink()
    legacy_server.retire_legacy_server(legacy_stack.settings, legacy_stack.record, capture, legacy_stack.runtime)
    assert legacy_stack.runtime.requests == []


# --- DNS verification --------------------------------------------------------------------


def dns_response(ident: int, name: str, addresses: list[str]) -> bytes:
    qname = b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\x00"
    question = qname + struct.pack("!HH", 1, 1)
    answers = b"".join(qname + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(a) for a in addresses)
    return struct.pack("!6H", ident, 0x8180, 1, len(addresses), 0, 0) + question + answers


class FakeUDP:
    def __init__(self, responder):
        self.responder = responder
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, value):
        pass

    def connect(self, address):
        self.address = address

    def send(self, data):
        self.sent = data

    def recv(self, size):
        return self.responder(self.sent)


def test_resolve_a_parses_answers_and_echoes_the_request_identifier(monkeypatch):
    def responder(sent):
        ident = struct.unpack_from("!H", sent)[0]
        return dns_response(ident, "probe.example", ["192.0.2.7"])

    monkeypatch.setattr(legacy_server.socket, "socket", lambda *a, **k: FakeUDP(responder))
    assert legacy_server.resolve_a("192.0.2.10", 53, "probe.example") == ["192.0.2.7"]


def test_resolve_a_returns_none_without_a_response(monkeypatch):
    def responder(sent):
        raise socket.timeout("no answer")

    monkeypatch.setattr(legacy_server.socket, "socket", lambda *a, **k: FakeUDP(responder))
    assert legacy_server.resolve_a("192.0.2.10", 53, "probe.example", attempts=1, timeout=0.01) is None


def test_verify_public_dns_bindings_checks_the_preserved_answer(monkeypatch):
    bindings = {"zone": "laptop.portmap", "wildcard": "192.0.2.10", "records": {}, "publish": {"address": "192.0.2.10", "port": 53}}
    monkeypatch.setattr(legacy_server, "resolve_a", lambda *a, **k: ["192.0.2.10"])
    legacy_server.verify_public_dns_bindings(bindings)
    monkeypatch.setattr(legacy_server, "resolve_a", lambda *a, **k: ["203.0.113.1"])
    with pytest.raises(PortmapError, match="not the legacy 192.0.2.10"):
        legacy_server.verify_public_dns_bindings(bindings)
    monkeypatch.setattr(legacy_server, "resolve_a", lambda *a, **k: None)
    with pytest.raises(PortmapError, match="stopped answering"):
        legacy_server.verify_public_dns_bindings(bindings)


def test_verify_public_dns_bindings_without_publish_is_a_no_op(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("no query expected")

    monkeypatch.setattr(legacy_server, "resolve_a", explode)
    legacy_server.verify_public_dns_bindings({"zone": "laptop.portmap", "publish": None})


# --- migration metadata ------------------------------------------------------------------


def test_failed_migration_metadata_is_retained_and_cleared(tmp_path):
    settings = make_settings(tmp_path)
    record = legacy_record(settings, tmp_path, tmp_path / "client")
    legacy_server.save_migration(settings.state_dir, record=record, phase="dns", error="boom", details={"stopped": True})
    loaded = legacy_server.load_migration(settings.state_dir)
    assert loaded["phase"] == "dns" and loaded["error"] == "boom"
    assert loaded["record"] == record and loaded["details"] == {"stopped": True}
    legacy_server.clear_migration(settings.state_dir)
    assert legacy_server.load_migration(settings.state_dir) is None


# --- cutover orchestration through migrate_legacy_server ---------------------------------


class MigrationHost:
    def __init__(self, tmp_path):
        self.access_dir = tmp_path / "client-state"
        self.events: list[tuple] = []
        self.errors: dict[str, Exception] = {}
        self.bindings_calls: list = []
        self.profiles: dict = {}
        self.saved_local = None
        self.infra_state = {"phase": "ready", "owner": "new-owner", "runtime": {}, "public_dns": None, "public_backend_port": BACKEND_PORT}
        self.infra_report = {"running": True, "gateway": {"running": True, "public_backend_port": BACKEND_PORT}}
        self.sync_result = {"running": True, "gateway": {"running": True, "public_backend_port": BACKEND_PORT}}

    def names(self):
        return [event[0] for event in self.events]

    def events_named(self, name):
        return [event for event in self.events if event[0] == name]

    def local_calls(self):
        return [event[1] for event in self.events if event[0] == "set_local_server"]

    def stub(self, name, result=None):
        def call(*arguments, **keywords):
            self.events.append((name, arguments, keywords))
            error = self.errors.get(name)
            if error is not None:
                raise error
            return result() if callable(result) else result
        return call


@pytest.fixture
def migration(tmp_path, monkeypatch, legacy_stack):
    host = MigrationHost(tmp_path)
    monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(host.access_dir))
    monkeypatch.setattr(server, "setup_client", host.stub("setup_client"))
    monkeypatch.setattr(server, "set_local_server", host.stub("set_local_server", lambda: host.sync_result))
    monkeypatch.setattr(server, "_load_client_local", lambda directory: host.saved_local)
    monkeypatch.setattr(server, "probe_gateway", host.stub("probe_gateway", SimpleNamespace(domain="x", http_port=80, address="127.0.0.1", scheme="http", catalog={}, catalog_url="")))
    monkeypatch.setattr(server, "infrastructure_status", host.stub("infrastructure_status", lambda: host.infra_report))
    monkeypatch.setattr(server, "load_infrastructure", host.stub("load_infrastructure", lambda: host.infra_state))
    monkeypatch.setattr(server, "ensure_ingress", host.stub("ensure_ingress", {"name": "public"}))
    monkeypatch.setattr(server, "stop_ingress", host.stub("stop_ingress", {"stopped": True}))
    monkeypatch.setattr(server, "ingress_status", lambda state_dir, *, name: {"owned": True, "running": True, "bind_address": "192.0.2.10", "port": 80})
    monkeypatch.setattr(server, "ContainerRuntime", SimpleNamespace(connect=lambda settings: legacy_stack.runtime, from_record=lambda record: legacy_stack.runtime))

    def bindings_call(state, bindings):
        host.bindings_calls.append((state, bindings))
        error = host.errors.get("set_public_dns_bindings" if bindings is not None else "clear_public_dns_bindings")
        if error is not None:
            raise error
        host.infra_state["public_dns"] = bindings
        return state

    monkeypatch.setattr(infrastructure, "set_public_dns_bindings", bindings_call, raising=False)
    monkeypatch.setattr(legacy_server, "resolve_a", lambda *a, **k: ["192.0.2.10"])
    host.saved_local = {"domain": legacy_stack.settings.dns_domain, "backend_port": LEGACY_BACKEND_PORT, "http_port": 80}
    monkeypatch.setattr(server, "agent_status", lambda effective: SimpleNamespace(as_dict=lambda: {"running": True}))
    return host


def test_migrate_returns_none_without_legacy_state(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "load_legacy_record", lambda settings: None)
    assert server.migrate_legacy_server(make_settings(tmp_path)) is None


def test_migration_cuts_the_legacy_stack_over_end_to_end(migration, legacy_stack):
    settings = legacy_stack.settings
    result = server.migrate_legacy_server(settings)

    # Containers stop only after the new backend is proven, then are removed.
    assert [method for method, _ in legacy_stack.runtime.requests] == ["POST", "POST", "POST", "DELETE", "DELETE", "DELETE"]
    # The preserved DNS binding payload is exact.
    assert len(migration.bindings_calls) == 1
    state, bindings = migration.bindings_calls[0]
    assert state == migration.infra_state
    assert bindings["zone"] == settings.dns_domain
    assert bindings["publish"] == {"address": "192.0.2.10", "port": 53}
    assert bindings["address"] == "172.20.0.99"
    assert bindings["wildcard"] == "192.0.2.10"
    assert bindings["forward"] == "/etc/resolv.conf"
    assert bindings["listen_port"] == 1054
    # State transition and cleanup.
    record = json.loads((settings.state_dir / "server" / "instance.json").read_text(encoding="utf-8"))
    assert record["version"] == 2 and record["lan"] == {"bind_address": "192.0.2.10", "port": 80}
    assert not (settings.state_dir / "server" / "migration.json").exists()
    assert result["migrated"] is True
    assert result["dns_publish"] == {"address": "192.0.2.10", "port": 53}
    assert result["lan"] == {"bind_address": "192.0.2.10", "port": 80}
    assert sorted(result["retired"]) == ["catalog", "dns", "traefik"]


def test_migration_failure_after_stop_restores_the_legacy_stack(migration, legacy_stack):
    settings = legacy_stack.settings
    migration.errors["set_public_dns_bindings"] = PortmapError("dns recreate boom")
    with pytest.raises(PortmapError, match="legacy server migration failed at dns"):
        server.migrate_legacy_server(settings)

    # Every legacy container was stopped and then started again.
    assert [verb for _, verb in legacy_stack.runtime.verbs()] == ["stop", "stop", "stop", "start", "start", "start"]
    for container in legacy_stack.runtime.containers.values():
        assert container["State"]["Running"] is True
    # The clear-bindings rollback ran, the new LAN entry was withdrawn and the
    # legacy ingress restored at its original backend port.
    assert migration.bindings_calls[-1][1] is None
    ensure = migration.events_named("ensure_ingress")
    assert ensure[-1][1] == (Path(legacy_stack.record["client_state_dir"]),)
    assert ensure[-1][2]["target_port"] == LEGACY_BACKEND_PORT
    assert migration.local_calls()[-1] == (migration.access_dir, migration.saved_local)
    # The legacy record and failure metadata are retained for a safe retry.
    assert legacy_server.load_legacy_record(settings) == legacy_stack.record
    assert json.loads((settings.state_dir / "server" / "instance.json").read_text())["version"] == 1
    assert legacy_server.load_migration(settings.state_dir)["phase"] == "dns"


def test_migration_staging_failure_touches_nothing_legacy(migration, legacy_stack):
    settings = legacy_stack.settings
    migration.errors["set_local_server"] = PortmapError("sync boom")
    with pytest.raises(PortmapError, match="legacy server migration failed at stage"):
        server.migrate_legacy_server(settings)
    assert legacy_stack.runtime.requests == []
    assert migration.events_named("stop_ingress") == []
    assert migration.events_named("ensure_ingress") == []
    assert legacy_server.load_legacy_record(settings) == legacy_stack.record
    assert legacy_server.load_migration(settings.state_dir)["phase"] == "stage"


def test_migration_partial_retirement_keeps_new_chain_and_resumes(migration, monkeypatch, legacy_stack):
    settings = legacy_stack.settings
    runtime = legacy_stack.runtime
    request = runtime.request
    deleted = []

    def interrupted_delete(method, path, *args, **kwargs):
        if method == "DELETE":
            if deleted:
                raise PortmapError("runtime disconnected during retirement")
            deleted.append(path)
        return request(method, path, *args, **kwargs)

    monkeypatch.setattr(runtime, "request", interrupted_delete)
    with pytest.raises(PortmapError):
        server.migrate_legacy_server(settings)
    assert len(runtime.containers) == 2
    assert all(not container["State"]["Running"] for container in runtime.containers.values())
    assert json.loads((settings.state_dir / "server" / "instance.json").read_text())["version"] == 2
    pending = legacy_server.load_migration(settings.state_dir)
    assert pending["phase"] == "retire"
    assert migration.infra_state["public_dns"]["address"] == "172.20.0.99"
    assert migration.local_calls()[-1][1] == {"domain": settings.dns_domain, "http_port": settings.http_port}

    monkeypatch.setattr(runtime, "request", request)
    result = server.migrate_legacy_server(settings)
    assert runtime.containers == {}
    assert result["migrated"] is True
    assert legacy_server.load_migration(settings.state_dir) is None
    assert json.loads((settings.state_dir / "server" / "instance.json").read_text())["version"] == 2


def test_partial_legacy_stop_restores_already_stopped_container(migration, monkeypatch, legacy_stack):
    runtime = legacy_stack.runtime
    request = runtime.request
    stopped = []

    def partial_stop(method, path, *args, **kwargs):
        if path.endswith("/stop"):
            if stopped:
                raise PortmapError("stop failed")
            stopped.append(path)
        return request(method, path, *args, **kwargs)

    monkeypatch.setattr(runtime, "request", partial_stop)
    with pytest.raises(PortmapError):
        server.migrate_legacy_server(legacy_stack.settings)
    assert all(container["State"]["Running"] for container in runtime.containers.values())
    assert legacy_server.load_legacy_record(legacy_stack.settings) == legacy_stack.record


def test_failed_dns_release_never_restarts_legacy_dns(migration, legacy_stack):
    migration.errors["set_public_dns_bindings"] = PortmapError("DNS handoff failed")
    migration.errors["clear_public_dns_bindings"] = PortmapError("DNS binding still held")
    with pytest.raises(PortmapError):
        server.migrate_legacy_server(legacy_stack.settings)
    assert all(not container["State"]["Running"] for container in legacy_stack.runtime.containers.values())
    assert not any(verb == "start" for _, verb in legacy_stack.runtime.verbs())
    assert legacy_server.load_migration(legacy_stack.settings.state_dir)["phase"] == "dns"


def test_start_server_cuts_over_after_setup_client(migration, legacy_stack):
    settings = legacy_stack.settings
    result = server.start_server(settings, ["up", "-d"])

    # Client setup runs first and outside the server lock; the migration then
    # reuses the same transaction the standalone helper exposes.
    assert migration.names()[0] == "setup_client"
    assert [method for method, _ in legacy_stack.runtime.requests] == ["POST", "POST", "POST", "DELETE", "DELETE", "DELETE"]
    assert legacy_server.load_legacy_record(settings) is None
    assert result["version"] == 2 and result["configured"] is True
