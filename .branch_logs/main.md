# main

<!-- git-guard: ref=refs/heads/main -->

- Release `V0.2` from `dev`, including the portmap gateway compose rename,
  Vite/React catalog UI, host DNS helpers, container DNS forwarding,
  catalog restart/down/start actions, repo/wt-root/branch grouping, and
  running/dead branch controls.
- Release `V0.3` from `dev`, including the host-side worktree agent, container
  access to HTTP-like portmap URLs, catalog compose restart/down/start actions,
  submodule discovery across superproject worktrees, corrected submodule branch
  display semantics, and docs clarifying that portmap manages port resources
  and endpoint indexes rather than protocol behavior.
- Release `V0.6` from `dev`: add local client discovery, direct/SSH connections,
  isolated split DNS and one shared native HTTP gateway with projected catalog
  links. Verify real Docker/Podman Debian VM access, ordinary .coder SSH
  configuration, browser routing, private-state cleanup and immediate restart
  behavior. Homebrew supplies the native CoreDNS and Traefik dependencies.
- Release `V0.7` from `dev`: distribute an independent `portmap-client` bundle
  through the catalog web page, with versioned manifests, verified archives,
  and a user-space installer. Publish the verified Linux amd64/glibc 2.31+
  artifact; remove client commands and native-client dependencies from the
  server installation path. Verify frozen execution without Python/Homebrew,
  real SSH/DNS access and teardown, browser downloads, installed server assets,
  and 147 focused regressions.
- Release `V0.8` from `dev`: fix cold-cache downloads through signed GitHub
  asset-CDN URLs while retaining all redirect trust checks. Preserve V0.7
  immutability; publish matching 0.8.0 package/client metadata and artifacts.
  Verify real upstream fetching with no monkeypatches, 99 focused regressions,
  causal pre-fix failure, and the rebuilt standalone bundle without Python.
- Release `V0.9` from `dev`: display real terminal download progress in the
  piped client installer, keep non-terminal output quiet with visible failures,
  and announce verification/extraction stages. Verify the descriptor boundary
  with 66 focused regressions and actual archive installation under a PTY.
  Include the hostname-derived source gateway default.
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

- Release `V0.12` (`portmap 0.12.0`): unify infrastructure in Docker/rootless
  Podman containers while keeping CLI/agent/SSH ordinary-user owned. Setup
  retains the private port-80 entry; local and LAN publication remain explicit.
- Deliver the accepted Linux amd64 immutable bundle and matching source
  manifest. Include readiness-gated native/Compose migration with rollback and
  resumable retirement, targeted multi-machine catalog controls and LAN isolation.
- Verify 590 source tests and the real two-VM runtime/installer matrix, then
  restore original NSS/dependency state and remove scoped test resources without
  changing original business, global DNS, unrelated services or VM power/RAM.
