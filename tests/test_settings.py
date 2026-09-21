from pathlib import Path
from types import SimpleNamespace

from portmap.cli import main
from portmap.settings import load_portmap_settings, resolve_runtime


def test_load_portmap_settings_reads_root_toml_and_detects_host_ip(tmp_path: Path, monkeypatch) -> None:
    detected_host = "detected-host"
    (tmp_path / "portmap.toml").write_text(
        """
[gateway]
http_bind = "0.0.0.0"
http_port = 18080
catalog_bind = "0.0.0.0"
catalog_port = 180
dns_bind = "0.0.0.0"
dns_port = 5353
dns_domain = "debug.lan"
dns_forward = "/etc/resolv.conf"
network = "test_gateway"

[ports]
tcp_start = 21000
udp_start = 22000
range_start = 50000

[state]
dir = "~/.local/state/portmap-test"
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("portmap.settings.detect_host_ip", lambda: detected_host)

    runtime_dir = tmp_path / "runtime"
    settings = load_portmap_settings(environ={"PORTMAP_ROOT": str(tmp_path), "XDG_RUNTIME_DIR": str(runtime_dir)})

    assert settings.dns_domain == "debug.lan"
    assert settings.host_ip == detected_host
    assert settings.http_port == 18080
    assert settings.catalog_port == 180
    assert settings.dns_port == 5353
    assert settings.gateway_network == "test_gateway"
    assert settings.tcp_port_start == 21000
    assert settings.udp_port_start == 22000
    assert settings.range_port_start == 50000
    assert settings.gateway_env()["PORTMAP_DNS_BIND"] == detected_host
    assert settings.gateway_env()["PORTMAP_DNS_TARGET_IP"] == detected_host
    assert settings.gateway_env()["PORTMAP_DNS_FORWARD"] == "/etc/resolv.conf"
    assert settings.agent_runtime_dir == runtime_dir / "portmap"
    assert settings.agent_socket == runtime_dir / "portmap" / "agent.sock"
    assert settings.gateway_env()["PORTMAP_AGENT_RUNTIME_HOST_DIR"] == str(runtime_dir / "portmap")
    assert settings.gateway_env()["PORTMAP_AGENT_SOCKET"] == "/run/portmap/agent.sock"


def test_gateway_cli_uses_root_toml_and_runtime_host_ip(tmp_path: Path, monkeypatch) -> None:
    detected_host = "detected-host"
    (tmp_path / "portmap.toml").write_text(
        """
[gateway]
http_port = 18080
catalog_port = 180
dns_port = 5353
dns_domain = "debug.lan"
network = "test_gateway"
""".lstrip(),
        encoding="utf-8",
    )
    recorded = {}

    def fake_run(command, *, check, env):
        recorded["command"] = command
        recorded["env"] = env
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("PORTMAP_ROOT", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    monkeypatch.setattr("portmap.settings.detect_host_ip", lambda: detected_host)
    monkeypatch.setattr("portmap.cli.subprocess.run", fake_run)

    assert main(["gateway", "config"]) == 0

    assert recorded["command"] == [
        "docker",
        "compose",
        "-f",
        str(tmp_path / "docker-compose.yml"),
        "config",
    ]
    assert recorded["env"]["PORTMAP_DNS_DOMAIN"] == "debug.lan"
    assert recorded["env"]["PORTMAP_DNS_BIND"] == detected_host
    assert recorded["env"]["PORTMAP_DNS_TARGET_IP"] == detected_host
    assert recorded["env"]["PORTMAP_DNS_FORWARD"] == "/etc/resolv.conf"
    assert recorded["env"]["PORTMAP_HTTP_PORT"] == "18080"
    assert recorded["env"]["PORTMAP_CATALOG_PORT"] == "180"
    assert recorded["env"]["PORTMAP_DNS_PORT"] == "5353"
    assert recorded["env"]["PORTMAP_GATEWAY_NETWORK"] == "test_gateway"
    assert recorded["env"]["PORTMAP_AGENT_SOCKET"] == "/run/portmap/agent.sock"
    assert recorded["env"]["PORTMAP_AGENT_RUNTIME_HOST_DIR"].endswith("/portmap")


def test_resolve_runtime_explicit_socket_wins() -> None:
    name, socket_path = resolve_runtime(
        {"PORTMAP_RUNTIME_SOCKET": "/run/podman/podman.sock"},
        {},
    )
    assert (name, socket_path) == ("podman", "/run/podman/podman.sock")


def test_resolve_runtime_explicit_backend_podman() -> None:
    name, socket_path = resolve_runtime({"PORTMAP_RUNTIME": "podman"}, {})
    assert name == "podman"
    assert socket_path == "/run/podman/podman.sock"


def test_resolve_runtime_config_backend_and_socket() -> None:
    name, socket_path = resolve_runtime(
        {},
        {"backend": "docker", "socket": "/custom/docker.sock"},
    )
    assert (name, socket_path) == ("docker", "/custom/docker.sock")


def test_resolve_runtime_docker_host_unix_socket_detects_podman() -> None:
    name, socket_path = resolve_runtime(
        {"DOCKER_HOST": "unix:///run/user/1000/podman/podman.sock"},
        {},
    )
    assert (name, socket_path) == ("podman", "/run/user/1000/podman/podman.sock")


def _listen_unix(path: Path):
    import socket as socket_module

    server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    return server


def test_resolve_runtime_auto_probes_live_docker_socket(tmp_path: Path, monkeypatch) -> None:
    docker_socket = tmp_path / "docker.sock"
    server = _listen_unix(docker_socket)
    monkeypatch.setattr("portmap.settings.DEFAULT_DOCKER_SOCKET", str(docker_socket))
    try:
        name, socket_path = resolve_runtime({}, {})
        assert (name, socket_path) == ("docker", str(docker_socket))
    finally:
        server.close()


def test_resolve_runtime_auto_ignores_stale_docker_socket(tmp_path: Path, monkeypatch) -> None:
    stale_docker = tmp_path / "docker.sock"
    stale_docker.touch()  # leftover file, nothing listening
    podman_socket = tmp_path / "podman.sock"
    server = _listen_unix(podman_socket)
    monkeypatch.setattr("portmap.settings.DEFAULT_DOCKER_SOCKET", str(stale_docker))
    monkeypatch.setattr("portmap.settings.DEFAULT_PODMAN_SOCKET", str(podman_socket))
    try:
        name, socket_path = resolve_runtime({}, {})
        assert (name, socket_path) == ("podman", str(podman_socket))
    finally:
        server.close()


def test_resolve_runtime_auto_defaults_to_docker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("portmap.settings.DEFAULT_DOCKER_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setattr("portmap.settings.DEFAULT_PODMAN_SOCKET", str(tmp_path / "missing-podman.sock"))
    name, socket_path = resolve_runtime({}, {})
    assert name == "docker"


def test_settings_expose_runtime_and_docker_host(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("portmap.settings.detect_host_ip", lambda: "detected-host")
    settings = load_portmap_settings(
        environ={
            "PORTMAP_ROOT": str(tmp_path),
            "PORTMAP_RUNTIME": "podman",
            "PORTMAP_RUNTIME_SOCKET": "/run/podman/podman.sock",
        }
    )
    assert settings.runtime == "podman"
    assert settings.runtime_socket == "/run/podman/podman.sock"
    assert settings.docker_host == "unix:///run/podman/podman.sock"
    env = settings.gateway_env()
    assert env["DOCKER_HOST"] == "unix:///run/podman/podman.sock"
    assert env["PORTMAP_RUNTIME_SOCKET"] == "/run/podman/podman.sock"
    assert env["PORTMAP_DOCKER_SOCKET"] == "/run/podman/podman.sock"


def test_resolve_runtime_invalid_backend_raises() -> None:
    import pytest

    from portmap.errors import PortmapError

    with pytest.raises(PortmapError, match="invalid runtime backend"):
        resolve_runtime({"PORTMAP_RUNTIME": "podmann"}, {})


def test_resolve_runtime_remote_docker_host_passes_through() -> None:
    name, socket_path = resolve_runtime({"DOCKER_HOST": "tcp://remote:2375"}, {})
    assert name == "docker"
    assert socket_path == "tcp://remote:2375"


def test_resolve_runtime_explicit_socket_normalizes_scheme() -> None:
    name, socket_path = resolve_runtime(
        {"PORTMAP_RUNTIME_SOCKET": "unix:///run/podman/podman.sock"},
        {},
    )
    assert (name, socket_path) == ("podman", "/run/podman/podman.sock")


def test_remote_runtime_docker_host_verbatim(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("portmap.settings.detect_host_ip", lambda: "detected-host")
    settings = load_portmap_settings(
        environ={"PORTMAP_ROOT": str(tmp_path), "DOCKER_HOST": "tcp://remote:2375"}
    )
    assert settings.docker_host == "tcp://remote:2375"
    assert settings.gateway_env()["DOCKER_HOST"] == "tcp://remote:2375"
    # A remote endpoint is not a mountable socket path.
    assert settings.gateway_env()["PORTMAP_RUNTIME_SOCKET"] == ""
