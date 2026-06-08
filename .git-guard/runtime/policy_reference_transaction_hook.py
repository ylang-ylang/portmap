#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

try:
    from .branch_logs import merge_in_progress, prepare_merge_commit, validate_pre_commit
    from .branch_policy import update_committed_state, validate_prepared
    from .common import AGENT_REJECT_HINT, HookReject
    from .config import load_config, load_json_object
    from .git_ops import append_log, read_push_updates, read_updates, required_env
    from .managed_files import enforce_git_guard_managed_files_staged
    from .tag_policy import validate_pre_push
except ImportError:  # pragma: no cover - installed hook script mode
    from branch_logs import merge_in_progress, prepare_merge_commit, validate_pre_commit
    from branch_policy import update_committed_state, validate_prepared
    from common import AGENT_REJECT_HINT, HookReject
    from config import load_config, load_json_object
    from git_ops import append_log, read_push_updates, read_updates, required_env
    from managed_files import enforce_git_guard_managed_files_staged
    from tag_policy import validate_pre_push


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: reference_transaction_hook.py <phase|pre-push|pre-commit|pre-merge-commit>", file=sys.stderr)
        return 2

    command = sys.argv[1]
    repo = Path(required_env("GG_REPO_PATH"))
    policy_path = Path(required_env("GG_POLICY_JSON"))
    config_path = Path(os.environ.get("GG_CONFIG_JSON", repo / ".git-guard" / "config.json"))
    state_path = Path(os.environ.get("GG_STATE_JSON", repo / ".git" / "git-guard-state.json"))
    log_path = os.environ.get("GG_LOG_PATH")
    policy: dict[str, Any] = {}

    try:
        policy = load_json_object(policy_path, invalid_code="POLICY_INVALID")
        config = load_config(config_path)

        if command == "pre-push":
            if len(sys.argv) != 4:
                raise HookReject("HOOK_PRE_PUSH_USAGE", argv=sys.argv[1:])
            validate_pre_push(repo, policy, config, sys.argv[2], sys.argv[3], read_push_updates(sys.stdin))
            return 0

        if command == "pre-commit":
            if len(sys.argv) != 2:
                raise HookReject("HOOK_PRE_COMMIT_USAGE", argv=sys.argv[1:])
            is_merge_commit = merge_in_progress(repo)
            if is_merge_commit:
                prepare_merge_commit(repo, config)
            enforce_git_guard_managed_files_staged(repo, config)
            validate_pre_commit(repo, policy, config, require_branch_log_change=True)
            return 0

        if command == "pre-merge-commit":
            if len(sys.argv) != 2:
                raise HookReject("HOOK_PRE_MERGE_COMMIT_USAGE", argv=sys.argv[1:])
            prepare_merge_commit(repo, config)
            enforce_git_guard_managed_files_staged(repo, config)
            validate_pre_commit(repo, policy, config, require_branch_log_change=True)
            return 0

        if len(sys.argv) != 2:
            raise HookReject("HOOK_REFERENCE_TRANSACTION_USAGE", argv=sys.argv[1:])

        updates = read_updates(sys.stdin)
        if log_path:
            append_log(Path(log_path), command, updates)

        if command == "prepared":
            validate_prepared(repo, policy, config, state_path, updates)
        elif command == "committed":
            update_committed_state(repo, policy, state_path, updates)
        elif command == "aborted":
            return 0
        else:
            raise HookReject("HOOK_UNSUPPORTED_PHASE", phase=command)
    except HookReject as exc:
        print(f"git-guard: {exc}", file=sys.stderr)
        if exc.code == "WORKTREE_BRANCH_CREATION_NOT_ALLOWED":
            print(
                "git-guard: branch creation is blocked only in this linked worktree; "
                "create the branch by adding a new worktree directory from the main worktree instead.",
                file=sys.stderr,
            )
        source_path = policy_hint_path(repo, policy)
        if source_path:
            print(f"git-guard: see policy: {source_path}", file=sys.stderr)
        print(f"git-guard: agent guidance: {AGENT_REJECT_HINT}", file=sys.stderr)
        return 1

    return 0


def policy_hint_path(repo: Path, policy: dict[str, Any]) -> str | None:
    source_path = policy.get("source", {}).get("path")
    if not source_path:
        return None

    path = Path(source_path)
    if path.is_absolute():
        return str(path)
    return str((repo / path).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
