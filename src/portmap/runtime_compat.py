"""Verify (and if needed repair) the user's container runtime for portmap.

``prepare_runtime`` is the runtime compatibility transaction behind the
standard entries: ``portmap setup``, ``portmap run`` and ``portmap up`` all
reach it through :func:`portmap.infrastructure.sync_infrastructure` before any
portmap-owned container or network exists; ``python -m
portmap.runtime_compat`` re-runs the identical check purely as an explicit
diagnostic. For the one known incompatible target profile -- an ordinary
user's rootless Podman whose Netavark/Aardvark network helpers predate the
stable-bridge fix -- it
installs the official, SHA256-pinned 1.9.0 helper pair into a versioned
user-data directory, writes an owned ``containers.conf.d`` drop-in that only
prepends that directory to the preserved helper search paths, and restarts
exactly the verified idle ``podman system service`` process that owns the
selected socket, preserving its command, environment and working directory.
When the selected service runs with a startup-generated ``CONTAINERS_CONF``
base configuration (as on Coder-style workspaces), that file is never
modified: the replacement service additionally receives the owned override
via ``CONTAINERS_CONF_OVERRIDE``, which podman's configuration loader always
merges last. After such a repair, the very next standard entry re-verifies
the live service, so a workspace rebuild that swaps the API service back to
the distro helpers is repaired again by rerunning setup -- never bypassed.
Config files an earlier deployment of this very transaction already wrote
are recognized as owned (header plus the portmap helper directory leading
the search list) and reused, or refreshed in place when the surrounding
configuration changed; an already-compatible service is never restarted.

Everything runs as the ordinary user. Distro binaries, global configuration,
capabilities, sysctls, the pause process, containers and cached images are
never touched; legacy resources left by older portmap versions are invisible
to this transaction and stay owned by the regular legacy migration.
Unsupported situations (rootful engines, other platforms, foreign or
conflicting configuration, running workloads, systemd-supervised sockets,
unexpected service processes) fail explicitly before any mutation, and any
failure after the first owned mutation is rolled back. On hosts without
systemd the replacement service is started detached (its own session), so
both foreground ``portmap run`` and background ``portmap setup`` keep it
alive past their own lifetime; no engine is ever installed or started here --
a missing runtime API still fails setup explicitly.

Limitations (intentional, per the deployment contract):
- Only the rootless linux/amd64 Podman with a Netavark backend older than
  1.9 is repaired; newer helpers and Docker backends are already compatible.
- Only an exact ``podman system service --time=0 unix://<selected socket>``
  API process is restarted; any other socket peer is refused, never killed.
- The helper selection is only proven once the restarted service's own
  native info reports the fixed helpers.
"""
from __future__ import annotations

import errno
import gzip
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from .container_runtime import ContainerRuntime
from .errors import PortmapError
from .settings import socket_alive

__all__ = ["prepare_runtime"]

HELPER_VERSION = "1.9.0"
HELPER_MIN_VERSION = (1, 9)
NETAVARK_NAME = "netavark"
AARDVARK_NAME = "aardvark-dns"
NETAVARK_URL = f"https://github.com/containers/{NETAVARK_NAME}/releases/download/v{HELPER_VERSION}/{NETAVARK_NAME}.gz"
NETAVARK_SHA256 = "e9ddebbf1aa6f56f455c0dbd22d9cf6cae5353eff295a8ee1db18c5284963f26"
AARDVARK_URL = f"https://github.com/containers/{AARDVARK_NAME}/releases/download/v{HELPER_VERSION}/{AARDVARK_NAME}.gz"
AARDVARK_SHA256 = "d0861cf20ced7012c1de9b7a7e12f28f2dc0dbfd83630d7210ee2fcd93149822"

# Owned drop-in: only ever prepends the user helper directory to the
# preserved helper search paths; the header plus that leading entry is the
# ownership marker recognized across product versions, and files carrying it
# may be refreshed (never foreign ones).
DROP_IN_NAME = "portmap-network-helpers.conf"
DROP_IN_HEADER = "# Owned by the portmap deployment: fixed stable-bridge network helpers."
# The selected API service is restarted with CONTAINERS_CONF_OVERRIDE pointing
# at this owned file so the fixed helpers win even when the service runs with
# a startup-generated CONTAINERS_CONF base configuration (never rewritten).
OVERRIDE_NAME = "containers-helper-override.conf"

# Distro helper directories that must stay searchable next to the user one.
DISTRO_HELPER_DIRS = ("/usr/local/libexec/podman", "/usr/libexec/podman", "/usr/local/lib/podman", "/usr/lib/podman")
SYSTEM_CONTAINERS_CONF = ("/usr/share/containers/containers.conf", "/etc/containers/containers.conf")
SYSTEM_CONTAINERS_DROP_DIR = "/etc/containers/containers.conf.d"

RECEIPT_NAME = "runtime-compat.json"
API_LOG_NAME = "podman-api.log"
NATIVE_INFO_PATH = "/v4.0.0/libpod/info"

SO_PEERCRED = 17  # linux; not exposed by the socket module
_TERM_WAIT_SECONDS = 15.0
_KILL_WAIT_SECONDS = 10.0
_READY_WAIT_SECONDS = 60.0
_DOWNLOAD_TIMEOUT = 60.0
_HELPER_PROBE_TIMEOUT = 20.0


def prepare_runtime(runtime: ContainerRuntime, state_dir: Path) -> dict:
    """Verify (and if needed repair) the connected container runtime.

    Returns ``{"backend", "socket_path", "rootless", "changed", "repair",
    "netavark", "aardvark", "receipt"}``. ``repair`` is ``None`` unless the
    known rootless-Podman Netavark fix was applied this call. Docker (or any
    Docker-compatible engine) needs no repair. Failures raise
    :class:`portmap.errors.PortmapError`; every owned mutation is rolled back
    first.
    """
    state_dir = Path(state_dir)
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise PortmapError(f"cannot create the runtime state directory {state_dir}: {exc}") from exc

    result = {
        "backend": runtime.backend,
        "socket_path": runtime.socket_path,
        "rootless": runtime.rootless,
        "changed": False,
        "repair": None,
        "netavark": None,
        "aardvark": None,
        "receipt": str(state_dir / RECEIPT_NAME),
    }
    if runtime.backend != "podman":
        # Docker (or a Docker-compatible engine) needs no repair here.
        return _finish(result, state_dir)

    native = runtime.request("GET", NATIVE_INFO_PATH)
    host = _native_host(native)
    _require_netavark_backend(host)
    selection = _helper_selection(host)
    result["netavark"] = {"path": selection["netavark_path"], "version": selection["netavark_version"]}
    result["aardvark"] = {"path": selection["aardvark_path"], "version": selection["aardvark_version"]}
    if selection["netavark_ok"]:
        # Never downgrade an already-fixed Netavark to repair an unrelated helper.
        return _finish(result, state_dir)

    _require_rootless(host)
    repair = _repair_runtime(runtime, selection, host, state_dir)
    result["changed"] = True
    result["repair"] = repair
    result["netavark"] = {"path": repair["netavark"]["path"], "version": repair["netavark"]["version"]}
    result["aardvark"] = {"path": repair["aardvark"]["path"], "version": repair["aardvark"]["version"]}
    return _finish(result, state_dir)


def _fail(message: str):
    raise PortmapError(message)


# --------------------------------------------------------------------------
# Classification


def _native_host(native) -> dict:
    if not isinstance(native, dict) or not isinstance(native.get("host"), dict):
        _fail("podman native info returned an invalid host section")
    return native["host"]


def _require_netavark_backend(host: dict) -> None:
    if host.get("networkBackend") != "netavark":
        _fail(f"unsupported podman network backend {host.get('networkBackend')!r}; only the netavark stable-bridge fix is known")


def _require_rootless(host: dict) -> None:
    rootless = host.get("security", {}).get("rootless")
    if rootless is not True:
        _fail("rootful podman cannot be repaired from user configuration; an administrator must update the network helpers")


def _parse_helper_version(text, name: str) -> tuple[int, ...]:
    match = re.search(rf"{re.escape(name)}\s+v?(\d+(?:\.\d+)*)", text or "", re.IGNORECASE)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _helper_selection(host: dict) -> dict:
    info = host.get("networkBackendInfo")
    if not isinstance(info, dict):
        _fail("podman native info did not report netavark helper details")
    netavark_path = info.get("path")
    netavark_version = info.get("version")
    dns = info.get("dns") if isinstance(info.get("dns"), dict) else {}
    aardvark_path = dns.get("path")
    aardvark_version = dns.get("version")
    if not isinstance(netavark_path, str) or not isinstance(netavark_version, str):
        _fail("podman native info returned invalid netavark helper details")
    if not isinstance(aardvark_path, str) or not isinstance(aardvark_version, str):
        _fail("podman native info returned invalid aardvark-dns helper details")
    return {
        "netavark_path": netavark_path,
        "netavark_version": netavark_version,
        "netavark_ok": _parse_helper_version(netavark_version, NETAVARK_NAME) >= HELPER_MIN_VERSION,
        "aardvark_path": aardvark_path,
        "aardvark_version": aardvark_version,
        "aardvark_ok": _parse_helper_version(aardvark_version, AARDVARK_NAME) >= HELPER_MIN_VERSION,
    }


def _require_supported_repair_platform(host: dict) -> None:
    if os.uname().sysname != "Linux" or host.get("arch") != "amd64":
        _fail(f"no pinned network helpers for {host.get('os')}/{host.get('arch')}; refusing a user-level netavark repair")


# --------------------------------------------------------------------------
# Repair transaction


def _repair_runtime(runtime, selection: dict, host: dict, state_dir: Path) -> dict:
    _require_supported_repair_platform(host)
    _require_idle(runtime)
    identity = _require_service_identity(runtime.socket_path)
    plan = _plan_config(identity.environ)
    mutations: dict = {
        "drop_in_created": False,
        "drop_in_updated": False,
        "drop_in_previous": None,
        "drop_dir_created": False,
        "containers_dir_created": False,
        "override_created": False,
        "override_updated": False,
        "override_previous": None,
        "helpers_created": [],
        "helpers_dir_created": False,
        "original_terminated": False,
        "replacement_pid": None,
        "replacement_token": None,
    }
    try:
        helpers = _install_helpers(mutations)
        _write_override(plan, mutations)
        _write_drop_in(plan, mutations)
        _require_idle(runtime)
        replacement = _restart_service(runtime, identity, state_dir, mutations, plan["override_path"])
        validated = _validate_fixed_selection(runtime, helpers["dir"])
    except Exception as exc:
        rollback_notes = _rollback(runtime, identity, plan, mutations, state_dir)
        detail = str(exc) or exc.__class__.__name__
        suffix = f"; rolled back ({'; '.join(rollback_notes)})" if rollback_notes else "; rolled back"
        if isinstance(exc, PortmapError):
            raise PortmapError(f"{detail}{suffix}") from exc
        raise PortmapError(f"netavark repair failed: {detail}{suffix}") from exc
    return {
        "reason": (
            f"rootless podman selected netavark {selection['netavark_version'] or 'unknown'} / "
            f"aardvark-dns {selection['aardvark_version'] or 'unknown'}; installed the official "
            f"v{HELPER_VERSION} stable-bridge helpers"
        ),
        "helpers_dir": str(helpers["dir"]),
        "netavark": {"path": str(helpers["netavark"]), "version": validated["netavark_version"], "sha256": helpers["netavark_sha256"]},
        "aardvark": {"path": str(helpers["aardvark"]), "version": validated["aardvark_version"], "sha256": helpers["aardvark_sha256"]},
        "drop_in": str(plan["drop_in"]),
        "config_override": {
            "path": str(plan["override_path"]),
            "env": "CONTAINERS_CONF_OVERRIDE",
            "base_config": plan["base_config"],
        },
        "helper_binaries_dir": plan["helper_binaries_dir"],
        "api_restart": {
            "socket_path": runtime.socket_path,
            "previous_pid": identity.pid,
            "replacement_pid": replacement["pid"],
            "replacement_starttime": replacement["starttime"],
            "binary": replacement["exe"],
            "command": replacement["argv"],
        },
    }


def _require_idle(runtime) -> None:
    containers = runtime.request("GET", "/containers/json?all=1")
    if not isinstance(containers, list):
        _fail("the container runtime returned an invalid container inventory")
    running = [item.get("Id") or item.get("Names") or "?" for item in containers if isinstance(item, dict) and item.get("State") == "running"]
    if running:
        _fail(f"refusing to restart the podman API service while workloads are running: {', '.join(map(str, running[:5]))}")


# --------------------------------------------------------------------------
# Service identity


def _socket_peer(socket_path: str) -> tuple[int, int, int]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(5.0)
            probe.connect(socket_path)
            credentials = probe.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, 12)
    except OSError as exc:
        _fail(f"cannot reach the runtime socket {socket_path} to identify its service process: {exc}")
    pid, uid, gid = struct.unpack("iII", credentials)
    if pid <= 0:
        _fail(f"the runtime socket {socket_path} did not report a peer process")
    return pid, uid, gid


def _proc_stat(pid: int) -> tuple[str, int]:
    text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    close = text.rfind(")")
    if close == -1:
        _fail(f"cannot parse /proc/{pid}/stat for the runtime service")
    fields = text[close + 1 :].split()
    if len(fields) < 20:
        _fail(f"cannot parse /proc/{pid}/stat for the runtime service")
    return fields[0], int(fields[19])  # state, starttime (field 22)


def _proc_identity(pid: int) -> SimpleNamespace:
    try:
        state, starttime = _proc_stat(pid)
    except (FileNotFoundError, ProcessLookupError):
        _fail(f"the runtime socket peer process {pid} disappeared")
    if state == "Z":
        _fail(f"the runtime socket peer process {pid} is already a zombie")
    status = {}
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                status[key] = value.split()
        argv = [part.decode("utf-8", "replace") for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
        environ = {}
        for part in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
            if b"=" in part:
                key, value = part.split(b"=", 1)
                environ[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    except OSError as exc:
        _fail(f"cannot read /proc/{pid} details for the runtime service: {exc}")
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = None
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError as exc:
        _fail(f"cannot preserve the runtime service working directory: {exc}")
    if not Path(cwd).is_dir():
        _fail(f"runtime service working directory is unavailable; leaving the API running: {cwd}")
    return SimpleNamespace(pid=pid, uid=int(status["Uid"][0]), argv=argv, exe=exe, cwd=cwd, environ=environ, starttime=starttime)


def _expected_service_argv(socket_path: str) -> list[str]:
    return ["podman", "system", "service", "--time=0", f"unix://{socket_path}"]


def _service_argv_start(argv: list[str], socket_path: str) -> int | None:
    """Index of the podman service command inside a verified service argv.

    Two exact shapes qualify: the plain ELF invocation
    ``podman system service --time=0 unix://<socket>``, and the same command
    launched through an interpreter (shebang), where the kernel reports the
    interpreter as argv[0]. Anything else is an unknown process.
    """
    expected_tail = ("system", "service", "--time=0", f"unix://{socket_path}")
    if len(argv) == 5 and Path(argv[0]).name == "podman" and tuple(argv[1:]) == expected_tail:
        return 0
    if len(argv) == 6 and Path(argv[1]).name == "podman" and tuple(argv[2:]) == expected_tail:
        return 1
    return None



def _argv_is_service(argv: list[str], socket_path: str) -> bool:
    return _service_argv_start(argv, socket_path) is not None


def _resolve_service_binary(argv0: str, environ: dict[str, str]) -> Path | None:
    candidate = Path(argv0)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    search_path = environ.get("PATH") or os.environ.get("PATH") or ""
    for entry in search_path.split(os.pathsep):
        if not entry:
            continue
        resolved = Path(entry) / candidate
        if resolved.is_file():
            return resolved
    return None


def _require_service_identity(socket_path: str) -> SimpleNamespace:
    if shutil.which("systemctl") is not None:
        _fail("systemctl is present, so the podman socket may be systemd-supervised; refusing an unmanaged API restart")
    peer_pid, peer_uid, _peer_gid = _socket_peer(socket_path)
    if peer_uid != os.getuid():
        _fail(f"the runtime socket peer uid {peer_uid} does not belong to the deployment user {os.getuid()}")
    identity = _proc_identity(peer_pid)
    if identity.uid != peer_uid:
        _fail(f"the runtime socket peer changed identity: reported uid {identity.uid}, socket uid {peer_uid}")
    start = _service_argv_start(identity.argv, socket_path)
    if start is None:
        _fail(
            "the runtime socket peer is not the expected idle user API service "
            f"{_expected_service_argv(socket_path)} (got {identity.argv}); refusing to restart it"
        )
    if "CONTAINERS_CONF_OVERRIDE" in identity.environ:
        # CONTAINERS_CONF itself is fine: the owned override (which always
        # wins in podman's loader) is added without touching that base file.
        _fail("the podman API service already runs with CONTAINERS_CONF_OVERRIDE; refusing to replace a foreign config override")
    identity.service_argv = identity.argv[start:]
    binary = _resolve_service_binary(identity.service_argv[0], identity.environ)
    if binary is None or not os.access(binary, os.X_OK):
        _fail(f"cannot resolve the podman API service binary from {identity.service_argv[0]!r}; refusing to restart it")
    identity.binary = binary
    return identity


def _identity_token(identity: SimpleNamespace) -> tuple:
    return (identity.pid, identity.starttime, tuple(identity.argv), identity.exe)


def _current_token(pid: int) -> tuple | None:
    try:
        state, starttime = _proc_stat(pid)
    except (FileNotFoundError, ProcessLookupError):
        return None
    if state == "Z":
        return None
    argv = [part.decode("utf-8", "replace") for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = None
    return (pid, starttime, tuple(argv), exe)


def _ensure_same_process(identity: SimpleNamespace) -> None:
    if _current_token(identity.pid) != _identity_token(identity):
        _fail(f"the podman API service process {identity.pid} changed identity; refusing to continue")


# --------------------------------------------------------------------------
# Owned drop-in


def _user_containers_dir() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "containers"


def _read_toml(path: Path):
    import tomllib

    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _fail(f"cannot parse existing container configuration {path}: {exc}")


def _helper_dirs_from_file(path: Path) -> list[str] | None:
    payload = _read_toml(path)
    if payload is None:
        return None
    engine = payload.get("engine")
    if not isinstance(engine, dict) or "helper_binaries_dir" not in engine:
        return None
    value = engine["helper_binaries_dir"]
    if not isinstance(value, list) or not all(isinstance(entry, str) and entry for entry in value):
        _fail(f"unsupported engine.helper_binaries_dir in {path}")
    return value


def _plan_config(service_environ: dict[str, str]) -> dict:
    containers_dir = _user_containers_dir()
    user_conf = containers_dir / "containers.conf"
    drop_dir = containers_dir / "containers.conf.d"
    drop_in = drop_dir / DROP_IN_NAME
    override_path = _helpers_dir().parent / OVERRIDE_NAME

    # Any user-level helper search path is custom configuration we must not
    # override or silently disable: refuse before mutating anything.
    user_files = [user_conf] if user_conf.exists() else []
    user_files += sorted(drop_dir.glob("*.conf")) if drop_dir.exists() else []
    for path in user_files:
        if path == drop_in:
            continue
        if _helper_dirs_from_file(path) is not None:
            _fail(f"{path} already configures engine.helper_binaries_dir; refusing to override custom container configuration")

    # With CONTAINERS_CONF set (podman's loader reads only that file), the
    # startup-generated base config is preserved verbatim and its helper
    # entries, if any, are kept in the search list; the owned override file is
    # passed to the replacement service via CONTAINERS_CONF_OVERRIDE, which the
    # loader always merges last. A base config that vanished with a rebuilt
    # workspace (/tmp) simply contributes no entries.
    base_config = service_environ.get("CONTAINERS_CONF") or None
    preserved: list[str] = []
    service_files = [Path(base_config)] if base_config else []
    if not base_config:
        service_files = [Path(name) for name in SYSTEM_CONTAINERS_CONF if Path(name).exists()]
        service_files += sorted(Path(SYSTEM_CONTAINERS_DROP_DIR).glob("*.conf")) if Path(SYSTEM_CONTAINERS_DROP_DIR).exists() else []
    for path in service_files:
        entries = _helper_dirs_from_file(path)
        if entries is not None:
            preserved = entries  # later files win, like podman's own loading order
    search = [str(_helpers_dir()), *preserved]
    for distro_dir in DISTRO_HELPER_DIRS:
        if distro_dir not in search:
            search.append(distro_dir)
    content = f"{DROP_IN_HEADER}\n[engine]\nhelper_binaries_dir = {json.dumps(search)}\n"

    drop_in_state = _owned_config_state(drop_in, "drop-in")
    override_state = _owned_config_state(override_path, "config override")
    return {
        "containers_dir": containers_dir,
        "drop_dir": drop_dir,
        "drop_in": drop_in,
        "content": content,
        "drop_in_current": drop_in_state.content,
        "override_current": override_state.content,
        "override_path": override_path,
        "helper_binaries_dir": search,
        "base_config": base_config,
    }


def _owned_config_state(path: Path, kind: str) -> SimpleNamespace:
    """Classify an owned-config candidate file.

    A file this product (or the already-verified predecessor deployment of
    the same transaction) wrote is recognized by the owned header plus the
    portmap helper directory leading the search list; such a file may be
    refreshed in place when the surrounding configuration changed (for
    example a rebuilt workspace whose base config lost preserved entries).
    Anything else that exists is foreign and is never overwritten, so a
    legacy owned layout can never collide into a permanent failure.
    """
    if not path.exists():
        return SimpleNamespace(content=None)
    if path.is_symlink():
        _fail(f"owned {kind} {path} is a symlink; refusing to use it")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot read existing {kind} {path}: {exc}")
    if not content.startswith(DROP_IN_HEADER + "\n"):
        _fail(f"{kind} {path} exists with unexpected content; refusing to overwrite it")
    payload = _read_toml(path)
    if payload is None:
        _fail(f"cannot read existing {kind} {path}")
    entries = payload.get("engine", {}).get("helper_binaries_dir") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries or entries[0] != str(_helpers_dir()):
        _fail(f"{kind} {path} does not lead its helper search with the portmap helpers; refusing to overwrite it")
    return SimpleNamespace(content=content)


def _write_drop_in(plan: dict, mutations: dict) -> None:
    current = plan["drop_in_current"]
    if current == plan["content"]:
        return
    if current is None:
        mutations["containers_dir_created"] = not plan["containers_dir"].exists()
        mutations["drop_dir_created"] = not plan["drop_dir"].exists()
    plan["drop_dir"].mkdir(mode=0o755, parents=True, exist_ok=True)
    _atomic_write(plan["drop_in"], plan["content"])
    if current is None:
        mutations["drop_in_created"] = True
    else:
        mutations["drop_in_previous"] = current
        mutations["drop_in_updated"] = True


def _write_override(plan: dict, mutations: dict) -> None:
    current = plan["override_current"]
    if current == plan["content"]:
        return
    plan["override_path"].parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    _atomic_write(plan["override_path"], plan["content"])
    if current is None:
        mutations["override_created"] = True
    else:
        mutations["override_previous"] = current
        mutations["override_updated"] = True


def _remove_override(plan: dict, mutations: dict) -> str | None:
    path = plan["override_path"]
    if not path.exists():
        return None
    if path.is_symlink() or path.read_text(encoding="utf-8") != plan["content"]:
        return f"retained externally changed {path}"
    if mutations.get("override_created"):
        path.unlink()
        return f"removed {path}"
    if mutations.get("override_updated"):
        _atomic_write(path, mutations["override_previous"])
        return f"restored previous {path}"
    return None


def _atomic_write(path: Path, content: str) -> None:
    staged = path.parent / f".{path.name}.{os.getpid()}.tmp"
    with staged.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(staged, 0o644)
    os.replace(staged, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_drop_in(plan: dict, mutations: dict) -> str | None:
    removed = None
    if plan["drop_in"].exists():
        path = plan["drop_in"]
        if path.is_symlink() or path.read_text(encoding="utf-8") != plan["content"]:
            removed = f"retained externally changed {path}"
        elif mutations["drop_in_created"]:
            path.unlink()
            removed = f"removed {path}"
        elif mutations.get("drop_in_updated"):
            _atomic_write(path, mutations["drop_in_previous"])
            removed = f"restored previous {path}"
    # Only directories this run created (and left empty) are removed again.
    if mutations["drop_dir_created"] and plan["drop_dir"].is_dir() and not any(plan["drop_dir"].iterdir()):
        plan["drop_dir"].rmdir()
    if mutations["containers_dir_created"] and plan["containers_dir"].is_dir() and not any(plan["containers_dir"].iterdir()):
        plan["containers_dir"].rmdir()
    return removed


# --------------------------------------------------------------------------
# Helper installation


def _helpers_dir() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "portmap-deploy" / "podman-helpers" / HELPER_VERSION


def _fetch(url: str) -> bytes:
    """Download helper bytes. Module-level so tests can stub the network."""
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as response:
        return response.read()


def _helper_reports_fixed_version(path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(path), "--version"], env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
            capture_output=True, text=True, timeout=_HELPER_PROBE_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0 and HELPER_VERSION in output and path.name in output.lower()


def _install_helper(name: str, url: str, sha256: str, helpers_dir: Path, mutations: dict) -> tuple[Path, str]:
    target = helpers_dir / name
    if target.is_file() and os.access(target, os.X_OK) and _helper_reports_fixed_version(target):
        return target, _file_sha256(target)
    try:
        blob = _fetch(url)
    except OSError as exc:
        _fail(f"cannot download the pinned {name} helper from {url}: {exc}")
    digest = hashlib.sha256(blob).hexdigest()
    if not hmac.compare_digest(digest, sha256.lower()):
        _fail(f"the downloaded {name} asset {url} does not match its pinned SHA-256 {sha256}")
    try:
        payload = gzip.decompress(blob)
    except (OSError, EOFError) as exc:
        _fail(f"the pinned {name} asset {url} is not a valid gzip archive: {exc}")
    if not payload:
        _fail(f"the pinned {name} asset {url} decompressed to an empty binary")
    staged = helpers_dir / f".{name}.{os.getpid()}.tmp"
    with staged.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(staged, 0o755)
    os.replace(staged, target)
    if target not in mutations["helpers_created"]:
        mutations["helpers_created"].append(target)
    if not _helper_reports_fixed_version(target):
        _fail(f"the installed {name} helper {target} does not report version {HELPER_VERSION}")
    return target, _file_sha256(target)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_helpers(mutations: dict) -> dict:
    helpers_dir = _helpers_dir()
    if not helpers_dir.is_dir():
        helpers_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        mutations["helpers_dir_created"] = True
    netavark, netavark_sha = _install_helper(NETAVARK_NAME, NETAVARK_URL, NETAVARK_SHA256, helpers_dir, mutations)
    aardvark, aardvark_sha = _install_helper(AARDVARK_NAME, AARDVARK_URL, AARDVARK_SHA256, helpers_dir, mutations)
    return {"dir": helpers_dir, "netavark": netavark, "aardvark": aardvark, "netavark_sha256": netavark_sha, "aardvark_sha256": aardvark_sha}


def _remove_helpers(mutations: dict) -> str | None:
    if not mutations["helpers_created"] and not mutations["helpers_dir_created"]:
        return None
    for path in mutations["helpers_created"]:
        if path.exists():
            path.unlink()
    if mutations["helpers_dir_created"]:
        # Everything under the directory this run created is ours.
        helpers_dir = _helpers_dir()
        for ancestor in (helpers_dir, helpers_dir.parent, helpers_dir.parent.parent):
            try:
                ancestor.rmdir()
            except OSError:
                break
    if not mutations["helpers_created"]:
        return None
    return f"removed {len(mutations['helpers_created'])} staged helper binary(s)"


# --------------------------------------------------------------------------
# API service restart


def _wait_for_exit(pid: int, timeout: float, token: tuple | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _current_token(pid)
        if current is None or (token is not None and current != token):
            return True
        time.sleep(0.1)
    current = _current_token(pid)
    return current is None or (token is not None and current != token)


def _terminate(pid: int, token: tuple) -> None:
    descriptor = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                descriptor = os.pidfd_open(pid)
            except OSError as exc:
                if exc.errno not in (errno.ENOSYS, errno.EINVAL):
                    raise
        for sig, timeout in ((signal.SIGTERM, _TERM_WAIT_SECONDS), (signal.SIGKILL, _KILL_WAIT_SECONDS)):
            current = _current_token(pid)
            if current is None:
                return
            if current != token:
                _fail(f"process {pid} changed identity; refusing to signal it")
            if descriptor is not None:
                signal.pidfd_send_signal(descriptor, sig)
            else:
                # Match the core's fallback on Python builds without pidfd APIs.
                os.kill(pid, sig)
            if _wait_for_exit(pid, timeout, token):
                return
        _fail(f"the podman API service {pid} did not exit after SIGKILL")
    except ProcessLookupError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _clear_stale_socket(socket_path: str, *, former_pid: int) -> None:
    path = Path(socket_path)
    deadline = time.monotonic() + _TERM_WAIT_SECONDS
    while True:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            _fail(f"runtime socket path changed ownership or type: {socket_path}")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1.0)
                probe.connect(socket_path)
                peer_pid, peer_uid, _ = struct.unpack("iII", probe.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, 12))
        except FileNotFoundError:
            return
        except ConnectionRefusedError:
            try:
                current = path.lstat()
            except FileNotFoundError:
                return
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                _fail(f"runtime socket changed while waiting for shutdown: {socket_path}")
            path.unlink()
            return
        if peer_pid != former_pid or peer_uid != os.getuid():
            _fail(f"another listener already owns the runtime socket {socket_path}")
        # A terminating thread group may retain the listener after its leader
        # exits. Wait only for that verified service, never for another owner.
        if time.monotonic() >= deadline:
            _fail(f"the stopped API service still holds {socket_path}")
        time.sleep(0.05)


def _spawn_service(argv: list[str], environ: dict[str, str], cwd: str, state_dir: Path, log_name: str) -> subprocess.Popen:
    log_path = state_dir / log_name
    with log_path.open("ab", 0) as log:
        return subprocess.Popen(
            argv, env=dict(environ), cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )


def _wait_ready(runtime, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + _READY_WAIT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _fail(f"the replacement podman API service exited with status {process.returncode}")
        if socket_alive(runtime.socket_path):
            try:
                runtime.request("GET", "/info")  # JSON body: proves real service responses
                return
            except PortmapError:
                pass
        time.sleep(0.2)
    _fail(f"the replacement podman API service did not serve {runtime.socket_path} within {_READY_WAIT_SECONDS:.0f}s")


def _restart_service(runtime, identity: SimpleNamespace, state_dir: Path, mutations: dict, override_path: Path) -> dict:
    _ensure_same_process(identity)
    if not Path(identity.cwd).is_dir():
        _fail(f"runtime service working directory disappeared; leaving the API running: {identity.cwd}")
    token = _identity_token(identity)
    argv = [str(identity.binary), *identity.service_argv[1:]]
    _terminate(identity.pid, token)
    mutations["original_terminated"] = True
    _clear_stale_socket(runtime.socket_path, former_pid=identity.pid)
    # Same command, environment and cwd; the only addition is the owned
    # CONTAINERS_CONF_OVERRIDE, which podman's loader always merges last.
    environment = dict(identity.environ)
    environment["CONTAINERS_CONF_OVERRIDE"] = str(override_path)
    process = _spawn_service(argv, environment, identity.cwd, state_dir, API_LOG_NAME)
    mutations["replacement_pid"] = process.pid
    mutations["replacement_token"] = _current_token(process.pid)
    _wait_ready(runtime, process)
    replacement = _proc_identity(process.pid)
    if not _argv_is_service(replacement.argv, runtime.socket_path):
        _fail(f"the replacement API service has an unexpected command {replacement.argv}")
    mutations["replacement_token"] = _identity_token(replacement)
    return {"pid": replacement.pid, "starttime": replacement.starttime, "argv": replacement.argv, "exe": replacement.exe}


def _validate_fixed_selection(runtime, helpers_dir: Path) -> dict:
    native = runtime.request("GET", NATIVE_INFO_PATH)
    host = _native_host(native)
    if host.get("security", {}).get("rootless") is not True:
        _fail("the restarted podman service is not rootless anymore; refusing the repaired runtime")
    info = host.get("networkBackendInfo")
    if not isinstance(info, dict):
        _fail("the restarted podman service did not report netavark helper details")
    expected_netavark = f"{NETAVARK_NAME} {HELPER_VERSION}"
    expected_aardvark = f"{AARDVARK_NAME} {HELPER_VERSION}"
    if info.get("path") != str(helpers_dir / NETAVARK_NAME) or info.get("version") != expected_netavark:
        _fail(f"the restarted podman service still selects {info.get('path')} ({info.get('version')}) instead of the fixed {expected_netavark}")
    dns = info.get("dns") if isinstance(info.get("dns"), dict) else {}
    if dns.get("path") != str(helpers_dir / AARDVARK_NAME) or dns.get("version") != expected_aardvark:
        _fail(f"the restarted podman service still selects {dns.get('path')} ({dns.get('version')}) instead of the fixed {expected_aardvark}")
    return {"netavark_version": expected_netavark, "aardvark_version": expected_aardvark}


# --------------------------------------------------------------------------
# Rollback


def _rollback(runtime, identity: SimpleNamespace, plan: dict, mutations: dict, state_dir: Path) -> list[str]:
    """Undo every owned mutation; best-effort, never raises."""
    notes: list[str] = []
    try:
        note = _remove_override(plan, mutations)
        if note:
            notes.append(note)
    except OSError as exc:
        notes.append(f"failed removing {plan['override_path']}: {exc}")
    try:
        note = _remove_drop_in(plan, mutations)
        if note:
            notes.append(note)
    except OSError as exc:
        notes.append(f"failed removing {plan['drop_in']}: {exc}")
    pid = mutations.get("replacement_pid")
    if pid is not None:
        token = mutations.get("replacement_token")
        try:
            if token is not None and _current_token(pid) == token:
                _terminate(pid, token)
                notes.append(f"stopped owned replacement API service {pid}")
            elif _current_token(pid) is not None:
                notes.append(f"retained replacement PID {pid}: its ownership is no longer provable")
        except (OSError, PortmapError) as exc:
            notes.append(f"failed stopping owned replacement API service {pid}: {exc}")
    try:
        note = _remove_helpers(mutations)
        if note:
            notes.append(note)
    except OSError as exc:
        notes.append(f"failed removing staged helpers: {exc}")
    if mutations.get("original_terminated"):
        try:
            _clear_stale_socket(runtime.socket_path, former_pid=mutations.get("replacement_pid") or identity.pid)
            argv = [str(identity.binary), *identity.service_argv[1:]]
            process = _spawn_service(argv, identity.environ, identity.cwd, state_dir, API_LOG_NAME)
            deadline = time.monotonic() + _READY_WAIT_SECONDS
            while time.monotonic() < deadline and not socket_alive(runtime.socket_path):
                if process.poll() is not None:
                    raise RuntimeError(f"restored service exited with status {process.returncode}")
                time.sleep(0.2)
            if not socket_alive(runtime.socket_path):
                raise RuntimeError("restored service did not listen again")
            notes.append(f"restored the previous API service on {runtime.socket_path} (pid {process.pid})")
        except Exception as exc:  # noqa: BLE001 - rollback must keep going
            notes.append(
                f"FAILED to restore the previous API service {identity.argv}: {exc}; restart it manually with the "
                "same command as the deployment user"
            )
    return notes


# --------------------------------------------------------------------------
# Receipt


def _finish(result: dict, state_dir: Path) -> dict:
    receipt = dict(result)
    receipt["at"] = time.time()
    receipt["uid"] = os.getuid()
    staged = state_dir / f".{RECEIPT_NAME}.{os.getpid()}.tmp"
    with staged.open("w", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    os.chmod(staged, 0o600)
    os.replace(staged, state_dir / RECEIPT_NAME)
    return result




