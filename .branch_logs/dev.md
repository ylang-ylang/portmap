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
