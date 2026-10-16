"""Host ingress publication with two selectable implementations.

``systemd`` (the original, now legacy backend) installs a pair of
deterministic system units derived from the calling user's uid, the portmap
state directory, and the role name (``private`` or ``public``): a ``.socket``
unit that owns the bind address (so privileged ports bind without giving the
portmap process capabilities or a root client) and a
``systemd-socket-proxyd`` ``.service`` that runs unprivileged, with no
capabilities, in the host network namespace, forwarding every accepted
connection to the container-published loopback port
``127.0.0.1:<target_port>``. Unit files are root-owned with fixed content
carrying a portmap ownership marker; they are never enabled at boot and are
only removed after the marker and file ownership have been re-validated.
Sudo is used solely for exact unit installation/removal and systemctl
control commands; status is read-only.

``container`` (see :mod:`portmap.runtime_ingress`) instead runs one
independently owned HAProxy container per role, published by the container
engine on the exact ``<bind-address>:<port>`` and forwarding over the gateway
bridge network, with no host privilege at all. It is the default for every
new publication: the choice does not depend on rootless engines, privileged
ports, sudo, systemd, or host low-port sysctls, and the engine's own
create/start/publish result is the authoritative error.

Selection is recorded per role in the state directory: an existing record
without a ``backend`` field means the systemd implementation, and every
status/stop operation dispatches through that record so mixed installations
stay consistent. ``auto`` reuses such an owned record — and any backend
explicitly recorded in the infrastructure state — instead of moving an
existing installation to another implementation. The systemd backend remains
available for explicit advanced selection and for recovering or migrating
units portmap already owns, but it is never an automatic fallback: when the
shared container infrastructure state is missing, ingress reports a setup
error instead of installing units.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .client_runtime import native_environment
from .errors import PortAllocationError, PortmapError
from .permissions import tighten_private_descriptor

INGRESS_ROLES = ("private", "public")
INGRESS_BACKENDS = ("auto", "container", "systemd")
PRIVATE_BIND_ADDRESS = "127.0.0.1"
TARGET_ADDRESS = "127.0.0.1"

UNIT_DIR = Path("/etc/systemd/system")
SYSTEMD_RUN_DIR = Path("/run/systemd/system")
PROXYD_CANDIDATES = (
    Path("/usr/lib/systemd/systemd-socket-proxyd"),
    Path("/lib/systemd/systemd-socket-proxyd"),
)
UNIT_PREFIX = "portmap-ingress"
ROOT_UID = 0  # required owner of every managed unit file and the proxyd binary
UNIT_FILE_MODE = 0o644

_COMMAND_TIMEOUT = 30.0
_RELOAD_TIMEOUT = 60.0
_ACTIVATION_TIMEOUT = 10.0
_ACTIVATION_POKE_INTERVAL = 2.0
_POLL_INTERVAL = 0.1


# --- validation -----------------------------------------------------------------


def _role(value: object) -> str:
    if value not in INGRESS_ROLES:
        raise PortmapError(f"ingress name must be one of {list(INGRESS_ROLES)}, got {value!r}")
    return value  # type: ignore[return-value]


def _bind_address(role: str, value: object) -> str:
    if not isinstance(value, str):
        raise PortmapError(f"{role} ingress bind address must be a literal IP address string")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise PortmapError(f"{role} ingress bind address must be a literal IP address, got {value!r}") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
        raise PortmapError("ingress bind address must not contain a zone identifier")
    if role == "private":
        if str(address) != PRIVATE_BIND_ADDRESS:
            raise PortmapError(f"private ingress must bind exactly {PRIVATE_BIND_ADDRESS}, got {str(address)}")
    else:
        if address.is_unspecified:
            raise PortmapError("public ingress requires an explicit host IP address, not a wildcard")
        if address.is_loopback:
            raise PortmapError(
                "public ingress requires a non-loopback host IP address so LAN publication cannot expose private routes"
            )
        if address.is_multicast or address.is_reserved:
            raise PortmapError(f"public ingress cannot bind {str(address)}: not a usable unicast host address")
    return str(address)


def _port(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise PortmapError(f"ingress {label} must be an integer between 1 and 65535, got {value!r}")
    return value


# --- naming, ownership markers, and unit content ---------------------------------


def unit_names(state_dir: Path, name: str) -> tuple[str, str]:
    """Deterministic (socket, service) unit names for this user and state dir."""
    role = _role(name)
    digest = hashlib.sha256(str(Path(state_dir).resolve()).encode("utf-8")).hexdigest()[:8]
    base = f"{UNIT_PREFIX}-{role}-{os.getuid()}-{digest}"
    return f"{base}.socket", f"{base}.service"


def _marker(state_dir: Path, role: str) -> str:
    resolved = Path(state_dir).resolve()
    return f"# {UNIT_PREFIX} uid={os.getuid()} role={role} state={resolved}"


def _socket_unit_content(marker: str, role: str, bind: str, port: int) -> str:
    endpoint = f"[{bind}]:{port}" if ":" in bind else f"{bind}:{port}"
    return (
        f"{marker}\n"
        "# Managed by portmap ingress; remove with portmap instead of editing.\n"
        "[Unit]\n"
        f"Description=portmap {role} ingress {endpoint} (uid {os.getuid()})\n"
        "\n"
        "[Socket]\n"
        f"ListenStream={endpoint}\n"
        + ("BindIPv6Only=ipv6-only\n" if ":" in bind else "")
    )


def _service_unit_content(
    marker: str,
    role: str,
    socket_unit: str,
    bind: str,
    port: int,
    target_port: int,
    proxyd: Path,
) -> str:
    lines = [
        marker,
        "# Managed by portmap ingress; remove with portmap instead of editing.",
        "[Unit]",
        f"Description=portmap {role} ingress forward {bind}:{port} -> {TARGET_ADDRESS}:{target_port}",
        f"Requires={socket_unit}",
        f"After={socket_unit}",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        # The listening socket is owned by the .socket unit; this service only
        # receives it via socket activation and may stay unprivileged. Backends
        # are container-published loopback ports: rootless Podman may DNAT that
        # traffic to the container bridge address, so the unit deliberately
        # carries no address-restricted cgroup BPF policy (a loopback-only
        # allowlist would silently drop DNATed connections).
        f"ExecStart={proxyd} {TARGET_ADDRESS}:{target_port}",
        "DynamicUser=yes",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "RestrictAddressFamilies=AF_INET AF_UNIX" + (" AF_INET6" if ":" in bind else ""),
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "SystemCallArchitectures=native",
        "UMask=0077",
    ]
    return "\n".join(lines) + "\n"


# --- installed-unit inspection ----------------------------------------------------


def _inspect_unit(
    path: Path,
    marker: str,
    expected_variants: Sequence[str] | None,
) -> tuple[str, str | None, str | None]:
    """Classify an installed unit file as absent/ours/changed/foreign.

    Returns (state, foreign_reason, content). Ownership, regular-file (no
    symlink), root ownership, mode, and the portmap marker are all required
    before a unit is considered ours; content variants mark parameter drift.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "absent", None, None
    except OSError as exc:
        return "foreign", f"cannot inspect {path}: {exc}", None
    if stat.S_ISLNK(info.st_mode):
        return "foreign", f"{path} is a symlink", None
    if not stat.S_ISREG(info.st_mode):
        return "foreign", f"{path} is not a regular file", None
    if info.st_uid != ROOT_UID:
        return "foreign", f"{path} is owned by uid {info.st_uid}, not root", None
    if info.st_mode & 0o022:
        return "foreign", f"{path} is group- or world-writable", None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "foreign", f"cannot read {path}: {exc}", None
    if not content.startswith(marker + "\n"):
        return "foreign", f"{path} has no portmap ownership marker for this state directory", None
    if expected_variants is not None and content not in expected_variants:
        return "changed", None, content
    return "ours", None, content


def _inspect_pair(
    socket_path: Path,
    service_path: Path,
    marker: str,
    expected: tuple[str, tuple[str, ...]] | None,
) -> dict[str, tuple[str, str | None, str | None]]:
    socket_variants = None if expected is None else (expected[0],)
    service_variants = None if expected is None else expected[1]
    return {
        "socket": _inspect_unit(socket_path, marker, socket_variants),
        "service": _inspect_unit(service_path, marker, service_variants),
    }


def _refuse_foreign(installed: dict[str, tuple[str, str | None, str | None]]) -> None:
    reasons = [
        f"{key} unit: {reason or 'not owned by this portmap state'}"
        for key, (state, reason, _) in installed.items()
        if state == "foreign"
    ]
    if reasons:
        raise PortmapError(
            "refusing to touch ingress units that are not owned by this portmap state (" + "; ".join(reasons) + ")"
        )


def _parse_params(socket_content: str | None, service_content: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if socket_content:
        match = re.search(r"^ListenStream=(\[[0-9A-Fa-f:.]+\]|\d+\.\d+\.\d+\.\d+):(\d+)$", socket_content, re.MULTILINE)
        if match:
            params["bind_address"] = match.group(1).strip("[]")
            params["port"] = int(match.group(2))
    if service_content:
        match = re.search(rf"^ExecStart=\S+ {re.escape(TARGET_ADDRESS)}:(\d+)$", service_content, re.MULTILINE)
        if match:
            params["target_port"] = int(match.group(1))
    return params


# --- command plumbing -------------------------------------------------------------


def discover_proxyd() -> Path:
    """Locate the installed systemd-socket-proxyd, rejecting unsafe candidates."""
    rejected: list[str] = []
    for candidate in PROXYD_CANDIDATES:
        try:
            resolved = Path(os.path.realpath(candidate))
            info = resolved.stat()
        except OSError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ROOT_UID
            or info.st_mode & 0o022
            or not os.access(resolved, os.X_OK)
        ):
            rejected.append(str(candidate))
            continue
        return resolved
    searched = ", ".join(str(candidate) for candidate in PROXYD_CANDIDATES)
    detail = f" (rejected as unsafe: {', '.join(rejected)})" if rejected else ""
    raise PortmapError(
        "systemd-socket-proxyd was not found"
        f" (searched: {searched}{detail}); install the systemd package that ships it, for example: sudo apt install systemd"
    )


def _systemctl_path() -> str:
    found = shutil.which("systemctl")
    if not found:
        raise PortmapError("systemctl was not found in PATH; systemd ingress requires a systemd system manager")
    return found


def _require_systemd() -> None:
    if not sys.platform.startswith("linux"):
        raise PortmapError(f"systemd ingress is unsupported on {sys.platform}; it requires a Linux host running systemd")
    if not SYSTEMD_RUN_DIR.is_dir():
        raise PortmapError(
            f"systemd is not managing this system ({SYSTEMD_RUN_DIR} is absent); systemd ingress units cannot be managed"
        )
    _systemctl_path()


def _run(command: list[str], *, timeout: float = _COMMAND_TIMEOUT) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            env=native_environment(),
        )
    except FileNotFoundError as exc:
        raise PortmapError(f"required command {command[0]!r} was not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise PortmapError(f"command {command[0]!r} timed out after {timeout:.0f}s") from exc
    return result.returncode, result.stdout, result.stderr


def _privileged(command: list[str], *, use_sudo: bool, timeout: float = _COMMAND_TIMEOUT) -> tuple[int, str, str]:
    """Run exactly one root operation: directly as root, else through sudo."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return _run(command, timeout=timeout)
    if not use_sudo:
        raise PortmapError(
            "this ingress operation needs administrator privileges: pass use_sudo=True "
            "(portmap runs one exact sudo command per step) or run as root"
        )
    return _run(["sudo", "--", *command], timeout=timeout)


def _show(unit: str, properties: Sequence[str]) -> dict[str, str]:
    command = [_systemctl_path(), "show"]
    for prop in properties:
        command += ["-p", prop]
    command.append(unit)
    code, out, _ = _run(command)
    values = {prop: "" for prop in properties}
    if code != 0:
        return values
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in values:
            values[key] = value
    return values


def _unit_active(unit: str) -> bool:
    return _show(unit, ("ActiveState",))["ActiveState"] == "active"


def _is_enabled(unit: str) -> bool:
    code, output, _ = _run([_systemctl_path(), "is-enabled", unit])
    return code == 0 and output.strip() in {"enabled", "enabled-runtime"}


def _address_in_use(bind: str, port: int) -> bool:
    """Unprivileged positive check: only a completed connection proves a listener."""
    try:
        with socket.create_connection((bind, port), timeout=1.0):
            return True
    except OSError:
        return False


def _daemon_reload(use_sudo: bool) -> None:
    code, out, err = _privileged([_systemctl_path(), "daemon-reload"], use_sudo=use_sudo, timeout=_RELOAD_TIMEOUT)
    if code != 0:
        raise PortmapError(f"systemctl daemon-reload failed: {(err or out).strip()}")


def _stop_units(socket_unit: str, service_unit: str, *, use_sudo: bool) -> None:
    code, out, err = _privileged([_systemctl_path(), "stop", socket_unit, service_unit], use_sudo=use_sudo)
    if code != 0:
        raise PortmapError(f"stopping portmap ingress units failed: {(err or out).strip()}")


def _start_socket(socket_unit: str, bind: str, port: int, *, use_sudo: bool) -> None:
    code, out, err = _privileged([_systemctl_path(), "start", socket_unit], use_sudo=use_sudo)
    if code != 0:
        detail = (err or out).strip()
        if re.search(r"address already in use", detail, re.IGNORECASE):
            raise PortAllocationError(
                f"another listener already uses {bind}:{port}; portmap never stops unrelated services "
                f"({socket_unit}: {detail})"
            )
        raise PortmapError(f"starting {socket_unit} failed: {detail}")


# --- unit installation ------------------------------------------------------------


def _stage(content: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="ingress-unit-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return Path(temporary)


def _install_unit(staging: Path, destination: Path, content: str, *, use_sudo: bool) -> None:
    code, out, err = _privileged(
        ["install", "-m", "0644", "-o", "root", "-g", "root", "--", str(staging), str(destination)],
        use_sudo=use_sudo,
    )
    if code != 0:
        raise PortmapError(f"installing system unit {destination.name} failed: {(err or out).strip()}")
    _validate_installed_unit(destination, content)


def _validate_installed_unit(destination: Path, content: str) -> None:
    try:
        info = destination.lstat()
    except OSError as exc:
        raise PortmapError(f"installed unit {destination} cannot be inspected: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PortmapError(f"installed unit {destination} is not a root-owned regular file")
    if info.st_uid != ROOT_UID:
        raise PortmapError(f"installed unit {destination} is owned by uid {info.st_uid}, not root")
    if stat.S_IMODE(info.st_mode) != UNIT_FILE_MODE:
        raise PortmapError(
            f"installed unit {destination} has mode {stat.S_IMODE(info.st_mode):o}, expected {UNIT_FILE_MODE:o}"
        )
    try:
        installed = destination.read_text(encoding="utf-8")
    except OSError as exc:
        raise PortmapError(f"installed unit {destination} cannot be read back: {exc}") from exc
    if installed != content:
        raise PortmapError(f"installed unit {destination} does not match the content portmap generated")


def _remove_unit_files(paths: tuple[Path, ...], *, use_sudo: bool) -> None:
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PortmapError(f"cannot inspect {path}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID:
            raise PortmapError(f"refusing to remove {path}: not a root-owned regular portmap unit file")
    code, out, err = _privileged(["rm", "-f", "--", *[str(path) for path in paths]], use_sudo=use_sudo)
    if code != 0:
        raise PortmapError(f"removing portmap ingress units failed: {(err or out).strip()}")
    for path in paths:
        if path.exists() or path.is_symlink():
            raise PortmapError(f"{path} could not be removed")


# --- activation --------------------------------------------------------------------


def _activate(bind: str, port: int, service_unit: str) -> tuple[bool, str | None]:
    """Trigger socket activation with real connections until the service reports active."""
    deadline = time.monotonic() + _ACTIVATION_TIMEOUT
    last_poke = 0.0
    while True:
        if time.monotonic() - last_poke >= _ACTIVATION_POKE_INTERVAL:
            last_poke = time.monotonic()
            try:
                with socket.create_connection((bind, port), timeout=_ACTIVATION_POKE_INTERVAL):
                    pass
            except OSError:
                pass
        props = _show(service_unit, ("ActiveState", "Result"))
        state = props["ActiveState"]
        if state == "active":
            return True, None
        if state == "failed":
            return False, f"Result={props['Result'] or 'failed'}"
        if time.monotonic() >= deadline:
            return False, f"ActiveState={state or 'unknown'}, Result={props['Result'] or 'unknown'}"
        time.sleep(_POLL_INTERVAL)


def _activate_or_raise(bind: str, port: int, service_unit: str) -> None:
    """Trigger socket activation with real connections until the service is active."""
    ok, detail = _activate(bind, port, service_unit)
    if not ok:
        raise PortmapError(
            f"ingress service {service_unit} did not activate"
            + (f" ({detail})" if detail else "")
            + f"; inspect with: systemctl status {service_unit} && journalctl -u {service_unit} --no-pager"
        )


# --- rollback helpers ---------------------------------------------------------------


def _restore_previous(
    previous: dict[str, str | None],
    socket_path: Path,
    service_path: Path,
    socket_unit: str,
    staging_dir: Path,
    *,
    use_sudo: bool,
    notes: list[str],
) -> None:
    try:
        for content, destination in ((previous["socket"], socket_path), (previous["service"], service_path)):
            if content is None:
                continue
            staging = _stage(content, staging_dir)
            try:
                _install_unit(staging, destination, content, use_sudo=use_sudo)
            finally:
                staging.unlink(missing_ok=True)
        _daemon_reload(use_sudo)
        if previous["socket"] is not None:
            code, out, err = _privileged([_systemctl_path(), "start", socket_unit], use_sudo=use_sudo)
            if code != 0:
                notes.append(f"restored units but restarting {socket_unit} failed: {(err or out).strip()}")
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never masked
        notes.append(f"could not restore previous ingress units: {exc}")


def _remove_everything(
    socket_path: Path,
    service_path: Path,
    socket_unit: str,
    service_unit: str,
    state_dir: Path,
    role: str,
    *,
    use_sudo: bool,
    notes: list[str],
) -> None:
    for verb, units in (("stop", [socket_unit, service_unit]), ("reset-failed", [service_unit, socket_unit])):
        try:
            _privileged([_systemctl_path(), verb, *units], use_sudo=use_sudo)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{verb}: {exc}")
    try:
        _remove_unit_files((socket_path, service_path), use_sudo=use_sudo)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"remove units: {exc}")
    try:
        _daemon_reload(use_sudo)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"daemon-reload: {exc}")
    _clear_state(state_dir, role)


# --- persisted ingress state ---------------------------------------------------------


def _state_file(state_dir: Path, role: str) -> Path:
    return state_dir / "ingress" / f"{role}.json"


def _save_state(state_dir: Path, role: str, payload: dict[str, Any]) -> None:
    path = _state_file(state_dir, role)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _load_state(state_dir: Path, role: str) -> dict[str, Any] | None:
    path = _state_file(state_dir, role)
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if (stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
                    and stat.S_IMODE(metadata.st_mode) & 0o077):
                tighten_private_descriptor(path, handle.fileno(), message=f"ingress ownership record is not private: {path}")
                metadata = os.fstat(handle.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077):
                raise PortmapError(f"ingress ownership record is not private: {path}")
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PortmapError(f"cannot read ingress ownership record {path}: {exc}") from exc
    if not isinstance(record, dict) or record.get("backend") not in (None, "systemd", "container"):
        raise PortmapError(f"invalid ingress ownership record: {path}")
    return record


def _clear_state(state_dir: Path, role: str) -> None:
    _state_file(state_dir, role).unlink(missing_ok=True)


# --- public interface ----------------------------------------------------------------


def ensure_ingress(
    state_dir: Path,
    *,
    name: str,
    bind_address: str,
    port: int,
    target_port: int,
    use_sudo: bool = True,
    backend: str = "auto",
    infrastructure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Install (or update) the selected ingress and leave it bound and verified.

    ``backend`` selects the implementation: ``container`` publishes an owned
    HAProxy container on the exact bind address through the container engine
    without any host privilege, ``systemd`` installs the root-owned socket
    units, and ``auto`` follows the recorded selection — first a backend
    explicitly recorded in the infrastructure state, then this role's saved
    owned record (a legacy record without a ``backend`` field means systemd)
    — and otherwise selects the container backend regardless of the port,
    rootless engines, sudo, or systemd availability. Real create/start/bind
    errors are authoritative: portmap never falls back to another backend or
    port, and a missing shared container infrastructure state is reported as
    a setup error instead of installing units.

    Idempotent for the active backend: when the exact resource is installed
    and running, no privileged command or engine mutation runs. Any failure
    inside a change transaction restores the previous owned ingress or
    removes freshly created resources before raising.
    """
    role = _role(name)
    bind = _bind_address(role, bind_address)
    port = _port(port, f"{role} ingress bind port")
    target_port = _port(target_port, "ingress target port")
    if backend not in INGRESS_BACKENDS:
        raise PortmapError(f"ingress backend must be one of {list(INGRESS_BACKENDS)}, got {backend!r}")
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    chosen, infrastructure_state = _select_backend(
        state_dir, role, requested=backend, infrastructure=infrastructure
    )
    record = _load_state(state_dir, role)
    saved_backend = None if record is None else ("container" if record.get("backend") == "container" else "systemd")
    previous = None
    if saved_backend is not None and saved_backend != chosen:
        # An explicit switch of implementations must first release the port
        # through the backend that currently owns it; never the other way.
        previous = (saved_backend, record.get("bind_address"), record.get("port"), record.get("target_port"))
        _stop_dispatched(state_dir, role, saved_backend, use_sudo=use_sudo)
    try:
        if chosen == "container":
            from . import runtime_ingress

            status = runtime_ingress.ensure_ingress_container(
                state_dir, name=role, bind_address=bind, port=port, target_port=target_port,
                infrastructure=infrastructure_state,
            )
        else:
            status = _ensure_systemd(state_dir, role=role, bind=bind, port=port, target_port=target_port, use_sudo=use_sudo)
    except BaseException as exc:
        if previous is not None and all(previous[1:]):
            try:
                _ensure_previous(state_dir, role, previous, use_sudo=use_sudo)
            except Exception as restore:
                raise PortmapError(f"{exc}; restoring the previous ingress failed: {restore}") from exc
        raise
    status["backend"] = chosen
    return status


def _select_backend(
    state_dir: Path,
    role: str,
    *,
    requested: str,
    infrastructure: Mapping[str, Any] | None,
) -> tuple[str, Mapping[str, Any] | None]:
    """Resolve the implementation for this call, reusing owned state first."""
    if requested in ("container", "systemd"):
        state = infrastructure
        if state is None and requested == "container":
            from . import runtime_ingress

            state = runtime_ingress.load_infrastructure_state(state_dir)
        return requested, state
    from . import runtime_ingress

    state = infrastructure if infrastructure is not None else runtime_ingress.load_infrastructure_state(state_dir)
    recorded = state.get("ingress_backend") if isinstance(state, Mapping) else None
    if recorded in ("container", "systemd"):
        return recorded, state
    record = _load_state(state_dir, role)
    if record is not None:
        # The backend that owns this role's published port keeps owning it;
        # legacy records without a "backend" field are systemd installations.
        return ("container" if record.get("backend") == "container" else "systemd"), state
    # Every new publication goes through the container engine on the exact
    # requested address and port. Rootless engines, privileged ports, sudo,
    # systemd availability, and host low-port sysctls never steer the
    # choice: the engine's own create/start/publish failure — or, when the
    # shared infrastructure state is missing, its setup error — is
    # authoritative and never falls back to installing units.
    return "container", state


def _stop_dispatched(state_dir: Path, role: str, backend: str, *, use_sudo: bool) -> None:
    if backend == "container":
        from . import runtime_ingress

        runtime_ingress.stop_ingress_container(state_dir, name=role)
    else:
        _stop_systemd(state_dir, role=role, use_sudo=use_sudo)


def _ensure_previous(state_dir: Path, role: str, previous: tuple[str | int | None, ...], *, use_sudo: bool) -> None:
    backend, bind, port, target_port = previous
    if backend == "container":
        from . import runtime_ingress

        runtime_ingress.ensure_ingress_container(
            state_dir, name=role, bind_address=str(bind), port=int(port), target_port=int(target_port)
        )
    else:
        _ensure_systemd(
            state_dir, role=role, bind=_bind_address(role, str(bind)),
            port=_port(port, "ingress bind port"), target_port=_port(target_port, "ingress target port"),
            use_sudo=use_sudo,
        )


def _ensure_systemd(
    state_dir: Path,
    *,
    role: str,
    bind: str,
    port: int,
    target_port: int,
    use_sudo: bool,
) -> dict[str, Any]:
    """Install (or update) the ingress units and leave the socket bound and verified."""
    _require_systemd()
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    proxyd = discover_proxyd()
    marker = _marker(state_dir, role)
    socket_unit, service_unit = unit_names(state_dir, role)
    socket_path = UNIT_DIR / socket_unit
    service_path = UNIT_DIR / service_unit
    socket_content = _socket_unit_content(marker, role, bind, port)
    service_content = _service_unit_content(marker, role, socket_unit, bind, port, target_port, proxyd)
    installed = _inspect_pair(socket_path, service_path, marker, (socket_content, (service_content,)))
    _refuse_foreign(installed)
    previous = {
        key: (content if state in ("ours", "changed") else None)
        for key, (state, _, content) in installed.items()
    }
    had_units = any(state != "absent" for state, _, _ in installed.values())
    staging_dir = state_dir / "ingress"
    unchanged = all(state == "ours" for state, _, _ in installed.values())
    changed = not unchanged
    notes: list[str] = []
    mutated = False
    try:
        if not changed and _unit_active(socket_unit):
            # Activation probing can raise after a successful install; arm
            # rollback for that failure without marking clean re-checks changed.
            try:
                _activate_or_raise(bind, port, service_unit)
            except BaseException:
                mutated = True
                raise
        else:
            if had_units:
                mutated = True
                _stop_units(socket_unit, service_unit, use_sudo=use_sudo)
            if _address_in_use(bind, port):
                raise PortAllocationError(
                    f"{bind}:{port} is already served by another listener; portmap never stops unrelated services. "
                    "Stop the current owner of that port or choose a different port."
                )
            staging_socket = _stage(socket_content, staging_dir)
            try:
                staging_service = _stage(service_content, staging_dir)
            except BaseException:
                staging_socket.unlink(missing_ok=True)
                raise
            try:
                mutated = True
                _install_unit(staging_socket, socket_path, socket_content, use_sudo=use_sudo)
                _install_unit(staging_service, service_path, service_content, use_sudo=use_sudo)
            finally:
                staging_socket.unlink(missing_ok=True)
                staging_service.unlink(missing_ok=True)
            _daemon_reload(use_sudo)
            _start_socket(socket_unit, bind, port, use_sudo=use_sudo)
            _activate_or_raise(bind, port, service_unit)
        _save_state(
            state_dir,
            role,
            {
                "version": 1,
                "name": role,
                "bind_address": bind,
                "port": port,
                "target_port": target_port,
                "socket_unit": socket_unit,
                "service_unit": service_unit,
                "proxyd": str(proxyd),
                "uid": os.getuid(),
                "state": str(state_dir.resolve()),
            },
        )
    except BaseException as exc:
        if not mutated:
            raise
        if had_units:
            _restore_previous(previous, socket_path, service_path, socket_unit, staging_dir, use_sudo=use_sudo, notes=notes)
        else:
            _remove_everything(
                socket_path, service_path, socket_unit, service_unit, state_dir, role, use_sudo=use_sudo, notes=notes
            )
        if notes:
            raise PortmapError(f"{exc}; ingress rollback also failed: {'; '.join(notes)}") from exc
        raise
    status = _systemd_status(state_dir, role)
    status["changed"] = changed or mutated
    return status


def ingress_status(state_dir: Path, *, name: str) -> dict[str, Any]:
    """Read-only report of the ingress recorded for this state directory and role."""
    role = _role(name)
    record = _load_state(Path(state_dir), role)
    if record is not None and record.get("backend") == "container":
        from . import runtime_ingress

        return runtime_ingress.ingress_status_container(state_dir, name=role)
    return _systemd_status(state_dir, role)


def _systemd_status(state_dir: Path, role: str) -> dict[str, Any]:
    """Read-only report of the systemd socket ingress for this role."""
    status: dict[str, Any] = {
        "name": role,
        "backend": "systemd",
        "socket_unit": None,
        "service_unit": None,
        "installed": False,
        "owned": False,
        "running": False,
        "bind_address": None,
        "port": None,
        "target_port": None,
        "socket_state": None,
        "service_state": None,
        "enabled": False,
        "error": None,
    }
    if not sys.platform.startswith("linux"):
        status["error"] = f"systemd ingress is unsupported on {sys.platform}"
        return status
    state_dir = Path(state_dir)
    socket_unit, service_unit = unit_names(state_dir, role)
    status["socket_unit"] = socket_unit
    status["service_unit"] = service_unit
    marker = _marker(state_dir, role)
    installed = _inspect_pair(UNIT_DIR / socket_unit, UNIT_DIR / service_unit, marker, None)
    reasons = [reason for state, reason, _ in installed.values() if state == "foreign" and reason]
    present = [key for key, (state, _, _) in installed.items() if state != "absent"]
    status["installed"] = bool(present)
    if reasons:
        status["error"] = "; ".join(reasons)
    elif len(present) == 1:
        status["error"] = f"incomplete ingress installation: only the {present[0]} unit is present"
    status["owned"] = installed["socket"][0] == "ours" and installed["service"][0] == "ours"
    params = _parse_params(installed["socket"][2], installed["service"][2])
    saved = _load_state(state_dir, role) or {}
    for key in ("bind_address", "port", "target_port"):
        if key in params:
            status[key] = params[key]
        elif saved.get(key) is not None:
            status[key] = saved[key]
    systemd_available = SYSTEMD_RUN_DIR.is_dir()
    if systemd_available:
        try:
            status["socket_state"] = _show(socket_unit, ("ActiveState",))["ActiveState"] or None
            status["service_state"] = _show(service_unit, ("ActiveState",))["ActiveState"] or None
            status["enabled"] = _is_enabled(socket_unit)
        except PortmapError as exc:
            if status["error"] is None:
                status["error"] = str(exc)
    status["running"] = (
        status["owned"]
        and status["socket_state"] == "active"
        and status["service_state"] == "active"
    )
    return status


def stop_ingress(state_dir: Path, *, name: str, use_sudo: bool = True) -> dict[str, Any]:
    """Stop and remove the ingress recorded as owned by this state directory."""
    role = _role(name)
    record = _load_state(Path(state_dir), role)
    if record is not None and record.get("backend") == "container":
        from . import runtime_ingress

        return runtime_ingress.stop_ingress_container(state_dir, name=role)
    return _stop_systemd(state_dir, role=role, use_sudo=use_sudo)


def _stop_systemd(state_dir: Path, role: str, *, use_sudo: bool) -> dict[str, Any]:
    """Stop and remove only ingress units validated as owned by this state directory."""
    if not sys.platform.startswith("linux"):
        raise PortmapError(f"systemd ingress is unsupported on {sys.platform}; it requires a Linux host running systemd")
    state_dir = Path(state_dir)
    socket_unit, service_unit = unit_names(state_dir, role)
    socket_path = UNIT_DIR / socket_unit
    service_path = UNIT_DIR / service_unit
    marker = _marker(state_dir, role)
    installed = _inspect_pair(socket_path, service_path, marker, None)
    _refuse_foreign(installed)
    files_present = any(state != "absent" for state, _, _ in installed.values())
    socket_state = None
    if SYSTEMD_RUN_DIR.is_dir():
        try:
            socket_state = _show(socket_unit, ("ActiveState",))["ActiveState"] or None
        except PortmapError:
            socket_state = None
    result: dict[str, Any] = {
        "name": role,
        "socket_unit": socket_unit,
        "service_unit": service_unit,
        "installed": files_present or socket_state == "active",
        "running": False,
        "stopped": True,
        "removed": False,
    }
    if not files_present and socket_state != "active":
        _clear_state(state_dir, role)
        return result
    _require_systemd()
    _stop_units(socket_unit, service_unit, use_sudo=use_sudo)
    _privileged([_systemctl_path(), "reset-failed", service_unit, socket_unit], use_sudo=use_sudo)
    _remove_unit_files((socket_path, service_path), use_sudo=use_sudo)
    _daemon_reload(use_sudo)
    _clear_state(state_dir, role)
    result["installed"] = False
    result["removed"] = True
    return result
