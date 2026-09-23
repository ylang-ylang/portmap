# feat/standalone-client-download

<!-- git-guard: ref=refs/heads/feat/standalone-client-download -->

- Split workstation access into `portmap-client`; remove client commands from
  the server CLI and keep client state/runtime imports independent of the server.
- Ship a Linux amd64/glibc 2.31+ frozen bundle with Python, CoreDNS, Traefik,
  packaged catalog assets, and runtime redistribution notices.
- Add manifest-backed same-origin installer/archive endpoints, bounded streaming,
  checksum-verified cache, and a visible web download panel showing only real
  published platforms.
- Install into an owned versioned user-space directory without autostart, DNS
  changes, Homebrew, or Python prerequisites. Reject unsafe archive members,
  checksum/size failures, unrelated paths, and missing required tools.
- Independently exercise the frozen executable, real SSH access through the
  bundled DNS/HTTP processes, system resolver integration, read-only symlink
  installation, immediate restart, and complete teardown without touching the
  existing server gateway.
- Verify real web downloads, a Python/Brew-free install, clipboard interaction,
  unsupported-platform display, and installer failure preservation.
- Document the 0.6 client teardown/reconnect migration, supported platform and
  resolver requirements, build procedure, trusted distribution, and server-only
  Homebrew installation.
