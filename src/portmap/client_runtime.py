"""Runtime helpers for the standalone client: state paths, bundled native
binaries, child environments, and frozen worker commands.

This module must stay free of server-runtime imports (settings, planner,
agent, catalog): the frozen portmap-client executable imports it first."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Mapping, Sequence

# In a source checkout this is src/portmap/client_bin; in the frozen onedir
# bundle it is <install>/_internal/portmap/client_bin, because __file__ of
# bundled modules resolves under the PyInstaller payload root.
BUNDLED_BIN_DIR = Path(__file__).resolve().parent / "client_bin"

# Loader search variables a PyInstaller application may add for itself. Native
# system children (ssh, ps, ip, resolvectl, coredns from PATH) must not inherit
# entries that point into the client bundle.
_LIBRARY_PATH_VARIABLES = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH")

_WORKER_FLAGS = ("--state-dir", "--port", "--owner")


def frozen() -> bool:
    """True when running from the frozen portmap-client executable."""
    return bool(getattr(sys, "frozen", False))


def client_state_dir() -> Path:
    """Client-owned state directory, independent of server configuration."""
    configured = os.environ.get("PORTMAP_CLIENT_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return (Path(base).expanduser() / "portmap-client").resolve()


def bundled_binary(name: str) -> Path | None:
    """Return the bundled native binary shipped inside the client package."""
    if not name or Path(name).name != name:
        return None
    candidate = BUNDLED_BIN_DIR / name
    try:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    except OSError:
        return None
    return None


def find_native_binary(name: str) -> Path | None:
    """Resolve a native dependency: bundled binary first, then PATH."""
    return bundled_binary(name) or (Path(found) if (found := shutil.which(name)) else None)


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


def serve_catalog_command(*, state_dir: str, port: int | str, owner: str) -> list[str]:
    """Argv that (re)launches the catalog-view worker.

    Frozen: the same executable re-invokes its hidden worker subcommand so the
    child shares the bundle runtime. Source: the package stays importable via
    the PYTHONPATH the launcher sets."""
    arguments = ["--state-dir", str(state_dir), "--port", str(port), "--owner", str(owner)]
    if frozen():
        return [sys.executable, "_serve-catalog", *arguments]
    return [sys.executable, "-m", "portmap.client_view", *arguments]


def serve_catalog_arguments(command: Sequence[str]) -> dict[str, str] | None:
    """Parse a recorded catalog-worker command without assuming the current
    process runs in the same mode (frozen or source) as its recorder."""
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
        return None
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
