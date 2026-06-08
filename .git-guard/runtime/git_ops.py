from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

try:
    from .common import HookReject, PushUpdate, RefUpdate, ZERO
except ImportError:  # pragma: no cover - installed hook script mode
    from common import HookReject, PushUpdate, RefUpdate, ZERO

GIT_LOCAL_ENV_VARS = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_QUARANTINE_PATH",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
}

def read_updates(stdin: Any) -> list[RefUpdate]:
    updates: list[RefUpdate] = []
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(" ", 2)
        if len(parts) != 3:
            raise HookReject("HOOK_INPUT_INVALID", line=line)
        updates.append(RefUpdate(old=parts[0], new=parts[1], ref=parts[2]))
    return updates

def read_push_updates(stdin: Any) -> list[PushUpdate]:
    updates: list[PushUpdate] = []
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(" ")
        if len(parts) != 4:
            raise HookReject("HOOK_PRE_PUSH_INPUT_INVALID", line=line)
        updates.append(PushUpdate(local_ref=parts[0], local_sha=parts[1], remote_ref=parts[2], remote_sha=parts[3]))
    return updates

def append_log(path: Path, phase: str, updates: list[RefUpdate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for update in updates:
            stream.write(f"{phase} {update.old} {update.new} {update.ref}\n")

def refs_matching(repo: Path, pattern: str) -> list[str]:
    refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/heads").stdout.splitlines()
    return [ref for ref in refs if re.match(pattern, ref)]

def current_branch_ref(repo: Path) -> str | None:
    result = git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()

def git_path(repo: Path, name: str) -> Path:
    result = git(repo, "rev-parse", "--path-format=absolute", "--git-path", name)
    return Path(result.stdout.strip())

def first_matching(items: list[dict[str, Any]], key: str, ref: str) -> dict[str, Any] | None:
    for item in items:
        if re.match(item[key], ref):
            return item
    return None

def matches_ref_pattern(pattern: str, ref: str) -> bool:
    regex = "^" + re.escape(pattern).replace("\\*", ".+") + "$"
    return re.match(regex, ref) is not None

def ref_exists(repo: Path, ref: str) -> bool:
    return git(repo, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0

def rev_parse(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", ref).stdout.strip()

def peeled_rev_parse(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{}}").stdout.strip()

def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    if ancestor == ZERO or descendant == ZERO:
        return False
    return git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0

def ref_contains(repo: Path, ref_or_sha: str, sha: str) -> bool:
    if ref_or_sha.startswith("refs/") and not ref_exists(repo, ref_or_sha):
        return False
    return is_ancestor(repo, sha, ref_or_sha)

def is_policy_ref(ref: str) -> bool:
    return ref.startswith("refs/heads/") or ref.startswith("refs/tags/")

def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = git_with_env(repo, os.environ.copy(), *args, check=False)
    if check and result.returncode != 0:
        raise HookReject("GIT_COMMAND_FAILED", command="git " + " ".join(args), stderr=result.stderr.strip())
    return result

def git_literal_pathspec(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_LITERAL_PATHSPECS"] = "1"
    result = git_with_env(repo, env, *args, check=False)
    if check and result.returncode != 0:
        raise HookReject("GIT_COMMAND_FAILED", command="git " + " ".join(args), stderr=result.stderr.strip())
    return result

def git_with_env(repo: Path, env: dict[str, str], *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    clean_env = clean_git_env(env)
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=clean_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise HookReject("GIT_COMMAND_FAILED", command="git " + " ".join(args), stderr=result.stderr.strip())
    return result

def clean_git_env(env: dict[str, str]) -> dict[str, str]:
    clean_env = dict(env)
    for name in GIT_LOCAL_ENV_VARS:
        clean_env.pop(name, None)
    return clean_env

def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise HookReject("ENV_MISSING", name=name)
    return value

def short_sha(value: str | None) -> str | None:
    if value is None:
        return None
    if value == ZERO:
        return ZERO
    return value[:12]

def format_version(version: tuple[int, ...] | None) -> str | None:
    if version is None:
        return None
    return "v" + ".".join(str(part) for part in version)
