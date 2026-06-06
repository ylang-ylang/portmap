# dev

<!-- git-guard: ref=refs/heads/dev -->

- Merge `feat/container-http-url-dns` so portmap-managed containers can resolve
  and consume HTTP-like portmap URLs from inside Docker bridge networks.
- Include Git Guard runtime/config auto-sync updates required by the installed
  hook during the merge.
- Merge `feat/catalog-compose-restart-action` so the catalog can restart
  portmap-managed compose projects from the web UI.
- Merge `feat/uv-tool-short-commands` so new repos can use the installed
  `uv tool` command, current-directory defaults, and forwarded portmap DNS.
- Merge `feat/compose-readme-rules` so generated project onboarding documents
  include Docker Compose rules while scaffold content lives in package template
  files instead of embedded Python strings.
- Merge `feat/vite-catalog-ui` so the catalog frontend is a Vite/React app
  with a mock compose preview, project/branch panels, compact endpoint tables,
  action feedback logs, and generated onboarding guidance for user-facing
  endpoint ordering.
- Include Git Guard runtime auto-sync updates required by the installed hook
  during the merge.
- Merge `feat/default-compose-name` so the main portmap gateway compose uses
  Docker Compose's default `docker-compose.yml` filename while the mock compose
  remains separate.
- Merge `feat/host-dns-cli` so portmap can install and remove host-level
  systemd-resolved split DNS drop-ins with `portmap dns set/unset`.
- Merge `feat/fronted` so the catalog DNS status keeps red/green state colors
  in dark mode and the browser tab shows the `pM` favicon.
- Merge `feat/catalog-public-static` so the production Python catalog server
  serves Vite public root static assets such as `/favicon.svg`.
- Merge `feat/catalog-worktree-start` so the catalog groups entries by repo
  identity and linked `.git` wt root, exposes running/dead branch controls,
  and keeps startable history as dead branch discovery instead of a separate UI
  level.
- Merge `feat/transparent-compose` so the main gateway compose file remains
  transparent in repos without `.portmap` runtime state and no longer requires
  `PORTMAP_STATE_DIR` just to parse `docker compose down`.
- Merge `feat/host-agent` so the catalog can use a small host-side agent for
  Git worktree discovery and host compose starts, while docs clarify that
  portmap manages port resources and endpoint indexes rather than protocol
  behavior.
- Merge `feat/fronted` so empty running/dead branch controls are fixed-width
  and visually muted when their counts are zero.
- Merge `feat/fronted` spacing follow-up so fixed branch controls do not clip
  icons and restart/down actions have clearer separation.
- Merge `feat/branch-tip-order` so catalog branches are ordered by newest Git
  tip first and endpoint/service tables preserve `.portmap/endpoints.toml`
  declaration order.
- Merge `feat/worktree-status-badges` so running containers with deleted
  worktree directories show red deleted branch badges, and submodule checkouts
  show submodule badges beside branch names.
- Merge `feat/worktree-status-badges` follow-up so host agent worktree metadata
  overrides catalog-container path checks and avoids false deleted badges for
  existing host worktrees.
- Merge `feat/submodule-worktrees` so the host agent can discover startable
  portmap-ready submodules across sibling superproject worktrees.
- Merge `feat/submodule-display-group` so submodule instances group by parent
  repo path and show the superproject branch as the primary branch label.
