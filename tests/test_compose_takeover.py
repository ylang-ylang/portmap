from pathlib import Path
from types import SimpleNamespace

from portmap.cli import main
from portmap.compose_takeover import build_docker_compose_command, plan_docker_compose_command, shell_hook


def test_compose_takeover_injects_generated_override(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    override = tmp_path / ".portmap" / "docker-compose.override.generated.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    override.parent.mkdir()
    override.write_text("services: {}\n", encoding="utf-8")

    command = build_docker_compose_command(["up", "-d"], cwd=tmp_path)

    assert command == [
        "docker",
        "compose",
        "-f",
        str(compose),
        "-f",
        str(override),
        "up",
        "-d",
    ]


def test_compose_takeover_plan_reports_injected_override(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    override = tmp_path / ".portmap" / "docker-compose.override.generated.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    override.parent.mkdir()
    override.write_text("services: {}\n", encoding="utf-8")

    plan = plan_docker_compose_command(["up", "-d"], cwd=tmp_path)

    assert plan.injected is True
    assert plan.compose_file == compose
    assert plan.override_file == override


def test_compose_takeover_passes_through_without_generated_override(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    assert build_docker_compose_command(["ps"], cwd=tmp_path) == ["docker", "compose", "ps"]


def test_compose_takeover_passes_through_explicit_file_args(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    override = tmp_path / ".portmap" / "docker-compose.override.generated.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    override.parent.mkdir()
    override.write_text("services: {}\n", encoding="utf-8")

    assert build_docker_compose_command(["-f", "custom.yml", "up", "-d"], cwd=tmp_path) == [
        "docker",
        "compose",
        "-f",
        "custom.yml",
        "up",
        "-d",
    ]


def test_compose_takeover_passes_through_version_command(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    override = tmp_path / ".portmap" / "docker-compose.override.generated.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    override.parent.mkdir()
    override.write_text("services: {}\n", encoding="utf-8")

    assert build_docker_compose_command(["version"], cwd=tmp_path) == ["docker", "compose", "version"]


def test_shell_hook_can_enable_compose_takeover() -> None:
    hook = shell_hook(compose_takeover=True)

    assert "export PORTMAP_COMPOSE_TAKEOVER=1" in hook
    assert "portmap docker-compose --" in hook
    assert 'command docker "$@"' in hook


def test_shell_hook_reads_env_file_switch(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PORTMAP_COMPOSE_TAKEOVER=true\n", encoding="utf-8")

    hook = shell_hook(compose_takeover=False, env_file=env_file)

    assert "export PORTMAP_COMPOSE_TAKEOVER=1" in hook


def test_docker_compose_cli_prints_takeover_hint(tmp_path: Path, monkeypatch, capsys) -> None:
    compose = tmp_path / "docker-compose.yml"
    override = tmp_path / ".portmap" / "docker-compose.override.generated.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    override.parent.mkdir()
    override.write_text("services: {}\n", encoding="utf-8")

    recorded = {}

    def fake_run(command, check=False):
        recorded["command"] = command
        recorded["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("portmap.cli.subprocess.run", fake_run)

    assert main(["docker-compose", "--", "up", "-d"]) == 0

    stderr = capsys.readouterr().err
    assert "portmap: docker compose takeover active" in stderr
    assert str(override) in stderr
    assert recorded["command"][0:5] == ["docker", "compose", "-f", str(compose), "-f"]
