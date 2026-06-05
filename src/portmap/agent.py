from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import socketserver
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from .agent_client import AgentUnavailable, agent_health
from .settings import PortmapSettings


AGENT_CONTAINER_SOCKET = "/run/portmap/agent.sock"


@dataclass(frozen=True)
class AgentProcessStatus:
    running: bool
    socket: Path
    pid_file: Path
    pid: int | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "socket": str(self.socket),
            "pid_file": str(self.pid_file),
            "pid": self.pid,
            "message": self.message,
        }


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "portmap-agent/0.1"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/healthz", "/readyz"}:
            self.write_json({"ok": True, "message": "agent ready"})
            return
        if parsed.path == "/worktrees":
            try:
                self.write_json(collect_agent_worktrees())
            except Exception as exc:  # pragma: no cover - exercised by runtime integration.
                self.write_json({"ok": False, "message": str(exc), "worktrees": []}, status=500)
            return
        self.write_json({"ok": False, "message": "not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/compose-up":
            payload = self.read_json()
            worktree = str(payload.get("worktree") or "")
            try:
                result = compose_up_worktree(worktree)
                self.write_json(result)
            except Exception as exc:  # pragma: no cover - exercised by runtime integration.
                self.write_json({"ok": False, "message": str(exc), "worktree": worktree}, status=400)
            return
        self.write_json({"ok": False, "message": "not found"}, status=404)

    def read_json(self) -> dict[str, Any]:
        length = min(int(self.headers.get("content-length", "0") or "0"), 65536)
        if length == 0:
            return {}
        payload = self.rfile.read(length).decode("utf-8", errors="replace")
        value = json.loads(payload)
        return value if isinstance(value, dict) else {}

    def write_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"portmap-agent: {fmt % args}", file=sys.stderr)


def collect_agent_worktrees() -> dict[str, Any]:
    from .catalog import container_to_service, discover_catalog_worktrees, docker_get, merge_catalog_history

    containers = docker_get("/containers/json?all=0")
    services = [
        service
        for container in containers
        if (service := container_to_service(container)) is not None
    ]
    generated_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    worktrees = discover_catalog_worktrees(services)
    worktrees = merge_catalog_history(worktrees, services, generated_at=generated_at)
    return {
        "ok": True,
        "generated_at": generated_at,
        "worktrees": worktrees,
    }


def compose_up_worktree(worktree: str) -> dict[str, Any]:
    from .catalog import compose_up_worktree as catalog_compose_up_worktree

    return catalog_compose_up_worktree(worktree)


def run_agent_server(socket_path: Path, *, pid_file: Path | None = None) -> None:
    resolved_socket = socket_path.expanduser()
    resolved_socket.parent.mkdir(parents=True, exist_ok=True)
    if resolved_socket.exists() or resolved_socket.is_socket():
        resolved_socket.unlink()
    if pid_file is not None:
        pid_file.expanduser().parent.mkdir(parents=True, exist_ok=True)
        pid_file.expanduser().write_text(f"{os.getpid()}\n", encoding="utf-8")

    server = ThreadingUnixHTTPServer(str(resolved_socket), AgentHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            resolved_socket.unlink()
        except FileNotFoundError:
            pass
        if pid_file is not None:
            try:
                pid_file.expanduser().unlink()
            except FileNotFoundError:
                pass


def agent_status(settings: PortmapSettings) -> AgentProcessStatus:
    pid = read_pid(settings.agent_pid_file)
    try:
        agent_health(socket_path=settings.agent_socket, timeout=0.5)
    except AgentUnavailable as exc:
        return AgentProcessStatus(
            running=False,
            socket=settings.agent_socket,
            pid_file=settings.agent_pid_file,
            pid=pid,
            message=str(exc),
        )
    except RuntimeError as exc:
        return AgentProcessStatus(
            running=False,
            socket=settings.agent_socket,
            pid_file=settings.agent_pid_file,
            pid=pid,
            message=str(exc),
        )
    return AgentProcessStatus(
        running=True,
        socket=settings.agent_socket,
        pid_file=settings.agent_pid_file,
        pid=pid,
        message="agent ready",
    )


def ensure_agent_started(settings: PortmapSettings) -> AgentProcessStatus:
    current = agent_status(settings)
    if current.running:
        return current

    settings.agent_runtime_dir.mkdir(parents=True, exist_ok=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.agent_log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(settings.gateway_env())
    env["PORTMAP_ROOT"] = str(settings.root)
    env["PORTMAP_AGENT_SOCKET"] = str(settings.agent_socket)
    with log_file.open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "portmap.agent",
                "serve",
                "--socket",
                str(settings.agent_socket),
                "--pid-file",
                str(settings.agent_pid_file),
            ],
            cwd=settings.root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.monotonic() + 5
    last_message = ""
    while time.monotonic() < deadline:
        status = agent_status(settings)
        if status.running:
            return status
        last_message = status.message
        if process.poll() is not None:
            break
        time.sleep(0.1)

    return AgentProcessStatus(
        running=False,
        socket=settings.agent_socket,
        pid_file=settings.agent_pid_file,
        pid=process.pid,
        message=last_message or f"agent failed to start; see {log_file}",
    )


def stop_agent(settings: PortmapSettings) -> AgentProcessStatus:
    pid = read_pid(settings.agent_pid_file)
    if pid is None:
        return AgentProcessStatus(
            running=False,
            socket=settings.agent_socket,
            pid_file=settings.agent_pid_file,
            message="agent pid file not found",
        )

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        cleanup_agent_files(settings)
        return AgentProcessStatus(
            running=False,
            socket=settings.agent_socket,
            pid_file=settings.agent_pid_file,
            pid=pid,
            message="agent process was not running",
        )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            cleanup_agent_files(settings)
            return AgentProcessStatus(
                running=False,
                socket=settings.agent_socket,
                pid_file=settings.agent_pid_file,
                pid=pid,
                message="agent stopped",
            )
        time.sleep(0.1)

    return AgentProcessStatus(
        running=True,
        socket=settings.agent_socket,
        pid_file=settings.agent_pid_file,
        pid=pid,
        message="agent did not stop before timeout",
    )


def cleanup_agent_files(settings: PortmapSettings) -> None:
    for path in (settings.agent_pid_file, settings.agent_socket):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m portmap.agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="serve the portmap host agent")
    serve.add_argument("--socket", type=Path, required=True)
    serve.add_argument("--pid-file", type=Path)
    serve.set_defaults(func=cmd_serve)
    return parser


def cmd_serve(args: argparse.Namespace) -> int:
    run_agent_server(args.socket, pid_file=args.pid_file)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
