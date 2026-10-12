# main

<!-- git-guard: ref=refs/heads/main -->

- Release `V0.10` (`portmap 0.10.0`) from dev: one command for remote access
  and local publishing, direct DNS-to-remote routing, shared private HTTP port 80
  for SSH/local roles, and separately scoped LAN ingress.
- Include the standalone Web installer, verified release manifest, and optional
  Homebrew install/upgrade guidance. Keep generated bundles outside Git history.
- Preserve the tested application/runtime behavior while reconstructing only the
  unpublished source changes on top of the existing published history.

- Release `V0.11` (`portmap 0.11.0`): protect newly launched DNS, SSH,
  gateway and host-agent workers from logind login-session cleanup without
  changing their user credentials or global login/DNS policy.
- Verify 391 non-integration tests and the standalone clean-container CLI.
  On the real notebook, close the actual creating SSH session, confirm the
  same DNS/SSH/view/Traefik PIDs still serve portless HTTP, reinstall the
  running 0.11.0 bundle, then restore direct access with no local HTTP gateway.
