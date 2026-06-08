from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from .common import HookReject, LocalPolicyTag, RefUpdate, SourceCandidate, ZERO, append_unique, format_context_value
    from .config import config_bool
    from .git_ops import format_version, git, is_ancestor, peeled_rev_parse, ref_contains, ref_exists, refs_matching, rev_parse, short_sha
    from .policy import source_ref_regex
    from .state import load_state, pending_tag_items
except ImportError:  # pragma: no cover - installed hook script mode
    from common import HookReject, LocalPolicyTag, RefUpdate, SourceCandidate, ZERO, append_unique, format_context_value
    from config import config_bool
    from git_ops import format_version, git, is_ancestor, peeled_rev_parse, ref_contains, ref_exists, refs_matching, rev_parse, short_sha
    from policy import source_ref_regex
    from state import load_state, pending_tag_items

def validate_pre_push(
    repo: Path,
    policy: dict[str, Any],
    config: dict[str, Any],
    remote_name: str,
    remote_url: str,
    updates: list[PushUpdate],
) -> None:
    if not policy.get("tag_rules"):
        return

    remote = remote_name or remote_url
    remote_tags = remote_tag_map(repo, remote)
    pushed_tags = {update.local_ref for update in updates if update.local_ref.startswith("refs/tags/") and update.local_sha != ZERO}
    missing_tags: list[LocalPolicyTag] = []

    for tag in local_policy_tags(repo, policy):
        remote_sha = remote_tags.get(tag.ref)
        if remote_sha is None:
            if tag.ref not in pushed_tags:
                missing_tags.append(tag)
            continue
        if remote_sha != tag.object_sha:
            raise HookReject(
                "PUSH_TAG_CONFLICT",
                tag=tag.ref,
                remote=remote_name,
                local=short_sha(tag.object_sha),
                upstream=short_sha(remote_sha),
            )

    if config_bool(config, "pre_push", "auto_push_missing_tags"):
        auto_push_missing_tags(repo, remote, remote_name, missing_tags)

def local_policy_tags(repo: Path, policy: dict[str, Any]) -> list[LocalPolicyTag]:
    tags: dict[str, LocalPolicyTag] = {}
    for line in git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/tags").stdout.splitlines():
        tag_ref, object_sha = line.split(" ", 1)
        for rule in policy.get("tag_rules", []):
            if not re.match(rule["tag_ref_regex"], tag_ref):
                continue
            target_sha = peeled_rev_parse(repo, tag_ref)
            if tag_target_satisfies_rule(repo, policy, rule, target_sha):
                tags[tag_ref] = LocalPolicyTag(ref=tag_ref, object_sha=object_sha, target_sha=target_sha)
                break
    return [tags[ref] for ref in sorted(tags)]

def tag_target_satisfies_rule(repo: Path, policy: dict[str, Any], rule: dict[str, Any], target_sha: str) -> bool:
    try:
        return ref_contains(repo, tag_rule_target_ref(policy, rule), target_sha)
    except HookReject:
        return False

def remote_tag_map(repo: Path, remote: str) -> dict[str, str]:
    result = git(repo, "ls-remote", "--tags", remote)
    tags: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, ref = line.split(None, 1)
        if ref.endswith("^{}"):
            continue
        tags[ref] = sha
    return tags

def auto_push_missing_tags(repo: Path, remote: str, display_remote: str, tags: list[LocalPolicyTag]) -> None:
    if not tags:
        return

    tag_refs = [tag.ref for tag in tags]
    print(
        "git-guard: auto-pushing missing release tags "
        f"remote={format_context_value(display_remote)} tags={format_context_value(tag_refs)}",
        file=sys.stderr,
    )
    for tag in tags:
        result = git(repo, "push", "--no-verify", remote, f"{tag.ref}:{tag.ref}", check=False)
        if result.returncode != 0:
            raise HookReject(
                "PUSH_TAG_SYNC_FAILED",
                tag=tag.ref,
                remote=display_remote,
                stderr=result.stderr.strip(),
            )
        print(
            f"git-guard: auto-pushed release tag tag={tag.ref} remote={format_context_value(display_remote)}",
            file=sys.stderr,
        )

def validate_tag(repo: Path, policy: dict[str, Any], proposed: dict[str, str], update: RefUpdate) -> None:
    if update.old != ZERO:
        raise HookReject("TAG_MOVE_NOT_ALLOWED", tag=update.ref, old=short_sha(update.old), new=short_sha(update.new))
    state = load_state(Path(os.environ.get("GG_STATE_JSON", repo / ".git" / "git-guard-state.json")))
    target_matches = tag_target_matches(repo, policy, state, update.ref, update.new, proposed)
    if target_matches and not [item for item in target_matches if item["tag_matches"]]:
        raise HookReject(
            "TAG_TARGET_TAG_PATTERN_MISMATCH",
            tag=update.ref,
            target=short_sha(update.new),
            target_refs=[item["target_ref"] for item in target_matches],
            allowed_patterns=[item["tag_pattern"] for item in target_matches],
        )

    rules = [rule for rule in policy.get("tag_rules", []) if re.match(rule["tag_ref_regex"], update.ref)]
    if not rules:
        raise HookReject("TAG_NAME_NOT_ALLOWED", tag=update.ref)
    tag_version = parse_version_ref(update.ref)
    failures: list[HookReject] = []

    for rule in rules:
        target_ref = tag_rule_target_ref(policy, rule)
        target_sha = target_ref_sha(repo, target_ref, proposed)
        if target_sha is None:
            failures.append(HookReject("TAG_TARGET_BRANCH_MISSING", tag=update.ref, target_ref=target_ref))
            continue

        if not tag_target_ref_satisfies_rule(repo, rule, target_ref, target_sha, update.new):
            failures.append(HookReject(tag_target_ref_failure_code(rule), tag=update.ref, target=short_sha(update.new), target_ref=target_ref))
            continue

        source_refs = tag_source_candidates_for_target(repo, policy, state, rule, update.new)
        if not source_refs and not tag_requires_source_context(rule):
            source_refs = [rule["source"]]
        if not source_refs:
            failures.append(
                HookReject(
                    "TAG_TARGET_MISSING_SOURCE",
                    tag=update.ref,
                    target=short_sha(update.new),
                    source=rule["source"],
                    target_ref=target_ref,
                )
            )
            continue

        for source_ref in source_refs:
            if tag_rule_allows_version(repo, state, rule, source_ref, update.ref, tag_version):
                return

    raise preferred_tag_failure(failures, update.ref)

def tag_target_matches(
    repo: Path,
    policy: dict[str, Any],
    state: dict[str, Any],
    tag_ref: str,
    tag_sha: str,
    proposed: dict[str, str],
) -> list[dict[str, Any]]:
    matches = []
    for rule in policy.get("tag_rules", []):
        target_ref = tag_rule_target_ref(policy, rule)
        target_sha = target_ref_sha(repo, target_ref, proposed)
        if target_sha is None:
            continue
        if not tag_target_ref_satisfies_rule(repo, rule, target_ref, target_sha, tag_sha):
            continue
        if tag_requires_source_context(rule) and not tag_source_candidates_for_target(repo, policy, state, rule, tag_sha):
            continue
        matches.append(
            {
                "target": rule["target"],
                "target_ref": target_ref,
                "tag_pattern": rule.get("tag_pattern"),
                "tag_matches": re.match(rule["tag_ref_regex"], tag_ref) is not None,
            }
        )
    return matches

def tag_target_ref_satisfies_rule(repo: Path, rule: dict[str, Any], target_ref: str, target_sha: str, tag_sha: str) -> bool:
    if tag_required(rule):
        return target_sha == tag_sha
    return ref_contains(repo, target_sha, tag_sha)

def tag_source_candidates_for_target(
    repo: Path,
    policy: dict[str, Any],
    state: dict[str, Any],
    rule: dict[str, Any],
    tag_sha: str,
) -> list[str]:
    source_refs: list[str] = []

    for _, item in pending_tag_items(state.get("pending_tags", {})):
        if not pending_tag_matches_rule(item, rule):
            continue
        if item.get("target_sha") != tag_sha:
            continue
        source_ref = item.get("source_ref")
        if isinstance(source_ref, str):
            append_unique(source_refs, source_ref)

    for source_ref in refs_matching(repo, source_ref_regex(policy, rule["source"])):
        source_sha = rev_parse(repo, source_ref)
        if ref_contains(repo, tag_sha, source_sha):
            append_unique(source_refs, source_ref)

    return source_refs

def tag_required(rule: dict[str, Any]) -> bool:
    return bool(rule.get("tag_required", True))

def tag_requires_source_context(rule: dict[str, Any]) -> bool:
    return "=" in rule.get("tag_tokens", [])

def tag_target_ref_failure_code(rule: dict[str, Any]) -> str:
    if tag_required(rule):
        return "TAG_TARGET_NOT_TARGET_HEAD"
    return "TAG_TARGET_NOT_TARGET_HISTORY"

def tag_rule_target_ref(policy: dict[str, Any], rule: dict[str, Any]) -> str:
    target_ref = rule.get("target_ref")
    if isinstance(target_ref, str) and target_ref:
        return target_ref

    for merge_rule in policy.get("merge_rules", []):
        if merge_rule.get("source") != rule.get("source"):
            continue
        if merge_rule.get("target") != rule.get("target"):
            continue
        if merge_rule.get("tag_pattern") != rule.get("tag_pattern"):
            continue
        target_ref = merge_rule.get("target_ref")
        if isinstance(target_ref, str) and target_ref:
            return target_ref

    target = rule.get("target")
    if isinstance(target, str) and target:
        return f"refs/heads/{target}"
    raise HookReject("POLICY_TAG_TARGET_REF_MISSING", source=rule.get("source"), target=rule.get("target"))

def target_ref_sha(repo: Path, target_ref: str, proposed: dict[str, str]) -> str | None:
    proposed_sha = proposed.get(target_ref)
    if proposed_sha and proposed_sha != ZERO:
        return proposed_sha
    if ref_exists(repo, target_ref):
        return rev_parse(repo, target_ref)
    return None

def tag_rule_allows_version(
    repo: Path,
    state: dict[str, Any],
    rule: dict[str, Any],
    source_ref: str,
    tag_ref: str,
    tag_version: tuple[int, ...],
) -> bool:
    tokens = rule.get("tag_tokens", [])
    if not tag_tokens_match(tokens, tag_version):
        raise HookReject("TAG_PATTERN_COMPONENT_MISMATCH", tag=tag_ref, pattern=rule.get("tag_pattern"))

    if "=" in tokens:
        base = state.get("branch_bases", {}).get(source_ref)
        if not base or not base.get("base_release_tag"):
            raise HookReject("TAG_BASE_RELEASE_MISSING", tag=tag_ref, source_ref=source_ref)
        base_version = parse_version_name(base["base_release_tag"])
        if tag_version[:2] != base_version[:2]:
            raise HookReject(
                "TAG_VERSION_LINE_MISMATCH",
                tag=tag_ref,
                source_ref=source_ref,
                expected_major=base_version[0],
                expected_minor=base_version[1],
                actual_major=tag_version[0],
                actual_minor=tag_version[1],
            )
        latest = max_existing_version(repo, major=base_version[0], minor=base_version[1])
        if latest is None or tag_version > latest:
            return True
        raise HookReject("TAG_VERSION_NOT_INCREMENTAL", tag=tag_ref, version=format_version(tag_version), latest=format_version(latest))

    latest = max_existing_version(repo)
    if latest is None:
        return True
    if tag_version > latest:
        return True
    raise HookReject("TAG_VERSION_NOT_INCREMENTAL", tag=tag_ref, version=format_version(tag_version), latest=format_version(latest))

def preferred_tag_failure(failures: list[HookReject], tag_ref: str) -> HookReject:
    if not failures:
        return HookReject("TAG_RULE_NOT_SATISFIED", tag=tag_ref)
    priority = {
        "TAG_VERSION_LINE_MISMATCH": 0,
        "TAG_VERSION_NOT_INCREMENTAL": 1,
        "TAG_PATTERN_COMPONENT_MISMATCH": 2,
        "TAG_BASE_RELEASE_MISSING": 3,
        "TAG_TARGET_NOT_TARGET_HEAD": 4,
        "TAG_TARGET_NOT_TARGET_HISTORY": 5,
        "TAG_TARGET_MISSING_SOURCE": 6,
        "TAG_TARGET_BRANCH_MISSING": 7,
        "TAG_SOURCE_BRANCH_MISSING": 8,
    }
    return min(failures, key=lambda failure: priority.get(failure.code, 100))

def tag_tokens_match(tokens: list[str], version: tuple[int, ...]) -> bool:
    if len(tokens) != len(version):
        return False
    for token, component in zip(tokens, version):
        if token in {"#", "="}:
            continue
        if int(token) != component:
            return False
    return True

def max_existing_version(repo: Path, major: int | None = None, minor: int | None = None) -> tuple[int, ...] | None:
    versions: list[tuple[int, ...]] = []
    for ref in git(repo, "for-each-ref", "--format=%(refname)", "refs/tags").stdout.splitlines():
        try:
            version = parse_version_ref(ref)
        except HookReject:
            continue
        if major is not None and version[0] != major:
            continue
        if minor is not None and version[1] != minor:
            continue
        versions.append(version)
    if not versions:
        return None
    return max(versions)

def latest_reachable_release_tag(repo: Path, commit: str) -> str | None:
    tags: list[tuple[tuple[int, ...], str]] = []
    for ref in git(repo, "for-each-ref", "--format=%(refname)", "refs/tags").stdout.splitlines():
        try:
            version = parse_version_ref(ref)
        except HookReject:
            continue
        if len(version) != 3 or version[2] != 0:
            continue
        if is_ancestor(repo, ref, commit):
            tags.append((version, ref.removeprefix("refs/tags/")))
    if not tags:
        return None
    return max(tags)[1]

def parse_version_ref(ref: str) -> tuple[int, ...]:
    if ref.startswith("refs/tags/"):
        return parse_version_name(ref.removeprefix("refs/tags/"))
    return parse_version_name(ref)

def parse_version_name(name: str) -> tuple[int, ...]:
    match = re.fullmatch(r"[vV]([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", name)
    if not match:
        raise HookReject("TAG_VERSION_INVALID", tag=name)
    return tuple(int(part) for part in match.groups() if part is not None)

def update_pending_tags(repo: Path, pending_tags: dict[str, Any], candidate: SourceCandidate, target_sha: str) -> None:
    rule = candidate.rule
    tag_pattern = rule.get("tag_pattern")
    tag_ref_regex = rule.get("tag_ref_regex")
    if not tag_pattern or not tag_ref_regex:
        return
    if not tag_required(rule):
        return

    key = pending_tag_key(candidate.ref, rule["target_ref"], target_sha, tag_pattern)
    if matching_tag_exists(repo, tag_ref_regex, target_sha):
        pending_tags.pop(key, None)
        return

    pending_tags[key] = {
        "source": rule["source"],
        "target": rule["target"],
        "source_ref": candidate.ref,
        "source_sha": candidate.sha,
        "target_ref": rule["target_ref"],
        "target_sha": target_sha,
        "merge_rule_id": rule["id"],
        "tag_pattern": tag_pattern,
        "tag_ref_regex": tag_ref_regex,
    }

def clear_satisfied_pending_tags(pending_tags: dict[str, Any], updates: list[RefUpdate]) -> None:
    for update in updates:
        if not update.ref.startswith("refs/tags/") or update.new == ZERO:
            continue
        for key, item in pending_tag_items(pending_tags):
            if update.new == item["target_sha"] and re.match(item["tag_ref_regex"], update.ref):
                pending_tags.pop(key, None)

def pending_tag_key(source_ref: str, target_ref: str, target_sha: str, tag_pattern: str) -> str:
    return "|".join([source_ref, target_ref, target_sha, tag_pattern])

def pending_tag_matches_rule(item: dict[str, Any], rule: dict[str, Any]) -> bool:
    return (
        item.get("source") == rule.get("source")
        and item.get("target_ref") == rule.get("target_ref")
        and item.get("tag_pattern") == rule.get("tag_pattern")
    )

def matching_tag_exists(repo: Path, tag_ref_regex: str, target_sha: str) -> bool:
    for ref in git(repo, "for-each-ref", "--format=%(refname)", "refs/tags").stdout.splitlines():
        if re.match(tag_ref_regex, ref) and peeled_rev_parse(repo, ref) == target_sha:
            return True
    return False
