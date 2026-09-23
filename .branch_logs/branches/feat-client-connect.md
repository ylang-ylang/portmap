# feat/client-connect

<!-- git-guard: ref=refs/heads/feat/client-connect -->

- Add client discovery, explicit/manual SSH fallback, private OpenSSH tunnel
  lifecycle, and multi-host connection management without Coder/Tailscale APIs.
- Aggregate registered hostname.portmap zones through local CoreDNS and a
  native Traefik HTTP listener. Project remote catalogs onto the shared local
  port; preserve application Host routing and remote catalog actions.
- Use a dedicated Linux resolver link/address so systemd 249 works without
  changing global DNS. Preflight private state and attempt all owned cleanup
  steps on partial failure; never infer ownership from a PID alone.
- Verify 104 focused regressions, including real native Traefik lifecycle and
  routing, then exercise simultaneous direct Docker and SSH Podman access on
  two Debian VMs. OS DNS, browser links, auto SSH fallback, independent
  disconnect, failure cleanup, and ordinary .coder SSH configuration passed.
- Match native HTTP listener reuse semantics when reserving local ports:
  immediate restart after TCP TIME_WAIT succeeds, while a live listener still
  blocks binding. Reproduced with real TCP active-close and verified by 105
  focused regressions before the release.
