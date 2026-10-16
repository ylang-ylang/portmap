import json
import os
from types import SimpleNamespace

import pytest

from portmap import infrastructure
from portmap.client_dns import DNS_ADDRESS
from portmap.errors import PortmapError
from portmap.infrastructure_config import IMAGES, resource_labels
from portmap.settings import load_portmap_settings


def saved_stack(tmp_path, *, resolver=None):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    owner = "a" * 32
    instance = state_dir / "infrastructure" / owner
    instance.mkdir(parents=True, mode=0o700)
    state = {
        "version": 1,
        "uid": os.getuid(),
        "phase": "ready",
        "owner": owner,
        "state_dir": str(state_dir),
        "instance_dir": str(instance),
        "runtime": {"backend": "docker", "socket_path": "/test/docker.sock", "rootless": False},
        "http_port": 80,
        "listen_port": 18080,
        "public_backend_port": 18081,
        "management_port": 18082,
        "dns_port": 1053,
        "names": {role: f"test-{role}" for role in IMAGES},
        "containers": {},
    }
    if resolver is not None:
        state["resolver"] = resolver
        state["dns_port"] = resolver["port"]
    path = state_dir / "infrastructure.json"
    path.write_text(json.dumps(state))
    path.chmod(0o600)
    return state_dir, path, state


def test_existing_stack_keeps_its_dns_binding_when_read_after_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(infrastructure.sys, "platform", "linux")
    state_dir, path, _ = saved_stack(tmp_path)
    before = path.read_bytes()

    loaded = infrastructure.load_infrastructure(state_dir)

    assert loaded["resolver"]["backend"] == "resolved"
    assert loaded["resolver"]["address"] == DNS_ADDRESS
    assert loaded["resolver"]["port"] == 1053
    assert path.read_bytes() == before


def test_forward_stack_keeps_original_upstreams_across_reload(tmp_path):
    state_dir, _, _ = saved_stack(tmp_path, resolver={
        "backend": "forward", "address": "127.0.0.1", "port": 53,
        "forwarders": ["192.0.2.53", "2001:db8::53"],
    })
    loaded = infrastructure.load_infrastructure(state_dir)
    assert loaded["resolver"]["forwarders"] == ["192.0.2.53", "2001:db8::53"]
    assert loaded["resolver"]["address"] == "127.0.0.1"
    assert loaded["dns_port"] == 53


def test_recorded_resolver_cannot_disagree_with_published_dns_port(tmp_path):
    state_dir, path, state = saved_stack(tmp_path, resolver={
        "backend": "external", "address": "127.0.0.1", "port": 1053, "forwarders": [],
    })
    state["dns_port"] = 2053
    path.write_text(json.dumps(state))
    with pytest.raises(PortmapError):
        infrastructure.load_infrastructure(state_dir)


@pytest.mark.parametrize("address", ["0.0.0.0", "192.0.2.99"])
def test_forward_resolver_state_cannot_publish_global_dns_on_a_public_address(tmp_path, address):
    state_dir, _, _ = saved_stack(tmp_path, resolver={
        "backend": "forward", "address": address, "port": 53, "forwarders": ["192.0.2.53"],
    })
    with pytest.raises(PortmapError):
        infrastructure.load_infrastructure(state_dir)


@pytest.mark.parametrize("reference", ["network-name", "network-id", "owner"])
def test_new_environment_does_not_retarget_surviving_stopped_consumers(tmp_path, monkeypatch, reference):
    state_dir, path, state = saved_stack(tmp_path)
    settings = load_portmap_settings(environ={
        "PORTMAP_ROOT": str(tmp_path), "PORTMAP_STATE_DIR": str(tmp_path / "publishing"),
        "PORTMAP_AGENT_RUNTIME_DIR": str(tmp_path / "agent"),
        "PORTMAP_DNS_DOMAIN": "proof.portmap", "PORTMAP_GATEWAY_NETWORK": "proof-network",
    })
    state.update(
        network={"name": settings.gateway_network, "id": "missing-network", "created": True},
        domain=settings.dns_domain, agent_runtime_dir=str(settings.agent_runtime_dir.resolve()),
        publishing_state_dir=str(settings.state_dir.resolve()), dns_forward=settings.dns_forward,
        host_dns_enabled=False, resolver=None, dns_port=None, environment={"boot_id": "previous"},
    )
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    networks = {}
    labels = {}
    if reference == "network-name":
        networks[settings.gateway_network] = {"NetworkID": ""}
    elif reference == "network-id":
        networks["retained-alias"] = {"NetworkID": state["network"]["id"]}
    else:
        labels = resource_labels(state)
    consumer = {"Id": "stopped-consumer", "State": "exited", "Labels": labels, "NetworkSettings": {"Networks": networks}}
    runtime = SimpleNamespace(
        **state["runtime"],
        record=lambda: state["runtime"], inspect_network=lambda identity: None,
        request=lambda method, route: [consumer],
    )
    monkeypatch.setattr(infrastructure.ContainerRuntime, "connect", lambda settings: runtime)
    monkeypatch.setattr(infrastructure, "resolver_environment", lambda: {"boot_id": "current"})

    with pytest.raises(PortmapError, match="containers still reference"):
        infrastructure.sync_infrastructure(state_dir, {}, None, settings=settings, host_dns=False)

    assert path.read_bytes() == before


def test_runtime_compatibility_refusal_blocks_setup_before_any_owned_mutation(tmp_path, monkeypatch):
    """Every standard entry (setup/run/up) converges on sync_infrastructure;
    its runtime compatibility transaction must run right after connecting and
    fail setup explicitly before any portmap-owned state exists."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    settings = load_portmap_settings(environ={
        "PORTMAP_ROOT": str(tmp_path), "PORTMAP_STATE_DIR": str(tmp_path / "publishing"),
        "PORTMAP_AGENT_RUNTIME_DIR": str(tmp_path / "agent"),
    })
    runtime = SimpleNamespace(
        record=lambda: {"backend": "docker", "socket_path": "/test/docker.sock", "rootless": False}
    )
    monkeypatch.setattr(infrastructure.ContainerRuntime, "connect", lambda settings: runtime)
    calls = []

    def refusing_prepare(recorded_runtime, recorded_state_dir):
        calls.append((recorded_runtime, recorded_state_dir))
        raise PortmapError("refusing to restart the podman API service while workloads are running")

    monkeypatch.setattr(infrastructure, "prepare_runtime", refusing_prepare)
    with pytest.raises(PortmapError, match="workloads are running"):
        infrastructure.sync_infrastructure(state_dir, {}, None, settings=settings, host_dns=False)

    assert calls == [(runtime, state_dir)]
    assert not (state_dir / "infrastructure.json").exists()
