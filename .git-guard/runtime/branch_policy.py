from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

try:
    from .branch_logs import enforce_branch_log_update
    from .common import HookReject, RefUpdate, SourceCandidate, ZERO, warn
    from .config import config_bool
    from .git_ops import first_matching, git, is_ancestor, is_policy_ref, ref_contains, ref_exists, refs_matching, rev_parse, short_sha
    from .policy import is_allowed_branch_ref, required_target_refs, rule_targets_ref
    from .state import enforce_pending_lock, enforce_pending_tag_lock, load_state, save_state
    from .submodule_policy import enforce_submodule_main_guard
    from .tag_policy import clear_satisfied_pending_tags, latest_reachable_release_tag, update_pending_tags, validate_tag
except ImportError:  # pragma: no cover - installed hook script mode
    from branch_logs import enforce_branch_log_update
    from common import HookReject, RefUpdate, SourceCandidate, ZERO, warn
    from config import config_bool
    from git_ops import first_matching, git, is_ancestor, is_policy_ref, ref_contains, ref_exists, refs_matching, rev_parse, short_sha
    from policy import is_allowed_branch_ref, required_target_refs, rule_targets_ref
    from state import enforce_pending_lock, enforce_pending_tag_lock, load_state, save_state
    from submodule_policy import enforce_submodule_main_guard
    from tag_policy import clear_satisfied_pending_tags, latest_reachable_release_tag, update_pending_tags, validate_tag

def validate_prepared(repo: Path, policy: dict[str, Any], config: dict[str, Any], state_path: Path, updates: list[RefUpdate]) -> None:
    meaningful_updates = [update for update in updates if update.old != update.new]

    enforce_linked_worktree_branch_creation_guard(repo, config, meaningful_updates)
    enforce_submodule_main_guard(repo, config, meaningful_updates)
    validate_branch_rename_target(policy, meaningful_updates)

    state = load_state(state_path)
    pending = state.get("pending", {})
    pending_tags = state.get("pending_tags", {})
    proposed = {update.ref: update.new for update in meaningful_updates if update.new != ZERO}

    for update in meaningful_updates:
        if not is_policy_ref(update.ref):
            continue

        if update.ref.startswith("refs/heads/"):
            validate_branch_name(policy, update.ref)
            enforce_branch_log_update(repo, policy, config, update)

        enforce_pending_lock(repo, pending, update)
        enforce_pending_tag_lock(repo, policy, pending_tags, update)

        if update.ref.startswith("refs/tags/"):
            validate_tag(repo, policy, proposed, update)
            continue

        if update.ref.startswith("refs/heads/") and update.old == ZERO:
            validate_branch_creation_or_replacement(repo, policy, config, proposed, update)
            continue

        if update.ref.startswith("refs/heads/") and update.ref not in set(policy.get("protected_refs", [])):
            validate_managed_branch_update(repo, policy, config, update)

        if update.ref in set(policy.get("protected_refs", [])):
            validate_protected_target_update(repo, policy, config, proposed, update)

def validate_branch_name(policy: dict[str, Any], ref: str) -> None:
    if is_allowed_branch_ref(policy, ref):
        return
    raise HookReject("BRANCH_NAME_NOT_ALLOWED", ref=ref)

def validate_branch_rename_target(policy: dict[str, Any], updates: list[RefUpdate]) -> None:
    deleted_heads = [
        update
        for update in updates
        if update.ref.startswith("refs/heads/") and update.old != ZERO and update.new == ZERO
    ]
    if not deleted_heads:
        return

    if not any(update.ref == "HEAD" and update.old != ZERO and update.new == ZERO for update in updates):
        return

    target_ref = branch_rename_target_ref_from_parent()
    if not target_ref:
        raise HookReject("BRANCH_RENAME_TARGET_UNOBSERVABLE", ref=deleted_heads[0].ref)
    validate_branch_name(policy, target_ref)

def branch_rename_target_ref_from_parent() -> str | None:
    argv = parent_process_argv()
    if not argv:
        return None
    target = branch_rename_target_from_argv(argv)
    if not target:
        return None
    if target.startswith("refs/heads/"):
        return target
    return f"refs/heads/{target}"

def parent_process_argv() -> list[str]:
    parent_pid = os.getppid()
    argv = proc_cmdline_argv(parent_pid)
    if argv:
        return argv
    return ps_command_argv(parent_pid)

def proc_cmdline_argv(pid: int) -> list[str]:
    proc_path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = proc_path.read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]

def ps_command_argv(pid: int) -> list[str]:
    result = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        return shlex.split(result.stdout.strip())
    except ValueError:
        return []

def branch_rename_target_from_argv(argv: list[str]) -> str | None:
    try:
        branch_index = next(index for index, item in enumerate(argv) if Path(item).name == "branch")
    except StopIteration:
        return None

    args = argv[branch_index + 1 :]
    if not any(arg in {"-m", "-M", "--move", "--Move"} for arg in args):
        return None

    positional: list[str] = []
    end_of_options = False
    for arg in args:
        if end_of_options:
            positional.append(arg)
            continue
        if arg == "--":
            end_of_options = True
            continue
        if arg in {"-m", "-M", "--move", "--Move"}:
            continue
        if arg.startswith("-"):
            continue
        positional.append(arg)

    if not positional:
        return None
    return positional[-1]

def enforce_linked_worktree_branch_creation_guard(repo: Path, config: dict[str, Any], updates: list[RefUpdate]) -> None:
    if not config_bool(config, "worktree", "reject_branch_creation_in_linked_worktree"):
        return

    created_branch = next(
        (update for update in updates if update.old == ZERO and update.new != ZERO and update.ref.startswith("refs/heads/")),
        None,
    )
    if not created_branch or not is_linked_worktree(repo):
        return
    raise HookReject("WORKTREE_BRANCH_CREATION_NOT_ALLOWED", ref=created_branch.ref)

def is_linked_worktree(repo: Path) -> bool:
    git_dir = git(repo, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    common_dir = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return git_dir != common_dir

def validate_branch_creation_or_replacement(
    repo: Path,
    policy: dict[str, Any],
    config: dict[str, Any],
    proposed: dict[str, str],
    update: RefUpdate,
) -> None:
    if ref_exists(repo, update.ref):
        existing = RefUpdate(old=rev_parse(repo, update.ref), new=update.new, ref=update.ref)
        if update.ref in set(policy.get("protected_refs", [])):
            validate_protected_target_update(repo, policy, config, proposed, existing)
        else:
            validate_managed_branch_update(repo, policy, config, existing)
        return

    if update.ref in set(policy.get("protected_refs", [])):
        return

    edge = first_matching(policy.get("branch_from", []), "target_ref_regex", update.ref)
    if not edge:
        raise HookReject("BRANCH_CREATION_NOT_ALLOWED", ref=update.ref)

    source_ref = edge["source_ref"]
    if not ref_exists(repo, source_ref):
        raise HookReject("BRANCH_SOURCE_MISSING", ref=update.ref, source_ref=source_ref)
    if rev_parse(repo, source_ref) != update.new:
        raise HookReject("BRANCH_SOURCE_MISMATCH", ref=update.ref, source_ref=source_ref, new=short_sha(update.new))

def validate_managed_branch_update(
    repo: Path,
    policy: dict[str, Any],
    config: dict[str, Any],
    update: RefUpdate,
) -> None:
    if update.new == ZERO:
        return
    if not is_ancestor(repo, update.old, update.new):
        raise HookReject("MANAGED_BRANCH_NON_FAST_FORWARD", ref=update.ref, old=short_sha(update.old), new=short_sha(update.new))

    for source_ref in introduced_policy_branch_heads(repo, policy, update):
        rule = merge_rule_for_source(policy, source_ref, update.ref)
        if rule:
            continue
        raise HookReject(
            "MANAGED_BRANCH_SOURCE_NOT_ALLOWED",
            ref=update.ref,
            source_ref=source_ref,
            old=short_sha(update.old),
            new=short_sha(update.new),
        )

def introduced_policy_branch_heads(repo: Path, policy: dict[str, Any], update: RefUpdate) -> list[str]:
    heads: list[tuple[str, str]] = []
    for ref in git(repo, "for-each-ref", "--format=%(refname)", "refs/heads").stdout.splitlines():
        if ref == update.ref or not is_allowed_branch_ref(policy, ref):
            continue
        sha = rev_parse(repo, ref)
        if is_ancestor(repo, sha, update.new) and not is_ancestor(repo, sha, update.old):
            heads.append((ref, sha))
    return [ref for ref, _ in maximal_branch_heads(repo, heads)]

def maximal_branch_heads(repo: Path, heads: list[tuple[str, str]]) -> list[tuple[str, str]]:
    maximal = []
    for ref, sha in heads:
        if any(sha != other_sha and is_ancestor(repo, sha, other_sha) for _, other_sha in heads):
            continue
        maximal.append((ref, sha))
    return maximal

def merge_rule_for_source(policy: dict[str, Any], source_ref: str, target_ref: str) -> dict[str, Any] | None:
    for rule in policy.get("merge_rules", []):
        if rule_targets_ref(rule, target_ref) and re.match(rule["source_ref_regex"], source_ref):
            return rule
    return None

def validate_protected_target_update(
    repo: Path,
    policy: dict[str, Any],
    config: dict[str, Any],
    proposed: dict[str, str],
    update: RefUpdate,
) -> None:
    if update.old == ZERO:
        return
    if update.new == ZERO:
        raise HookReject("PROTECTED_REF_DELETE", ref=update.ref)
    if not is_ancestor(repo, update.old, update.new):
        raise HookReject("PROTECTED_REF_NON_FAST_FORWARD", ref=update.ref, old=short_sha(update.old), new=short_sha(update.new))

    if not config_bool(config, "protected_branches", "enabled"):
        warn("PROTECTED_BRANCH_GUARD_DISABLED", ref=update.ref)
        return

    if direct_commit_allowed(repo, policy, update):
        return

    candidates = source_candidates_for_target(repo, policy, update)
    if not candidates:
        raise HookReject("PROTECTED_REF_NO_ALLOWED_SOURCE", ref=update.ref, old=short_sha(update.old), new=short_sha(update.new))
    if len(candidates) > 1:
        raise HookReject(
            "PROTECTED_REF_MULTIPLE_SOURCES",
            ref=update.ref,
            sources=[candidate.ref for candidate in candidates],
        )

    candidate = candidates[0]
    enforce_sync_merge_required(repo, candidate, update)

    required = required_target_refs(policy, candidate.rule["source"])
    if len(required) <= 1:
        return

    completed_before = [target_ref for target_ref in required if ref_contains(repo, target_ref, candidate.sha)]
    if update.ref in completed_before:
        return

    next_index = len(completed_before)
    next_required = required[next_index] if next_index < len(required) else None
    if update.ref != next_required:
        raise HookReject(
            "MULTI_TARGET_ORDER",
            ref=update.ref,
            source_ref=candidate.ref,
            source_sha=short_sha(candidate.sha),
            expected_ref=next_required,
        )

def direct_commit_allowed(repo: Path, policy: dict[str, Any], update: RefUpdate) -> bool:
    for item in policy.get("direct_commit_refs", []):
        if re.match(item["ref_regex"], update.ref):
            return not introduced_policy_branch_heads(repo, policy, update)
    return False

def source_candidates_for_target(repo: Path, policy: dict[str, Any], update: RefUpdate) -> list[SourceCandidate]:
    candidates: list[SourceCandidate] = []
    for rule in policy.get("merge_rules", []):
        if not rule_targets_ref(rule, update.ref):
            continue
        for ref in refs_matching(repo, rule["source_ref_regex"]):
            sha = rev_parse(repo, ref)
            if is_ancestor(repo, sha, update.new) and not is_ancestor(repo, sha, update.old):
                candidates.append(SourceCandidate(ref=ref, sha=sha, rule=rule))
    return maximal_source_candidates(repo, candidates)

def enforce_sync_merge_required(repo: Path, candidate: SourceCandidate, update: RefUpdate) -> None:
    if not candidate.rule.get("sync_merge_required"):
        return
    if is_ancestor(repo, update.old, candidate.sha):
        return
    raise HookReject(
        "SYNC_MERGE_REQUIRED",
        source_ref=candidate.ref,
        source=short_sha(candidate.sha),
        target_ref=update.ref,
        target=short_sha(update.old),
    )

def maximal_source_candidates(repo: Path, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
    maximal = []
    for candidate in candidates:
        if any(candidate.sha != other.sha and is_ancestor(repo, candidate.sha, other.sha) for other in candidates):
            continue
        maximal.append(candidate)
    return maximal

def update_committed_state(repo: Path, policy: dict[str, Any], state_path: Path, updates: list[RefUpdate]) -> None:
    meaningful_updates = [update for update in updates if update.old != update.new]
    state = load_state(state_path)
    pending = state.setdefault("pending", {})
    pending_tags = state.setdefault("pending_tags", {})
    branch_bases = state.setdefault("branch_bases", {})

    for update in meaningful_updates:
        if update.ref.startswith("refs/heads/") and update.old == ZERO and update.new != ZERO:
            edge = first_matching(policy.get("branch_from", []), "target_ref_regex", update.ref)
            if edge:
                branch_bases[update.ref] = {
                    "source_ref": edge["source_ref"],
                    "base_sha": update.new,
                    "base_release_tag": latest_reachable_release_tag(repo, update.new),
                }

        if update.ref not in set(policy.get("protected_refs", [])) or update.new == ZERO:
            continue

        candidates = source_candidates_for_target(repo, policy, update)
        if len(candidates) != 1:
            continue

        candidate = candidates[0]
        update_pending_tags(repo, pending_tags, candidate, update.new)

        required = required_target_refs(policy, candidate.rule["source"])
        if len(required) <= 1:
            continue

        completed = [target_ref for target_ref in required if ref_contains(repo, target_ref, candidate.sha)]
        if set(completed) == set(required):
            pending.pop(candidate.ref, None)
        else:
            pending[candidate.ref] = {
                "source_sha": candidate.sha,
                "required_target_refs": required,
                "completed_target_refs": completed,
                "remaining_target_refs": [ref for ref in required if ref not in completed],
            }

    clear_satisfied_pending_tags(pending_tags, meaningful_updates)
    save_state(state_path, state)
