#!/usr/bin/env bash
set -eu
repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
git config --worktree --unset core.hooksPath >/dev/null 2>&1 || true
git config --local core.hooksPath .git-guard/hooks
printf "%s\n" "enabled git-guard hooks for repository $repo_root"
printf "%s\n" "core.hooksPath=.git-guard/hooks"
