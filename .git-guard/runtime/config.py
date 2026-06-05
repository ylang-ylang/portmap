from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from .common import HookReject
except ImportError:  # pragma: no cover - installed hook script mode
    from common import HookReject

DEFAULT_CONFIG = {
    "branch_logs": {
        "path": ".branch_logs/",
        "force_diff_required": True,
    },
    "pre_push": {
        "auto_push_missing_tags": True,
    },
    "protected_branches": {
        "enabled": True,
    },
    "runtime": {
        "auto_sync": True,
        "require_managed_files_staged": True,
    },
    "submodules": {
        "allowed_branches": ["main", "case/*/*"],
        "main_guard": True,
    },
    "worktree": {
        "reject_branch_creation_in_linked_worktree": True,
    },
}

def load_json_object(path: Path, invalid_code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HookReject(invalid_code, path=path, error=str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise HookReject(invalid_code, path=path, error=exc.msg) from exc

    if not isinstance(value, dict):
        raise HookReject(invalid_code, path=path, error="expected JSON object")
    return value

def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_config()
    return merge_config(load_json_object(path, invalid_code="CONFIG_INVALID"))

def default_config() -> dict[str, Any]:
    return merge_defaults({}, DEFAULT_CONFIG)

def merge_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = merge_defaults({}, DEFAULT_CONFIG)
    for key, value in config.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_defaults(value, merged[key])
        else:
            merged[key] = value
    normalize_config(merged)
    return merged

def normalize_config(config: dict[str, Any]) -> None:
    branch_logs = config.get("branch_logs")
    if isinstance(branch_logs, dict):
        branch_logs.pop("force_required", None)

def merge_defaults(config: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    merged = dict(config)
    for key, default_value in defaults.items():
        current_value = merged.get(key)
        if isinstance(current_value, dict) and isinstance(default_value, dict):
            merged[key] = merge_defaults(current_value, default_value)
        elif key not in merged:
            merged[key] = merge_defaults({}, default_value) if isinstance(default_value, dict) else default_value
    return merged

def config_bool(config: dict[str, Any], section: str, key: str) -> bool:
    section_value = config.get(section, {})
    if not isinstance(section_value, dict):
        raise HookReject("CONFIG_INVALID", key=section, expected="object")

    default_value = DEFAULT_CONFIG[section][key]
    value = section_value.get(key, default_value)
    if not isinstance(value, bool):
        raise HookReject("CONFIG_INVALID", key=f"{section}.{key}", expected="boolean")
    return value

def config_string_list(config: dict[str, Any], section: str, key: str) -> list[str]:
    section_value = config.get(section, {})
    if not isinstance(section_value, dict):
        raise HookReject("CONFIG_INVALID", key=section, expected="object")

    default_value = DEFAULT_CONFIG[section][key]
    value = section_value.get(key, default_value)
    if not isinstance(value, list) or not value:
        raise HookReject("CONFIG_INVALID", key=f"{section}.{key}", expected="non-empty string list")

    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise HookReject("CONFIG_INVALID", key=f"{section}.{key}", expected="non-empty string list")
        items.append(item)
    return items
