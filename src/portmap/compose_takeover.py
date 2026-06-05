from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

GENERATED_OVERRIDE = ".portmap/docker-compose.override.generated.yml"
GENERATED_STATE = ".portmap/state.json"

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
    compose_project: str | None = None


def plan_docker_compose_command(args: list[str], *, cwd: Path) -> ComposeBrokerPlan:
    clean_args = strip_remainder_separator(args)
    if should_passthrough(clean_args):
        return ComposeBrokerPlan(command=["docker", "compose", *clean_args], injected=False)

    compose_file = find_compose_file(cwd)
    override_file = cwd / GENERATED_OVERRIDE
    if compose_file is None or not override_file.exists():
        return ComposeBrokerPlan(command=["docker", "compose", *clean_args], injected=False)

    compose_project = generated_compose_project(cwd)
    project_args = []
    if compose_project and not has_explicit_project_name(clean_args):
        project_args = ["-p", compose_project]

    return ComposeBrokerPlan(
        command=[
            "docker",
            "compose",
            *project_args,
            "-f",
            str(compose_file),
            "-f",
            str(override_file),
            *clean_args,
        ],
        injected=True,
        compose_file=compose_file,
        override_file=override_file,
        compose_project=compose_project if project_args else None,
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


def has_explicit_project_name(args: list[str]) -> bool:
    for index, arg in enumerate(args):
        if arg in {"-p", "--project-name"}:
            return True
        if arg.startswith("--project-name="):
            return True
        if arg.startswith("-p") and arg != "-p":
            return True
        if index > 0 and args[index - 1] in {"-p", "--project-name"}:
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


def generated_compose_project(cwd: Path) -> str | None:
    state_file = cwd / GENERATED_STATE
    if not state_file.exists():
        return None
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    value = payload.get("compose_project")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
