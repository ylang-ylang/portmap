from pathlib import Path

from portmap.compose_takeover import build_docker_compose_command, shell_hook


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
