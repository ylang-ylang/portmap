from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from .common import HookReject, RefUpdate, SubmoduleBranchTip, SubmoduleGitlink, ZERO, warn
    from .config import config_bool, config_string_list
    from .git_ops import git, is_ancestor, matches_ref_pattern, short_sha
except ImportError:  # pragma: no cover - installed hook script mode
    from common import HookReject, RefUpdate, SubmoduleBranchTip, SubmoduleGitlink, ZERO, warn
    from config import config_bool, config_string_list
    from git_ops import git, is_ancestor, matches_ref_pattern, short_sha

def enforce_submodule_main_guard(repo: Path, config: dict[str, Any], updates: list[RefUpdate]) -> None:
    if not config_bool(config, "submodules", "main_guard"):
        return
    allowed_branches = config_string_list(config, "submodules", "allowed_branches")

    checked: set[tuple[str, str]] = set()
    for update in updates:
        if update.new == ZERO or not update.ref.startswith("refs/heads/"):
            continue
        for gitlink in submodule_gitlinks(repo, update.new):
            key = (gitlink.path, gitlink.sha)
            if key in checked:
                continue
            checked.add(key)
            validate_submodule_main_guard(repo, gitlink, allowed_branches)

def submodule_gitlinks(repo: Path, commit: str) -> list[SubmoduleGitlink]:
    result = git(repo, "ls-tree", "-r", "-z", commit)
    gitlinks: list[SubmoduleGitlink] = []
    for raw_entry in result.stdout.split("\0"):
        if not raw_entry:
            continue
        meta, path = raw_entry.split("\t", 1)
        parts = meta.split()
        if len(parts) == 3 and parts[0] == "160000" and parts[1] == "commit":
            gitlinks.append(SubmoduleGitlink(path=path, sha=parts[2]))
    return gitlinks

def validate_submodule_main_guard(repo: Path, gitlink: SubmoduleGitlink, allowed_branches: list[str]) -> None:
    submodule = repo / gitlink.path
    if not is_git_worktree(submodule):
        raise HookReject("SUBMODULE_REPO_MISSING", path=gitlink.path, commit=short_sha(gitlink.sha))

    if not commit_exists(submodule, gitlink.sha):
        raise HookReject("SUBMODULE_COMMIT_MISSING", path=gitlink.path, commit=short_sha(gitlink.sha))

    remote_tips = submodule_branch_tips(submodule, "refs/remotes/origin", allowed_branches)
    local_tips = submodule_branch_tips(submodule, "refs/heads", allowed_branches)
    if not remote_tips and not local_tips:
        raise HookReject(
            "SUBMODULE_ALLOWED_BRANCH_REF_MISSING",
            path=gitlink.path,
            commit=short_sha(gitlink.sha),
            allowed_branches=allowed_branches,
        )

    remote_match = first_containing_branch_tip(submodule, gitlink.sha, remote_tips)
    if remote_match is not None:
        tip, exact = remote_match
        if exact:
            return
        warn(
            submodule_remote_behind_warning_code(tip),
            path=gitlink.path,
            commit=short_sha(gitlink.sha),
            branch=tip.branch,
            remote_ref=tip.ref,
            remote_sha=short_sha(tip.sha),
        )
        return

    local_match = first_containing_branch_tip(submodule, gitlink.sha, local_tips)
    if local_match is not None:
        tip, _ = local_match
        warn(
            submodule_local_branch_warning_code(tip),
            path=gitlink.path,
            commit=short_sha(gitlink.sha),
            branch=tip.branch,
            local_ref=tip.ref,
            local_sha=short_sha(tip.sha),
        )
        return

    raise HookReject(
        "SUBMODULE_COMMIT_NOT_ALLOWED",
        path=gitlink.path,
        commit=short_sha(gitlink.sha),
        allowed_branches=allowed_branches,
        remote_refs=[tip.ref for tip in remote_tips],
        local_refs=[tip.ref for tip in local_tips],
    )

def submodule_branch_tips(repo: Path, namespace: str, allowed_branches: list[str]) -> list[SubmoduleBranchTip]:
    tips: list[SubmoduleBranchTip] = []
    ref_items: list[tuple[str, str, str]] = []
    output = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", namespace).stdout
    for line in output.splitlines():
        ref, sha = line.split(" ", 1)
        prefix = f"{namespace}/"
        if not ref.startswith(prefix):
            continue
        branch = ref.removeprefix(prefix)
        ref_items.append((branch, ref, sha))

    seen_refs: set[str] = set()
    for pattern in allowed_branches:
        for branch, ref, sha in ref_items:
            if ref in seen_refs or not matches_ref_pattern(pattern, branch):
                continue
            seen_refs.add(ref)
            tips.append(SubmoduleBranchTip(pattern=pattern, branch=branch, ref=ref, sha=sha))
    return tips

def first_containing_branch_tip(
    repo: Path,
    commit: str,
    tips: list[SubmoduleBranchTip],
) -> tuple[SubmoduleBranchTip, bool] | None:
    for tip in tips:
        if commit == tip.sha:
            return tip, True
        if is_ancestor(repo, commit, tip.sha):
            return tip, False
    return None

def submodule_remote_behind_warning_code(tip: SubmoduleBranchTip) -> str:
    if tip.pattern == "main" and tip.branch == "main":
        return "SUBMODULE_BEHIND_ORIGIN_MAIN"
    return "SUBMODULE_BEHIND_ALLOWED_REMOTE_BRANCH"

def submodule_local_branch_warning_code(tip: SubmoduleBranchTip) -> str:
    if tip.pattern == "main" and tip.branch == "main":
        return "SUBMODULE_NOT_ON_ORIGIN_MAIN_BUT_ON_LOCAL_MAIN"
    return "SUBMODULE_NOT_ON_ALLOWED_REMOTE_BRANCH_BUT_ON_LOCAL_BRANCH"

def is_git_worktree(path: Path) -> bool:
    return git(path, "rev-parse", "--show-toplevel", check=False).returncode == 0

def commit_exists(repo: Path, commit: str) -> bool:
    return git(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode == 0

def optional_rev_parse(repo: Path, ref: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", ref, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()
