# Shared git-guard hook helpers. Source this file from hook wrappers.
git_guard_setup() {
  GG_REPO_ROOT="$(git rev-parse --show-toplevel)"
  git_dir="$(git rev-parse --git-dir)"
  case "$git_dir" in
    /*) resolved_git_dir="$git_dir" ;;
    *) resolved_git_dir="$GG_REPO_ROOT/$git_dir" ;;
  esac
  export GG_REPO_PATH="$GG_REPO_ROOT"
  export GG_POLICY_JSON="$GG_REPO_ROOT/.git-guard/policy.json"
  export GG_CONFIG_JSON="$GG_REPO_ROOT/.git-guard/config.json"
  export GG_STATE_JSON="$resolved_git_dir/git-guard-state.json"
  export GG_LOG_PATH="$resolved_git_dir/git-guard-hook.log"
  GG_RUNTIME="$GG_REPO_ROOT/.git-guard/runtime/policy_reference_transaction_hook.py"
}

git_guard_exec_runtime() {
  [ -f "$GG_RUNTIME" ] && [ -f "$GG_POLICY_JSON" ] || exit 0
  export PYTHONDONTWRITEBYTECODE=1
  exec python3 "$GG_RUNTIME" "$@"
}

git_guard_runtime_sync() {
  [ -f "$GG_CONFIG_JSON" ] || return 0
  current_ref="$(git symbolic-ref -q HEAD 2>/dev/null || true)"
  if [ -n "$current_ref" ] && [ -f "$GG_POLICY_JSON" ]; then
    python3 -c 'import json,sys; data=json.load(open(sys.argv[1], encoding="utf-8")); protected=data.get("protected_refs", []); sys.exit(0 if sys.argv[2] in protected else 1)' "$GG_POLICY_JSON" "$current_ref" && return 0
  fi
  python3 -c 'import json,sys; data=json.load(open(sys.argv[1], encoding="utf-8")); runtime=data.get("runtime", {}); value=runtime.get("auto_sync", True) if isinstance(runtime, dict) else True; sys.exit(0 if value is not False else 1)' "$GG_CONFIG_JSON" || return 0
  git_guard_command="${GIT_GUARD_BIN:-git-guard}"
  read -r -a git_guard_args <<< "$git_guard_command"
  command -v "${git_guard_args[0]}" >/dev/null 2>&1 || { printf "%s\n" "git-guard: runtime auto-sync skipped; git-guard command not found" >&2; return 0; }
  scope="$(git config --show-scope --get core.hooksPath 2>/dev/null | awk 'NR == 1 { print $1 }')" || scope=""
  case "$scope" in
    worktree|local|global) ;;
    *) scope="local" ;;
  esac
  if ! "${git_guard_args[@]}" install --repo "$GG_REPO_ROOT" --config "$GG_REPO_ROOT/.git-guard/contribution.md" --scope "$scope" >/dev/null; then
    printf "%s\n" "git-guard: runtime auto-sync failed; continuing with installed runtime" >&2
  fi
}

git_guard_setup
