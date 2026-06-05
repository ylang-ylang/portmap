from pathlib import Path
from types import SimpleNamespace

from portmap.cli import main
from portmap.settings import load_portmap_settings


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
