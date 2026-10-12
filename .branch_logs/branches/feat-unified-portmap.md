# feat/unified-portmap

<!-- git-guard: ref=refs/heads/feat/unified-portmap -->

- Unify remote access and local publishing under `portmap`; remove the separate
  client command and old public download endpoints rather than retaining aliases.
- Resolve direct connections to their remote IP and actual HTTP port without a
  local HTTP listener. Normalize equivalent default-port targets.
- Share native Traefik behind exact loopback port 80 for SSH and local publishing;
  keep each SSH forward private and send LAN ingress only to the local backend.
- Bind privileged sockets through owned systemd units with DynamicUser, empty
  capabilities, fixed targets, no boot enablement and no global low-port changes.
- Keep private-backend IP filtering but allow public Podman DNAT; real cross-host
  HTTP reproduced the original drop and passed after the role-specific correction.
- Import owned legacy access state, retain SSH configuration and remote ports,
  revive saved tunnels, and preserve access when the local publishing role stops.
- Build a Linux amd64/glibc 2.31+ standalone bundle containing Python, CoreDNS,
  Traefik and all local/remote roles. Native workers use the frozen executable;
  host utilities run without the bundle's dynamic-library environment.
- Serve verified unified manifests, archives and a one-line installer. Display
  optional Homebrew installation and upgrade commands beside standalone downloads.
- Verify exact text for all three web copy buttons through the actual native
  browser clipboard, including the insecure-HTTP copy path and narrow-screen scroll.
- Verify the real bundle without host Python or Docker, and install/publish with
  Docker and Podman as uid 1000. Both portless local and cross-host demo URLs pass;
  both ingress services run without capabilities and the low-port sysctl stays 1024.
- Fix teardown callback arity found by the real upgrade workflow, retain a direct
  connection cleanup regression, and verify successful packaged teardown.
- Run 381 unit tests successfully, with one integration test deselected; privileged
  DNS, ingress and container behavior is checked through the disposable VM scenarios.
- Import legacy state only before the first saved mapping, including an empty
  mapping; explicit state overrides must never touch the default legacy workers.
  Both ownership regressions have failing-before and passing-after evidence.
- Verify cold first-SSH publication, local-plus-SSH routing, local teardown
  preserving SSH access, and last-disconnect release of port 80. A real unrelated
  listener survives an explicitly refused connection with no partial profile.
- Execute the frozen Compose plugin against the existing managed demo; both real
  containers remain running and the temporary plugin uninstalls cleanly.
- Keep local readiness within the normal gateway-probe budget: a populated host's
  valid 1.157-second catalog response was rejected by the shortened one-second
  probe. Preserve failing-before real HTTP evidence and a passing latency regression.
- Bootstrap new clients from the advertised server IP when viewing a `.portmap`
  catalog. Withhold the command until that address is known; verify the actual
  IP-based command and native clipboard on a domain-based browser page.
- Migrate the real 0.9 deployment, preserve its existing application container,
  and serve that application alongside two actual SSH remotes through private
  port 80. Verify LAN isolation and restore one lost SSH transport via `setup`
  while the other transport and local application remain available.
- Exclude generated `dist/` artifacts from source control. Reconstruct the
  unpublished source change from the published dev base in a clean, guarded
  workspace; preserve the original workspace and verified release bundle without
  rewriting protected refs or importing the accidental binary commit history.
- Sync the clean dev integration and include the required main release ledger
  for the guarded `V0.10` merge; application and test sources are unchanged.
