"""Remote-access commands registered by the unified portmap CLI."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .client import SSHSelectionRequired, discover, setup_client, teardown_client
from .connection_config import managed_connect, managed_connections, managed_disconnect
from .settings import PortmapSettings, load_portmap_settings
from .errors import PortmapError


RESOLVER_BACKENDS = ("auto", "resolved", "forward", "external")
INGRESS_BACKENDS = ("auto", "container", "systemd")
RESOLVER_HELP = "advanced override; new Linux auto forwards through the original DNS, preserving supported owned integrations"
INGRESS_HELP = "advanced override; new auto uses container publishing, preserving owned legacy entries"

def selected_settings(args: argparse.Namespace) -> PortmapSettings:
    """Apply explicitly supplied --resolver/--ingress to the loaded settings."""
    replacements: dict[str, str] = {}
    if getattr(args, "resolver", None):
        replacements["resolver_backend"] = args.resolver
    if getattr(args, "ingress", None):
        replacements["ingress_backend"] = args.ingress
    settings = load_portmap_settings()
    return replace(settings, **replacements) if replacements else settings


def register_commands(subparsers: argparse._SubParsersAction) -> None:
    setup = subparsers.add_parser("setup", help="start Docker/Podman infrastructure, isolated split DNS and the shared loopback HTTP entry")
    setup.add_argument("--no-sudo", action="store_true")
    setup.add_argument("--http-port", type=int, help="explicit shared loopback HTTP port; defaults to 80, including direct-only access")
    setup.add_argument("--resolver", choices=RESOLVER_BACKENDS, help=RESOLVER_HELP)
    setup.add_argument("--ingress", choices=INGRESS_BACKENDS, help=INGRESS_HELP)
    setup.set_defaults(func=cmd_setup)

    run = subparsers.add_parser(
        "run",
        help="run infrastructure, saved connections and agent controls in the foreground",
    )
    run.add_argument("--no-sudo", action="store_true", help="do not request sudo for DNS configuration or legacy integration")
    run.add_argument("--http-port", type=int, help="explicit shared loopback HTTP port; defaults to 80, including direct-only access")
    run.add_argument("--resolver", choices=RESOLVER_BACKENDS, help=RESOLVER_HELP)
    run.add_argument("--ingress", choices=INGRESS_BACKENDS, help=INGRESS_HELP)
    run.set_defaults(func=cmd_run)

    teardown = subparsers.add_parser("teardown", help="disconnect remote access and restore host DNS, retaining shared project dependencies")
    teardown.add_argument("--no-sudo", action="store_true")
    teardown.set_defaults(func=cmd_teardown)

    discover = subparsers.add_parser("discover", help="probe a direct address or list local SSH host candidates")
    discover.add_argument("target", nargs="?")
    discover.add_argument("--ssh-config", type=Path)
    discover.add_argument("--timeout", type=float, default=3)
    discover.set_defaults(func=cmd_discover)

    connect = subparsers.add_parser("connect", help="connect directly by IP or through an SSH port forward")
    connect.add_argument("target", nargs="?", help="IP/URL, configured SSH alias, or explicit SSH target")
    connect.add_argument("--via", choices=("auto", "direct", "ssh"), default="auto")
    connect.add_argument("--ssh-target", help="explicit SSH target or configured alias")
    connect.add_argument("--ssh-config", type=Path)
    connect.add_argument("--remote-port", type=int, help="SSH bootstrap port for a non-default remote gateway")
    connect.add_argument("--tcp", action="store_true", help="also forward catalogued raw TCP ports (not UDP/ranges)")
    connect.add_argument("--timeout", type=float, default=5, help="HTTP probe timeout; SSH startup allows at least 30 seconds")
    connect.add_argument("--no-sudo", action="store_true", help="do not elevate for local HTTP ingress changes")
    connect.set_defaults(func=cmd_connect)

    connections = subparsers.add_parser("connections", help="list client-managed connections and usable endpoint URLs")
    connections.add_argument("--check", action="store_true", help="actively verify the gateway and SSH control connection")
    connections.set_defaults(func=cmd_connections)

    disconnect = subparsers.add_parser("disconnect", help="remove a client's DNS mapping and owned SSH tunnel")
    disconnect.add_argument("host", help="hostname or full hostname.portmap domain")
    disconnect.add_argument("--no-sudo", action="store_true", help="do not elevate for local HTTP ingress changes")
    disconnect.set_defaults(func=cmd_disconnect)

    worker = subparsers.add_parser("_serve-catalog", help=argparse.SUPPRESS)
    worker.set_defaults(func=cmd_serve_catalog)


def cmd_setup(args: argparse.Namespace) -> int:
    settings = selected_settings(args)
    print_json(setup_client(settings.client_state_dir, settings=settings, use_sudo=not args.no_sudo, http_port=args.http_port))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .lifecycle import run_foreground

    return run_foreground(selected_settings(args), use_sudo=not args.no_sudo, http_port=args.http_port)


def cmd_teardown(args: argparse.Namespace) -> int:
    print_json(teardown_client(load_portmap_settings().client_state_dir, use_sudo=not args.no_sudo))
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    print_json(discover(load_portmap_settings().client_state_dir, args.target, ssh_config=args.ssh_config, timeout=args.timeout))
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    state_dir = load_portmap_settings().client_state_dir
    target = args.target or args.ssh_target
    if not target:
        candidates = discover(state_dir, ssh_config=args.ssh_config)["ssh_hosts"]
        if not sys.stdin.isatty():
            raise PortmapError("provide an IP/URL or --via ssh TARGET; portmap discover lists local SSH aliases")
        if candidates:
            print("Local SSH candidates (not yet authenticated):", file=sys.stderr)
            for index, host in enumerate(candidates, 1):
                print(f"  {index}. {host}", file=sys.stderr)
        try:
            target = input("IP/URL, SSH target, or candidate number: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise PortmapError("connection cancelled") from exc
        if target.isdecimal() and 1 <= int(target) <= len(candidates):
            target = candidates[int(target) - 1]
            args.via = "ssh"
        elif not target:
            raise PortmapError("connection cancelled")
    options = {
        "via": args.via,
        "ssh_target": args.ssh_target,
        "ssh_config": args.ssh_config,
        "remote_port": args.remote_port,
        "include_tcp": args.tcp,
        "timeout": args.timeout,
        "use_sudo": not args.no_sudo,
    }
    try:
        result = managed_connect(state_dir, target, **options)
    except SSHSelectionRequired:
        if not sys.stdin.isatty():
            raise
        try:
            manual_target = input("Direct access unavailable. Enter an SSH target (blank to cancel): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise PortmapError("connection cancelled") from exc
        if not manual_target:
            raise PortmapError("connection cancelled")
        options.update(via="ssh", ssh_target=manual_target)
        result = managed_connect(state_dir, target, **options)
    print_json(result)
    return 0


def cmd_connections(args: argparse.Namespace) -> int:
    print_json(managed_connections(load_portmap_settings().client_state_dir, check=args.check))
    return 0


def cmd_disconnect(args: argparse.Namespace) -> int:
    print_json(managed_disconnect(load_portmap_settings().client_state_dir, args.host, use_sudo=not args.no_sudo))
    return 0


def cmd_serve_catalog(args: argparse.Namespace) -> int:
    """Container entry point for the frozen catalog process."""
    from .catalog import main as catalog_main

    catalog_main()
    return 0


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))

