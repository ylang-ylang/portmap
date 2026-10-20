# hotfix/all-worktree-visibility

- Based on published main v0.20.4; prepare v0.20.5 without reusing the superseded non-Git/state registry experiment.
- Explicit host worktree scan roots admit valid Git checkouts even without a Compose or endpoint contract; incidental siblings retain their existing service/contract qualification. No historical Git refs are materialized and non-Git directories remain excluded.
- Preserve valid checked-out worktrees in the existing catalog history. Recompute readiness from real files, combine missing Compose/endpoint reasons, and use the existing endpoint parser to reject empty/invalid contracts.
- Render non-startable catalog records in the existing stopped/dead list, display the readiness reason, and disable Start. Never fabricate services, routes or branch configuration.
- Update discovery regression coverage and project catalog guidance. Release uses rebuilt tracked UI assets and normal immutable publication, not installed-package patches.
