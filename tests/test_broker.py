from pathlib import Path

from portmap.broker import ensure_generated_override, should_auto_generate




def test_should_auto_generate_for_compose_runtime_commands() -> None:
    assert should_auto_generate(["up", "-d"]) is True
    assert should_auto_generate(["config"]) is True
    assert should_auto_generate(["down"]) is False
    assert should_auto_generate(["version"]) is False
    assert should_auto_generate(["-f", "custom.yml", "up"]) is False






def test_ensure_generated_override_skips_projects_without_portmap_config(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    result = ensure_generated_override(["up", "-d"], cwd=tmp_path, environ={})

    assert result.generated is False
