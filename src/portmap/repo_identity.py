from __future__ import annotations

import hashlib
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .slug import slugify


@dataclass(frozen=True)
class RepoIdentity:
    repo_id: str
    display_name: str
    git_root: Path | None


def resolve_repo_identity(
    *,
    project_directory: Path,
    repo_root: Path | None,
    repo_id: str | None,
    repo_name: str | None,
) -> RepoIdentity:
    root = repo_root or git_root(project_directory)
    override = load_repo_override(root or project_directory)
    display_name = repo_name or override.get("display_name") or infer_display_name(root or project_directory)
    repo_id = repo_id or override.get("repo_id")
    if repo_id:
        return RepoIdentity(repo_id=slugify(repo_id), display_name=display_name, git_root=root)

    first_commit = git_first_commit(root) if root else None
    if first_commit:
        stable_id = stable_hash(first_commit)
    else:
        stable_id = stable_hash(str((root or project_directory).resolve()))
    return RepoIdentity(repo_id=stable_id, display_name=display_name, git_root=root)


def git_root(path: Path) -> Path | None:
    result = git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def git_first_commit(root: Path | None) -> str | None:
    if root is None:
        return None
    result = git(root, "rev-list", "--max-parents=0", "HEAD")
    if result.returncode != 0:
        return None
    line = next((item.strip() for item in result.stdout.splitlines() if item.strip()), "")
    return line or None


def git_branch(path: Path) -> str | None:
    result = git(path, "branch", "--show-current")
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def infer_display_name(path: Path) -> str:
    name = path.name
    if "@" in name:
        name = name.split("@", 1)[0]
    return slugify(name, fallback="repo")


def load_repo_override(path: Path) -> dict[str, str]:
    config_path = path / ".portmap" / "repo.toml"
    if not config_path.exists():
        return {}
    payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for key in ("repo_id", "display_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    return result


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
