"""Explicit foreground lifetime for the portmap client access stack.

``portmap run`` initializes the configured container infrastructure, restores
saved connections and serves the agent controls through one owned foreground
worker process, then waits for SIGINT/SIGTERM/SIGHUP. Nothing escapes the
caller's session: stopping the runner reaps only the SSH masters this process
spawned, terminates its owned agent worker and the worker's whole process
group, and removes only metadata it owns. Saved connections, business
containers, an already running background agent and the shared
resolver/container infrastructure are preserved for the next start, manual or
project-triggered.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Iterator

from .agent import ForegroundAgent, start_foreground_agent
from .client import retire_stopped_transports, setup_client
from .client_runtime import foreground_lifetime, process_identity
from .client_tunnel import stop_tracked_tunnels
from .errors import PortmapError
from .settings import PortmapSettings

STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_RUNNER_LOCK = "runner.lock"
_RUNNER_OWNER = "runner-owner.json"


class InitializationCancelled(BaseException):
    """Stop-signal cancellation raised only while initialization runs.

    Derives from BaseException so it slices through ``except Exception``
    handlers exactly like KeyboardInterrupt: setup transactions roll back
    and re-raise it, and the foreground runner performs its normal cleanup.
    """


def runner_lock_path(state_dir: Path) -> Path:
    return state_dir / _RUNNER_LOCK


def runner_owner_path(state_dir: Path) -> Path:
    return state_dir / _RUNNER_OWNER


def _private_state_directory(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = state_dir.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o777 != 0o700:
        raise PortmapError(f"client state must be an owned private directory with mode 0700: {state_dir}")


def _active_runner(state_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(runner_owner_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@contextmanager
def _runner_lock(state_dir: Path) -> Iterator[None]:
    # Held for the whole foreground lifetime. Client operations (setup,
    # connect, disconnect) only ever take their own short-lived lock, so a
    # running foreground session never blocks portal mutations.
    handle = (state_dir / _RUNNER_LOCK).open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            active = _active_runner(state_dir)
            pid = active.get("pid") if active else None
            scope = f" (pid {pid})" if isinstance(pid, int) else ""
            raise PortmapError(
                f"another portmap run is already active{scope} for this state directory; "
                "stop it in its terminal or send it SIGTERM before starting another one"
            )
        yield
    finally:
        handle.close()


def _claim_runner(state_dir: Path) -> None:
    identity = process_identity(os.getpid())
    if identity is None:
        raise PortmapError("cannot record the foreground runner's process identity")
    uid, started, command = identity
    record = {"version": 1, "uid": uid, "pid": os.getpid(), "start_time": started, "command": command, "state_dir": str(state_dir)}
    fd, name = tempfile.mkstemp(prefix=".runner-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as payload:
            json.dump(record, payload)
        Path(name).replace(runner_owner_path(state_dir))
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def _runtime_environment(settings: PortmapSettings, state_dir: Path) -> Iterator[None]:
    # The runner and its spawned agent worker need the same runtime/state
    # selection, including lazy imports of the catalog's host operations.
    values = settings.gateway_env()
    values.update({
        "PORTMAP_ROOT": str(settings.root),
        "PORTMAP_AGENT_SOCKET": str(settings.agent_socket),
        "PORTMAP_CLIENT_STATE_DIR": str(state_dir),
    })
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _note(message: str, stream: IO[str]) -> None:
    print(f"portmap run: {message}", file=stream, flush=True)


def _report_ready(summary: dict[str, Any], stream: IO[str]) -> None:
    gateway = summary.get("gateway") if isinstance(summary, dict) else None
    if isinstance(gateway, dict) and isinstance(gateway.get("http_port"), int):
        address = gateway.get("address") or "127.0.0.1"
        port = gateway["http_port"]
        origin = f"http://{address}/" if port == 80 else f"http://{address}:{port}/"
        _note(f"the shared gateway is ready at {origin}", stream)
    for failure in summary.get("restore_failures") or []:
        _note(f"kept but did not restore {failure.get('domain')}: {failure.get('error')}", stream)
    _note("ready in the foreground; press Ctrl-C to stop", stream)


def run_foreground(
    settings: PortmapSettings,
    *,
    use_sudo: bool = True,
    http_port: int | None = None,
    stream: IO[str] = sys.stderr,
) -> int:
    """Initialize access, serve controls and retire owned workers on signals.

    The foreground scope covers API requests, initialization and shutdown.
    A stop signal during initialization cancels the in-flight setup through
    the same BaseException rollback as any setup failure; once cleanup
    starts, signals only flag completion and never interrupt it. Forced
    process termination still follows the external supervisor's policy.
    """
    state_dir = settings.client_state_dir.expanduser().resolve()
    _private_state_directory(state_dir)
    stopped = threading.Event()
    initializing = True

    def request_stop(_signum: int, _frame: Any) -> None:
        stopped.set()
        if initializing:
            # Cancel an unbounded setup call on the main thread; every
            # setup transaction rolls back on BaseException and re-raises.
            raise InitializationCancelled()

    exit_code = 0
    with _runner_lock(state_dir), foreground_lifetime(), _runtime_environment(settings, state_dir):
        previous_handlers = {number: signal.signal(number, request_stop) for number in STOP_SIGNALS}
        agent: ForegroundAgent | None = None
        try:
            _claim_runner(state_dir)
            agent = start_foreground_agent(settings, access_state_dir=state_dir)
            if agent is None:
                _note("reusing the running host agent", stream)
            else:
                _note("serving the host agent controls in a supervised worker", stream)
            try:
                summary = setup_client(state_dir, settings=settings, use_sudo=use_sudo, http_port=http_port)
            finally:
                # Past initialization a stop signal only ends the wait loop;
                # the final cleanup below must never be interrupted.
                initializing = False
            if not stopped.is_set():
                _report_ready(summary, stream)
            while not stopped.wait(timeout=1.0):
                pass
            _note("stopping; saved connections, business containers and the shared infrastructure are kept", stream)
        except InitializationCancelled:
            initializing = False
            _note("cancelled during initialization; saved connections, business containers and the shared infrastructure are kept", stream)
        finally:
            initializing = False
            try:
                # Terminate the owned worker group first: no in-flight agent
                # mutation (connect, compose, SSH spawn) may race the final
                # owned-master inventory and route cleanup below.
                if agent is not None:
                    try:
                        agent.stop()
                    except (OSError, PortmapError) as exc:
                        _note(f"could not retire the foreground agent: {exc}", stream)
                        exit_code = 1
                for failure in stop_tracked_tunnels():
                    _note(f"could not stop an owned SSH master: {failure}", stream)
                    exit_code = 1
                try:
                    for failure in retire_stopped_transports(state_dir, use_sudo=use_sudo):
                        _note(f"could not retire a stopped connection's route: {failure}", stream)
                        exit_code = 1
                except (PortmapError, OSError) as exc:
                    _note(f"could not republish routes for stopped connections: {exc}", stream)
                    exit_code = 1
                runner_owner_path(state_dir).unlink(missing_ok=True)
            finally:
                for number, handler in previous_handlers.items():
                    signal.signal(number, handler)
    return exit_code
