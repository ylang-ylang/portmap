from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    from .common import warn
    from .config import config_bool, config_int, config_string
    from .git_ops import current_branch_ref, git_with_timeout
except ImportError:  # pragma: no cover - installed hook script mode
    from common import warn
    from config import config_bool, config_int, config_string
    from git_ops import current_branch_ref, git_with_timeout


def auto_push_after_commit(repo: Path, policy: dict[str, Any], config: dict[str, Any]) -> None:
    if not config_bool(config, "auto_push", "enabled"):
        return

    ref = current_branch_ref(repo)
    if ref is None:
        return
    if ref in set(policy.get("protected_refs", [])) and not config_bool(config, "auto_push", "include_protected"):
        return

    remote = config_string(config, "auto_push", "remote")
    timeout = config_int(config, "auto_push", "timeout_seconds")

    upstream = git_with_timeout(repo, timeout, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    if upstream is not None and upstream.returncode == 0:
        ahead = git_with_timeout(repo, timeout, "rev-list", "--count", "@{u}..HEAD", check=False)
        if ahead is not None and ahead.returncode == 0 and ahead.stdout.strip() == "0":
            return

    result = git_with_timeout(repo, timeout, "push", "-u", remote, f"{ref}:{ref}", check=False)
    if result is None:
        warn("AUTO_PUSH_FAILED", ref=ref, remote=remote, stderr=f"timeout after {timeout}s")
        return
    if result.returncode != 0:
        lines = [line for line in result.stderr.strip().splitlines() if line]
        warn("AUTO_PUSH_FAILED", ref=ref, remote=remote, stderr=lines[-1] if lines else "unknown error")
        return
    print(f"git-guard: auto-pushed {ref} -> {remote}", file=sys.stderr)
