# feat/branch-tip-order

<!-- git-guard: ref=refs/heads/feat/branch-tip-order -->

- Add Git branch tip metadata to catalog worktree records.
- Sort running and dead branches by newest tip first, with branch name as the
  tie-breaker.
- Update mock catalog data, built frontend assets, and tests for tip-based
  branch ordering.
- Preserve `.portmap/endpoints.toml` declaration order through generated labels,
  catalog service ordering, endpoint table ordering, mock data, and tests.
