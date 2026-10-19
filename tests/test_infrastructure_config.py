import json
import os
import shutil
from types import SimpleNamespace

import pytest

from portmap import infrastructure, infrastructure_config
from portmap.client_tunnel import http_socket_dir
from portmap.container_runtime import ContainerRuntime


@pytest.fixture
def relay_socket_dir(tmp_path):
    directory = http_socket_dir(tmp_path / "state")
    yield directory
    shutil.rmtree(directory)


def stack_state(tmp_path, relay_socket_dir):
    roles = list(infrastructure_config.IMAGES)
    return {
        "owner": "test-owner",
        "uid": os.getuid(),
        "state_dir": str(tmp_path / "state"),
        "instance_dir": str(tmp_path / "instance"),
        "network": {"name": "test-gateway"},
        "project": "test-portmap",
        "names": {role: f"test-{role}" for role in roles},
        "addresses": {role: f"172.19.0.{index + 2}" for index, role in enumerate(roles)},
        "listen_port": 18080, "public_backend_port": 18081, "management_port": 18082,
        "dns_port": 1053,
        "resolver": {"backend": "external", "address": "127.0.0.1", "port": 1053, "forwarders": []},
        "host_dns_enabled": True, "container_upstreams": [],
        "http_port": 80, "domain": "test.portmap",
        "socket_dir": str(relay_socket_dir),
        "agent_runtime_dir": str(tmp_path / "agent"),
        "download_cache_dir": str(tmp_path / "downloads"),
    }


@pytest.mark.parametrize("role", ["catalog", "relay"])
def test_replaced_bind_directory_recreates_only_its_role(monkeypatch, tmp_path, relay_socket_dir, role):
    source = tmp_path / "site-packages"
    source.mkdir()
    monkeypatch.setattr(infrastructure_config, "frozen", lambda: False)
    monkeypatch.setattr(infrastructure_config, "pythonpath_dir", lambda: source)
    state = stack_state(tmp_path, relay_socket_dir)
    roles = list(state["names"])
    runtime = ContainerRuntime("podman", str(tmp_path / "podman.sock"), True)
    monkeypatch.setattr(ContainerRuntime, "request", lambda *args, **kwargs: {"ApiVersion": "1.44"})
    before = infrastructure_config.compose_config(state, runtime)
    assert infrastructure_config.compose_config(state, runtime) == before

    # The same pathname may now refer to a different object: a replaced Brew
    # keg or a relay socket directory recreated after runtime-directory loss.
    replaced = source if role == "catalog" else relay_socket_dir
    previous = replaced.with_name(replaced.name + "-previous")
    replaced.rename(previous)
    try:
        if role == "catalog":
            source.mkdir()
        else:
            http_socket_dir(tmp_path / "state")
        after = infrastructure_config.compose_config(state, runtime)
        assert before["services"][role]["volumes"] == after["services"][role]["volumes"]
        changed = {
            name for name in roles
            if before["services"][name] != after["services"][name]
        }
        assert changed == {role}
    finally:
        shutil.rmtree(previous)


@pytest.mark.parametrize("api_version", ["1.41", "1.44"])
@pytest.mark.parametrize("stored_mac", ["none", "opposite"])
def test_legacy_live_endpoints_survive_adoption_and_catalog_replacement(monkeypatch, tmp_path, relay_socket_dir, api_version, stored_mac):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(infrastructure_config, "frozen", lambda: False)
    monkeypatch.setattr(infrastructure_config, "pythonpath_dir", lambda: source)
    monkeypatch.setattr(ContainerRuntime, "request", lambda *args, **kwargs: {"ApiVersion": api_version})
    api = ContainerRuntime("podman", str(tmp_path / "podman.sock"), True)
    state = stack_state(tmp_path, relay_socket_dir)
    roles = list(state["names"])
    network = state["network"]["name"]
    macs = {role: f"02:18:00:00:00:{index:02x}" for index, role in enumerate(roles, 1)}
    containers = {
        state["names"][role]: {
            "Id": f"original-{role}", "State": {"Running": True},
            "NetworkSettings": {"Networks": {network: {
                "IPAddress": state["addresses"][role], "MacAddress": macs[role],
            }}},
        }
        for role in roles
    }
    state["containers"] = {role: containers[name]["Id"] for role, name in state["names"].items()}
    original_ids = dict(state["containers"])

    def recreate(path, arguments, **kwargs):
        # Model the observed Podman/Compose behavior: every targeted service
        # can be replaced even if its configuration hash did not change.
        services = json.loads(path.read_bytes())["services"]
        selected = [argument for argument in arguments if argument in roles] or roles
        for role in selected:
            row = containers[state["names"][role]]
            row["Id"] += "-replacement"
            endpoint = row["NetworkSettings"]["Networks"][network]
            if api.supports_network_mac_address:
                assert "mac_address" not in services[role], "modern engines require endpoint-scoped MAC"
            else:
                assert "mac_address" not in services[role]["networks"]["gateway"], "API 1.41 cannot encode per-network MAC"
            endpoint["MacAddress"] = services[role]["networks"]["gateway"].get("mac_address") or services[role].get("mac_address", "02:99:00:00:00:01")

    runtime = SimpleNamespace(
        rootless=True, file_user="0:0", socket_path=str(tmp_path / "podman.sock"),
        require_owned=lambda name, labels: containers[name], compose=recreate,
        supports_network_mac_address=api.supports_network_mac_address,
    )
    legacy = infrastructure_config.compose_config(state, runtime)
    for role, service in legacy["services"].items():
        endpoint = service["networks"]["gateway"]
        endpoint.pop("mac_address", None)
        service.pop("mac_address", None)
        if stored_mac == "opposite":
            if api.supports_network_mac_address:
                service["mac_address"] = macs[role]
            else:
                endpoint["mac_address"] = macs[role]
    directory = tmp_path / "instance"
    (directory / "config").mkdir(parents=True)
    (directory / "compose.yaml").write_bytes(infrastructure_config.json_bytes(legacy))

    infrastructure._reconcile_containers(state, runtime)
    assert state["containers"] == original_ids

    state["host_dns_enabled"] = False
    infrastructure._reconcile_containers(state, runtime)
    assert state["containers"]["catalog"] != original_ids["catalog"]
    assert {role: identity for role, identity in state["containers"].items() if role != "catalog"} == {
        role: identity for role, identity in original_ids.items() if role != "catalog"
    }
    assert {
        role: containers[name]["NetworkSettings"]["Networks"][network]["MacAddress"]
        for role, name in state["names"].items()
    } == macs


@pytest.mark.parametrize("seed_first", [False, True], ids=["existing-stack", "stopped-stack"])
def test_cold_resume_restores_private_relay_directory_before_engine_mount(monkeypatch, tmp_path, relay_socket_dir, seed_first):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(infrastructure_config, "frozen", lambda: False)
    monkeypatch.setattr(infrastructure_config, "pythonpath_dir", lambda: source)
    state = stack_state(tmp_path, relay_socket_dir)
    state.update(relay_ports={}, dns_forward="/etc/resolv.conf")
    containers = {}

    def resume(path, arguments, **kwargs):
        # Docker/Compose creates missing bind sources with ordinary directory
        # permissions. A later SSH forward refuses such an unowned directory.
        relay_socket_dir.mkdir(mode=0o755, exist_ok=True)
        services = json.loads(path.read_bytes())["services"]
        for role, name in state["names"].items():
            containers[name] = {
                "Id": f"resumed-{role}", "State": {"Running": True},
                "NetworkSettings": {"Networks": {state["network"]["name"]: {
                    "IPAddress": state["addresses"][role],
                    "MacAddress": services[role]["networks"]["gateway"]["mac_address"],
                }}},
            }

    runtime = SimpleNamespace(
        rootless=True, file_user="0:0", socket_path=str(tmp_path / "podman.sock"),
        supports_network_mac_address=True,
        require_owned=lambda name, labels: containers.get(name), compose=resume,
    )
    directory = tmp_path / "instance"
    (directory / "config" / "dynamic").mkdir(parents=True)
    (directory / "compose.yaml").write_bytes(
        infrastructure_config.json_bytes(infrastructure_config.compose_config(state, runtime))
    )
    shutil.rmtree(relay_socket_dir)
    if seed_first:
        infrastructure._seed(state, {}, None, runtime)

    infrastructure._reconcile_containers(state, runtime)

    assert relay_socket_dir.stat().st_uid == os.getuid()
    assert relay_socket_dir.stat().st_mode & 0o777 == 0o700
    # Exercise the real SSH forward ownership gate, including its owner record.
    assert http_socket_dir(tmp_path / "state") == relay_socket_dir


def test_failed_reconfigure_preserves_compiled_ssh_route_without_old_port_allocation(
    tmp_path, relay_socket_dir, monkeypatch,
):
    from pathlib import Path
    from portmap.errors import PortmapError

    state = stack_state(tmp_path, relay_socket_dir)
    state.update(
        phase="ready", relay_ports={"remote.portmap": 19001},
        relay_revision="old-relay", dns_marker="old-dns",
        dns_forward=["192.0.2.53"], containers={role: role for role in state["names"]},
        ingress_backend="container",
    )
    profiles = {"remote.portmap": {
        "domain": "remote.portmap", "via": "ssh", "target": "remote",
        "available": True, "remote_http_port": 80,
    }}
    snapshot = infrastructure_config.access_snapshot(state, profiles, None)
    backend = snapshot["machines"]["remote.portmap"]["backend_url"]
    state["revision"], routes = infrastructure_config.routes_config(state, snapshot)
    state["relay_ports"] = {}
    state_dir = Path(state["state_dir"])
    state_dir.mkdir(mode=0o700)
    Path(state["instance_dir"]).mkdir(mode=0o700)
    paths = infrastructure._configuration_paths(state)
    for key, content in {
        "access": json.dumps(snapshot).encode(),
        "routes": json.dumps(routes).encode(),
        "relay": b"previous relay configuration",
        "dns": b"previous DNS configuration",
    }.items():
        paths[key].parent.mkdir(parents=True, exist_ok=True)
        paths[key].write_bytes(content)

    runtime = SimpleNamespace(
        require_owned=lambda *args: {"State": {"Running": True}},
        signal_owned=lambda *args: None,
    )
    monkeypatch.setattr(infrastructure, "resolver_status", lambda *args: {"installed": False})
    monkeypatch.setattr(infrastructure, "resolver_environment", lambda: None)
    monkeypatch.setattr(infrastructure, "teardown_resolver", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        infrastructure, "_reconcile_containers",
        lambda current, runtime: current.update(containers={role: role for role in state["names"]}),
    )
    attempts = []

    def readiness(current, candidate_routes, **kwargs):
        attempts.append(current["revision"])
        if len(attempts) == 1:
            raise PortmapError("candidate not ready")

    monkeypatch.setattr(infrastructure, "_wait_ready", readiness)
    with pytest.raises(PortmapError):
        infrastructure._sync_existing(
            state, state_dir, runtime, {}, None, None,
            host_dns=False, use_sudo=False, native={}, source_dir=state_dir,
        )
    restored = json.loads(paths["access"].read_bytes())
    restored_routes = json.loads(paths["routes"].read_bytes())
    assert restored["machines"]["remote.portmap"]["backend_url"] == backend
    assert any(
        server["url"] == backend
        for service in restored_routes["http"]["services"].values()
        for server in service.get("loadBalancer", {}).get("servers", [])
    )
    assert paths["relay"].read_bytes() == b"previous relay configuration"
