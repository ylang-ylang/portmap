from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .errors import PortmapError
from .model import GenerateRequest
from .planner import generate_plan
from .registry import get_instance, list_repos, read_catalog, read_registry


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
    generate.add_argument("--http-port", type=int, default=8080)
    generate.add_argument("--tcp-port-start", type=int, default=18000)
    generate.add_argument("--udp-port-start", type=int, default=19000)
    generate.add_argument("--host-ip", default="127.0.0.1")
    generate.add_argument("--domain-suffix", default="debug.local")
    generate.add_argument("--gateway-network", default="portmap_gateway")
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


def cmd_generate(args: argparse.Namespace) -> int:
    compose_file = None if args.compose_json else args.compose_file
    request = GenerateRequest(
        compose_file=compose_file,
        compose_json_file=args.compose_json,
        project_directory=args.project_dir,
        out_dir=args.out_dir,
        config_file=args.config,
        branch=args.branch,
        repo_root=args.repo_root,
        repo_id=args.repo_id,
        repo_name=args.repo_name,
        http_port=args.http_port,
        tcp_port_start=args.tcp_port_start,
        udp_port_start=args.udp_port_start,
        host_ip=args.host_ip,
        domain_suffix=args.domain_suffix,
        gateway_network=args.gateway_network,
    )
    plan = generate_plan(request)
    plan.write(args.out_dir)
    print(
        json.dumps(
            {
                "repo_id": plan.repo_identity.repo_id,
                "repo": plan.repo_identity.display_name,
                "branch": plan.branch,
                "compose_project": plan.compose_project,
                "endpoints": [endpoint.name for endpoint in plan.endpoints],
                "out_dir": str(args.out_dir),
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


if __name__ == "__main__":
    raise SystemExit(main())
