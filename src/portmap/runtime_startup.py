"""Read-only activation of Portmap's persisted helpers by an external API starter.

The starter evaluates ``runtime-env activate`` before its existing Podman API
command and runs ``runtime-env verify-api`` once that API is ready. Neither
boundary installs helpers, changes configuration, or starts/restarts an engine.
Existing producer modes are preserved, including group-writable files under
the user's home; this consumer does not introduce a new permission policy.
Native children use the shared frozen-bundle loader isolation policy.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from .client_runtime import native_environment
from .runtime_compat import (
    AARDVARK_NAME,
    DROP_IN_HEADER as OWNED_HEADER,
    NETAVARK_NAME,
    OVERRIDE_NAME,
    _helpers_dir,
)

HELPERS = (NETAVARK_NAME, AARDVARK_NAME)


class ActivationError(RuntimeError):
    pass


def owned_override() -> tuple[Path, Path] | None:
    helpers_root = _helpers_dir().parent
    override = helpers_root / OVERRIDE_NAME
    explicit = os.environ.get("CONTAINERS_CONF_OVERRIDE")
    if explicit is not None and explicit != str(override):
        return None  # An explicit foreign override, including empty, stays authoritative.
    if override.is_symlink():
        raise ActivationError(f"owned helper override is a symlink: {override}")
    if not override.exists():
        if explicit is not None:
            raise ActivationError(f"selected helper override is missing: {override}")
        return None
    content = override.read_text(encoding="utf-8")
    if not content.startswith(OWNED_HEADER + "\n"):
        return None  # No ownership claim: do not adopt or replace foreign configuration.
    payload = tomllib.loads(content)
    engine = payload.get("engine")
    entries = engine.get("helper_binaries_dir") if isinstance(engine, dict) else None
    if not isinstance(entries, list) or not entries or not all(isinstance(p, str) and p for p in entries):
        raise ActivationError(f"owned helper override has no valid helper search list: {override}")
    helpers_dir = Path(entries[0])
    if helpers_dir.parent != helpers_root or helpers_dir.name in ("", ".", ".."):
        raise ActivationError(f"owned helper override does not lead with its versioned user-data helpers: {override}")
    return override, helpers_dir


def run(command: list[str], *, environment: dict[str, str] | None = None, include_stderr: bool = False) -> str:
    result = subprocess.run(command, env=native_environment(environment), capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ActivationError(f"{command[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout + (result.stderr if include_stderr else "")


def version(name: str, text: str) -> str:
    if not isinstance(text, str):
        raise ActivationError(f"{name} did not report a helper version")
    match = re.search(rf"\b{re.escape(name)}\s+v?(\d+(?:\.\d+)*)\b", text, re.IGNORECASE)
    if not match:
        raise ActivationError(f"{name} did not report a helper version")
    return match.group(1)


def helper_versions(helpers_dir: Path) -> dict[str, str]:
    versions = {}
    for name in HELPERS:
        binary = helpers_dir / name
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ActivationError(f"owned helper is missing or not executable: {binary}")
        versions[name] = version(name, run([str(binary), "--version"], include_stderr=True))
    return versions


def validate_selection(payload: dict, helpers_dir: Path, versions: dict[str, str]) -> None:
    if not isinstance(payload, dict):
        raise ActivationError("Podman did not return an info object")
    host = payload.get("host", {})
    security = host.get("security") if isinstance(host, dict) else None
    if not isinstance(security, dict) or security.get("rootless") is not True:
        raise ActivationError("Podman helper activation requires the existing rootless engine")
    info = host.get("networkBackendInfo")
    if host.get("networkBackend") != "netavark" or not isinstance(info, dict):
        raise ActivationError("Podman did not report its effective netavark helper selection")
    for name, selected in ((NETAVARK_NAME, info), (AARDVARK_NAME, info.get("dns"))):
        expected = str(helpers_dir / name)
        if not isinstance(selected, dict) or selected.get("path") != expected:
            raise ActivationError(f"Podman did not select the persisted {name} helper: expected {expected}")
        if version(name, selected.get("version", "")) != versions[name]:
            raise ActivationError(f"Podman selected an unexpected {name} version")


def activate(override: Path, helpers_dir: Path) -> None:
    environment = dict(os.environ)
    environment["CONTAINERS_CONF_OVERRIDE"] = str(override)
    versions = helper_versions(helpers_dir)
    native = json.loads(run(["podman", "--remote=false", "info", "--format=json"], environment=environment))
    validate_selection(native, helpers_dir, versions)
    # Emit only after the real CLI confirms selection. The existing API starter
    # evaluates this export so its API process and children inherit it.
    print("export CONTAINERS_CONF_OVERRIDE=" + shlex.quote(str(override)))


def verify_api(override: Path, helpers_dir: Path) -> None:
    if os.environ.get("CONTAINERS_CONF_OVERRIDE") != str(override):
        raise ActivationError("the Podman API starter did not inherit the owned helper override")
    endpoint = os.environ.get("DOCKER_HOST", "")
    if not endpoint.startswith("unix://") or not endpoint.removeprefix("unix://"):
        raise ActivationError("the existing Podman API must use its configured Unix socket")
    native = json.loads(run([
        "curl", "--fail", "--silent", "--show-error", "--unix-socket",
        endpoint.removeprefix("unix://"), "http://d/v4.0.0/libpod/info",
    ]))
    validate_selection(native, helpers_dir, helper_versions(helpers_dir))


def run_action(action: str) -> int:
    try:
        owned = owned_override()
        if owned is not None:
            (activate if action == "activate" else verify_api)(*owned)
    except (ActivationError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: persistent Podman helper activation failed: {exc}", file=sys.stderr)
        return 1
    return 0
