from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .broker import allocation_state_file as env_allocation_state_file
from .broker import domain_suffix as env_domain_suffix
from .broker import ensure_generated_override, host_ip as env_host_ip
from .broker import int_env
from .broker_shim import (
    broker_shim_status,
    docker_config_dir,
    install_compose_plugin_shim,
    uninstall_compose_plugin_shim,
)
from .compose_takeover import plan_docker_compose_command
from .errors import PortmapError
from .model import GenerateRequest
from .planner import generate_plan
from .registry import get_instance, list_repos, read_catalog, read_registry
from .scaffold import ensure_portmap_support_files, init_portmap
from .settings import load_portmap_settings


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PortmapError as exc:
        print(f"portmap: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portmap")
    parser.add_argument(
        "--registry",
        type=Path,
        help="legacy registry JSON path for query commands",
    )
    parser.add_argument(
        "--catalog-url",
        default=os.environ.get("PORTMAP_CATALOG_URL", "http://127.0.0.1/registry.json"),
        help="gateway catalog JSON URL for query commands",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create .portmap scaffold files for a compose project")
    init.add_argument("--project-dir", type=Path, default=Path.cwd())
    init.add_argument("--compose-file", type=Path)
    init.add_argument("--out-dir", type=Path, default=Path(".portmap"))
    init.add_argument("--force", action="store_true", help="overwrite existing scaffold files")
    init.set_defaults(func=cmd_init)

    docker_compose = subparsers.add_parser(
        "docker-compose",
        help="run host docker compose with the generated portmap override when available",
    )
    docker_compose.add_argument("compose_args", nargs=argparse.REMAINDER)
    docker_compose.set_defaults(func=cmd_docker_compose)

    gateway = subparsers.add_parser(
        "gateway",
        help="run the shared gateway compose from portmap.toml settings",
    )
    gateway.add_argument(
        "compose_args",
        nargs=argparse.REMAINDER,
        help="docker compose arguments; defaults to 'up -d'",
    )
    gateway.set_defaults(func=cmd_gateway)

    broker = subparsers.add_parser("broker", help="manage non-shell docker compose broker integration")
    broker_subparsers = broker.add_subparsers(dest="broker_command", required=True)

    broker_install = broker_subparsers.add_parser("install", help="install docker compose plugin shim")
    broker_install.add_argument(
        "--method",
        choices=("docker-plugin",),
        default="docker-plugin",
        help="broker integration method",
    )
    broker_install.add_argument("--docker-config", type=Path, default=docker_config_dir())
    broker_install.add_argument("--real-compose", type=Path)
    broker_install.add_argument("--portmap-root", type=Path)
    broker_install.add_argument("--force", action="store_true", help="overwrite an existing non-portmap plugin shim")
    broker_install.set_defaults(func=cmd_broker_install)

    broker_uninstall = broker_subparsers.add_parser("uninstall", help="remove docker compose plugin shim")
    broker_uninstall.add_argument(
        "--method",
        choices=("docker-plugin",),
        default="docker-plugin",
        help="broker integration method",
    )
    broker_uninstall.add_argument("--docker-config", type=Path, default=docker_config_dir())
    broker_uninstall.set_defaults(func=cmd_broker_uninstall)

    broker_status = broker_subparsers.add_parser("status", help="print docker compose plugin shim status")
    broker_status.add_argument("--docker-config", type=Path, default=docker_config_dir())
    broker_status.set_defaults(func=cmd_broker_status)

    broker_doctor = broker_subparsers.add_parser("doctor", help="print broker diagnostic status")
    broker_doctor.add_argument("--docker-config", type=Path, default=docker_config_dir())
    broker_doctor.set_defaults(func=cmd_broker_status)

    generate = subparsers.add_parser("generate", help="generate compose override and registry files")
    generate.add_argument("--compose-file", type=Path, default=Path("docker-compose.yml"))
    generate.add_argument("--compose-json", type=Path)
    generate.add_argument("--project-dir", type=Path, default=Path.cwd())
    generate.add_argument("--config", type=Path, required=True)
    generate.add_argument("--out-dir", type=Path, default=Path(".portmap"))
    generate.add_argument("--branch")
    generate.add_argument("--repo-root", type=Path)
    generate.add_argument("--repo-id")
    generate.add_argument("--repo-name")
    generate_defaults = env_generate_defaults()
    generate.add_argument("--http-port", type=int, default=generate_defaults["http_port"])
    generate.add_argument("--tcp-port-start", type=int, default=generate_defaults["tcp_port_start"])
    generate.add_argument("--udp-port-start", type=int, default=generate_defaults["udp_port_start"])
    generate.add_argument("--range-port-start", type=int, default=generate_defaults["range_port_start"])
    generate.add_argument("--host-ip", default=generate_defaults["host_ip"])
    generate.add_argument("--domain-suffix", default=generate_defaults["domain_suffix"])
    generate.add_argument("--gateway-network", default=generate_defaults["gateway_network"])
    generate.add_argument("--allocation-state-file", type=Path, default=generate_defaults["allocation_state_file"])
    generate.add_argument("--compose-project")
    generate.set_defaults(func=cmd_generate)

    list_cmd = subparsers.add_parser("list", help="list repos in a registry")
    list_cmd.set_defaults(func=cmd_list)

    status = subparsers.add_parser("status", help="print registry status")
    status.set_defaults(func=cmd_status)

    endpoints = subparsers.add_parser("endpoints", help="print endpoints for one repo instance")
    endpoints.add_argument("repo")
    endpoints.add_argument("instance")
    endpoints.set_defaults(func=cmd_endpoints)
    return parser


def cmd_init(args: argparse.Namespace) -> int:
    project_dir = args.project_dir.resolve()
    compose_file = resolve_compose_file(project_dir, args.compose_file)
    out_dir = resolve_project_path(project_dir, args.out_dir)
    result = init_portmap(
        project_directory=project_dir,
        compose_file=compose_file,
        out_dir=out_dir,
        force=args.force,
    )
    print(
        json.dumps(
            {
                "out_dir": str(result.out_dir),
                "created": [str(path) for path in result.created],
                "kept": [str(path) for path in result.kept],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_docker_compose(args: argparse.Namespace) -> int:
    ensure_result = ensure_generated_override(args.compose_args, cwd=Path.cwd(), environ=os.environ)
    if ensure_result.generated:
        print(
            "portmap: generated compose override "
            f"out_dir={ensure_result.out_dir} compose_project={ensure_result.compose_project}",
            file=sys.stderr,
        )
    plan = plan_docker_compose_command(args.compose_args, cwd=Path.cwd())
    if plan.injected:
        project_hint = f" -p {plan.compose_project}" if plan.compose_project else ""
        print(
            "portmap: docker compose takeover active; using"
            f"{project_hint} -f {plan.compose_file} -f {plan.override_file}",
            file=sys.stderr,
        )
    env = os.environ.copy()
    env["PORTMAP_BROKER_BYPASS"] = "1"
    return subprocess.run(plan.command, check=False, env=env).returncode


def cmd_gateway(args: argparse.Namespace) -> int:
    settings = load_portmap_settings(environ=os.environ)
    compose_args = strip_remainder(args.compose_args) or ["up", "-d"]
    env = os.environ.copy()
    env.update(settings.gateway_env())
    env["PORTMAP_ROOT"] = str(settings.root)
    env["PORTMAP_BROKER_BYPASS"] = "1"
    command = [
        "docker",
        "compose",
        "-f",
        str(settings.root / "docker-compose.yml"),
        *compose_args,
    ]
    return subprocess.run(command, check=False, env=env).returncode


def cmd_broker_install(args: argparse.Namespace) -> int:
    status = install_compose_plugin_shim(
        docker_config=args.docker_config.expanduser(),
        real_compose=args.real_compose.expanduser() if args.real_compose else None,
        portmap_root=args.portmap_root.expanduser() if args.portmap_root else None,
        force=args.force,
    )
    print_json(status.as_dict())
    return 0


def cmd_broker_uninstall(args: argparse.Namespace) -> int:
    status = uninstall_compose_plugin_shim(docker_config=args.docker_config.expanduser())
    print_json(status.as_dict())
    return 0


def cmd_broker_status(args: argparse.Namespace) -> int:
    status = broker_shim_status(docker_config=args.docker_config.expanduser())
    print_json(status.as_dict())
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    compose_file = None if args.compose_json else args.compose_file
    project_dir = args.project_dir.resolve()
    out_dir = resolve_project_path(project_dir, args.out_dir)
    resolved_compose_file = resolve_project_path(project_dir, compose_file) if compose_file is not None else None
    resolved_compose_json = resolve_project_path(project_dir, args.compose_json) if args.compose_json is not None else None
    config_file = resolve_project_path(project_dir, args.config)
    repo_root = resolve_project_path(project_dir, args.repo_root) if args.repo_root is not None else None
    request = GenerateRequest(
        compose_file=resolved_compose_file,
        compose_json_file=resolved_compose_json,
        project_directory=project_dir,
        out_dir=out_dir,
        config_file=config_file,
        branch=args.branch,
        repo_root=repo_root,
        repo_id=args.repo_id,
        repo_name=args.repo_name,
        http_port=args.http_port,
        tcp_port_start=args.tcp_port_start,
        udp_port_start=args.udp_port_start,
        range_port_start=args.range_port_start,
        host_ip=args.host_ip,
        domain_suffix=args.domain_suffix,
        gateway_network=args.gateway_network,
        container_dns_server=load_portmap_settings(environ=os.environ).effective_dns_bind,
        allocation_state_file=args.allocation_state_file,
        compose_project=args.compose_project,
    )
    plan = generate_plan(request)
    plan.write(out_dir)
    if resolved_compose_file is not None:
        ensure_portmap_support_files(
            project_directory=project_dir,
            compose_file=resolved_compose_file,
            out_dir=out_dir,
        )
    print(
        json.dumps(
            {
                "repo_id": plan.repo_identity.repo_id,
                "repo": plan.repo_identity.display_name,
                "branch": plan.branch,
                "compose_project": plan.compose_project,
                "endpoints": [endpoint.name for endpoint in plan.endpoints],
                "out_dir": str(out_dir),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    registry = load_query_registry(args)
    print_json(list_repos(registry))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print_json(load_query_registry(args))
    return 0


def cmd_endpoints(args: argparse.Namespace) -> int:
    registry = load_query_registry(args)
    print_json(get_instance(registry, args.repo, args.instance).get("endpoints", {}))
    return 0


def load_query_registry(args: argparse.Namespace) -> dict[str, Any]:
    if args.registry is not None:
        return read_registry(args.registry)
    return read_catalog(args.catalog_url)


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def resolve_project_path(project_dir: Path, path: Path) -> Path:
    if path.is_absolute():
        return path
    return project_dir / path


def resolve_compose_file(project_dir: Path, compose_file: Path | None) -> Path:
    if compose_file is not None:
        return resolve_project_path(project_dir, compose_file)
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        candidate = project_dir / name
        if candidate.exists():
            return candidate
    return project_dir / "docker-compose.yml"


def env_generate_defaults() -> dict[str, Any]:
    settings = load_portmap_settings(environ=os.environ)
    return {
        "http_port": int_env(os.environ, "PORTMAP_HTTP_PORT", settings.http_port),
        "tcp_port_start": int_env(os.environ, "PORTMAP_TCP_PORT_START", settings.tcp_port_start),
        "udp_port_start": int_env(os.environ, "PORTMAP_UDP_PORT_START", settings.udp_port_start),
        "range_port_start": int_env(os.environ, "PORTMAP_RANGE_PORT_START", settings.range_port_start),
        "host_ip": env_host_ip(os.environ),
        "domain_suffix": env_domain_suffix(os.environ),
        "gateway_network": settings.gateway_network,
        "allocation_state_file": env_allocation_state_file(os.environ),
    }


def strip_remainder(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


if __name__ == "__main__":
    raise SystemExit(main())
