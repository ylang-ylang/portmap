from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from .common import BranchLogSettings, HookReject, RefUpdate, ZERO
    from .config import DEFAULT_CONFIG
    from .git_ops import current_branch_ref, git, git_literal_pathspec, git_path, short_sha
    from .policy import is_allowed_branch_ref
except ImportError:  # pragma: no cover - installed hook script mode
    from common import BranchLogSettings, HookReject, RefUpdate, ZERO
    from config import DEFAULT_CONFIG
    from git_ops import current_branch_ref, git, git_literal_pathspec, git_path, short_sha
    from policy import is_allowed_branch_ref


BRANCH_LOG_BRANCHES_DIR = "branches"
BRANCH_LOG_REF_MARKER = "<!-- git-guard: ref="


def branch_log_settings(config: dict[str, Any]) -> BranchLogSettings:
    section = config.get("branch_logs", {})
    if not isinstance(section, dict):
        raise HookReject("CONFIG_INVALID", key="branch_logs", expected="object")

    raw_path = section.get("path", DEFAULT_CONFIG["branch_logs"]["path"])
    if not isinstance(raw_path, str):
        raise HookReject("CONFIG_INVALID", key="branch_logs.path", expected="string")

    path = normalize_branch_log_path(raw_path)
    force_diff_required = section.get(
        "force_diff_required",
        DEFAULT_CONFIG["branch_logs"]["force_diff_required"],
    )
    if not isinstance(force_diff_required, bool):
        raise HookReject("CONFIG_INVALID", key="branch_logs.force_diff_required", expected="boolean")

    return BranchLogSettings(path=path, force_diff_required=force_diff_required)


def normalize_branch_log_path(raw_path: str) -> str:
    path = raw_path.strip().replace("\\", "/")
    if not path:
        raise HookReject("CONFIG_INVALID", key="branch_logs.path", expected="non-empty repo-relative directory path")
    if not path.endswith("/"):
        raise HookReject("CONFIG_INVALID", key="branch_logs.path", expected="directory path ending with /")
    stripped = path.rstrip("/")
    if stripped.startswith("/") or stripped == "." or stripped.startswith("../") or "/../" in stripped or stripped.endswith("/.."):
        raise HookReject("CONFIG_INVALID", key="branch_logs.path", expected="repo-relative path without ..")
    if stripped == ".git" or stripped.startswith(".git/"):
        raise HookReject("CONFIG_INVALID", key="branch_logs.path", expected="path outside .git")
    return path


def validate_pre_commit(
    repo: Path,
    policy: dict[str, Any],
    config: dict[str, Any],
    require_branch_log_change: bool,
) -> None:
    settings = branch_log_settings(config)
    if not settings.force_diff_required:
        return

    ref = current_branch_ref(repo)
    if ref is None:
        return
    if not is_allowed_branch_ref(policy, ref):
        return

    owner_path = branch_log_path_for_ref(policy, settings, ref)
    created_owner_path = ensure_branch_log_file(repo, owner_path, ref)
    ensure_branch_log_owner(repo, owner_path, ref)

    created_owner = owner_path if created_owner_path else None
    untracked = [
        path
        for path in branch_log_untracked_files(repo, settings.path)
        if path != created_owner
    ]
    if untracked:
        raise HookReject(
            "BRANCH_LOG_UNTRACKED",
            path=settings.path,
            files=untracked[:5],
        )

    unstaged = [
        path
        for path in branch_log_unstaged_files(repo, settings.path)
        if path != created_owner
    ]
    if unstaged:
        raise HookReject(
            "BRANCH_LOG_UNSTAGED",
            path=settings.path,
            files=unstaged[:5],
        )

    if require_branch_log_change and branch_log_file_has_staged_diff(repo, owner_path):
        return
    raise HookReject("BRANCH_LOG_CHANGE_REQUIRED", ref=ref, path=owner_path)


def prepare_merge_commit(repo: Path, config: dict[str, Any]) -> None:
    return


def merge_in_progress(repo: Path) -> bool:
    return git_path(repo, "MERGE_HEAD").exists()


def enforce_branch_log_update(repo: Path, policy: dict[str, Any], config: dict[str, Any], update: RefUpdate) -> None:
    if not update.ref.startswith("refs/heads/"):
        return
    if update.old == ZERO or update.new == ZERO:
        return
    if not is_allowed_branch_ref(policy, update.ref):
        return

    settings = branch_log_settings(config)
    if not settings.force_diff_required:
        return

    owner_path = branch_log_path_for_ref(policy, settings, update.ref)
    for commit in first_parent_commits(repo, update.old, update.new):
        if not commit_changes_path(repo, commit, owner_path):
            raise HookReject(
                "BRANCH_LOG_DIFF_REQUIRED",
                ref=update.ref,
                path=owner_path,
                commit=short_sha(commit),
            )


def branch_log_path_for_ref(policy: dict[str, Any], settings: BranchLogSettings, ref: str) -> str:
    branch = branch_name_from_ref(ref)
    root = settings.path.rstrip("/")
    filename = f"{branch_log_slug(branch)}.md"
    if branch in policy.get("branches", {}).get("long_lived", []):
        return f"{root}/{filename}"
    return f"{root}/{BRANCH_LOG_BRANCHES_DIR}/{filename}"


def branch_name_from_ref(ref: str) -> str:
    prefix = "refs/heads/"
    if not ref.startswith(prefix):
        raise HookReject("BRANCH_LOG_REF_INVALID", ref=ref)
    return ref[len(prefix):]


def branch_log_slug(branch: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
    return slug or "branch"


def ensure_branch_log_file(repo: Path, owner_path: str, ref: str) -> bool:
    path = repo / owner_path
    if path.exists():
        return False
    branch = branch_name_from_ref(ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(branch_log_template(branch, ref), encoding="utf-8")
    return True


def branch_log_template(branch: str, ref: str) -> str:
    return "\n".join(
        [
            f"# {branch}",
            "",
            f"{BRANCH_LOG_REF_MARKER}{ref} -->",
            "",
        ]
    )


def ensure_branch_log_owner(repo: Path, owner_path: str, ref: str) -> None:
    path = repo / owner_path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HookReject("BRANCH_LOG_READ_FAILED", path=owner_path, error=str(exc)) from exc

    actual_ref = branch_log_marker_ref(text)
    if actual_ref and actual_ref != ref:
        raise HookReject(
            "BRANCH_LOG_NAME_COLLISION",
            path=owner_path,
            ref=ref,
            existing_ref=actual_ref,
        )


def branch_log_marker_ref(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(BRANCH_LOG_REF_MARKER) and stripped.endswith("-->"):
            return stripped[len(BRANCH_LOG_REF_MARKER):-3].strip()
    return None


def branch_log_file_has_staged_diff(repo: Path, path: str) -> bool:
    result = git_literal_pathspec(repo, "diff", "--cached", "--quiet", "--exit-code", "--", path, check=False)
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise HookReject("GIT_COMMAND_FAILED", command=f"git diff --cached --quiet --exit-code -- {path}", stderr=result.stderr.strip())


def first_parent_commits(repo: Path, old: str, new: str) -> list[str]:
    result = git(repo, "rev-list", "--first-parent", "--reverse", f"{old}..{new}")
    return [line for line in result.stdout.splitlines() if line]


def commit_changes_path(repo: Path, commit: str, path: str) -> bool:
    parents = git(repo, "rev-list", "--parents", "-n", "1", commit).stdout.strip().split()
    if len(parents) <= 1:
        result = git_literal_pathspec(repo, "diff-tree", "--root", "--quiet", "--exit-code", "-r", commit, "--", path, check=False)
    else:
        result = git_literal_pathspec(repo, "diff", "--quiet", "--exit-code", parents[1], commit, "--", path, check=False)
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise HookReject("GIT_COMMAND_FAILED", command=f"git diff --quiet --exit-code {commit} -- {path}", stderr=result.stderr.strip())


def branch_log_untracked_files(repo: Path, path: str) -> list[str]:
    untracked = git_literal_pathspec(repo, "ls-files", "--others", "--exclude-standard", "--", path).stdout.splitlines()
    ignored = git_literal_pathspec(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--", path).stdout.splitlines()
    return sorted(set(untracked + ignored))


def branch_log_unstaged_files(repo: Path, path: str) -> list[str]:
    result = git_literal_pathspec(repo, "diff", "--name-only", "--", path)
    return result.stdout.splitlines()
