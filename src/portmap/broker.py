from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .compose_takeover import find_compose_file, first_compose_command, should_passthrough, strip_remainder_separator
from .model import GenerateRequest
from .planner import generate_plan
from .scaffold import ensure_portmap_support_files
from .settings import load_portmap_settings


ENDPOINT_CONFIG = ".portmap/endpoints.toml"

AUTO_GENERATE_COMMANDS = {
    "up",
    "create",
    "start",
    "restart",
    "run",
    "config",
    "ps",
    "logs",
    "exec",
}


@dataclass(frozen=True)
class EnsureGenerateResult:
    generated: bool
    compose_file: Path | None = None
    config_file: Path | None = None
    out_dir: Path | None = None
    compose_project: str | None = None


def ensure_generated_override(
    args: list[str],
    *,
    cwd: Path,
    environ: Mapping[str, str] | None = None,
) -> EnsureGenerateResult:
    clean_args = strip_remainder_separator(args)
    if not should_auto_generate(clean_args):
        return EnsureGenerateResult(generated=False)

    compose_file = find_compose_file(cwd)
    config_file = cwd / ENDPOINT_CONFIG
    if compose_file is None or not config_file.exists():
        return EnsureGenerateResult(generated=False)

    env = os.environ if environ is None else environ
    settings = load_portmap_settings(environ=env)
    out_dir = cwd / ".portmap"
    request = GenerateRequest(
        compose_file=compose_file,
        compose_json_file=None,
        project_directory=cwd,
        out_dir=out_dir,
        config_file=config_file,
        http_port=settings.http_port,
        tcp_port_start=settings.tcp_port_start,
        udp_port_start=settings.udp_port_start,
        range_port_start=settings.range_port_start,
        host_ip=settings.host_ip,
        domain_suffix=settings.dns_domain,
        gateway_network=settings.gateway_network,
        allocation_state_file=allocation_state_file(env),
    )
    plan = generate_plan(request)
    plan.write(out_dir)
    ensure_portmap_support_files(
        project_directory=cwd,
        compose_file=compose_file,
        out_dir=out_dir,
    )
    return EnsureGenerateResult(
        generated=True,
        compose_file=compose_file,
        config_file=config_file,
        out_dir=out_dir,
        compose_project=plan.compose_project,
    )


def should_auto_generate(args: list[str]) -> bool:
    if should_passthrough(args):
        return False
    command = first_compose_command(args)
    return command in AUTO_GENERATE_COMMANDS


def int_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def host_ip(env: Mapping[str, str]) -> str:
    return load_portmap_settings(environ=env).host_ip


def domain_suffix(env: Mapping[str, str]) -> str:
    return load_portmap_settings(environ=env).dns_domain


def allocation_state_file(env: Mapping[str, str]) -> Path:
    explicit = env.get("PORTMAP_ALLOCATION_STATE_FILE") or env.get("PORTMAP_STATE_FILE")
    if explicit:
        return Path(explicit).expanduser()
    return load_portmap_settings(environ=env).allocation_state_file
