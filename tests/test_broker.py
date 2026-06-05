from pathlib import Path

from portmap.broker import ensure_generated_override, should_auto_generate


class FakePlan:
    compose_project = "sample_feat"

    def write(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "docker-compose.override.generated.yml").write_text("services: {}\n", encoding="utf-8")
        (out_dir / "state.json").write_text('{"compose_project": "sample_feat"}\n', encoding="utf-8")


def test_should_auto_generate_for_compose_runtime_commands() -> None:
    assert should_auto_generate(["up", "-d"]) is True
    assert should_auto_generate(["config"]) is True
    assert should_auto_generate(["down"]) is False
    assert should_auto_generate(["version"]) is False
    assert should_auto_generate(["-f", "custom.yml", "up"]) is False


def test_ensure_generated_override_writes_project_artifacts(tmp_path: Path, monkeypatch) -> None:
    detected_host = "detected-host"
    compose = tmp_path / "docker-compose.yml"
    config = tmp_path / ".portmap" / "endpoints.toml"
    compose.write_text("services: {}\n", encoding="utf-8")
    config.parent.mkdir()
    config.write_text(
        """
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 5173
""".lstrip(),
        encoding="utf-8",
    )
    recorded = {}

    def fake_generate_plan(request):
        recorded["request"] = request
        return FakePlan()

    monkeypatch.setattr("portmap.broker.generate_plan", fake_generate_plan)

    result = ensure_generated_override(
        ["up", "-d"],
        cwd=tmp_path,
        environ={
            "PORTMAP_HTTP_PORT": "28080",
            "PORTMAP_DNS_TARGET_IP": detected_host,
            "PORTMAP_DNS_DOMAIN": "debug.lan",
            "PORTMAP_GATEWAY_NETWORK": "portmap_gateway",
            "PORTMAP_ALLOCATION_STATE_FILE": str(tmp_path / "allocations.json"),
        },
    )

    assert result.generated is True
    assert result.compose_file == compose
    assert result.config_file == config
    assert result.compose_project == "sample_feat"
    assert (tmp_path / ".portmap" / "docker-compose.override.generated.yml").exists()
    assert (tmp_path / ".portmap" / "README.md").exists()
    assert recorded["request"].http_port == 28080
    assert recorded["request"].host_ip == detected_host
    assert recorded["request"].domain_suffix == "debug.lan"
    assert recorded["request"].container_dns_server == recorded["request"].host_ip
    assert recorded["request"].allocation_state_file == tmp_path / "allocations.json"


def test_ensure_generated_override_reads_portmap_root_config(tmp_path: Path, monkeypatch) -> None:
    detected_host = "detected-host"
    portmap_root = tmp_path / "portmap-root"
    project = tmp_path / "project"
    portmap_root.mkdir()
    project.mkdir()
    (portmap_root / "portmap.toml").write_text(
        """
[gateway]
http_port = 28082
dns_domain = "debug.lan"
network = "custom_gateway"

[ports]
tcp_start = 21000
udp_start = 22000
range_start = 50000

[state]
dir = "state-dir"
""".lstrip(),
        encoding="utf-8",
    )
    compose = project / "docker-compose.yml"
    config = project / ".portmap" / "endpoints.toml"
    compose.write_text("services: {}\n", encoding="utf-8")
    config.parent.mkdir()
    config.write_text(
        """
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 5173
""".lstrip(),
        encoding="utf-8",
    )
    recorded = {}

    def fake_generate_plan(request):
        recorded["request"] = request
        return FakePlan()

    monkeypatch.setattr("portmap.broker.generate_plan", fake_generate_plan)
    monkeypatch.setattr("portmap.settings.detect_host_ip", lambda: detected_host)

    result = ensure_generated_override(
        ["ps"],
        cwd=project,
        environ={"PORTMAP_ROOT": str(portmap_root)},
    )

    assert result.generated is True
    assert recorded["request"].http_port == 28082
    assert recorded["request"].domain_suffix == "debug.lan"
    assert recorded["request"].host_ip == detected_host
    assert recorded["request"].gateway_network == "custom_gateway"
    assert recorded["request"].container_dns_server == recorded["request"].host_ip
    assert recorded["request"].tcp_port_start == 21000
    assert recorded["request"].udp_port_start == 22000
    assert recorded["request"].range_port_start == 50000
    assert recorded["request"].allocation_state_file == portmap_root / "state-dir" / "allocations.json"


def test_ensure_generated_override_skips_projects_without_portmap_config(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    result = ensure_generated_override(["up", "-d"], cwd=tmp_path, environ={})

    assert result.generated is False
