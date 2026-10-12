"""Remote-access commands registered by the unified portmap CLI."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .client import SSHSelectionRequired, connect, connections, disconnect, discover, setup_client, teardown_client
from .client_runtime import client_state_dir
from .errors import PortmapError


def register_commands(subparsers: argparse._SubParsersAction) -> None:
    setup = subparsers.add_parser("setup", help="configure isolated split DNS and import existing access connections")
    setup.add_argument("--no-sudo", action="store_true")
    setup.add_argument("--http-port", type=int, help="explicit browser-facing HTTP port for SSH/local access; defaults to 80")
    setup.set_defaults(func=cmd_setup)

    teardown = subparsers.add_parser("teardown", help="disconnect remote access and remove only portmap's resolver route")
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
    connect.add_argument("--ssh-target", help="explicit SSH fallback, including full .coder aliases")
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
    worker.add_argument("--state-dir", type=Path, required=True)
    worker.add_argument("--port", type=int, required=True)
    worker.add_argument("--owner", required=True)
    worker.set_defaults(func=cmd_serve_catalog)


def cmd_setup(args: argparse.Namespace) -> int:
    print_json(setup_client(client_state_dir(), use_sudo=not args.no_sudo, http_port=args.http_port))
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    print_json(teardown_client(client_state_dir(), use_sudo=not args.no_sudo))
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    print_json(discover(client_state_dir(), args.target, ssh_config=args.ssh_config, timeout=args.timeout))
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    target = args.target or args.ssh_target
    if not target:
        candidates = discover(client_state_dir(), ssh_config=args.ssh_config)["ssh_hosts"]
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
        result = connect(client_state_dir(), target, **options)
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
        result = connect(client_state_dir(), target, **options)
    print_json(result)
    return 0


def cmd_connections(args: argparse.Namespace) -> int:
    print_json(connections(client_state_dir(), check=args.check))
    return 0


def cmd_disconnect(args: argparse.Namespace) -> int:
    print_json(disconnect(client_state_dir(), args.host, use_sudo=not args.no_sudo))
    return 0


def cmd_serve_catalog(args: argparse.Namespace) -> int:
    """Hidden worker: the frozen executable re-spawns itself here to serve the
    local catalog view; source mode never invokes this entry point."""
    from .client_view import main as view_main

    view_main(["--state-dir", str(args.state_dir), "--port", str(args.port), "--owner", args.owner])
    return 0


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))

