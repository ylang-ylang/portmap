from pathlib import Path

from portmap.cli import main
from portmap.scaffold import init_portmap


def write_compose(path: Path) -> None:
    path.write_text(
        """
services:
  frontend:
    image: node:22
    expose:
      - "5173"
""".lstrip(),
        encoding="utf-8",
    )


def test_init_portmap_writes_project_scaffold(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    write_compose(compose)

    result = init_portmap(
        project_directory=tmp_path,
        compose_file=compose,
        out_dir=tmp_path / ".portmap",
    )

    assert tmp_path / ".portmap" / "endpoints.toml" in result.created
    assert tmp_path / ".portmap" / "README.md" in result.created
    assert tmp_path / ".portmap" / ".gitignore" in result.created

    endpoints = (tmp_path / ".portmap" / "endpoints.toml").read_text(encoding="utf-8")
    assert '[endpoints.frontend]' in endpoints
    assert 'kind = "range"' in endpoints

    readme = (tmp_path / ".portmap" / "README.md").read_text(encoding="utf-8")
    assert "Required Repo Shape" in readme
    assert "Docker Compose Rules For Portmap" in readme
    assert "0.0.0.0:<container_port>" in readme
    assert "Prefer expose Over Fixed Host Ports" in readme
    assert "Do not set fixed `container_name`" in readme
    assert "Do Not Define The Gateway In The Project" in readme
    assert "PORTMAP_TURN_RANGE_MIN_PORT" in readme
    assert "docker compose" in readme
    assert "network_mode: host" in readme
    assert ".portmap/docker-compose.override.generated.yml" in readme
    assert "--project-dir ." not in readme

    gitignore = (tmp_path / ".portmap" / ".gitignore").read_text(encoding="utf-8")
    assert "docker-compose.override.generated.yml" in gitignore
    assert "state.json" in gitignore


def test_init_portmap_does_not_overwrite_existing_files_without_force(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    write_compose(compose)
    out_dir = tmp_path / ".portmap"
    out_dir.mkdir()
    readme = out_dir / "README.md"
    readme.write_text("custom\n", encoding="utf-8")

    result = init_portmap(project_directory=tmp_path, compose_file=compose, out_dir=out_dir)

    assert readme in result.kept
    assert readme.read_text(encoding="utf-8") == "custom\n"


def test_init_portmap_force_overwrites_existing_files(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    write_compose(compose)
    out_dir = tmp_path / ".portmap"
    out_dir.mkdir()
    readme = out_dir / "README.md"
    readme.write_text("custom\n", encoding="utf-8")

    result = init_portmap(project_directory=tmp_path, compose_file=compose, out_dir=out_dir, force=True)

    assert readme in result.created
    assert "Portmap For This Compose Repo" in readme.read_text(encoding="utf-8")


def test_init_cli_creates_portmap_directory(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    write_compose(compose)

    assert main(["init", "--project-dir", str(tmp_path)]) == 0

    assert (tmp_path / ".portmap" / "endpoints.toml").exists()
    assert (tmp_path / ".portmap" / "README.md").exists()
    assert (tmp_path / ".portmap" / ".gitignore").exists()
