from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from .common import HookReject
    from .config import config_bool
    from .git_ops import git
except ImportError:  # pragma: no cover - installed hook script mode
    from common import HookReject
    from config import config_bool
    from git_ops import git


MANAGED_ROOT = ".git-guard"


def enforce_git_guard_managed_files_staged(repo: Path, config: dict[str, Any]) -> None:
    if not config_bool(config, "runtime", "require_managed_files_staged"):
        return
    if not git_guard_managed_files_are_tracked(repo):
        return

    files = unstaged_or_untracked_git_guard_files(repo)
    if files:
        raise HookReject(
            "GIT_GUARD_MANAGED_FILES_NOT_STAGED",
            path=MANAGED_ROOT,
            files=files[:5],
        )


def git_guard_managed_files_are_tracked(repo: Path) -> bool:
    result = git(repo, "ls-files", "--", MANAGED_ROOT)
    return bool(result.stdout.strip())


def unstaged_or_untracked_git_guard_files(repo: Path) -> list[str]:
    result = git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--", MANAGED_ROOT)
    files: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        path = line[3:]
        if status == "??" or status[1] != " ":
            files.append(path)
    return files
