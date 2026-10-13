# feat/default-local-ingress

<!-- git-guard: ref=refs/heads/feat/default-local-ingress -->

- Keep the shared private HTTP entry ready after setup, including empty and
  direct-only profiles and the last disconnect. Direct DNS and HTTP still go
  straight to the remote address; only explicit teardown releases the entry.
- Keep setup distinct from application publishing, LAN exposure and boot
  enablement. Refuse an occupied port 80 without stopping foreign listeners
  or falling back to another port. Preserve existing ownership and rollback.
- Update CLI, installer, web instructions, README and architecture lifecycle
  documentation. Remove obsolete tests that require a lazy native gateway;
  retain behavior checks for direct bypass, idle-port ownership and hot reload.
- Native default-entry checkpoint verification: 391 non-integration tests
  passed; frontend and frozen Linux archive built; real web surface inspected.
  Independent read-only lifecycle review found no regression in its scope.
- Before the subsequent VM-only verification instruction, notebook acceptance
  passed install, foreign-listener refusal, setup, creator-SSH-session closure,
  empty/direct transitions, teardown/restart and same-version reinstall.
  Its direct server connection was restored (DNS 192.168.201.165, HTTP 200),
  with 127.0.0.1:80 retained and LAN port 80 refused. No application container
  was started. This native 0.12.0 bundle is a candidate, not a published release.
- Publication is paused for the accepted next change: all machines will require
  Docker or Podman and DNS, HTTP gateway and catalog will share a containerized
  infrastructure. This checkpoint does not claim that migration is implemented.
  New migration validation is restricted to PVE 192.168.201.16 VM 101 (Docker)
  and VM 102 (rootless Podman), never notebook 192.168.201.102. Addresses must
  be discovered by VM ID; preserve business containers and existing profiles
  until the replacement path is proven.
- Subsequent container implementation replaces host DNS/HTTP workers with one
  owned CoreDNS/Traefik/catalog/Unix-relay stack, shared by Docker and rootless
  Podman. Private machine selection and connection management share the host
  CLI control path; public catalogs remain local-only. Native code remains
  only where needed for ownership-checked retirement, not as a fallback.
- VM-only verification: rootless Podman source setup/status and Docker frozen
  candidate setup/status passed as uid 1000, with all four private portal APIs
  returning 200 at zero connections. Docker access was scoped to the test
  processes' supplementary socket group, without account or socket changes.
  Restarting the owned agent with that process-scoped group resolved its
  runtime access. Both VMs then passed real demo publication: host and
  container .portmap DNS/HTTP, raw TCP, and actual project/branch catalog data.
  Web business containers publish no host port; private port 80 remains the
  shared ingress. The pre-existing Docker business container stayed unchanged.
- The earlier container source suite passed 536 non-integration tests (one
  integration test deselected). Subsequent origin-port regression coverage
  passed eight cases; failed-check timestamp/edit coverage passed two cases.
  Real browser save/connect-failure/edit/delete actions passed. Desktop and
  390px populated mobile pages have no horizontal page overflow; wide service
  tables scroll only inside their own wrapper.
- Real rootless Compose discovery reproduced a stale rootful socket overriding
  the healthy rootless API only through the generated takeover shim. Managed
  dispatch now reaches the shared Python resolver before passthrough socket
  guessing. The same fixture passed afterward: direct CLI and generated shim
  both returned the identical two running demo containers.
- After those fixes, the staged current source suite passed 543 tests with one
  integration test deselected in 56.92s. This does not certify the older frozen
  candidate; final artifact rebuild and install/upgrade acceptance remain open.
- Explicit IPv6 LAN publication failed before binding: the frozen candidate
  rejected the verified VM101 IPv6 with "public ingress bind address must be a
  literal IPv4 address". The initial IPv6 port-80 probe was refused; global DNS
  and NSS stayed unchanged. The corrected source now accepts unscoped IPv6,
  emits IPv6-only socket activation, and reads IPv6 installed-unit parameters.
  Real VM101 source CLI publication then passed: exact IPv6:80 plus unchanged
  private127.0.0.1:80, public local-only catalog, connection controls404,
  non-local selection404, private controls200. Neither ingress is boot-enabled.
- The ingress behavior suite passed 49 tests on VM101. A separate VM102 attempt
  executed no tests because its runtime venv has no pytest; no dependency was
  installed there merely to duplicate the test environment.
- Cross-VM SSH plus local publishing passed host/container DNS and HTTP; empty,
  direct-only and last-disconnect states retained private port 80. Browser
  restart targeted only VM101's selected demo (same IDs, new StartedAt);
  VM102's demo and the original Docker business container were unchanged.
  Web disconnect/reconnect matched CLI state and measured HTTP-path latency.
- Real direct IPv6 initially exposed an IPv4-only bridge: host HTTP succeeded,
  container HTTP was unreachable and the private remote catalog returned 502.
  New owned bridges now enable dual-stack IPAM, while existing IPv4-only
  bridges fail closed without mutation. Actual Docker API creation/IPv6 HTTP/
  owned cleanup passed. VM102 current frozen setup/up/demo/direct then passed
  host and container HTTP plus remote catalog200 after scoped QA teardown;
  the separate boundary relay and global DNS/NSS/linger remained unchanged.
  Candidate SHA256: 97ef5c69d54f4e0711f7bd804852c8be672751047e48cb007298b8c8fc7c44b7.
- The post-IPv6 staged source suite passed 550 tests with one integration test
  deselected in 57.19s. Current frozen direct catalog selection returned the
  real remote worktree and endpoints. Its explicit Web Check returned 95.87ms
  HTTP latency, an ONLINE state and a fresh check timestamp; 390px rendering
  remained within the viewport.
- A real external listener occupied VM101 private port80. Ordinary-user setup
  refused it with exit2, left that listener and every business container
  unchanged, and opened no18080 fallback. The owned test ingress was restored
  after removing only the external QA fixture; DNS and NSS were unchanged.
- A genuine checkpoint bundle now serves a second logical origin on VM101
  loopback18081 with an independent network/state/agent and a broker-created
  demo. This is not a third physical machine and not migration acceptance;
  no version1 ownership record was fabricated for this routing fixture.
- Two genuine logical origins with the same human project/branch/endpoint
  reproduced a Traefik router collision. Internal names now include repository
  and worktree identity. VM101's corrected frozen candidate routes both real
  projects to their own containers without changing public URLs.
- The catalog previously missed the documented host download cache. It now
  mounts only the resolved cache directory and upgrades existing infrastructure
  records through normal reconciliation. Both VMs served the exact 8,014,859-byte
  candidate with SHA256 fa15c58ea3cfba4e5c228c70ddb2641b6f684c8f8a0532396d8f6576148eac2a.
  Related planner/download tests passed 88 cases on VM101.
- Real PAM/logind SSH creator sessions ended on both VMs while ordinary-user
  agents and container infrastructure survived outside the deleted login scopes.
  VM102 also retained its live SSH master; host/container local and remote HTTP
  returned the expected business containers. DNS/NSS/linger and business states
  stayed unchanged. Killing an entire non-PAM sshd service was rejected as an
  invalid logout fixture, not treated as a product lifetime failure.
- Same-version installation reproduced deletion of assets used by a running
  old worker. Each install now stages a new immutable bundle and switches only
  the launcher. VM101's download/installer suite passed 81 tests, including that
  failing-before/passing-after regression. Two real installs from the private
  catalog produced distinct paths; both frozen executables still ran afterward
  and the served manifest matched the fa15 candidate checksum.
- VM102 also completed two real immutable installs with both previous
  executables remaining usable. The final relay-startup source suite passed
  585 tests, with one integration test deselected, on VM101 as the ordinary user.
- VM102's private browser surface showed local publishing and two live SSH
  origins; host and container requests reached each expected business container.
  VM101's LAN surface exposed only its local projects, with imported registries
  and connection-management APIs unavailable. Desktop and mobile views were
  exercised in the real browser.
- A genuine native installation and live SSH master were created with the old
  frozen CLI before migration. Its directly owned private port 18081 now has a
  readiness-gated handover and captured-worker rollback. The real interruption
  after native Traefik exit and before commit passed all 34 scenario and 22
  final preservation checks: native HTTP was restored with a new owned PID,
  while old DNS/view, SSH master, business containers and global state survived.
- Startup diagnostics proved HAProxy kept its empty initial revision despite a
  newly published relay file; the first reload occurred only during rollback.
  Seeding desired relay endpoints before startup and checking saved readiness
  before live updates removed that initial reload dependency. The successful
  native handover proof exercised this fix from the frozen a1b69d4e candidate.
- Server cutover rollback and interrupted legacy retirement/resume acceptance
  are still required before final installed-upgrade acceptance and publication.
- Both VMs temporarily use the explicitly authorized NSS resolver integration.
  DNS server settings are unchanged; restoring original NSS/dependency state
  is required after acceptance. Container release and full legacy migration
  remain unclaimed.
