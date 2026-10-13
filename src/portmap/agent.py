from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import signal
import socket
import stat
import socketserver
import subprocess
import sys
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from . import __version__
from .agent_client import AgentUnavailable, agent_health
from .client_runtime import client_state_dir, ensure_persistent_scope, frozen, process_identity, worker_environment
from .errors import PortmapError
from .settings import PortmapSettings


AGENT_CONTAINER_SOCKET = "/run/portmap/agent.sock"
AGENT_PROTOCOL = 2


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
            self.write_json({"ok": True, "message": "agent ready", "protocol": AGENT_PROTOCOL, "version": __version__, "pid": os.getpid(), "access_state_dir": str(client_state_dir())})
            return
        if parsed.path == "/worktrees":
            try:
                self.write_json(collect_agent_worktrees())
            except Exception as exc:  # pragma: no cover - exercised by runtime integration.
                self.write_json({"ok": False, "message": str(exc), "worktrees": []}, status=500)
            return
        if parsed.path == "/access/connections":
            self.handle_access_connections_get(parsed.query)
            return
        self.write_json({"ok": False, "message": "not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/access/connections":
            self.handle_access_connections_post()
            return
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

    def handle_access_connections_get(self, query: str) -> None:
        from .connection_config import connection_targets
        check = _single_check_flag(query)
        if check is None:
            self.write_json({"error": "check must be exactly 1"}, status=400)
            return
        try:
            self.write_json(connection_targets(client_state_dir(), check=check))
        except PortmapError as exc:
            self.write_json({"error": str(exc)}, status=400)
        except Exception:  # pragma: no cover - unexpected host state.
            self.write_json({"error": "failed to read the connection list"}, status=500)

    def handle_access_connections_post(self) -> None:
        from .connection_config import mutate_connection_target
        try:
            payload = self.read_json()
        except ValueError:
            self.write_json({"error": "request body must be a JSON object"}, status=400)
            return
        try:
            # Web-originated control never uses interactive sudo.
            self.write_json(mutate_connection_target(client_state_dir(), payload, use_sudo=False))
        except PortmapError as exc:
            self.write_json({"error": str(exc)}, status=400)
        except Exception:  # pragma: no cover - unexpected host state.
            self.write_json({"error": "failed to update the connection list"}, status=500)

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


def _single_check_flag(query: str) -> bool | None:
    """True/False for an optional exactly-``check=1`` flag; None when invalid."""
    values = urllib.parse.parse_qs(query, keep_blank_values=True).get("check", [])
    if not values:
        return False
    if len(values) == 1 and values[0] == "1":
        return True
    return None


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


def _private_agent_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise PortmapError(f"agent directory is not owned and protected: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        if any(item.name not in {"agent.pid", "agent.sock", "agent.lock", "agent-owner.json"} for item in path.iterdir()):
            raise PortmapError(f"agent runtime must be a dedicated private directory: {path}")
        path.chmod(0o700)


@contextmanager
def _agent_lock(settings: PortmapSettings):
    _private_agent_directory(settings.agent_runtime_dir)
    fd = os.open(settings.agent_runtime_dir / "agent.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_agent_file(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=".agent-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def _socket_live(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(0.3)
            connection.connect(str(path))
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False


def _owned_agent_identity(settings: PortmapSettings, pid: int):
    identity = process_identity(pid)
    if identity is None:
        return None
    uid, started, command = identity
    if uid != os.getuid():
        raise PortmapError("refusing to signal an agent PID owned by another user")
    path = settings.agent_runtime_dir / "agent-owner.json"
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PortmapError("agent ownership record is not private")
            recorded = json.load(handle)
    except FileNotFoundError:
        # One-time upgrade of the old agent, whose private PID file was the
        # only record. Its complete server argv must match this exact socket.
        suffix = ["--socket", str(settings.agent_socket), "--pid-file", str(settings.agent_pid_file)]
        if not isinstance(command, list) or not Path(command[0]).is_absolute() or command[1:] not in (
            ["agent", "serve", *suffix], ["-m", "portmap.agent", "serve", *suffix],
        ):
            raise PortmapError("legacy agent PID does not match this owned socket and command")
    else:
        expected = {"version": 1, "uid": uid, "pid": pid, "start_time": started, "command": command, "socket": str(settings.agent_socket)}
        if recorded != expected:
            raise PortmapError("agent PID/start identity no longer matches its ownership record")
    return identity


def _signal_owned_agent(settings: PortmapSettings, pid: int) -> None:
    identity = _owned_agent_identity(settings, pid)
    if identity is None:
        return
    descriptor = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            descriptor = os.pidfd_open(pid)
        if process_identity(pid) != identity:
            raise PortmapError("agent process changed before shutdown; refusing to signal it")
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def run_agent_server(socket_path: Path, *, pid_file: Path | None = None) -> None:
    resolved_socket = socket_path.expanduser().resolve()
    _private_agent_directory(resolved_socket.parent)
    if resolved_socket.exists() or resolved_socket.is_symlink():
        metadata = resolved_socket.lstat()
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid() or _socket_live(resolved_socket):
            raise PortmapError("refusing to replace a live or unrelated agent socket")
        resolved_socket.unlink()
    server = ThreadingUnixHTTPServer(str(resolved_socket), AgentHandler)
    resolved_socket.chmod(0o600)
    if pid_file is not None:
        pid_file = pid_file.expanduser().resolve()
        _private_agent_directory(pid_file.parent)
        identity = process_identity(os.getpid())
        if identity is None:
            server.server_close()
            raise PortmapError("cannot record the host agent's process identity")
        uid, started, command = identity
        record = {"version": 1, "uid": uid, "pid": os.getpid(), "start_time": started, "command": command, "socket": str(resolved_socket)}
        _atomic_agent_file(resolved_socket.parent / "agent-owner.json", json.dumps(record))
        _atomic_agent_file(pid_file, f"{os.getpid()}\n")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if pid_file is None or read_pid(pid_file) == os.getpid():
            resolved_socket.unlink(missing_ok=True)
            if pid_file is not None:
                pid_file.unlink(missing_ok=True)
                (resolved_socket.parent / "agent-owner.json").unlink(missing_ok=True)


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


def ensure_agent_started(settings: PortmapSettings, *, use_sudo: bool = True, access_state_dir: Path | None = None) -> AgentProcessStatus:
    with _agent_lock(settings):
        return _start_agent(settings, use_sudo=use_sudo, access_state_dir=access_state_dir)


def _start_agent(settings: PortmapSettings, *, use_sudo: bool, access_state_dir: Path | None) -> AgentProcessStatus:
    access = (access_state_dir or settings.client_state_dir).resolve()
    current = agent_status(settings)
    if current.running:
        health = agent_health(socket_path=settings.agent_socket)
        if health.get("protocol") == AGENT_PROTOCOL and health.get("version") == __version__:
            if health.get("access_state_dir") != str(access):
                raise PortmapError("this agent socket serves a different client state directory; configure a separate agent runtime directory")
            if current.pid is None or health.get("pid") != current.pid or _owned_agent_identity(settings, current.pid) is None:
                raise PortmapError("agent health does not match the owned process identity")
            return current
    # Establish the new worker's lifetime before retiring a working version.
    ensure_persistent_scope(use_sudo=use_sudo)
    if current.running:
        stopped = _stop_agent(settings)
        if stopped.running:
            raise PortmapError(stopped.message)

    _private_agent_directory(settings.agent_runtime_dir)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.agent_log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    env = worker_environment()
    env.update(settings.gateway_env())
    env["PORTMAP_ROOT"] = str(settings.root)
    env["PORTMAP_AGENT_SOCKET"] = str(settings.agent_socket)
    env["PORTMAP_CLIENT_STATE_DIR"] = str(access)
    command = (
        [sys.executable, "agent", "serve"]
        if frozen()
        else [sys.executable, "-m", "portmap.agent", "serve"]
    )
    command += ["--socket", str(settings.agent_socket), "--pid-file", str(settings.agent_pid_file)]
    with log_file.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=settings.state_dir,
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
    with _agent_lock(settings):
        return _stop_agent(settings)


def _stop_agent(settings: PortmapSettings) -> AgentProcessStatus:
    pid = read_pid(settings.agent_pid_file)
    if pid is None:
        return AgentProcessStatus(
            running=False,
            socket=settings.agent_socket,
            pid_file=settings.agent_pid_file,
            message="agent pid file not found",
        )

    try:
        _signal_owned_agent(settings, pid)
    except ProcessLookupError:
        cleanup_agent_files(settings, pid)
        return AgentProcessStatus(
            running=False,
            socket=settings.agent_socket,
            pid_file=settings.agent_pid_file,
            pid=pid,
            message="agent process was not running",
        )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process_identity(pid) is None:
            cleanup_agent_files(settings, pid)
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


def cleanup_agent_files(settings: PortmapSettings, pid: int) -> None:
    if read_pid(settings.agent_pid_file) not in (None, pid):
        raise PortmapError("agent PID file changed during cleanup")
    if _socket_live(settings.agent_socket):
        raise PortmapError("agent socket is still live; refusing to unlink it")
    for path in (settings.agent_pid_file, settings.agent_socket, settings.agent_runtime_dir / "agent-owner.json"):
        path.unlink(missing_ok=True)


def read_pid(path: Path) -> int | None:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise PortmapError("agent PID file is not owned and protected")
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
        return pid if pid > 1 else None
    except ValueError:
        return None




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
