from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .compose_takeover import find_compose_file, first_compose_command, should_passthrough, strip_remainder_separator
from .model import GenerateRequest
from .planner import generate_plan
from .scaffold import ensure_portmap_support_files


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
    out_dir = cwd / ".portmap"
    request = GenerateRequest(
        compose_file=compose_file,
        compose_json_file=None,
        project_directory=cwd,
        out_dir=out_dir,
        config_file=config_file,
        http_port=int_env(env, "PORTMAP_HTTP_PORT", 8080),
        tcp_port_start=int_env(env, "PORTMAP_TCP_PORT_START", 18000),
        udp_port_start=int_env(env, "PORTMAP_UDP_PORT_START", 19000),
        range_port_start=int_env(env, "PORTMAP_RANGE_PORT_START", 49160),
        host_ip=host_ip(env),
        domain_suffix=domain_suffix(env),
        gateway_network=env.get("PORTMAP_GATEWAY_NETWORK", "portmap_gateway"),
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
    return (
        env.get("PORTMAP_HOST_IP")
        or env.get("PORTMAP_DNS_TARGET_IP")
        or env.get("PORTMAP_DNS_BIND")
        or "127.0.0.1"
    )


def domain_suffix(env: Mapping[str, str]) -> str:
    return env.get("PORTMAP_DOMAIN_SUFFIX") or env.get("PORTMAP_DNS_DOMAIN") or "debug.local"


def allocation_state_file(env: Mapping[str, str]) -> Path:
    explicit = env.get("PORTMAP_ALLOCATION_STATE_FILE") or env.get("PORTMAP_STATE_FILE")
    if explicit:
        return Path(explicit).expanduser()
    state_dir = Path(env.get("PORTMAP_STATE_DIR", "~/.local/state/portmap")).expanduser()
    return state_dir / "allocations.json"
