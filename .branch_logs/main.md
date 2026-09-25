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
