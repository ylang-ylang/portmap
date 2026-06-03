from __future__ import annotations

import re
from typing import Any

try:
    from .common import HookReject
    from .git_ops import matches_ref_pattern
except ImportError:  # pragma: no cover - installed hook script mode
    from common import HookReject
    from git_ops import matches_ref_pattern

def is_allowed_branch_ref(policy: dict[str, Any], ref: str) -> bool:
    allowed_refs = {f"refs/heads/{name}" for name in policy["branches"].get("long_lived", [])}
    if ref in allowed_refs:
        return True
    for family in policy["branches"].get("families", []):
        if matches_ref_pattern(f"refs/heads/{family}", ref):
            return True
    return False

def rule_targets_ref(rule: dict[str, Any], ref: str) -> bool:
    target_ref_regex = rule.get("target_ref_regex")
    if isinstance(target_ref_regex, str) and re.match(target_ref_regex, ref):
        return True
    return rule.get("target_ref") == ref

def required_target_refs(policy: dict[str, Any], source_pattern: str) -> list[str]:
    for item in policy.get("required_targets", []):
        if item.get("source") == source_pattern:
            return list(item.get("target_refs", []))
    return []

def source_ref_regex(policy: dict[str, Any], source_pattern: str) -> str:
    for item in policy.get("required_targets", []):
        if item.get("source") == source_pattern:
            return item["source_ref_regex"]
    raise HookReject("POLICY_SOURCE_REGEX_MISSING", source=source_pattern)
