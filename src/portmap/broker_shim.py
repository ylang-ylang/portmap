from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import PortmapError


SHIM_MARKER = "PORTMAP_DOCKER_COMPOSE_PLUGIN_SHIM"

DEFAULT_COMPOSE_PLUGIN_CANDIDATES = (
    Path("/usr/local/lib/docker/cli-plugins/docker-compose"),
    Path("/usr/local/libexec/docker/cli-plugins/docker-compose"),
    Path("/usr/lib/docker/cli-plugins/docker-compose"),
    Path("/usr/libexec/docker/cli-plugins/docker-compose"),
)


@dataclass(frozen=True)
class BrokerShimStatus:
    docker_config: Path
    shim_path: Path
    installed: bool
    real_compose: Path | None
    portmap_root: Path | None

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "docker_config": str(self.docker_config),
            "shim_path": str(self.shim_path),
            "installed": self.installed,
            "real_compose": str(self.real_compose) if self.real_compose else None,
            "portmap_root": str(self.portmap_root) if self.portmap_root else None,
        }


def docker_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = env.get("DOCKER_CONFIG")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".docker"


def compose_plugin_shim_path(docker_config: Path) -> Path:
    return docker_config / "cli-plugins" / "docker-compose"


def default_portmap_root() -> Path:
    env_root = os.environ.get("PORTMAP_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def find_real_compose_plugin(
    *,
    shim_path: Path | None = None,
    candidates: tuple[Path, ...] = DEFAULT_COMPOSE_PLUGIN_CANDIDATES,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    env = os.environ if environ is None else environ
    override = env.get("PORTMAP_REAL_DOCKER_COMPOSE")
    if override:
        candidate = Path(override).expanduser()
        if candidate.exists():
            return candidate
    resolved_shim = shim_path.resolve() if shim_path and shim_path.exists() else None
    for candidate in candidates:
        if not candidate.exists():
            continue
        if resolved_shim and candidate.resolve() == resolved_shim:
            continue
        return candidate
    return None


def render_compose_plugin_shim(*, real_compose: Path, portmap_root: Path) -> str:
    return f"""#!/bin/sh
# {SHIM_MARKER}
set -eu

REAL_COMPOSE={shell_quote(str(real_compose))}
PORTMAP_ROOT={shell_quote(str(portmap_root))}

if [ "${{1:-}}" = "docker-cli-plugin-metadata" ]; then
  exec "$REAL_COMPOSE" "$@"
fi

if [ "${{1:-}}" = "compose" ]; then
  shift
fi

unset DOCKER_CLI_PLUGIN_ORIGINAL_CLI_COMMAND
unset DOCKER_CLI_PLUGIN_SOCKET

if [ "${{PORTMAP_BROKER_BYPASS:-0}}" = "1" ]; then
  exec "$REAL_COMPOSE" "$@"
fi

if [ "${{PORTMAP_COMPOSE_TAKEOVER:-1}}" != "1" ]; then
  exec "$REAL_COMPOSE" "$@"
fi

if [ -f ".portmap/endpoints.toml" ]; then
  exec env -u VIRTUAL_ENV PORTMAP_BROKER_BYPASS=1 uv run --project "$PORTMAP_ROOT" portmap docker-compose -- "$@"
fi

exec "$REAL_COMPOSE" "$@"
"""


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def is_portmap_shim(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return SHIM_MARKER in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def install_compose_plugin_shim(
    *,
    docker_config: Path,
    real_compose: Path | None = None,
    portmap_root: Path | None = None,
    force: bool = False,
) -> BrokerShimStatus:
    shim_path = compose_plugin_shim_path(docker_config)
    resolved_real = real_compose or find_real_compose_plugin(shim_path=shim_path)
    if resolved_real is None:
        raise PortmapError("unable to find real Docker Compose plugin")
    if not resolved_real.exists():
        raise PortmapError(f"real Docker Compose plugin does not exist: {resolved_real}")

    if shim_path.exists() and not is_portmap_shim(shim_path) and not force:
        raise PortmapError(f"refusing to overwrite non-portmap compose plugin: {shim_path}")

    root = (portmap_root or default_portmap_root()).resolve()
    shim_path.parent.mkdir(parents=True, exist_ok=True)
    shim_path.write_text(
        render_compose_plugin_shim(real_compose=resolved_real, portmap_root=root),
        encoding="utf-8",
    )
    mode = shim_path.stat().st_mode
    shim_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return broker_shim_status(docker_config=docker_config)


def uninstall_compose_plugin_shim(*, docker_config: Path) -> BrokerShimStatus:
    shim_path = compose_plugin_shim_path(docker_config)
    if shim_path.exists():
        if not is_portmap_shim(shim_path):
            raise PortmapError(f"refusing to remove non-portmap compose plugin: {shim_path}")
        shim_path.unlink()
    return broker_shim_status(docker_config=docker_config)


def broker_shim_status(*, docker_config: Path) -> BrokerShimStatus:
    shim_path = compose_plugin_shim_path(docker_config)
    installed = is_portmap_shim(shim_path)
    real_compose = parse_assignment(shim_path, "REAL_COMPOSE") if installed else find_real_compose_plugin(shim_path=shim_path)
    portmap_root = parse_assignment(shim_path, "PORTMAP_ROOT") if installed else None
    return BrokerShimStatus(
        docker_config=docker_config,
        shim_path=shim_path,
        installed=installed,
        real_compose=real_compose,
        portmap_root=portmap_root,
    )


def parse_assignment(path: Path, name: str) -> Path | None:
    if not path.exists():
        return None
    prefix = f"{name}="
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("'\"'\"'", "'")
        return Path(value)
    return None
