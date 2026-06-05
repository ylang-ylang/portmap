# feat/uv-tool-short-commands

<!-- git-guard: ref=refs/heads/feat/uv-tool-short-commands -->

- Document installing portmap as a `uv tool` for short cross-repo commands.
- Prefer current-directory defaults in new-repo onboarding examples.
- Remove unnecessary `--project-dir .` from generated `.portmap/README.md`.
- Forward non-portmap DNS queries from CoreDNS so managed containers keep public
  DNS resolution when portmap DNS is injected.
