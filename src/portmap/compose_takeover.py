from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

GENERATED_OVERRIDE = ".portmap/docker-compose.override.generated.yml"

PASSTHROUGH_COMMANDS = {
    "completion",
    "help",
    "version",
}


@dataclass(frozen=True)
class ComposeBrokerPlan:
    command: list[str]
    injected: bool
    compose_file: Path | None = None
    override_file: Path | None = None


def plan_docker_compose_command(args: list[str], *, cwd: Path) -> ComposeBrokerPlan:
    clean_args = strip_remainder_separator(args)
    if should_passthrough(clean_args):
        return ComposeBrokerPlan(command=["docker", "compose", *clean_args], injected=False)

    compose_file = find_compose_file(cwd)
    override_file = cwd / GENERATED_OVERRIDE
    if compose_file is None or not override_file.exists():
        return ComposeBrokerPlan(command=["docker", "compose", *clean_args], injected=False)

    return ComposeBrokerPlan(
        command=[
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "-f",
            str(override_file),
            *clean_args,
        ],
        injected=True,
        compose_file=compose_file,
        override_file=override_file,
    )


def build_docker_compose_command(args: list[str], *, cwd: Path) -> list[str]:
    return plan_docker_compose_command(args, cwd=cwd).command


def strip_remainder_separator(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def should_passthrough(args: list[str]) -> bool:
    if has_explicit_compose_file(args):
        return True
    command = first_compose_command(args)
    return command in PASSTHROUGH_COMMANDS


def has_explicit_compose_file(args: list[str]) -> bool:
    for index, arg in enumerate(args):
        if arg in {"-f", "--file"}:
            return True
        if arg.startswith("--file="):
            return True
        if arg.startswith("-f") and arg != "-f":
            return True
        if index > 0 and args[index - 1] in {"-f", "--file"}:
            return True
    return False


def first_compose_command(args: list[str]) -> str | None:
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in {"-f", "--file", "-p", "--project-name", "--profile", "--env-file"}:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def find_compose_file(cwd: Path) -> Path | None:
    for name in COMPOSE_FILENAMES:
        candidate = cwd / name
        if candidate.exists():
            return candidate
    return None


def shell_hook(*, compose_takeover: bool, env_file: Path | None = None) -> str:
    env_takeover = read_env_switch(env_file, "PORTMAP_COMPOSE_TAKEOVER") if env_file is not None else None
    if compose_takeover:
        default_value = "1"
    elif env_takeover is not None:
        default_value = env_takeover
    else:
        default_value = "${PORTMAP_COMPOSE_TAKEOVER:-0}"
    return f"""# portmap shell hook
# Source this in bash/zsh to optionally route `docker compose` through portmap.
#
# Enable:
#   source <(portmap shell-hook --compose-takeover)
#
# Disable in the current shell:
#   portmap_compose_takeover_off

export PORTMAP_COMPOSE_TAKEOVER={default_value}

portmap_compose_takeover_on() {{
  export PORTMAP_COMPOSE_TAKEOVER=1
}}

portmap_compose_takeover_off() {{
  export PORTMAP_COMPOSE_TAKEOVER=0
}}

docker() {{
  if [ "$#" -gt 0 ] && [ "$1" = "compose" ] && [ "${{PORTMAP_COMPOSE_TAKEOVER:-0}}" = "1" ]; then
    shift
    portmap docker-compose -- "$@"
    return $?
  fi
  command docker "$@"
}}
"""


def read_env_switch(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key.strip() != name:
            continue
        value = raw_value.strip().strip('"').strip("'")
        return "1" if value.lower() in {"1", "true", "yes", "on"} else "0"
    return None
