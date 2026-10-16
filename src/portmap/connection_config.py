"""Durable desired connection targets shared by the CLI and the private portal.

This module is the host control facade over the existing client
implementation in :mod:`portmap.client`. It persists which targets the user
wants configured — including failed and disconnected ones, so they remain
editable — and forwards every connect, disconnect and listing operation to
the client. It implements no transport, DNS, gateway or SSH logic of its
own and never stores credentials: SSH authentication stays with the host's
OpenSSH configuration and agent.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import secrets
import stat
import tempfile
import threading
from functools import wraps
from pathlib import Path
from typing import Any

from . import client
from .errors import PortmapError
from .permissions import ensure_private_file

TARGETS_FILE_NAME = "connection-targets.json"
DOCUMENT_VERSION = 1
TARGET_VIA_VALUES = ("auto", "direct", "ssh")
SAVE_VIA_VALUES = ("direct", "ssh")
PERSISTED_STATUSES = ("connecting", "error")
MAX_TARGET_BYTES = 255
_mutation_threads = threading.RLock()
_mutation_context = threading.local()
_OPTION_FIELDS = {"ssh_target", "ssh_config", "remote_port", "include_tcp", "timeout"}


def _serialized_mutation(function):
    """Serialize the complete target/transport transaction, including CLI calls."""
    @wraps(function)
    def run(state_dir: Path, *args, **kwargs):
        identity = str(state_dir.resolve())
        with _mutation_threads:
            active = getattr(_mutation_context, "state", None)
            if active is not None:
                if active != identity:
                    raise PortmapError("cannot nest mutations of different connection stores")
                return function(state_dir, *args, **kwargs)
            with client.client_lock(state_dir):
                pass  # Validate/create the private directory before opening its lock.
            lock_path = state_dir / "connection-operations.lock"
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            ensure_private_file(lock_path, message="connection operation lock is not private and owned", force=0o600)
            with os.fdopen(fd, "a+") as handle:
                metadata = os.fstat(handle.fileno())
                if metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise PortmapError("connection operation lock is not private and owned")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                _mutation_context.state = identity
                try:
                    return function(state_dir, *args, **kwargs)
                finally:
                    _mutation_context.state = None
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return run


def _options(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _OPTION_FIELDS:
        raise PortmapError("invalid saved connection options")
    result = {}
    for key, item in value.items():
        if item is None:
            continue
        if key == "ssh_config":
            if not isinstance(item, (str, Path)) or not str(item):
                raise PortmapError("invalid saved SSH config path")
            item = str(Path(item).expanduser().resolve())
        elif key == "ssh_target":
            if not isinstance(item, str) or not item:
                raise PortmapError("invalid saved SSH target")
        elif key == "remote_port":
            if type(item) is not int or not 1 <= item <= 65535:
                raise PortmapError("invalid saved remote HTTP port")
        elif key == "include_tcp":
            if type(item) is not bool:
                raise PortmapError("invalid saved TCP forwarding option")
        elif isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or item <= 0:
            raise PortmapError("invalid saved connection timeout")
        result[key] = item
    return result


def _profile_options(profile: dict[str, Any]) -> dict[str, Any]:
    options = {"include_tcp": bool(profile.get("include_tcp"))}
    if profile.get("via") == "ssh":
        options.update(ssh_target=profile.get("ssh_target"), remote_port=profile.get("remote_http_port"))
        options["ssh_config"] = profile.get("ssh_config") or (profile.get("tunnel") or {}).get("config_file")
    return _options(options)


def _target_key(target: str, via: str) -> str:
    return target if via == "ssh" else client._direct_target_key(target)


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _new_id(rows: list[dict[str, Any]]) -> str:
    existing = {row["id"] for row in rows}
    while True:
        candidate = secrets.token_hex(6)
        if candidate not in existing:
            return candidate


def _validated_row(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PortmapError(f"invalid connection target state: {path}")
    row_id = raw.get("id")
    target = raw.get("target")
    via = raw.get("via")
    if not isinstance(row_id, str) or not row_id:
        raise PortmapError(f"invalid connection target state: {path}")
    if not isinstance(target, str) or not target.strip():
        raise PortmapError(f"invalid connection target state: {path}")
    if via not in TARGET_VIA_VALUES:
        raise PortmapError(f"invalid connection target state: {path}")
    row: dict[str, Any] = {"id": row_id, "target": target, "via": via}
    row["options"] = _options(raw.get("options", {}))
    domain = raw.get("domain")
    if domain is not None and (not isinstance(domain, str) or not domain):
        raise PortmapError(f"invalid connection target state: {path}")
    if domain is not None:
        row["domain"] = domain
    status = raw.get("status")
    if status is not None and status not in PERSISTED_STATUSES:
        raise PortmapError(f"invalid connection target state: {path}")
    if status is not None:
        row["status"] = status
    error = raw.get("error")
    if error is not None and not isinstance(error, str):
        raise PortmapError(f"invalid connection target state: {path}")
    if error is not None:
        row["error"] = error
    updated_at = raw.get("updated_at")
    if updated_at is not None:
        if not isinstance(updated_at, str):
            raise PortmapError(f"invalid connection target state: {path}")
        row["updated_at"] = updated_at
    return row


def _load_targets(state_dir: Path) -> list[dict[str, Any]]:
    """Read the desired-target store; the caller holds the client lock."""
    path = state_dir / TARGETS_FILE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise PortmapError(f"invalid connection target state: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != DOCUMENT_VERSION or not isinstance(payload.get("targets"), list):
        raise PortmapError(f"invalid connection target state: {path}")
    return [_validated_row(raw, path) for raw in payload["targets"]]


def _save_targets(state_dir: Path, rows: list[dict[str, Any]]) -> None:
    """Atomically replace the store inside the ownership-checked state dir."""
    fd, name = tempfile.mkstemp(prefix=".connection-targets-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": DOCUMENT_VERSION, "targets": rows}, handle, indent=2, sort_keys=True)
            handle.write("\n")
        Path(name).replace(state_dir / TARGETS_FILE_NAME)
    finally:
        Path(name).unlink(missing_ok=True)


def _row_matches_profile(row: dict[str, Any], profile: dict[str, Any]) -> bool:
    if row.get("domain") is not None and profile.get("domain") != row["domain"]:
        return False
    if _target_key(str(profile.get("target") or ""), row["via"]) != _target_key(row["target"], row["via"]):
        return False
    return row["via"] == "auto" or profile.get("via") == row["via"]


def _match_profile(row: dict[str, Any], client_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for profile in client_rows:
        if _row_matches_profile(row, profile):
            return profile
    return None


def _desired_row(row: dict[str, Any], profile: dict[str, Any] | None) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "target": row["target"],
        "via": row["via"],
        "has_connection": profile is not None,
        "status": "configured",
        "latency_ms": None,
        "last_checked_at": row.get("updated_at") if row.get("status") == "error" else None,
    }
    if profile is not None:
        result["domain"] = profile["domain"]
        result["status"] = profile.get("status") or "configured"
        result["actual_via"] = profile.get("via")
        result["latency_ms"] = profile.get("latency_ms")
        result["last_checked_at"] = profile.get("last_checked_at")
        if profile.get("error"):
            result["error"] = profile["error"]
        return result
    if row.get("domain"):
        result["domain"] = row["domain"]
        result["status"] = "disconnected"
    if row.get("status"):
        result["status"] = row["status"]
    if row.get("error"):
        result["error"] = row["error"]
    return result


def _adopted_row(profile: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": profile["domain"],
        "target": profile.get("target"),
        "via": profile.get("via"),
        "domain": profile["domain"],
        "status": profile.get("status") or "configured",
        "has_connection": True,
        "latency_ms": profile.get("latency_ms"),
        "last_checked_at": profile.get("last_checked_at"),
        "actual_via": profile.get("via"),
    }
    if profile.get("error"):
        result["error"] = profile["error"]
    return result


def _merged_rows(state_dir: Path, client_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with client.client_lock(state_dir):
        rows = _load_targets(state_dir)
    merged: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for row in rows:
        profile = _match_profile(row, client_rows)
        if profile is not None:
            claimed.add(profile["domain"])
        merged.append(_desired_row(row, profile))
    for profile in client_rows:
        if profile.get("domain") not in claimed:
            merged.append(_adopted_row(profile))
    return merged


def connection_targets(state_dir: Path, *, check: bool = False) -> dict[str, Any]:
    """The durable connection list: saved targets plus adopted client profiles."""
    client_rows = client.connections(state_dir, check=check)["connections"]
    return {"connections": _merged_rows(state_dir, client_rows), "generated_at": _now()}


def managed_connections(state_dir: Path, *, check: bool = False) -> dict[str, Any]:
    """The existing client listing plus durable ``configured_hosts`` rows."""
    result = client.connections(state_dir, check=check)
    result["configured_hosts"] = _merged_rows(state_dir, result["connections"])
    return result


def _begin_connect(state_dir: Path, target: str, via: str, options: dict[str, Any]) -> str:
    with client.client_lock(state_dir):
        rows = _load_targets(state_dir)
        row = next((item for item in rows if _target_key(item["target"], via) == _target_key(target, via) and item["via"] == via), None)
        if row is None:
            row = {"id": _new_id(rows), "target": target, "via": via}
            rows.append(row)
        row["options"] = options
        row["status"] = "connecting"
        row.pop("error", None)
        row["updated_at"] = _now()
        _save_targets(state_dir, rows)
        return row["id"]


def _error_text(exc: Exception) -> str:
    # PortmapError messages are user-facing; anything else may carry paths.
    return str(exc) if isinstance(exc, PortmapError) else "connection attempt failed"


def _record_outcome(state_dir: Path, row_id: str, *, profile: dict[str, Any] | None, error: str | None) -> None:
    with client.client_lock(state_dir):
        rows = _load_targets(state_dir)
        row = next((item for item in rows if item["id"] == row_id), None)
        if row is None:  # deleted while the attempt was running
            return
        if profile is not None:
            active = client._load(state_dir).get(profile["domain"])
            if active is None:
                raise PortmapError("connected profile disappeared before its configuration could be saved")
            row["options"] = {**row.get("options", {}), **_profile_options(active)}
            row["domain"] = profile.get("domain")
            row["target"] = profile.get("target") or row["target"]
            row.pop("status", None)
            row.pop("error", None)
        else:
            row["status"] = "error"
            row["error"] = error or "connection attempt failed"
        row["updated_at"] = _now()
        _save_targets(state_dir, rows)


@_serialized_mutation
def managed_connect(state_dir: Path, target: str, **options: Any) -> dict[str, Any]:
    """Connect through the existing client, keeping the target durable.

    The desired target is persisted before the attempt so failed targets
    remain listed and editable; the client's own result and exception
    semantics (including SSHSelectionRequired) are preserved unchanged.
    """
    if not isinstance(target, str) or not target.strip():
        raise PortmapError("connection target must be a non-empty string")
    via = options.get("via", "auto")
    if via not in TARGET_VIA_VALUES:
        raise PortmapError("connection mode must be auto, direct or ssh")
    saved_options = _options({key: value for key, value in options.items() if key in _OPTION_FIELDS})
    row_id = _begin_connect(state_dir, target.strip(), via, saved_options)
    try:
        result = client.connect(state_dir, target, **options)
    except Exception as exc:
        _record_outcome(state_dir, row_id, profile=None, error=_error_text(exc))
        raise
    _record_outcome(state_dir, row_id, profile=result, error=None)
    return result


def _profile_for_domain(client_rows: list[dict[str, Any]], domain: str) -> dict[str, Any] | None:
    key = domain.strip(".").lower()
    matches = [row for row in client_rows if str(row.get("domain", "")).strip(".").lower() == key]
    if len(matches) == 1:
        return matches[0]
    prefix = [
        row
        for row in client_rows
        if str(row.get("domain", "")).removesuffix(".portmap").strip(".").lower() == key
    ]
    return prefix[0] if len(prefix) == 1 else None


@_serialized_mutation
def managed_disconnect(state_dir: Path, domain: str, *, use_sudo: bool = True) -> dict[str, Any]:
    """Disconnect through the existing client, retaining the desired target."""
    with client.client_lock(state_dir):
        profile = _profile_for_domain(list(client._load(state_dir).values()), domain)
    result = client.disconnect(state_dir, domain, use_sudo=use_sudo)
    key = result.get("disconnected") or domain
    with client.client_lock(state_dir):
        rows = _load_targets(state_dir)
        changed = False
        retained = False
        for row in rows:
            if row.get("domain") != key and (profile is None or not _row_matches_profile(row, profile)):
                continue
            if not row.get("domain"):
                row["domain"] = key
            row.pop("status", None)
            row.pop("error", None)
            row["updated_at"] = _now()
            retained = True
            changed = True
        if not retained and profile is not None:
            # A CLI-created profile becomes an editable disconnected target.
            rows.append({
                "id": _new_id(rows),
                "target": profile.get("target") or key,
                "via": profile.get("via") if profile.get("via") in TARGET_VIA_VALUES else "auto",
                "domain": key,
                "options": _profile_options(profile),
                "updated_at": _now(),
            })
            changed = True
        if changed:
            _save_targets(state_dir, rows)
    return result


def _mutate_fields(payload: dict[str, Any], action: str, allowed: set[str]) -> None:
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise PortmapError(f"unexpected field(s) for action {action}: {', '.join(unexpected)}")


def _require_id(payload: dict[str, Any]) -> str:
    row_id = payload.get("id")
    if not isinstance(row_id, str) or not row_id.strip():
        raise PortmapError("connection id is required")
    return row_id.strip()


def _find_row(rows: list[dict[str, Any]], row_id: str) -> dict[str, Any] | None:
    return next((row for row in rows if row["id"] == row_id), None)


def _save_action(state_dir: Path, payload: dict[str, Any]) -> None:
    _mutate_fields(payload, "save", {"action", "id", "target", "via"})
    target = payload.get("target")
    if not isinstance(target, str) or not target.strip():
        raise PortmapError("connection target is required")
    target = target.strip()
    if len(target) > MAX_TARGET_BYTES:
        raise PortmapError("connection target is too long")
    via = payload.get("via")
    if via not in SAVE_VIA_VALUES:
        raise PortmapError("connection mode must be direct or ssh")
    row_id = payload.get("id")
    if row_id is not None and (not isinstance(row_id, str) or not row_id.strip()):
        raise PortmapError("connection id must be a non-empty string")
    with client.client_lock(state_dir):
        profiles = list(client._load(state_dir).values())
        rows = _load_targets(state_dir)
        if any(_target_key(row["target"], via) == _target_key(target, via) and row["via"] == via and row["id"] != row_id for row in rows):
            raise PortmapError(f"{target} is already saved as a {via} connection")
        if row_id is None:
            rows.append({"id": _new_id(rows), "target": target, "via": via, "updated_at": _now()})
        else:
            row = _find_row(rows, row_id)
            if row is None:
                raise PortmapError(f"no saved connection {row_id!r}")
            if _match_profile(row, profiles) is not None:
                raise PortmapError(f"{row.get('domain') or row['target']} is connected; disconnect it before editing")
            identity_changed = (row["target"], row["via"]) != (target, via)
            row["target"] = target
            row["via"] = via
            if identity_changed:
                row.pop("domain", None)
                options = row.get("options", {})
                options.pop("ssh_target", None)
                options.pop("remote_port", None)
                if via == "direct":
                    options.pop("ssh_config", None)
            row.pop("status", None)
            row.pop("error", None)
            row["updated_at"] = _now()
        _save_targets(state_dir, rows)


def _connect_action(state_dir: Path, payload: dict[str, Any], *, use_sudo: bool) -> None:
    _mutate_fields(payload, "connect", {"action", "id"})
    row_id = _require_id(payload)
    with client.client_lock(state_dir):
        row = _find_row(_load_targets(state_dir), row_id)
    if row is not None:
        target, via = row["target"], row["via"]
        options = dict(row.get("options", {}))
    else:
        with client.client_lock(state_dir):
            profile = _profile_for_domain(list(client._load(state_dir).values()), row_id)
        if profile is None:
            raise PortmapError(f"no saved connection {row_id!r}")
        target = profile.get("target") or row_id
        via = profile.get("via") if profile.get("via") in TARGET_VIA_VALUES else "auto"
        options = _profile_options(profile)
    if "ssh_config" in options:
        options["ssh_config"] = Path(options["ssh_config"])
    managed_connect(state_dir, target, via=via, use_sudo=use_sudo, **options)


def _disconnect_action(state_dir: Path, payload: dict[str, Any], *, use_sudo: bool) -> None:
    _mutate_fields(payload, "disconnect", {"action", "id"})
    row_id = _require_id(payload)
    profiles = client.connections(state_dir)["connections"]
    with client.client_lock(state_dir):
        row = _find_row(_load_targets(state_dir), row_id)
    if row is not None:
        active = _match_profile(row, profiles)
        if active is None:
            raise PortmapError(f"{row['target']} is not connected")
    else:
        active = _profile_for_domain(profiles, row_id)
        if active is None:
            raise PortmapError(f"no saved connection {row_id!r}")
    managed_disconnect(state_dir, active["domain"], use_sudo=use_sudo)


def _delete_action(state_dir: Path, payload: dict[str, Any], *, use_sudo: bool) -> None:
    _mutate_fields(payload, "delete", {"action", "id"})
    row_id = _require_id(payload)
    profiles = client.connections(state_dir)["connections"]
    with client.client_lock(state_dir):
        row = _find_row(_load_targets(state_dir), row_id)
    if row is not None:
        active = _match_profile(row, profiles)
    else:
        active = _profile_for_domain(profiles, row_id)
        if active is None:
            raise PortmapError(f"no saved connection {row_id!r}")
    if active is not None:
        # The owned active transport goes away first; a disconnect failure
        # retains the saved configuration for a retry.
        client.disconnect(state_dir, active["domain"], use_sudo=use_sudo)
    if row is None:
        return
    with client.client_lock(state_dir):
        rows = [item for item in _load_targets(state_dir) if item["id"] != row_id]
        _save_targets(state_dir, rows)


@_serialized_mutation
def mutate_connection_target(state_dir: Path, payload: dict[str, Any], *, use_sudo: bool = False) -> dict[str, Any]:
    """Apply one connection control action and return the updated target list.

    ``use_sudo`` defaults to False because web-originated control never
    uses interactive sudo; CLI wrappers pass their own default.
    """
    if not isinstance(payload, dict):
        raise PortmapError("connection request must be a JSON object")
    action = payload.get("action")
    if action == "save":
        _save_action(state_dir, payload)
    elif action == "connect":
        _connect_action(state_dir, payload, use_sudo=use_sudo)
    elif action == "disconnect":
        _disconnect_action(state_dir, payload, use_sudo=use_sudo)
    elif action == "delete":
        _delete_action(state_dir, payload, use_sudo=use_sudo)
    else:
        raise PortmapError("action must be save, connect, disconnect or delete")
    return connection_targets(state_dir)
