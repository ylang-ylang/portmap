# feat/host-dns-cli

<!-- git-guard: ref=refs/heads/feat/host-dns-cli -->

- Add `portmap dns set` and `portmap dns unset` commands for host-level
  systemd-resolved split DNS setup.
- Add host DNS helpers for rendering, installing, removing, and restarting the
  systemd-resolved drop-in.
- Document the resolved drop-in fallback when per-link `resolvectl` DNS setup
  is unavailable.
- Cover the DNS commands with focused CLI tests.
