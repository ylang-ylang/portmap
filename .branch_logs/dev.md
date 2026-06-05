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
