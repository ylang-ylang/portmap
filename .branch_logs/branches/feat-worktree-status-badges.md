# feat/worktree-status-badges

<!-- git-guard: ref=refs/heads/feat/worktree-status-badges -->

- Mark running services whose recorded worktree directory no longer exists as
  `deleted` in catalog metadata.
- Mark services and worktrees that are Git submodule checkouts as `submodule`
  with their superproject path.
- Show deleted/submodule badges next to branch names in the catalog UI; deleted
  branch names render in red.
- Update mock catalog data, built frontend assets, and catalog tests for both
  worktree status cases.
- Treat host agent worktree metadata as authoritative when enriching service
  records, so the catalog container does not mark existing host paths as deleted
  just because they are not mounted inside the catalog container.
