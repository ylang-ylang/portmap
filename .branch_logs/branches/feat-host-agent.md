# feat/host-agent

<!-- git-guard: ref=refs/heads/feat/host-agent -->

- Add a host-side Unix-socket agent for Git worktree discovery and compose
  starts.
- Wire the catalog container to read startable worktrees from the host agent
  and enrich running services with host worktree root metadata.
- Add `portmap up/down/restart` and `portmap agent start/stop/status/serve`
  lifecycle commands.
- Mount the agent runtime directory into the catalog container while keeping
  gateway compose defaults transparent.
- Update README and architecture docs with the agent/gateway split and the
  external network entrypoint scope.
- Clarify that portmap manages port resources and repo/worktree/branch indexes,
  not protocol behavior.
- Add TODO tracking for HTTPS plumbing, diagnostics, stale cleanup, endpoint
  discovery hints, agent integration API, and adjacent-tool comparison.
