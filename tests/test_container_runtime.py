"""Docker Compose CLI selection and invocation on the container runtime.

The entrypoint selection must prefer a usable ``docker compose`` plugin, fall
back to an installed standalone ``docker-compose`` client without ever
writing user-level docker configuration, refuse explicitly when neither is
usable, and never retry a real compose failure on the other command.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import container_runtime
from portmap.errors import PortmapError


@pytest.fixture(autouse=True)
def fresh_entrypoint():
    container_runtime.compose_entrypoint.cache_clear()
    yield
    container_runtime.compose_entrypoint.cache_clear()


def install_fake_run(monkeypatch, responses, calls):
    def fake_run(command, **kwargs):
        command = list(command)
        calls.append((command, kwargs))
        for prefix, response in responses:
            if command[: len(prefix)] == list(prefix):
                return SimpleNamespace(returncode=response[0], stdout=response[1], stderr=response[2])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(container_runtime.subprocess, "run", fake_run)


def install_which(monkeypatch, available):
    monkeypatch.setattr(container_runtime.shutil, "which", lambda name: "/usr/bin/" + name if name in available else None)


def test_usable_plugin_is_preferred(monkeypatch):
    calls: list = []
    install_which(monkeypatch, {"docker", "docker-compose"})
    install_fake_run(monkeypatch, [(["docker", "compose", "version"], (0, "2.30.0\n", ""))], calls)

    assert container_runtime.compose_entrypoint() == ("docker", "compose")
    assert len(calls) == 1  # the standalone client is never probed


def test_missing_plugin_falls_back_to_the_standalone_client(monkeypatch):
    calls: list = []
    install_which(monkeypatch, {"docker", "docker-compose"})
    install_fake_run(
        monkeypatch,
        [
            (["docker", "compose", "version"], (1, "", "docker: 'compose' is not a docker command")),
            (["/usr/bin/docker-compose", "version"], (0, "2.30.0\n", "")),
        ],
        calls,
    )

    entrypoint = container_runtime.compose_entrypoint()
    assert entrypoint == ("/usr/bin/docker-compose",)
    # No user-level docker configuration is touched anywhere.
    assert not any("cli-plugins" in " ".join(command) or command[:1] == ["ln"] for command, _ in calls)


def test_broken_standalone_client_is_not_selected(monkeypatch):
    calls: list = []
    install_which(monkeypatch, {"docker-compose"})
    install_fake_run(monkeypatch, [(["/usr/bin/docker-compose", "version"], (127, "", "not found"))], calls)

    with pytest.raises(PortmapError, match="neither"):
        container_runtime.compose_entrypoint()


def test_missing_clients_fail_explicitly(monkeypatch):
    install_which(monkeypatch, set())
    install_fake_run(monkeypatch, [], [])
    with pytest.raises(PortmapError, match="Docker Compose CLI"):
        container_runtime.compose_entrypoint()


def test_compose_uses_the_selected_entrypoint_with_the_runtime_socket(monkeypatch):
    calls: list = []
    install_which(monkeypatch, {"docker-compose"})
    install_fake_run(
        monkeypatch,
        [
            (["/usr/bin/docker-compose", "version"], (0, "2.30.0\n", "")),
            (["/usr/bin/docker-compose", "-f"], (0, "", "")),
        ],
        calls,
    )
    runtime = container_runtime.ContainerRuntime("podman", "/run/user/1000/podman/podman.sock", True)

    runtime.compose(Path("/stack/compose.yaml"), ["up", "-d"], capture=True)

    command, kwargs = calls[-1]
    assert command == ["/usr/bin/docker-compose", "-f", "/stack/compose.yaml", "up", "-d"]
    environment = kwargs["env"]
    assert environment["DOCKER_HOST"] == "unix:///run/user/1000/podman/podman.sock"
    assert environment["PORTMAP_BROKER_BYPASS"] == "1"
    assert "DOCKER_CONTEXT" not in environment


def test_compose_business_failure_is_never_retried_on_the_other_command(monkeypatch):
    calls: list = []
    install_which(monkeypatch, {"docker", "docker-compose"})
    install_fake_run(
        monkeypatch,
        [
            (["docker", "compose", "version"], (0, "2.30.0\n", "")),
            (["docker", "compose", "-f"], (1, "", "Error response from daemon: port is already allocated")),
        ],
        calls,
    )
    runtime = container_runtime.ContainerRuntime("docker", "/var/run/docker.sock", False)

    with pytest.raises(PortmapError, match="port is already allocated"):
        runtime.compose(Path("/stack/compose.yaml"), ["up", "-d"])

    # One selection probe and exactly one compose invocation: no fallback.
    assert [command[:3] for command, _ in calls] == [
        ["docker", "compose", "version"],
        ["docker", "compose", "-f"],
    ]
