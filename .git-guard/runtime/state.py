from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from .common import HookReject, RefUpdate, ZERO
    from .git_ops import is_ancestor, ref_contains, short_sha
except ImportError:  # pragma: no cover - installed hook script mode
    from common import HookReject, RefUpdate, ZERO
    from git_ops import is_ancestor, ref_contains, short_sha

def enforce_pending_lock(repo: Path, pending: dict[str, Any], update: RefUpdate) -> None:
    if not pending or update.new == ZERO or not update.ref.startswith("refs/heads/"):
        return

    for source_ref, item in pending.items():
        source_sha = item["source_sha"]
        remaining = set(item["remaining_target_refs"])

        if update.ref == source_ref and update.new != source_sha:
            raise HookReject(
                "PENDING_SOURCE_MOVED",
                source_ref=source_ref,
                expected_sha=short_sha(source_sha),
                new=short_sha(update.new),
            )

        if update.ref in remaining:
            if not is_ancestor(repo, source_sha, update.new):
                raise HookReject(
                    "PENDING_TARGET_MISSING_SOURCE",
                    ref=update.ref,
                    source_ref=source_ref,
                    source_sha=short_sha(source_sha),
                )
            return

        if update.ref not in set(item["completed_target_refs"]):
            raise HookReject(
                "PENDING_MULTI_TARGET_INCOMPLETE",
                ref=update.ref,
                source_ref=source_ref,
                source_sha=short_sha(source_sha),
                remaining=sorted(remaining),
            )

def enforce_pending_tag_lock(
    repo: Path,
    policy: dict[str, Any],
    pending_tags: dict[str, Any],
    update: RefUpdate,
) -> None:
    if not pending_tags or not update.ref.startswith("refs/heads/"):
        return

    for _, item in pending_tag_items(pending_tags):
        target_sha = item["target_sha"]
        if update.ref == item["target_ref"] and update.new != target_sha:
            raise HookReject(
                "PENDING_TAG_TARGET_MOVED",
                target_ref=item["target_ref"],
                expected_sha=short_sha(target_sha),
                new=short_sha(update.new),
                source_ref=item["source_ref"],
                tag_pattern=item["tag_pattern"],
            )

def pending_tag_items(pending_tags: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (key, item)
        for key, item in pending_tags.items()
        if isinstance(item, dict)
        and isinstance(item.get("target_ref"), str)
        and isinstance(item.get("target_sha"), str)
        and isinstance(item.get("tag_ref_regex"), str)
    ]

def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"pending": {}, "pending_tags": {}}
    return normalize_state(json.loads(path.read_text(encoding="utf-8")))

def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    pending_tags = state.get("pending_tags", {})
    if isinstance(pending_tags, dict):
        state["pending_tags"] = {
            key: item
            for key, item in pending_tag_items(pending_tags)
        }
    else:
        state["pending_tags"] = {}
    if not isinstance(state.get("pending", {}), dict):
        state["pending"] = {}
    return state

def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
