from pathlib import Path

import pytest

from portmap.errors import PortmapError
from portmap.settings import load_portmap_settings, resolve_runtime


def test_load_portmap_settings_reads_root_toml_and_detects_host_ip(tmp_path: Path, monkeypatch) -> None:
    detected_host = "detected-host"
    (tmp_path / "portmap.toml").write_text(
        """
[gateway]
http_bind = "0.0.0.0"
http_port = 18080
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

    settings = load_portmap_settings(environ={"PORTMAP_ROOT": str(tmp_path)})

    assert settings.dns_domain == "debug.lan"
    assert settings.host_ip == detected_host
    assert settings.http_port == 18080
    assert settings.dns_port == 5353
    assert settings.gateway_network == "test_gateway"
    assert settings.tcp_port_start == 21000
    assert settings.udp_port_start == 22000
    assert settings.range_port_start == 50000
    assert settings.gateway_env()["PORTMAP_DNS_BIND"] == detected_host
    assert settings.gateway_env()["PORTMAP_DNS_TARGET_IP"] == detected_host
    assert settings.gateway_env()["PORTMAP_DNS_FORWARD"] == "/etc/resolv.conf"


def test_resolve_runtime_explicit_socket_wins() -> None:
    name, socket_path = resolve_runtime(
        {"PORTMAP_RUNTIME_SOCKET": "/run/podman/podman.sock"},
        {},
    )
    assert (name, socket_path) == ("podman", "/run/podman/podman.sock")




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
    podman_socket = tmp_path / "podman.sock"
    podman_server = _listen_unix(podman_socket)
    monkeypatch.setattr("portmap.settings.DEFAULT_DOCKER_SOCKET", str(docker_socket))
    monkeypatch.setattr("portmap.settings.DEFAULT_PODMAN_SOCKET", str(podman_socket))
    try:
        name, socket_path = resolve_runtime({"XDG_RUNTIME_DIR": str(tmp_path)}, {})
        assert (name, socket_path) == ("docker", str(docker_socket))
    finally:
        server.close()
        podman_server.close()


def test_resolve_runtime_auto_ignores_stale_docker_socket(tmp_path: Path, monkeypatch) -> None:
    stale_docker = tmp_path / "docker.sock"
    stale_docker.touch()  # leftover file, nothing listening
    podman_socket = tmp_path / "podman.sock"
    server = _listen_unix(podman_socket)
    monkeypatch.setattr("portmap.settings.DEFAULT_DOCKER_SOCKET", str(stale_docker))
    monkeypatch.setattr("portmap.settings.DEFAULT_PODMAN_SOCKET", str(podman_socket))
    try:
        name, socket_path = resolve_runtime({"XDG_RUNTIME_DIR": str(tmp_path)}, {})
        assert (name, socket_path) == ("podman", str(podman_socket))
    finally:
        server.close()


def test_resolve_runtime_auto_defaults_to_docker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("portmap.settings.DEFAULT_DOCKER_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setattr("portmap.settings.DEFAULT_PODMAN_SOCKET", str(tmp_path / "missing-podman.sock"))
    name, socket_path = resolve_runtime({"XDG_RUNTIME_DIR": str(tmp_path)}, {})
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



def test_resolve_root_prefers_source_checkout(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "checkout"
    (repo / "src" / "portmap").mkdir(parents=True)
    (repo / "pyproject.toml").touch()
    monkeypatch.setattr("portmap.settings.__file__", str(repo / "src" / "portmap" / "settings.py"))

    from portmap.settings import resolve_portmap_root

    assert resolve_portmap_root(environ={}) == repo


def test_user_config_fallback(tmp_path: Path, monkeypatch) -> None:
    user_config = tmp_path / "home" / ".config" / "portmap" / "portmap.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text('[gateway]\nhttp_port = 18081\n', encoding="utf-8")
    empty_root = tmp_path / "root"
    empty_root.mkdir()
    monkeypatch.setattr("portmap.settings.USER_CONFIG_PATH", user_config)
    monkeypatch.setattr("portmap.settings.detect_host_ip", lambda: "detected-host")

    settings = load_portmap_settings(environ={"PORTMAP_ROOT": str(empty_root)})
    assert settings.http_port == 18081


def test_default_dns_domain_uses_slugified_hostname(monkeypatch) -> None:
    from portmap.settings import default_dns_domain

    monkeypatch.setattr("portmap.settings.socket.gethostname", lambda: "ylang-U22")
    assert default_dns_domain() == "ylang-u22.portmap"

    monkeypatch.setattr("portmap.settings.socket.gethostname", lambda: "coderstress")
    assert default_dns_domain() == "coderstress.portmap"


def test_default_dns_domain_falls_back_for_localhost(monkeypatch) -> None:
    from portmap.settings import default_dns_domain

    monkeypatch.setattr("portmap.settings.socket.gethostname", lambda: "localhost")
    assert default_dns_domain() == "portmap"

    monkeypatch.setattr("portmap.settings.socket.gethostname", lambda: "___")
    assert default_dns_domain() == "portmap"


def test_settings_dns_domain_defaults_to_hostname_domain(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("portmap.settings.detect_host_ip", lambda: "detected-host")
    monkeypatch.setattr("portmap.settings.socket.gethostname", lambda: "ylang-U22")
    settings = load_portmap_settings(environ={"PORTMAP_ROOT": str(tmp_path)})
    assert settings.dns_domain == "ylang-u22.portmap"
    assert settings.gateway_env()["PORTMAP_DNS_DOMAIN"] == "ylang-u22.portmap"


def test_settings_dns_domain_explicit_config_wins(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "portmap.toml").write_text('[gateway]\ndns_domain = "debug.lan"\n', encoding="utf-8")
    monkeypatch.setattr("portmap.settings.detect_host_ip", lambda: "detected-host")
    monkeypatch.setattr("portmap.settings.socket.gethostname", lambda: "ylang-U22")
    settings = load_portmap_settings(environ={"PORTMAP_ROOT": str(tmp_path)})
    assert settings.dns_domain == "debug.lan"


def test_access_backend_environment_overrides_file(tmp_path: Path) -> None:
    (tmp_path / "portmap.toml").write_text(
        '[access]\nresolver = "resolved"\ningress = "systemd"\n', encoding="utf-8"
    )
    settings = load_portmap_settings(
        root=tmp_path,
        environ={
            "PORTMAP_HOST_IP": "192.0.2.20",
            "PORTMAP_RUNTIME_SOCKET": "/test/docker.sock",
            "PORTMAP_RESOLVER_BACKEND": "forward",
            "PORTMAP_INGRESS_BACKEND": "container",
        },
    )
    assert settings.resolver_backend == "forward"
    assert settings.ingress_backend == "container"
    configured = load_portmap_settings(root=tmp_path, environ={"PORTMAP_HOST_IP": "192.0.2.20"})
    assert configured.resolver_backend == "resolved"
    assert configured.ingress_backend == "systemd"


@pytest.mark.parametrize("key", ["resolver", "ingress"])
def test_unknown_access_backend_is_rejected(tmp_path: Path, key: str) -> None:
    (tmp_path / "portmap.toml").write_text(f'[access]\n{key} = "unsupported"\n', encoding="utf-8")
    with pytest.raises(PortmapError):
        load_portmap_settings(root=tmp_path, environ={"PORTMAP_HOST_IP": "192.0.2.20"})
