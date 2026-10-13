"""State paths, child environments and host-worker login-session lifetime.

This module must stay free of server-runtime imports (settings, planner,
agent, catalog): the unified frozen portmap executable imports it first."""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping

from .errors import PortmapError


# Loader search variables a PyInstaller application may add for itself. Native
# system children (ssh, ip, resolvectl, container CLIs) must not inherit
# entries that point into the portmap bundle.
_LIBRARY_PATH_VARIABLES = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH")



def frozen() -> bool:
    """True when running from the frozen unified portmap executable."""
    return bool(getattr(sys, "frozen", False))


def client_state_dir(environ: Mapping[str, str] | None = None, *, shared_state_dir: Path | None = None) -> Path:
    """Client-role state directory under the shared portmap state root.

    PORTMAP_CLIENT_STATE_DIR stays a role-specific isolation override; the
    normal default is ${PORTMAP_STATE_DIR or XDG_STATE_HOME/portmap}/client so
    one unified command shares a single state root across roles.
    """
    env = os.environ if environ is None else environ
    isolated = env.get("PORTMAP_CLIENT_STATE_DIR")
    if isolated:
        return Path(isolated).expanduser().resolve()
    root = shared_state_dir if shared_state_dir is not None else env.get("PORTMAP_STATE_DIR")
    if root:
        return (Path(root).expanduser() / "client").resolve()
    base = env.get("XDG_STATE_HOME") or "~/.local/state"
    return (Path(base).expanduser() / "portmap" / "client").resolve()


def legacy_client_state_dir() -> Path:
    """State directory of the removed standalone portmap-client command."""
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return (Path(base).expanduser() / "portmap-client").resolve()


def _bundle_library_prefixes() -> tuple[Path, ...]:
    if not frozen():
        return ()
    prefixes = []
    executable = getattr(sys, "executable", "")
    if executable:
        prefixes.append(Path(executable).resolve().parent)
    payload = getattr(sys, "_MEIPASS", "")
    if payload:
        prefixes.append(Path(payload).resolve())
    return tuple(prefixes)


def _inside(candidate: str, prefix: Path) -> bool:
    try:
        Path(candidate).expanduser().resolve().relative_to(prefix)
    except (OSError, ValueError):
        return False
    return True


def native_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for external native/system subprocesses.

    Entries of the dynamic-loader search path that point into the frozen
    bundle are dropped; a user's own entries and every other variable pass
    through untouched. In source mode nothing is modified."""
    environment = dict(os.environ if base is None else base)
    prefixes = _bundle_library_prefixes()
    if not prefixes:
        return environment
    for name in _LIBRARY_PATH_VARIABLES:
        value = environment.get(name)
        if not value:
            environment.pop(name, None)
            continue
        kept = [entry for entry in value.split(os.pathsep) if entry and not any(_inside(entry, prefix) for prefix in prefixes)]
        if kept:
            environment[name] = os.pathsep.join(kept)
        else:
            environment.pop(name, None)
    return environment


def worker_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a self-spawned frozen worker.

    PYINSTALLER_RESET_ENVIRONMENT tells the child bootloader to drop the
    parent's inherited loader modifications and rebuild its own bundle
    runtime, so repeated self-spawns do not accumulate search paths. Ignored
    by a plain Python child in source mode."""
    environment = dict(os.environ if base is None else base)
    if frozen():
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


# logind keeps each login session's processes in a session-<id>.scope cgroup;
# when the session ends (an SSH disconnect under KillUserProcesses=yes) every
# process in that scope is SIGTERMed, including portmap's background workers.
_LOGIN_SESSION_SCOPE = re.compile(r"(?:^|/)session-[0-9A-Za-z]+\.scope(?:/|$)")
_SCOPE_ADOPT_WAIT = 5.0
_SCOPE_ADOPT_POLL = 0.05
# Name (adopted scope), "" (no escape was needed), or None (not yet decided).
_persistent_scope: str | None = None


def _cgroup_paths() -> list[str]:
    """Cgroup membership paths of this process, unified or legacy hierarchy."""
    try:
        content = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.rpartition(":")[2] for line in content.splitlines()]


def _login_session_scope() -> str | None:
    """The disposable logind session scope holding this process, if any."""
    for path in _cgroup_paths():
        if _LOGIN_SESSION_SCOPE.search(path):
            return path
    return None


def ensure_persistent_scope(*, use_sudo: bool = True) -> str | None:
    """Keep this process's background workers past the login session that
    started them.

    On Linux, when this process runs inside a disposable logind
    session-<id>.scope, it first adopts its own PID into a transient
    system-manager scope through one exact StartTransientUnit call (mode fail,
    CollectMode inactive-or-failed, no unit files and no boot enablement).
    Only the calling PID moves: every worker keeps running as this same
    unprivileged user with unchanged groups, environment, recorded Popen PID
    and argv. Outside such a login session, and on non-Linux systems, nothing
    is done because no session escape is needed. One process requests at most
    one scope; later calls are no-ops.

    Root calls busctl directly. An ordinary caller that declined elevation
    fails here, before any killable worker exists, rather than silently
    spawning one that logind would terminate with the session."""
    global _persistent_scope
    if _persistent_scope is not None or not sys.platform.startswith("linux"):
        return _persistent_scope
    session = _login_session_scope()
    if session is None:
        _persistent_scope = ""
        return None
    root = hasattr(os, "geteuid") and os.geteuid() == 0
    if not root and not use_sudo:
        raise PortmapError(
            "keeping portmap background workers past this login session needs administrator "
            "privileges: drop --no-sudo (portmap runs one exact sudo command) or run as root"
        )
    name = f"portmap-{os.getuid()}-{os.getpid()}-{secrets.token_hex(4)}.scope"
    command = [
        "busctl", "call",
        "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager", "StartTransientUnit",
        "ssa(sv)a(sa(sv))",
        name, "fail",
        "2", "PIDs", "au", "1", str(os.getpid()),
        "CollectMode", "s", "inactive-or-failed",
        "0",
    ]
    if not root:
        command = ["sudo", "--", *command]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=20, check=False,
            env=native_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortmapError(
            f"could not move this process out of login session {session}: {exc}. "
            "No background portmap worker was started"
        ) from exc
    if result.returncode != 0:
        raise PortmapError(
            f"could not move this process out of login session {session}: "
            f"{(result.stderr or result.stdout).strip()[:1000]}. No background portmap worker was started"
        )
    deadline = time.monotonic() + _SCOPE_ADOPT_WAIT
    while time.monotonic() < deadline:
        if any(name in path for path in _cgroup_paths()):
            _persistent_scope = name
            return name
        time.sleep(_SCOPE_ADOPT_POLL)
    raise PortmapError(
        f"this process did not enter transient scope {name} within {_SCOPE_ADOPT_WAIT:g}s; "
        "no background portmap worker was started"
    )



def process_identity(pid: int) -> tuple[int, str, list[str] | str] | None:
    """Read UID, start identity and argv before controlling an owned worker."""
    try:
        if sys.platform.startswith("linux"):
            process = Path(f"/proc/{pid}")
            fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            argv = (process / "cmdline").read_bytes().rstrip(b"\x00").split(b"\x00")
            return process.stat().st_uid, fields[19], [os.fsdecode(arg) for arg in argv]
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "uid=,lstart=,command="],
            capture_output=True, text=True, timeout=1, check=False,
            env={**native_environment(), "LC_ALL": "C"},
        )
        fields = result.stdout.strip().split(None, 6)
        if result.returncode == 0 and len(fields) == 7:
            return int(fields[0]), " ".join(fields[1:6]), fields[6]
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    return None
