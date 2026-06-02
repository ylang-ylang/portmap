from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

try:
    from .common import BranchLogSettings, HookReject, RefUpdate, ZERO
    from .config import DEFAULT_CONFIG
    from .git_ops import current_branch_ref, git_literal_pathspec, git_path, short_sha
    from .policy import is_allowed_branch_ref
except ImportError:  # pragma: no cover - installed hook script mode
    from common import BranchLogSettings, HookReject, RefUpdate, ZERO
    from config import DEFAULT_CONFIG
    from git_ops import current_branch_ref, git_literal_pathspec, git_path, short_sha
    from policy import is_allowed_branch_ref

def branch_log_settings(config: dict[str, Any]) -> BranchLogSettings:
    section = config.get("branch_logs", {})
    if not isinstance(section, dict):
        raise HookReject("CONFIG_INVALID", key="branch_logs", expected="object")

    raw_path = section.get("path", DEFAULT_CONFIG["branch_logs"]["path"])
    if not isinstance(raw_path, str):
        raise HookReject("CONFIG_INVALID", key="branch_logs.path", expected="string")

    normalized_raw_path = raw_path.strip().replace("\\", "/")
    path = normalize_branch_log_path(raw_path)
    is_directory = normalized_raw_path.endswith("/")
    force_required = section.get("force_required", DEFAULT_CONFIG["branch_logs"]["force_required"])
    if not isinstance(force_required, bool):
        raise HookReject("CONFIG_INVALID", key="branch_logs.force_required", expected="boolean")

    return BranchLogSettings(path=path, force_required=force_required, is_directory=is_directory)

def normalize_branch_log_path(raw_path: str) -> str:
    path = raw_path.strip().replace("\\", "/")
    if not path:
        raise HookReject("CONFIG_INVALID", key="branch_logs.path", expected="non-empty repo-relative path")
    if path.startswith("/") or path == "." or path.startswith("../") or "/../" in path or path.endswith("/.."):
        raise HookReject("CONFIG_INVALID", key="branch_logs.path", expected="repo-relative path without ..")
    if path == ".git" or path.startswith(".git/"):
        raise HookReject("CONFIG_INVALID", key="branch_logs.path", expected="path outside .git")
    return path

def validate_pre_commit(
    repo: Path,
    policy: dict[str, Any],
    config: dict[str, Any],
    require_branch_log_change: bool,
) -> None:
    settings = branch_log_settings(config)
    if settings.is_directory:
        ensure_branch_log_gitkeep(repo, settings)

    untracked = branch_log_untracked_files(repo, settings.path)
    if untracked:
        raise HookReject(
            "BRANCH_LOG_UNTRACKED",
            path=settings.path,
            files=untracked[:5],
        )

    unstaged = branch_log_unstaged_files(repo, settings.path)
    if unstaged:
        raise HookReject(
            "BRANCH_LOG_UNSTAGED",
            path=settings.path,
            files=unstaged[:5],
        )

    if not settings.force_required:
        return

    ref = current_branch_ref(repo)
    if ref is None:
        return
    if not is_allowed_branch_ref(policy, ref):
        return
    if not require_branch_log_change:
        if branch_log_required_path_tracked(repo, settings):
            return
        ensure_branch_log_gitkeep(repo, settings)
        if branch_log_required_path_tracked(repo, settings):
            return
        raise HookReject("BRANCH_LOG_REQUIRED", ref=ref, path=settings.path)
    if meaningful_branch_log_staged_files(repo, settings):
        return
    raise HookReject("BRANCH_LOG_CHANGE_REQUIRED", ref=ref, path=settings.path)

def prepare_merge_commit(repo: Path, config: dict[str, Any]) -> None:
    settings = branch_log_settings(config)
    normalize_branch_log_directory_to_gitkeep(repo, settings)

def merge_in_progress(repo: Path) -> bool:
    return git_path(repo, "MERGE_HEAD").exists()

def normalize_branch_log_directory_to_gitkeep(repo: Path, settings: BranchLogSettings) -> None:
    if not settings.is_directory:
        return

    path = settings.path
    git_literal_pathspec(repo, "rm", "-r", "--cached", "--ignore-unmatch", "--", path)

    branch_log_path = repo / path
    if branch_log_path.is_dir():
        shutil.rmtree(branch_log_path)
    elif branch_log_path.exists():
        branch_log_path.unlink()

    ensure_branch_log_gitkeep(repo, settings)

def ensure_branch_log_gitkeep(repo: Path, settings: BranchLogSettings) -> None:
    if not settings.force_required or not settings.is_directory:
        return

    placeholder = branch_log_gitkeep_path(repo, settings)
    placeholder.parent.mkdir(parents=True, exist_ok=True)
    if not placeholder.exists():
        placeholder.write_text("", encoding="utf-8")
    git_literal_pathspec(repo, "add", "--", str(placeholder.relative_to(repo)))

def branch_log_gitkeep_path(repo: Path, settings: BranchLogSettings) -> Path:
    return repo / settings.path.rstrip("/") / ".gitkeep"

def branch_log_required_path_tracked(repo: Path, settings: BranchLogSettings) -> bool:
    if settings.is_directory:
        path = branch_log_gitkeep_path(repo, settings).relative_to(repo).as_posix()
        return bool(git_literal_pathspec(repo, "ls-files", "--cached", "--", path).stdout.splitlines())
    return branch_log_tracked_in_index(repo, settings.path)

def meaningful_branch_log_staged_files(repo: Path, settings: BranchLogSettings) -> list[str]:
    files = git_literal_pathspec(repo, "diff", "--cached", "--name-only", "--", settings.path).stdout.splitlines()
    if not settings.is_directory:
        return files
    gitkeep = branch_log_gitkeep_path(repo, settings).relative_to(repo).as_posix()
    return [path for path in files if path != gitkeep]

def enforce_branch_log_target_invariant(repo: Path, config: dict[str, Any], update: RefUpdate) -> None:
    settings = branch_log_settings(config)
    if not branch_log_path_changed(repo, update.old, update.new, settings.path):
        return
    if branch_log_tree_is_gitkeep_only(repo, update.new, settings):
        return
    raise HookReject(
        "BRANCH_LOG_TARGET_CHANGED",
        ref=update.ref,
        path=settings.path,
        old=short_sha(update.old),
        new=short_sha(update.new),
    )

def branch_log_tree_is_gitkeep_only(repo: Path, commit: str, settings: BranchLogSettings) -> bool:
    if not settings.force_required or not settings.is_directory:
        return False
    expected = f"{settings.path.rstrip('/')}/.gitkeep"
    files = git_literal_pathspec(repo, "ls-tree", "-r", "--name-only", commit, "--", settings.path).stdout.splitlines()
    return files == [expected]

def branch_log_untracked_files(repo: Path, path: str) -> list[str]:
    untracked = git_literal_pathspec(repo, "ls-files", "--others", "--exclude-standard", "--", path).stdout.splitlines()
    ignored = git_literal_pathspec(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--", path).stdout.splitlines()
    return sorted(set(untracked + ignored))

def branch_log_unstaged_files(repo: Path, path: str) -> list[str]:
    result = git_literal_pathspec(repo, "diff", "--name-only", "--", path)
    return result.stdout.splitlines()

def branch_log_tracked_in_index(repo: Path, path: str) -> bool:
    result = git_literal_pathspec(repo, "ls-files", "--cached", "--", path)
    return bool(result.stdout.splitlines())

def branch_log_path_changed(repo: Path, old: str, new: str, path: str) -> bool:
    if old == ZERO or new == ZERO:
        return False
    result = git_literal_pathspec(repo, "diff", "--quiet", "--exit-code", old, new, "--", path, check=False)
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise HookReject("GIT_COMMAND_FAILED", command=f"git diff --quiet --exit-code {old} {new} -- {path}", stderr=result.stderr.strip())
