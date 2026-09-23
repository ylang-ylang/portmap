from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Mapping, TypedDict

from .errors import PortmapError


class TunnelDescriptor(TypedDict):
    version: int
    target: str
    key: str
    controlpath: str
    config_file: str | None
    log_file: str
    owner_token: str


_RUNTIME_PREFIX = f"portmap-ssh-{os.getuid()}-"
_DETAIL_BYTES = 4096
_HOST = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*\Z")
_SCOPE = re.compile(r"[A-Za-z0-9_.-]+\Z")
_CHILDREN: dict[str, subprocess.Popen[bytes]] = {}


def start_tunnel(
    target: str,
    *,
    state_dir: Path,
    key: str,
    config_file: Path | None = None,
    timeout: float = 10,
) -> TunnelDescriptor:
    """Start a private master; persist the returned dict unchanged as JSON.

    Targets are OpenSSH aliases, IP literals, or user@host, not shell commands
    or SSH URLs. Authentication and proxies come from the local SSH config.
    Logs are retained under state_dir/ssh, including after failure or stop.
    Readiness means the control socket responds, not that a remote API works.
    """
    _validate_target(target)
    timeout = _timeout(timeout)
    if not isinstance(key, str) or not key or len(key) > 1024:
        raise PortmapError("SSH tunnel key must be a nonempty string of at most 1024 characters")

    runtime: Path | None = None
    child: subprocess.Popen[bytes] | None = None
    deadline = time.monotonic() + timeout
    try:
        log_dir = Path(state_dir).expanduser().resolve() / "ssh"
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = str(Path(config_file).expanduser().resolve()) if config_file is not None else None
        runtime = Path(tempfile.mkdtemp(prefix=_RUNTIME_PREFIX, dir=Path("/tmp").resolve()))
        runtime.chmod(0o700)
        token = secrets.token_hex(16)
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        tunnel: TunnelDescriptor = {
            "version": 1,
            "target": target,
            "key": key,
            "controlpath": str(runtime / "control"),
            "config_file": config,
            "log_file": str(log_dir / f"{key_hash}-{token}.log"),
            "owner_token": token,
        }
        with os.fdopen(os.open(runtime / "owner.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as owner:
            json.dump(tunnel, owner)

        command = ["ssh", "-M", "-N", "-T", "-S", tunnel["controlpath"]]
        # Leave host-key policy to the user's SSH config. In particular,
        # authenticated ProxyCommand providers may own the host-key trust
        # decision. We never add a verification bypass; unknown normal hosts
        # still fail noninteractively under BatchMode.
        for option in (
            "BatchMode=yes",
            "ControlPersist=no",
            "ForkAfterAuthentication=no",
            "ClearAllForwardings=yes",
            "PermitLocalCommand=no",
            "LocalCommand=none",
            "RemoteCommand=none",
            "RequestTTY=no",
            "StdinNull=yes",
            "Tunnel=no",
            "ForwardAgent=no",
            "ForwardX11=no",
            "GatewayPorts=no",
            "ExitOnForwardFailure=yes",
            "ConnectionAttempts=1",
            f"ConnectTimeout={max(1, min(math.ceil(timeout), 2147483647))}",
        ):
            command.extend(("-o", option))
        if config is not None:
            command.extend(("-F", config))
        command.extend(("--", target))

        with os.fdopen(os.open(tunnel["log_file"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as log:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                start_new_session=True,
            )
        while True:
            status = child.poll()
            if status is not None:
                raise PortmapError(_failure(tunnel, f"SSH tunnel to {target!r} exited with status {status}"))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PortmapError(_failure(tunnel, f"SSH tunnel to {target!r} timed out after {timeout:g}s"))
            if (runtime / "control").exists():
                try:
                    status, _ = _control(tunnel, "check", timeout=min(remaining, 0.25))
                except PortmapError:
                    status = 1
                if status == 0 and child.poll() is None:
                    _CHILDREN[tunnel["controlpath"]] = child
                    return tunnel
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    except BaseException as exc:
        try:
            if child is not None:
                _terminate_child(child)
            if runtime is not None:
                _remove_runtime(runtime)
        except (OSError, subprocess.TimeoutExpired) as cleanup_error:
            raise PortmapError(f"{exc}; could not clean up SSH tunnel {runtime}: {cleanup_error}") from exc
        if isinstance(exc, (OSError, ValueError)):
            raise PortmapError(f"Could not start SSH tunnel to {target!r}: {exc}") from exc
        raise


def add_forward(
    tunnel: Mapping[str, object],
    *,
    local_address: str,
    local_port: int,
    remote_address: str = "127.0.0.1",
    remote_port: int,
    timeout: float = 5,
) -> None:
    """Bind a loopback TCP listener; the caller must separately probe health."""
    _forward(tunnel, "forward", local_address, local_port, remote_address, remote_port, timeout)


def cancel_forward(
    tunnel: Mapping[str, object],
    *,
    local_address: str,
    local_port: int,
    remote_address: str = "127.0.0.1",
    remote_port: int,
    timeout: float = 5,
) -> None:
    """Cancel a listener using exactly the endpoint tuple used to create it."""
    _forward(tunnel, "cancel", local_address, local_port, remote_address, remote_port, timeout)


def tunnel_alive(tunnel: Mapping[str, object], *, timeout: float = 2) -> bool:
    """Check only our master, without rereading SSH config or opening a session."""
    timeout = _timeout(timeout)
    runtime = _owned_runtime(tunnel)
    if runtime is None or not (runtime / "control").exists():
        return False
    try:
        status, _ = _control(tunnel, "check", timeout=timeout)
    except PortmapError:
        return False
    return status == 0


def stop_tunnel(tunnel: Mapping[str, object], *, timeout: float = 5) -> None:
    """Exit our master and remove its private runtime files; never signal a PID.

    An already-removed or stale socket is harmless. A live but unresponsive
    master is left intact for a later retry, rather than orphaned by unlinking.
    """
    timeout = _timeout(timeout)
    deadline = time.monotonic() + timeout
    runtime = _owned_runtime(tunnel)
    if runtime is None:
        return
    controlpath = str(runtime / "control")
    try:
        if (runtime / "control").exists():
            status, detail = _control(tunnel, "exit", timeout=timeout)
            if status != 0 and not _socket_closed(controlpath, deadline):
                raise PortmapError(_failure(tunnel, "Could not stop SSH tunnel", detail))
        while not _socket_closed(controlpath, deadline):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PortmapError("SSH tunnel did not stop before the deadline; its control socket was retained")
            time.sleep(min(0.05, remaining))
        child = _CHILDREN.get(controlpath)
        if child is not None:
            child.wait(timeout=max(0, deadline - time.monotonic()))
            del _CHILDREN[controlpath]
        _remove_runtime(runtime)
    except subprocess.TimeoutExpired as exc:
        raise PortmapError("SSH tunnel did not exit before the deadline; its runtime files were retained") from exc
    except OSError as exc:
        raise PortmapError(f"Could not clean up SSH tunnel: {exc}") from exc


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PortmapError("SSH timeout must be a finite positive number")
    try:
        timeout = float(value)
    except OverflowError as exc:
        raise PortmapError("SSH timeout is too large") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise PortmapError("SSH timeout must be a finite positive number")
    return timeout


def _validate_target(target: object) -> None:
    if not isinstance(target, str) or not target or len(target) > 1024:
        raise PortmapError("SSH target must be an alias, IP address, or user@host")
    user, separator, host = target.rpartition("@")
    if separator:
        if not _HOST.fullmatch(user):
            raise PortmapError("Invalid SSH username")
    else:
        host = target
    _address(host)


def _address(value: object, *, loopback: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise PortmapError("SSH forwarding address must be a hostname or IP address")
    address = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    if "%" in address and not _SCOPE.fullmatch(address.partition("%")[2]):
        raise PortmapError("Invalid IPv6 scope in SSH address")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        if loopback:
            raise PortmapError("SSH local forwarding address must be a numeric loopback address") from None
        if not _HOST.fullmatch(value):
            raise PortmapError("Invalid SSH hostname or IP address") from None
        return value
    if loopback and not parsed.is_loopback:
        raise PortmapError("SSH local forwarding address must be a numeric loopback address")
    return f"[{parsed}]" if parsed.version == 6 else str(parsed)


def _port(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise PortmapError("SSH forwarding ports must be integers between 1 and 65535")
    return value


def _forward(
    tunnel: Mapping[str, object],
    operation: str,
    local_address: str,
    local_port: int,
    remote_address: str,
    remote_port: int,
    timeout: float,
) -> None:
    specification = f"{_address(local_address, loopback=True)}:{_port(local_port)}:{_address(remote_address)}:{_port(remote_port)}"
    timeout = _timeout(timeout)
    if _owned_runtime(tunnel) is None:
        raise PortmapError("SSH tunnel is no longer present")
    status, detail = _control(tunnel, operation, timeout=timeout, specification=specification)
    # OpenSSH reports a refused cancellation on stderr but exits zero.
    # Control commands use ERROR logging, so this is not informational output.
    if status != 0 or (operation == "cancel" and detail):
        raise PortmapError(_failure(tunnel, f"SSH {operation} failed for {specification}", detail))


def _owned_runtime(tunnel: Mapping[str, object]) -> Path | None:
    """Authenticate persisted metadata before touching any master or its files."""
    if not isinstance(tunnel, Mapping):
        raise PortmapError("Invalid SSH tunnel descriptor")
    fields = ("version", "target", "key", "controlpath", "config_file", "log_file", "owner_token")
    if (
        type(tunnel.get("version")) is not int
        or tunnel["version"] != 1
        or any(not isinstance(tunnel.get(name), str) for name in fields if name not in ("version", "config_file"))
        or (tunnel.get("config_file") is not None and not isinstance(tunnel["config_file"], str))
        or not re.fullmatch(r"[0-9a-f]{32}", str(tunnel.get("owner_token", "")))
    ):
        raise PortmapError("Invalid SSH tunnel descriptor")
    _validate_target(tunnel["target"])
    controlpath = Path(str(tunnel["controlpath"]))
    runtime = controlpath.parent
    if (
        controlpath.name != "control"
        or runtime.parent != Path("/tmp").resolve()
        or not runtime.name.startswith(_RUNTIME_PREFIX)
        or len(os.fsencode(controlpath)) > 90
    ):
        raise PortmapError("SSH control path is not a private portmap tunnel")
    try:
        info = runtime.lstat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PortmapError(f"Cannot inspect SSH tunnel ownership: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PortmapError("SSH tunnel directory is not private or is not owned by this user")
    try:
        with os.fdopen(os.open(runtime / "owner.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as owner:
            info = os.fstat(owner.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise PortmapError("SSH tunnel ownership record is not private")
            recorded = json.loads(owner.read(65537))
        if recorded != {name: tunnel.get(name) for name in fields}:
            raise PortmapError("SSH tunnel descriptor does not match its ownership record")
        try:
            info = controlpath.lstat()
        except FileNotFoundError:
            return runtime
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise PortmapError("SSH control path is not an owned Unix socket")
    except (OSError, ValueError) as exc:
        raise PortmapError(f"Cannot verify SSH tunnel ownership: {exc}") from exc
    return runtime


def _control(
    tunnel: Mapping[str, object],
    operation: str,
    *,
    timeout: float,
    specification: str | None = None,
) -> tuple[int, str]:
    # -O commands fail if the socket is unavailable; unlike ordinary sessions,
    # they never fall back to making a fresh SSH connection. -F none also avoids
    # rerunning Match exec, proxies, and other side-effectful user config.
    command = ["ssh", "-F", "none", "-o", "LogLevel=ERROR", "-S", str(tunnel["controlpath"]), "-O", operation]
    if specification is not None:
        command.extend(("-L", specification))
    command.extend(("--", str(tunnel["target"])))
    try:
        with tempfile.TemporaryFile() as error_log:
            try:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=error_log,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                detail = _tail(error_log)
                raise PortmapError(f"SSH {operation} timed out" + (f": {detail}" if detail else "")) from exc
            return result.returncode, _tail(error_log)
    except OSError as exc:
        raise PortmapError(f"Could not run SSH {operation}: {exc}") from exc


def _tail(stream: BinaryIO) -> str:
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - _DETAIL_BYTES))
    text = stream.read(_DETAIL_BYTES).decode("utf-8", errors="replace").strip()
    return "".join(character if character.isprintable() or character in "\n\t" else "?" for character in text)


def _failure(tunnel: Mapping[str, object], message: str, detail: str = "") -> str:
    if not detail:
        try:
            with os.fdopen(os.open(str(tunnel["log_file"]), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as log:
                detail = _tail(log)
        except OSError:
            pass
    return message + (f": {detail}" if detail else "") + f" (log: {tunnel['log_file']})"


def _terminate_child(child: subprocess.Popen[bytes]) -> None:
    # Only this invocation's unreaped Popen child is signalled. Persisted
    # descriptors contain no PID, so PID reuse can never target another process.
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=1)


def _socket_closed(controlpath: str, deadline: float) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(max(0.001, min(0.1, deadline - time.monotonic())))
        try:
            probe.connect(controlpath)
        except OSError as exc:
            return exc.errno in (errno.ENOENT, errno.ECONNREFUSED)
    return False


def _remove_runtime(runtime: Path) -> None:
    # The caller either just created this directory or checked its ownership.
    # Never recurse through a persisted path or delete files we do not own.
    for name in ("control", "owner.json"):
        (runtime / name).unlink(missing_ok=True)
    runtime.rmdir()
