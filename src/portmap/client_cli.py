"""Standalone portmap-client CLI: local DNS/HTTP/SSH access to remote portmap
gateways without the server runtime, a local Python, or Docker."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .client import SSHSelectionRequired, connect, connections, disconnect, discover, setup_client, teardown_client
from .client_runtime import client_state_dir
from .errors import PortmapError


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PortmapError as exc:
        print(f"portmap-client: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portmap-client",
        description="Connect this machine to remote portmap gateways through local DNS and HTTP.",
    )
    parser.add_argument("--version", action="version", version=f"portmap-client {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="start local DNS/HTTP gateways and install isolated split DNS")
    setup.add_argument("--no-sudo", action="store_true")
    setup.add_argument("--http-port", type=int, help="one local HTTP port for every connected host (otherwise choose an available port)")
    setup.set_defaults(func=cmd_setup)

    teardown = subparsers.add_parser("teardown", help="disconnect clients and remove only portmap's resolver route")
    teardown.add_argument("--no-sudo", action="store_true")
    teardown.set_defaults(func=cmd_teardown)

    status = subparsers.add_parser("status", help="show local DNS and saved connections")
    status.add_argument("--check", action="store_true")
    status.set_defaults(func=cmd_connections)

    discover = subparsers.add_parser("discover", help="probe a direct address or list local SSH host candidates")
    discover.add_argument("target", nargs="?")
    discover.add_argument("--ssh-config", type=Path)
    discover.add_argument("--timeout", type=float, default=3)
    discover.set_defaults(func=cmd_discover)

    connect = subparsers.add_parser("connect", help="connect a remote gateway by IP or an SSH port forward")
    connect.add_argument("target", nargs="?", help="IP/URL, configured SSH alias, or explicit SSH target")
    connect.add_argument("--via", choices=("auto", "direct", "ssh"), default="auto")
    connect.add_argument("--ssh-target", help="explicit SSH fallback, including full .coder aliases")
    connect.add_argument("--ssh-config", type=Path)
    connect.add_argument("--remote-port", type=int, help="SSH bootstrap port for a non-default remote gateway")
    connect.add_argument("--tcp", action="store_true", help="also forward catalogued raw TCP ports (not UDP/ranges)")
    connect.add_argument("--timeout", type=float, default=5, help="HTTP probe timeout; SSH startup allows at least 30 seconds")
    connect.set_defaults(func=cmd_connect)

    connections = subparsers.add_parser("connections", help="list client-managed connections and usable endpoint URLs")
    connections.add_argument("--check", action="store_true", help="actively verify the gateway and SSH control connection")
    connections.set_defaults(func=cmd_connections)

    disconnect = subparsers.add_parser("disconnect", help="remove a client's DNS mapping and owned SSH tunnel")
    disconnect.add_argument("host", help="hostname or full hostname.portmap domain")
    disconnect.set_defaults(func=cmd_disconnect)

    worker = subparsers.add_parser("_serve-catalog", help=argparse.SUPPRESS)
    worker.add_argument("--state-dir", type=Path, required=True)
    worker.add_argument("--port", type=int, required=True)
    worker.add_argument("--owner", required=True)
    worker.set_defaults(func=cmd_serve_catalog)
    # argparse still lists SUPPRESSed subcommands in help on Python 3.12/3.13;
    # drop the pseudo-entry so only the machine-generated path can reach it.
    subparsers._choices_actions = [action for action in subparsers._choices_actions if action.dest != "_serve-catalog"]
    return parser


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
            raise PortmapError("provide an IP/URL or --via ssh TARGET; portmap-client discover lists local SSH aliases")
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
    print_json(disconnect(client_state_dir(), args.host))
    return 0


def cmd_serve_catalog(args: argparse.Namespace) -> int:
    """Hidden worker: the frozen executable re-spawns itself here to serve the
    local catalog view; source mode never invokes this entry point."""
    from .client_view import main as view_main

    view_main(["--state-dir", str(args.state_dir), "--port", str(args.port), "--owner", args.owner])
    return 0


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
