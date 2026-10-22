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
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from . import __version__
from .agent_client import AgentUnavailable, agent_health
from .client_runtime import client_state_dir, ensure_persistent_scope, foreground_lifetime, frozen, process_identity, worker_environment
from .errors import PortmapError
from .permissions import ensure_private_directory, ensure_private_file, tighten_private_descriptor
from .settings import PortmapSettings, pythonpath_dir


AGENT_CONTAINER_SOCKET = "/run/portmap/agent.sock"
# Protocol includes independently managed host and container DNS roles.
AGENT_PROTOCOL = 4
# Fits within the owned foreground worker's five-second graceful stop window.
MAINTENANCE_STOP_TIMEOUT = 4.0


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

    def served_access_state_dir(self) -> Path:
        # Owned workers pin the HTTP controls and maintenance to one directory.
        return getattr(self.server, "access_state_dir", None) or client_state_dir()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/healthz", "/readyz"}:
            self.write_json({"ok": True, "message": "agent ready", "protocol": AGENT_PROTOCOL, "version": __version__, "pid": os.getpid(), "access_state_dir": str(self.served_access_state_dir())})
            return
        if parsed.path == "/version":
            self.handle_update_version()
            return
        if parsed.path == "/updates/status":
            self.handle_updates_status(parsed.query)
            return
        if parsed.path.startswith("/updates/jobs/"):
            self.handle_updates_job(parsed.path[len("/updates/jobs/"):])
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
        if parsed.path == "/updates/start":
            self.handle_updates_start()
            return
        if parsed.path == "/updates/token":
            self.handle_updates_token()
            return
        if parsed.path == "/updates/credentials":
            self.handle_updates_credentials()
            return
        if parsed.path == "/updates/local/start":
            self.handle_updates_local_start()
            return
        if parsed.path == "/update-agent":
            self.handle_update_agent_dispatch()
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
            self.write_json(connection_targets(self.served_access_state_dir(), check=check))
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
            self.write_json(mutate_connection_target(self.served_access_state_dir(), payload, use_sudo=False))
        except PortmapError as exc:
            self.write_json({"error": str(exc)}, status=400)
        except Exception:  # pragma: no cover - unexpected host state.
            self.write_json({"error": "failed to update the connection list"}, status=500)

    def handle_update_version(self) -> None:
        from .update_control import version_info

        self.write_json(version_info(state_dir=self.served_access_state_dir()))

    def handle_updates_status(self, query: str) -> None:
        from .update_control import updates_status

        check = _single_check_flag(query)
        if check is None:
            self.write_json({"error": "check must be exactly 1"}, status=400)
            return
        try:
            self.write_json(updates_status(state_dir=self.served_access_state_dir(), check=check))
        except PortmapError as exc:
            self.write_json({"error": str(exc)}, status=400)
        except Exception:  # pragma: no cover - unexpected host state.
            self.write_json({"error": "failed to read the update status"}, status=500)

    def handle_updates_job(self, batch_id: str) -> None:
        from .update_control import get_batch

        if not batch_id or len(batch_id) > 128:
            self.write_json({"error": "unknown update batch"}, status=404)
            return
        try:
            batch = get_batch(batch_id, state_dir=self.served_access_state_dir())
        except PortmapError:
            self.write_json({"error": "unknown update batch"}, status=404)
            return
        if batch is None:
            self.write_json({"error": "unknown update batch"}, status=404)
            return
        self.write_json({"job": batch})

    def handle_updates_start(self) -> None:
        from .update_control import BatchConflictError, start_batch

        try:
            payload = self.read_json()
        except ValueError:
            self.write_json({"error": "request body must be a JSON object"}, status=400)
            return
        try:
            batch = start_batch(payload, state_dir=self.served_access_state_dir())
        except BatchConflictError as exc:
            # One active batch per manager; the live job travels with the
            # conflict so the UI keeps displaying it instead of duplicating.
            self.write_json({"error": str(exc), "job": exc.batch}, status=409)
            return
        except PortmapError as exc:
            self.write_json({"error": str(exc)}, status=400)
            return
        except Exception:  # pragma: no cover - unexpected host state.
            self.write_json({"error": "failed to start the update batch"}, status=500)
            return
        self.write_json({"job": batch}, status=202)

    def handle_updates_local_start(self) -> None:
        """Start the local update from the real installed agent process."""
        from .update_control import engine_error_status, start_local_job

        try:
            payload = self.read_json()
        except ValueError:
            self.write_json({"error": "request body must be a JSON object"}, status=400)
            return
        try:
            result = start_local_job(payload, state_dir=self.served_access_state_dir())
        except PortmapError as exc:
            self.write_json({"error": str(exc)}, status=engine_error_status(exc, 409))
            return
        except Exception:  # pragma: no cover - unexpected host state.
            self.write_json({"error": "failed to start the local update"}, status=500)
            return
        self.write_json(result, status=202)

    def handle_updates_token(self) -> None:
        from .update_control import reveal_local_capability

        try:
            payload = self.read_json()
        except ValueError:
            self.write_json({"error": "request body must be a JSON object"}, status=400)
            return
        try:
            self.write_json(reveal_local_capability(payload, state_dir=self.served_access_state_dir()))
        except PortmapError as exc:
            self.write_json({"error": str(exc)}, status=400)
        except Exception:  # pragma: no cover - unexpected host state.
            self.write_json({"error": "failed to reveal the update capability"}, status=500)

    def handle_updates_credentials(self) -> None:
        from .update_control import store_remote_credential

        try:
            payload = self.read_json()
        except ValueError:
            self.write_json({"error": "request body must be a JSON object"}, status=400)
            return
        try:
            self.write_json(store_remote_credential(payload, state_dir=self.served_access_state_dir()))
        except PortmapError as exc:
            self.write_json({"error": str(exc)}, status=400)
        except Exception:  # pragma: no cover - unexpected host state.
            self.write_json({"error": "failed to store the update capability"}, status=500)

    def handle_update_agent_dispatch(self) -> None:
        """Verify and run one remote ``/api/update-agent`` request.

        The catalog relays the request verbatim (method, exact path, body,
        listener scope and authentication headers); the capability never
        leaves this host, so signature verification happens here.
        """
        from .update_control import handle_remote_request

        try:
            payload = self.read_json()
        except ValueError:
            self.write_json({"error": "request body must be a JSON object"}, status=400)
            return
        method = payload.get("method")
        path = payload.get("path")
        scope = payload.get("scope")
        body = payload.get("body", "")
        signature = payload.get("signature")
        timestamp = payload.get("timestamp")
        nonce = payload.get("nonce")
        if method not in ("GET", "POST") or scope not in ("private", "public"):
            self.write_json({"error": "malformed update-agent dispatch"}, status=400)
            return
        if not isinstance(path, str) or not path.startswith("/api/update-agent/") or len(path) > 512:
            self.write_json({"error": "malformed update-agent dispatch"}, status=400)
            return
        if not isinstance(body, str) or len(body) > 65536:
            self.write_json({"error": "malformed update-agent dispatch"}, status=400)
            return
        if any(value is not None and not isinstance(value, str) for value in (signature, timestamp, nonce)):
            self.write_json({"error": "malformed update-agent dispatch"}, status=400)
            return
        try:
            status, response_body, headers = handle_remote_request(
                method=method,
                path=path,
                body=body.encode("utf-8"),
                scope=scope,
                signature=signature,
                timestamp=timestamp,
                nonce=nonce,
                state_dir=self.served_access_state_dir(),
            )
        except Exception:  # pragma: no cover - unexpected host state.
            self.write_json({"error": "update-agent dispatch failed"}, status=500)
            return
        self.write_json({"status": status, "body": response_body.decode("utf-8"), "headers": headers})

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
    from .catalog import WorktreeScan, container_to_service, discover_catalog_worktrees, docker_get, merge_catalog_history

    containers = docker_get("/containers/json?all=0")
    services = [
        service
        for container in containers
        if (service := container_to_service(container)) is not None
    ]
    generated_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    scan = WorktreeScan()
    worktrees = discover_catalog_worktrees(services, scan=scan)
    worktrees = merge_catalog_history(worktrees, services, generated_at=generated_at, scan=scan)
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
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise PortmapError(f"agent directory is not owned and protected: {path}")
    if metadata.st_mode & 0o777 != 0o700:
        for item in path.iterdir():
            record = item.lstat()
            if (item.name not in {"agent.pid", "agent.sock", "agent.lock", "agent-owner.json"}
                    or record.st_uid != os.getuid() or stat.S_ISLNK(record.st_mode)
                    or (not metadata.st_mode & 0o022
                        and stat.S_ISREG(record.st_mode) and record.st_mode & 0o022)):
                raise PortmapError(f"agent runtime must be a dedicated private directory: {path}")
        if metadata.st_mode & 0o022:
            ensure_private_directory(path, message=f"agent directory is not owned and protected: {path}")
        else:
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
            tighten_private_descriptor(path, handle.fileno(), message="agent ownership record is not private")
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


def _bind_agent_server(
    socket_path: Path, pid_file: Path | None = None, *, stop_event: threading.Event | None = None,
) -> ThreadingUnixHTTPServer:
    """Bind the owned agent socket and record process ownership; never serve."""
    from .client import run_connection_maintenance

    access_state_dir = client_state_dir()
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
        server.pid_file = pid_file
    server.access_state_dir = access_state_dir
    server.maintenance_stop = stop_event if stop_event is not None else threading.Event()
    server.maintenance_thread = threading.Thread(
        target=run_connection_maintenance,
        args=(server.access_state_dir, server.maintenance_stop),
        name="portmap-connection-maintenance",
        daemon=True,
    )
    try:
        server.maintenance_thread.start()
    except BaseException:
        _retire_agent_server(server)
        raise
    return server


def _retire_agent_server(server: ThreadingUnixHTTPServer) -> None:
    """Stop maintenance, close the server and remove only owned metadata."""
    server.maintenance_stop.set()
    if server.maintenance_thread.ident is not None:
        server.maintenance_thread.join(timeout=MAINTENANCE_STOP_TIMEOUT)
    server.server_close()
    resolved_socket = Path(str(server.server_address))
    pid_file = getattr(server, "pid_file", None)
    if pid_file is None or read_pid(pid_file) == os.getpid():
        resolved_socket.unlink(missing_ok=True)
        if pid_file is not None:
            pid_file.unlink(missing_ok=True)
            (resolved_socket.parent / "agent-owner.json").unlink(missing_ok=True)


def run_agent_server(socket_path: Path, *, pid_file: Path | None = None) -> None:
    stopped = threading.Event()
    server = None
    previous_handlers = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stopped.set()

    try:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous_handlers[number] = signal.signal(number, request_stop)
        # Install cancellation before binding can start maintenance: a stop
        # during startup must not leave an uncancelled reconnect attempt.
        server = _bind_agent_server(socket_path, pid_file, stop_event=stopped)
        # Bounded polling avoids shutdown() deadlocking on the serving thread.
        server.timeout = 0.2
        while not stopped.is_set():
            server.handle_request()
    finally:
        try:
            if server is not None:
                _retire_agent_server(server)
        finally:
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)


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


def _agent_worker_environment(settings: PortmapSettings, access: Path) -> dict[str, str]:
    """Environment shared by background and foreground agent workers.

    ``worker_environment`` passes the caller's own environment through, so a
    worker keeps the caller's SSH agent socket, SSH config and HOME.
    """
    env = worker_environment()
    env.update(settings.gateway_env())
    env["PORTMAP_ROOT"] = str(settings.root)
    env["PORTMAP_AGENT_SOCKET"] = str(settings.agent_socket)
    env["PORTMAP_CLIENT_STATE_DIR"] = str(access)
    if not frozen():
        env["PYTHONPATH"] = os.pathsep.join(
            entry for entry in (str(pythonpath_dir()), env.get("PYTHONPATH", "")) if entry
        )
    return env


def _agent_serve_command(settings: PortmapSettings, *, foreground: bool = False) -> list[str]:
    """Spawn argv for an agent worker through the module or frozen CLI."""
    command = (
        [sys.executable, "agent", "serve"]
        if frozen()
        else [sys.executable, "-m", "portmap.agent", "serve"]
    )
    command += ["--socket", str(settings.agent_socket), "--pid-file", str(settings.agent_pid_file)]
    if foreground:
        command.append("--foreground-lifetime")
    return command


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

    env = _agent_worker_environment(settings, access)
    command = _agent_serve_command(settings)
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


# Bounded escalation for stopping one owned foreground worker group.
FOREGROUND_START_TIMEOUT = 5.0
FOREGROUND_STOP_GRACE = 5.0
FOREGROUND_KILL_WAIT = 10.0


@dataclass
class ForegroundAgent:
    """One owned foreground agent worker supervised by this runner process.

    The worker serves the agent controls in its own process group inside the
    runner's session. ``stop()`` terminates that group with a bounded
    SIGTERM/SIGKILL escalation, reaps the leader and only then removes the
    metadata the worker owns, so a hung request, compose call or SSH
    descendant can neither outlive a normal shutdown nor mutate state after
    the runner's final route cleanup. If the runner itself is killed without
    notice, the worker stays in the runner's session and cgroup: whatever
    supervisor policy took the runner takes the worker with it, and an
    unattended worker is either reused as the healthy running agent by the
    next start or stopped through the same ownership rules as any agent.
    """

    settings: PortmapSettings
    process: subprocess.Popen
    log_file: Path

    def stop(self) -> None:
        if self.process.returncode is not None:
            # A previous stop already reaped the leader and cleaned up;
            # signalling again could reach a reused process group id.
            return
        with _agent_lock(self.settings):
            _stop_foreground_worker(self.settings, self.process)


def _spawn_foreground_worker(settings: PortmapSettings, access: Path) -> tuple[subprocess.Popen, Path]:
    """Spawn the owned foreground worker with its own process group."""
    _private_agent_directory(settings.agent_runtime_dir)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.agent_log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = _agent_worker_environment(settings, access)
    command = _agent_serve_command(settings, foreground=True)
    with log_file.open("ab") as log:
        # Its own process group, but the caller's session and cgroup: the
        # runner alone supervises the worker, and terminal signals or a
        # supervisor acting on the runner still reach the whole tree.
        process = subprocess.Popen(
            command,
            cwd=settings.state_dir,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            process_group=0,
            start_new_session=False,
        )
    return process, log_file


def start_foreground_agent(
    settings: PortmapSettings, *, access_state_dir: Path | None = None,
) -> ForegroundAgent | None:
    """Start the owned foreground agent worker, or reuse the running agent.

    Returns None when a healthy agent already serves this client state
    directory; that reused agent is never stopped or replaced by the caller.
    A worker whose protocol or version no longer matches is retired under
    the same lock and ownership rules as a background restart before this
    process spawns its own owned worker. The caller must ``stop()`` the
    returned worker before exiting: the runner owns the worker, its process
    group and the socket/PID metadata, and no foreground worker ever escapes
    into a background lifetime.
    """
    access = (access_state_dir or settings.client_state_dir).resolve()
    with _agent_lock(settings):
        current = agent_status(settings)
        if current.running:
            try:
                health = agent_health(socket_path=settings.agent_socket)
            except RuntimeError:
                # It died between the status probe and here; spawn fresh.
                health = {}
            if health.get("protocol") == AGENT_PROTOCOL and health.get("version") == __version__:
                if health.get("access_state_dir") != str(access):
                    raise PortmapError(
                        "this agent socket serves a different client state directory; "
                        "configure a separate agent runtime directory"
                    )
                if current.pid is None or health.get("pid") != current.pid or _owned_agent_identity(settings, current.pid) is None:
                    raise PortmapError("agent health does not match the owned process identity")
                return None
            stopped = _stop_agent(settings)
            if stopped.running:
                raise PortmapError(stopped.message)
        process, log_file = _spawn_foreground_worker(settings, access)
        try:
            _await_foreground_worker(settings, process, access)
        except BaseException:
            # Startup failed and no readiness was claimed: the owned child,
            # its whole group and its files must not partially survive.
            try:
                _stop_foreground_worker(settings, process)
            except Exception:  # pragma: no cover - never mask the startup failure
                pass
            raise
        return ForegroundAgent(settings=settings, process=process, log_file=log_file)


def _await_foreground_worker(settings: PortmapSettings, process: subprocess.Popen, access: Path) -> None:
    """Claim readiness only for the healthy owned worker process itself."""
    log_file = settings.agent_log_file
    last_message = "agent worker did not become healthy"
    deadline = time.monotonic() + FOREGROUND_START_TIMEOUT
    while True:
        try:
            health = agent_health(socket_path=settings.agent_socket, timeout=0.5)
        except RuntimeError as exc:
            health = {}
            last_message = str(exc)
        if health.get("ok") and (
            health.get("protocol") == AGENT_PROTOCOL
            and health.get("version") == __version__
            and health.get("access_state_dir") == str(access)
            and health.get("pid") == process.pid
            and read_pid(settings.agent_pid_file) == process.pid
            and _owned_agent_identity(settings, process.pid) is not None
        ):
            return
        if health.get("ok"):
            last_message = "agent worker is not the healthy owned worker of this runner"
        # Never poll()/wait() here: an unreaped leader keeps its process
        # group owned by this runner until the stop sequence signals it.
        if process_identity(process.pid) is None:
            raise PortmapError(f"foreground agent worker exited during startup; see {log_file}")
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    raise PortmapError(f"{last_message}; see {log_file}")


def _signal_owned_group(pgid: int, number: int) -> None:
    """Signal exactly one owned worker group; a gone group is already stopped."""
    try:
        os.killpg(pgid, number)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise PortmapError(f"cannot signal the owned agent worker group: {exc}") from exc


def _live_owned_group_members(pgid: int) -> int | None:
    """Live (non-zombie) member count of one group; None when unknowable."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    live = 0
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[1].split()
        except OSError:
            continue
        if len(fields) > 2 and fields[2] == b"%d" % pgid and fields[0] != b"Z":
            live += 1
    return live


def _stop_foreground_worker(settings: PortmapSettings, process: subprocess.Popen) -> None:
    """Terminate the owned worker group with bounded grace, then clean up.

    The leader stays unreaped until after the final group SIGKILL: an
    unreaped leader (alive or zombie) keeps the group id impossible to
    reuse, so every group signal provably reaches only this runner's own
    workers even while hung descendants still occupy the group.
    """
    pgid = process.pid  # process_group=0 made the worker its own group leader
    _signal_owned_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + FOREGROUND_STOP_GRACE
    while time.monotonic() < deadline:
        members = _live_owned_group_members(pgid)
        if members == 0:
            break
        if members is None and process_identity(pgid) is None:
            # Without /proc the leader is the best available wind-down proxy.
            break
        time.sleep(0.05)
    # Anything still in the owned group - a hung request, a compose call, an
    # SSH descendant - dies here, before the leader is reaped.
    _signal_owned_group(pgid, signal.SIGKILL)
    try:
        process.wait(timeout=FOREGROUND_KILL_WAIT)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - SIGKILL is final on POSIX
        raise PortmapError("foreground agent worker did not die after SIGKILL") from exc
    cleanup_agent_files(settings, pgid)


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
        ensure_private_file(path, message="agent PID file is not owned and protected", strip=0o022)
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
    serve.add_argument("--foreground-lifetime", action="store_true", help=argparse.SUPPRESS)
    serve.set_defaults(func=cmd_serve)
    return parser


def cmd_serve(args: argparse.Namespace) -> int:
    if args.foreground_lifetime:
        # Supervised foreground-runner worker: serving stays inside one
        # explicit foreground lifetime and never adopts a manager scope.
        with foreground_lifetime():
            run_agent_server(args.socket, pid_file=args.pid_file)
    else:
        run_agent_server(args.socket, pid_file=args.pid_file)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
