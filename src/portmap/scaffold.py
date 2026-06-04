from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from .errors import ConfigError


SCAFFOLD_TEMPLATE_DIR = "scaffold_templates"


@dataclass(frozen=True)
class ScaffoldResult:
    out_dir: Path
    created: tuple[Path, ...]
    kept: tuple[Path, ...]


def init_portmap(
    *,
    project_directory: Path,
    compose_file: Path,
    out_dir: Path,
    force: bool = False,
) -> ScaffoldResult:
    if not compose_file.exists():
        raise ConfigError(f"compose file does not exist: {compose_file}")

    created: list[Path] = []
    kept: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)

    write_if_missing_or_forced(
        out_dir / "endpoints.toml",
        read_scaffold_template("endpoints.toml"),
        force=force,
        created=created,
        kept=kept,
    )
    created_support, kept_support = ensure_portmap_support_files(
        project_directory=project_directory,
        compose_file=compose_file,
        out_dir=out_dir,
        force=force,
    )
    created.extend(created_support)
    kept.extend(kept_support)

    return ScaffoldResult(out_dir=out_dir, created=tuple(created), kept=tuple(kept))


def ensure_portmap_support_files(
    *,
    project_directory: Path,
    compose_file: Path,
    out_dir: Path,
    force: bool = False,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    created: list[Path] = []
    kept: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    write_if_missing_or_forced(
        out_dir / "README.md",
        portmap_readme(project_directory=project_directory, compose_file=compose_file, out_dir=out_dir),
        force=force,
        created=created,
        kept=kept,
    )
    write_if_missing_or_forced(
        out_dir / ".gitignore",
        read_scaffold_template("portmap.gitignore"),
        force=force,
        created=created,
        kept=kept,
    )
    return tuple(created), tuple(kept)


def write_if_missing_or_forced(
    path: Path,
    content: str,
    *,
    force: bool,
    created: list[Path],
    kept: list[Path],
) -> None:
    if path.exists() and not force:
        kept.append(path)
        return
    path.write_text(content, encoding="utf-8")
    created.append(path)


def portmap_readme(*, project_directory: Path, compose_file: Path, out_dir: Path) -> str:
    compose_display = relative_display(compose_file, project_directory)
    out_display = relative_display(out_dir, project_directory)
    endpoint_config = f"{out_display}/endpoints.toml"
    override = f"{out_display}/docker-compose.override.generated.yml"

    return render_scaffold_template(
        "README.md",
        {
            "{{COMPOSE_FILE}}": compose_display,
            "{{ENDPOINT_CONFIG}}": endpoint_config,
            "{{OUT_DIR}}": out_display,
            "{{OVERRIDE_FILE}}": override,
        },
    )


def read_scaffold_template(filename: str) -> str:
    return files("portmap").joinpath(SCAFFOLD_TEMPLATE_DIR, filename).read_text(encoding="utf-8")


def render_scaffold_template(filename: str, replacements: dict[str, str]) -> str:
    content = read_scaffold_template(filename)
    for placeholder, value in replacements.items():
        content = content.replace(placeholder, value)
    return content


def relative_display(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
