"""One-time handover and retirement of the removed native portmap workers.

Captured UID/start-time/argv identities guard every signal. Normal operation
never starts native workers. A directly bound legacy HTTP listener can be
released after the container backend is ready; a failed cutover restores only
that captured listener, using its existing executable and configuration.

Ingress units, resolver links, SSH masters, the agent and containers belong to
the controller, not this module.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import signal
import stat
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .client_runtime import ensure_persistent_scope, native_environment, process_identity
from .errors import PortmapError

_ADDRESS = "127.0.0.1"
_INGRESS_NAME = "private"
_MAX_JSON = 4 * 1024 * 1024
_STOP_TIMEOUT = 5.0
_KILL_TIMEOUT = 1.0
_POLL_INTERVAL = 0.05
_WORKER_FLAGS = ("--state-dir", "--port", "--owner")


# --- safe file access --------------------------------------------------------------


def _private_dir(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PortmapError(f"legacy gateway state directory is not private or owned by this user: {path}")


def _private_file(fd: int, path: Path) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PortmapError(f"legacy gateway state file is not private or owned by this user: {path}")


def _read_bytes(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        _private_file(stream.fileno(), path)
        data = stream.read(_MAX_JSON + 1)
    if len(data) > _MAX_JSON:
        raise PortmapError(f"legacy gateway state file is too large: {path}")
    return data


def _safe_descriptor_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise PortmapError(f"cannot inspect legacy client DNS state {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PortmapError(f"legacy client DNS state {path} is not a regular file")
    if info.st_uid != os.getuid():
        raise PortmapError(f"legacy client DNS state {path} is owned by uid {info.st_uid}, not this user")
    if info.st_mode & 0o022:
        raise PortmapError(f"legacy client DNS state {path} is group- or world-writable")


# --- process identity ---------------------------------------------------------------


def _command_matches(observed: list[str] | str, recorded: list[str] | str) -> bool:
    if isinstance(observed, list):
        return observed == (recorded if isinstance(recorded, list) else recorded.split())
    return observed == (recorded if isinstance(recorded, str) else " ".join(recorded))


def _identity_matches(entry: dict[str, Any]) -> bool:
    """True when the captured pid still is the exact captured process."""
    identity = process_identity(entry["pid"])
    if identity is None or identity[0] != os.getuid() or identity[1] != entry["start_time"]:
        return False
    return _command_matches(identity[2], entry["command"])


def _signal_captured(entry: dict[str, Any], sig: int) -> None:
    pidfd = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                pidfd = os.pidfd_open(entry["pid"])
            except OSError as exc:
                if exc.errno not in (errno.ENOSYS, errno.EINVAL):
                    raise
        if not _identity_matches(entry):
            return
        if pidfd is not None:
            signal.pidfd_send_signal(pidfd, sig)
        else:
            os.kill(entry["pid"], sig)
    except ProcessLookupError:
        pass
    finally:
        if pidfd is not None:
            os.close(pidfd)


# --- legacy descriptor validation ----------------------------------------------------


def _port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise PortmapError(f"Legacy gateway port must be an integer between 1 and 65535, got {value!r}")
    return value


def _worker_arguments(command: list[str]) -> dict[str, str] | None:
    """Parse a recorded catalog-view command without assuming the current
    process runs in the same mode (frozen or source) as its recorder."""
    arguments = list(command)
    if len(arguments) < 2 or not Path(arguments[0]).is_absolute():
        return None
    if arguments[1] == "-m":
        if len(arguments) < 3 or arguments[2] != "portmap.client_view":
            return None
        arguments = [arguments[0], *arguments[3:]]
    elif arguments[1] == "_serve-catalog":
        arguments = [arguments[0], *arguments[2:]]
    else:
        return None
    if len(arguments) != 7 or arguments[1::2] != list(_WORKER_FLAGS):
        return None
    values = arguments[2::2]
    if not all(values) or not values[1].isdecimal() or not 1 <= int(values[1]) <= 65535:
        return None
    return {"state_dir": values[0], "port": values[1], "owner": values[2]}


def _valid_recorded_command(state: dict[str, Any], kind: str, command: object) -> bool:
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        return False
    if not Path(command[0]).is_absolute():
        return False
    if kind == "traefik":
        return len(command) == 2 and command[1] == f"--configFile={state['config_path']}"
    arguments = _worker_arguments(command)
    return (
        arguments is not None
        and arguments["state_dir"] == state["state_dir"]
        and arguments["port"] == str(state["view_port"])
        and arguments["owner"] == state["owner"]
    )


def _validate_gateway(state_dir: Path, state: dict[str, Any]) -> None:
    """The ownership validation the native gateway applied before control."""
    owner = state.get("owner")
    if (
        type(state.get("version")) is not int or state["version"] not in (1, 2)
        or type(state.get("uid")) is not int or state["uid"] != os.getuid()
        or not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner)
        or state.get("state_dir") != str(state_dir) or state.get("address") != _ADDRESS
    ):
        raise PortmapError("Invalid legacy local gateway descriptor; refusing to retire its processes")
    runtime = state_dir / "gateway" / owner
    if (
        state.get("instance_dir") != str(runtime)
        or state.get("config_path") != str(runtime / "traefik.yaml")
        or state.get("dynamic_path") != str(runtime / "dynamic" / "routes.yaml")
    ):
        raise PortmapError("Legacy gateway configuration is not inside its private instance directory")
    if state["version"] == 2:
        for name in ("http_port", "listen_port", "view_port", "management_port"):
            _port(state.get(name))
        if len({state["listen_port"], state["view_port"], state["management_port"]}) != 3:
            raise PortmapError("Legacy gateway listeners must use distinct private ports")
        ingress = state.get("ingress")
        if ingress is not None:
            if (
                not isinstance(ingress, dict)
                or ingress.get("name") != _INGRESS_NAME
                or ingress.get("port") != state["http_port"]
                or ingress.get("target_port") != state["listen_port"]
                or state["http_port"] == state["listen_port"]
            ):
                raise PortmapError("Invalid private ingress record in legacy gateway descriptor")
    else:
        for name in ("http_port", "view_port", "management_port"):
            _port(state.get(name))
        if len({state[name] for name in ("http_port", "view_port", "management_port")}) != 3:
            raise PortmapError("Legacy gateway listeners must use distinct ports")
    if not isinstance(state.get("revision"), str) or not re.fullmatch(r"[0-9a-f]{64}", state["revision"]):
        raise PortmapError("Invalid legacy local gateway route revision")
    for directory in (runtime.parent, runtime, runtime / "dynamic"):
        _private_dir(directory)
    try:
        marker = json.loads(_read_bytes(runtime / "owner.json"))
    except OSError as exc:
        raise PortmapError(f"cannot read legacy gateway ownership record: {exc}") from exc
    except ValueError as exc:
        raise PortmapError(f"Invalid legacy gateway ownership record: {exc}") from exc
    if not isinstance(marker, dict) or marker != {key: value for key, value in state.items() if key != "revision"}:
        raise PortmapError("Legacy gateway descriptor does not match its private ownership record")
    for kind in ("traefik", "view"):
        process = state.get(kind)
        if process is not None and (
            not isinstance(process, dict)
            or type(process.get("pid")) is not int or process["pid"] <= 1
            or not isinstance(process.get("start_time"), str) or not process["start_time"]
            or not _valid_recorded_command(state, kind, process.get("command"))
        ):
            raise PortmapError(f"Invalid {kind} process identity in legacy gateway descriptor")


# --- capture --------------------------------------------------------------------------


def _capture_dns(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / "dns.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PortmapError(f"cannot read legacy client DNS state {path}: {exc}") from exc
    _safe_descriptor_file(path)
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise PortmapError(f"invalid legacy client DNS state {path}: {exc}") from exc
    if not isinstance(record, dict):
        raise PortmapError(f"invalid legacy client DNS state {path}")
    pid = record.get("pid")
    owner = record.get("owner")
    if (
        isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1
        or not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner)
    ):
        raise PortmapError(f"invalid legacy client DNS descriptor {path}")
    config = state_dir / "Corefile"
    entry: dict[str, Any] = {
        "kind": "coredns",
        "file": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "owner": owner,
        "config": str(config),
        "pid": pid,
    }
    identity = process_identity(pid)
    if identity is None:
        entry["state"] = "dead"
    else:
        uid, start_time, argv = identity
        if uid != os.getuid() or str(config) not in argv or "-conf" not in argv:
            entry["state"] = "mismatch"
        else:
            entry.update({"state": "owned", "start_time": start_time, "command": argv})
    return entry


def _capture_gateway(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / "gateway.json"
    try:
        raw = _read_bytes(path)
        state = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PortmapError(f"cannot read legacy gateway state {path}: {exc}") from exc
    except ValueError as exc:
        raise PortmapError(f"invalid legacy gateway state {path}: {exc}") from exc
    if not isinstance(state, dict):
        raise PortmapError(f"invalid legacy gateway state {path}")
    try:
        _validate_gateway(state_dir, state)
    except OSError as exc:
        raise PortmapError(f"cannot validate legacy gateway state {path}: {exc}") from exc
    processes: list[dict[str, Any]] = []
    for kind in ("traefik", "view"):
        process = state.get(kind)
        if process is None:
            continue
        entry: dict[str, Any] = {"kind": kind, "pid": process["pid"]}
        identity = process_identity(process["pid"])
        if identity is None:
            entry["state"] = "dead"
        else:
            uid, start_time, argv = identity
            if uid != os.getuid() or start_time != process["start_time"] or not _command_matches(argv, process["command"]):
                entry["state"] = "mismatch"
            else:
                entry.update({"state": "owned", "start_time": process["start_time"], "command": list(process["command"])})
        processes.append(entry)
    return {
        "file": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "owner": state["owner"],
        "version": state["version"],
        "record": state,
        "processes": processes,
    }


def capture_native_workers(state_dir: Path) -> dict[str, Any]:
    """Capture the exact recorded identity of the legacy native workers.

    Validates the old ``dns.json`` and ``gateway.json`` (version 1 and 2)
    descriptors with the ownership rules the native gateway used: caller uid,
    owner token, private instance paths, safe file ownership, and per-process
    uid/start time/exact argv. Nothing is stopped or removed by this call.

    Returns a snapshot for :func:`retire_native_workers`:

    ``{"version": 1, "uid": <uid>, "state_dir": "<abs>", "dns": …, "gateway": …}``

    where each present descriptor contributes ``{"file", "sha256", "owner"}``
    plus per captured pid ``{"kind", "pid", "state"}`` with ``state`` one of
    ``"owned"`` (live and matching: also ``start_time`` and ``command``),
    ``"dead"`` (no such process), or ``"mismatch"`` (live but not the
    recorded worker; never signaled, state retained). ``dns``/``gateway`` are
    None when no descriptor exists. A present-but-invalid descriptor raises
    ``PortmapError`` so uncertain state is never silently discarded.
    """
    state_dir = Path(state_dir).expanduser().resolve()
    return {
        "version": 1,
        "uid": os.getuid(),
        "state_dir": str(state_dir),
        "dns": _capture_dns(state_dir),
        "gateway": _capture_gateway(state_dir),
    }


def _validate_snapshot(state_dir: Path, snapshot: dict[str, Any]) -> None:
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("version") != 1
        or snapshot.get("uid") != os.getuid()
        or snapshot.get("state_dir") != str(state_dir)
    ):
        raise PortmapError("native worker snapshot does not match this state directory; capture the native workers first")


def _unchanged_gateway(state_dir: Path, captured: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    raw = _read_bytes(state_dir / "gateway.json")
    if hashlib.sha256(raw).hexdigest() != captured["sha256"]:
        raise PortmapError("legacy gateway descriptor changed before HTTP handover; retained")
    record = json.loads(raw)
    _validate_gateway(state_dir, record)
    return record, raw


def release_native_http(state_dir: Path, snapshot: dict[str, Any], port: int, *, use_sudo: bool = True) -> None:
    """Release only a captured Traefik directly bound to the replacement port.

    The controller must prove the new backend first. Arm restoration before
    signaling, including interruption while waiting for the old listener to exit.
    DNS and the catalog worker remain available until the full cutover commits.
    """
    state_dir = Path(state_dir).expanduser().resolve()
    _validate_snapshot(state_dir, snapshot)
    gateway = snapshot.get("gateway")
    if not gateway:
        return
    record = gateway["record"]
    if record.get("listen_port", record["http_port"]) != port:
        return
    entry = next((item for item in gateway["processes"] if item["kind"] == "traefik"), None)
    if entry is None or entry["state"] == "dead":
        return
    if entry["state"] != "owned" or not _identity_matches(entry):
        raise PortmapError("legacy HTTP listener identity changed; refusing to release it")
    # A rollback replacement must outlive this login just as the original did.
    # Establish that lifetime before stopping anything, not during recovery.
    ensure_persistent_scope(use_sudo=use_sudo)
    _unchanged_gateway(state_dir, gateway)
    gateway["released_http"] = True
    problem = _stop_captured(entry)
    if problem is not None:
        raise PortmapError(f"legacy HTTP listener handover failed: {problem}")


def _atomic_bytes(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".native-handover-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def restore_native_http(state_dir: Path, snapshot: dict[str, Any]) -> None:
    """Undo this transaction's direct-listener release, never start a fallback.

    The controller must first release its replacement ingress. Both legacy
    ownership records are updated to the restored process, so subsequent setup
    or teardown can still control only the correct worker.
    """
    state_dir = Path(state_dir).expanduser().resolve()
    _validate_snapshot(state_dir, snapshot)
    gateway = snapshot.get("gateway")
    if not gateway or not gateway.get("released_http"):
        return
    original = next(item for item in gateway["processes"] if item["kind"] == "traefik")
    record, raw = _unchanged_gateway(state_dir, gateway)
    # An interrupted stop may have delivered SIGTERM without waiting for exit.
    # A still-matching PID is not evidence that the old listener will survive.
    problem = _stop_captured(original)
    if problem is not None:
        raise PortmapError(f"cannot settle the released legacy HTTP listener: {problem}")
    runtime = Path(record["instance_dir"])
    owner_path = runtime / "owner.json"
    owner_raw = _read_bytes(owner_path)
    # Do not launch with a replaced/symlinked configuration after a failed cutover.
    _read_bytes(Path(record["config_path"]))
    process = None
    restored = None
    published: list[tuple[Path, bytes, bytes]] = []
    try:
        log_fd = os.open(runtime / "traefik-rollback.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(log_fd, "ab") as log:
            _private_file(log.fileno(), runtime / "traefik-rollback.log")
            process = subprocess.Popen(
                original["command"], cwd=runtime, env=native_environment(),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        identity = process_identity(process.pid)
        if identity is None or identity[0] != os.getuid() or not _command_matches(identity[2], original["command"]):
            raise PortmapError("restored legacy HTTP listener did not establish its owned identity")
        restored = {"pid": process.pid, "start_time": identity[1], "command": original["command"]}
        updated = {**record, "traefik": {**record["traefik"], **restored}}
        ownership = {key: value for key, value in updated.items() if key != "revision"}
        for path, value, previous in ((owner_path, ownership, owner_raw), (state_dir / "gateway.json", updated, raw)):
            content = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
            published.append((path, content, previous))
            _atomic_bytes(path, content)
        deadline = time.monotonic() + _STOP_TIMEOUT
        port = record.get("listen_port", record["http_port"])
        while time.monotonic() < deadline:
            if process.poll() is not None or not _identity_matches(restored):
                break
            try:
                with socket.create_connection((_ADDRESS, port), timeout=0.2):
                    gateway["released_http"] = False
                    return
            except OSError:
                time.sleep(_POLL_INTERVAL)
        raise PortmapError(f"restored legacy HTTP listener did not become ready; see {runtime / 'traefik-rollback.log'}")
    except BaseException as exc:
        if process is None:
            raise
        failures = []
        if restored is not None:
            problem = _stop_captured(restored)
            if problem is not None:
                failures.append(problem)
        else:
            # Popen owns this unreaped child even if /proc identity failed.
            try:
                process.terminate()
                try:
                    process.wait(timeout=_STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=_KILL_TIMEOUT)
            except Exception as cleanup:
                failures.append(str(cleanup))
        if not failures:
            try:
                process.wait(timeout=_KILL_TIMEOUT)
                for path, content, previous in reversed(published):
                    current = _read_bytes(path)
                    if current == previous:
                        continue
                    if current != content:
                        raise PortmapError(f"legacy ownership record changed during rollback: {path}")
                    _atomic_bytes(path, previous)
            except Exception as cleanup:
                failures.append(str(cleanup))
        if failures:
            raise PortmapError(f"legacy HTTP restoration failed: {exc}; cleanup also failed: {'; '.join(failures)}") from exc
        raise


# --- retirement ------------------------------------------------------------------------


def _stop_captured(entry: dict[str, Any]) -> str | None:
    """SIGTERM, then SIGKILL, only while the pid stays the captured process."""
    deadline = time.monotonic() + _STOP_TIMEOUT
    _signal_captured(entry, signal.SIGTERM)
    while time.monotonic() < deadline and _identity_matches(entry):
        time.sleep(_POLL_INTERVAL)
    if _identity_matches(entry):
        _signal_captured(entry, signal.SIGKILL)
        deadline = time.monotonic() + _KILL_TIMEOUT
        while time.monotonic() < deadline and _identity_matches(entry):
            time.sleep(_POLL_INTERVAL)
    if _identity_matches(entry):
        return "did not exit after SIGTERM and SIGKILL; state retained"
    return None


def _remove_captured_record(file: str, sha256: str) -> str | None:
    path = Path(file)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot re-read captured descriptor {path}: {exc}; retained"
    if hashlib.sha256(raw).hexdigest() != sha256:
        return f"{path} changed since capture; retained"
    try:
        path.unlink()
    except OSError as exc:
        return f"cannot remove captured descriptor {path}: {exc}"
    return None


def retire_native_workers(state_dir: Path, snapshot: dict[str, Any]) -> None:
    """Stop only the native workers captured by :func:`capture_native_workers`.

    Each captured pid is revalidated (uid, start time, exact argv) immediately
    before every signal, delivered through a pidfd where the kernel supports
    it. Ingress units, resolver links, SSH masters, the agent and containers
    are never touched. The old descriptor files are removed only when they
    are still byte-for-byte the captured record and no captured worker
    remains live or uncertain; otherwise state is preserved and the problems
    are raised as one ``PortmapError``.
    """
    state_dir = Path(state_dir).expanduser().resolve()
    _validate_snapshot(state_dir, snapshot)
    problems: list[str] = []
    for group in ("dns", "gateway"):
        captured = snapshot.get(group)
        if not isinstance(captured, dict):
            continue
        unresolved = False
        entries = captured["processes"] if group == "gateway" else [captured]
        for entry in entries:
            state = entry.get("state")
            if state == "mismatch":
                problems.append(
                    f"{group} pid {entry.get('pid')} is live but does not match the captured identity; its descriptor was retained"
                )
                unresolved = True
            elif state == "owned":
                problem = _stop_captured(entry)
                if problem is not None:
                    problems.append(f"{group} {entry.get('kind', 'coredns')} pid {entry.get('pid')}: {problem}")
                    unresolved = True
        if not unresolved:
            problem = _remove_captured_record(captured["file"], captured["sha256"])
            if problem is not None:
                problems.append(problem)
    if problems:
        raise PortmapError("legacy native worker retirement incomplete: " + "; ".join(problems))
