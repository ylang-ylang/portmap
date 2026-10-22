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

- Release `V0.13` (`portmap 0.13.0`): support one-operation Brew setup/recovery
  against the user's existing local runtime, including same-path source mount
  replacement and ordinary-user worker survival beyond the installer service.
- Keep Settings last and right-aligned in the machine tab row, with Downloads
  and Connections inside it. Keep service empty states in machine views,
  remove the technical metadata row, and probe canonical machine catalog URLs.
- Verify 595 source cases, actual ordinary-user frozen CLI, and real notebook
  Brew install/reinstall with post-cleanup HTTP/assets and preserved state.
  Record the later notebook network outage as a limit on final native UI and
  download verification, not a successful check.
- Validate the compiled UI with recorded API data in Chromium, including the
  local-only `本机` badge and keyboard/remote navigation. Keep the final notebook
  badge/manifest synchronization pending its network recovery.

- Release `V0.14` (`portmap 0.14.0`) with portable container HTTP, resolver-file
  DNS, explicit foreground/background startup and safe standard-entry runtime
  compatibility. Docker or Podman remains required; host capabilities and
  low-port policy are never silently changed.
- Preserve private/public isolation, project DNS consumers and SSH recovery.
  Eliminate Brew-only Compose wiring and deployment-only runtime repair.
- Independent source regression: 842 passed with 2 integration cases deselected;
  tap regression: 9 passed. Frozen Linux amd64 archive SHA256:
  `42d41062f8e42d42e60da26a4e3f68f962f56ffaeca31eaad6a125d2a00398dd`.
- Release `V0.15` (`portmap 0.15.0`) to recover persistent state whose group
  permissions were reset on volume remount, only within an already-private
  owned scope. Keep foreign/shared paths refused and preserve state contents.
- Cover persisted agent, client/server and ingress records; reject symlinks,
  mismatched descriptors, hard-link aliases and parent-traversal proofs.
  Independent regression: 870 passed, 2 integration cases deselected.
- Frozen archive SHA256:
  `41dd2d75fc86f66b72021c68bc7b1c684a8181d0427cd23d1920c6a4a58c5fdd`.
- Release `V0.16` (`portmap 0.16.0`) with Web Settings version/build identities,
  authenticated target-owned updates, durable jobs and remote-first batches.
  Restore existing roles, DNS and saved connections and verify restarted versions.
- Preserve compact hostname/transport tabs and completion-paced catalog refresh.
  Independent regression: 988 passed, 2 integration cases deselected; real
  isolated Podman API and Chromium Settings smoke passed, as did frozen CLI
  and safe missing-job worker entry execution.
- Frozen archive SHA256:
  `f189d3efbb314731a9fc6cc204a09c91a2d19b6a33f1cc56dc5447d03549c550`.
- Release `V0.17` (`portmap 0.17.0`): align explicit release-check/start request
  budgets with target operations without slowing cached status probes; report
  latest version before the first SSH pairing.
- Real 9.4–9.9-second SSH checks, signed responses and same-version refusal
  verified. Independent regression: 990 passed, 2 integration cases deselected.
- Frozen archive SHA256:
  `78ede196c96706e2522340359a23c800ddf2581269c7ef667180b83815210dbd`.
- Release `V0.18` (`portmap 0.18.0`) to remove ambiguous bare Brew lookups and
  false sudo-validation refusals under an existing NOPASSWD policy. Never grant
  additional permissions; preserve failure-before-install on actual refusal.
- Real qualified Brew queries and harmless privilege execution verified;
  independent regression: 991 passed, 2 integration cases deselected.
- Frozen archive SHA256:
  `cbf81910f6f42624ea24e91af0d4ac78d52a5cb99fefad748aa27f12fdae6813`.
- Release `V0.19` (`portmap 0.19.0`): automated immutable distribution of the linux-amd64 archive, sha256 checksum and matching release manifest; the Brew tap is updated by the reusable release-distribution workflow in the same run.
- Release v0.20.0 (portmap 0.20.0): adopt dedicated release/* and hotfix/* branches;
  maintainers prepare protected main merges/tags; CI validates the exact commit,
  publishes source-bound manifests and completes checked Brew distribution.
  Remove dev-triggered publication and CI source/tag writes; preserve immutable
  retries, source-install downloads, the project Skill URL and simplified UI.
- Hotfix v0.20.1: existing-agent SSH recovery and fresh route availability after
  transport loss, with bounded retries/cancellation and preserved user disconnects.
  Publish only already-owned runtime routes; never repeatedly rewrite host DNS or
  restart resolved. Carry forward the verified draft-aware release workflow.
- Hotfix v0.20.2: prevent false DELETED labels for host paths invisible to catalog,
  including non-Git projects and missing agent records. Only host authority can
  establish deletion or Git metadata; retain existing tri-state model and actions.
- Hotfix v0.20.3: simplify update cards to a readiness button with necessary
  progress/errors, automatically check availability, and make agent guidance English.
  Keep versions/builds and direct authorization in Details; no automatic upgrades.
- Hotfix v0.20.4: support project-root-relative [compose].file in
  .portmap/endpoints.toml with CLI override priority and no invalid-path fallback.
  Use the same selection for initialization, generation, discovery and UI start.
  Document Portmap-supported layouts without imposing a Git/Compose root-file rule.
- Hotfix v0.20.5: show every valid Git worktree under configured scan roots,
  independently of Compose readiness. Keep non-startable worktrees visible in
  stopped/history lists with diagnostic reasons and disabled Start; preserve
  service/contract qualification for incidental siblings and validate endpoints.
- Hotfix candidate v0.20.6 (published baseline remains v0.20.5): package
  read-only persisted-helper activation/API verification and native-child loader
  isolation. Future fixes use the installed stable Brew opt command after an
  authorized external-starter bridge; no automatic live-engine change or
  zero-downtime claim. Formal release is pending; live deployment stays paused.
- Completed v0.20.6 publication: GitHub Release and Brew formula PR #11 succeeded.
  The authorized voxmani-only maintenance upgraded CLI/catalog/agent to 0.20.6,
  selected owned 1.9.0 helpers, and restored the seven original business IDs;
  this did not deploy the external Coder starter bridge or replace its Pod.
- Hotfix v0.20.7: use the normal five-second remote catalog probe budget during
  saved SSH restoration instead of the one-second local control-check budget.
  Preserve bounded backoff, domain/ownership checks, route-only publication,
  cancellation and healthy peers. Real delayed-HTTP regression fails before
  and passes after; focused client/discovery verification passed 131 tests.
- Hotfix v0.20.8: expose immediate submission and per-machine update progress,
  terminal attempt results and status uncertainty without expanding Details.
  Persist remote nonterminal phases, fail unfinished targets when durable-worker
  launch/relaunch fails, and invalidate release-check caches from older updaters.
  Keep legacy tag migration as an explicit official Brew bootstrap, not a
  source patch or compatibility fallback; preserve failed job history.
- Hotfix v0.20.9: eliminate repeated sibling/submodule/history Git work within
  a scan and reconcile unambiguous family display identity without changing
  raw IDs or project files. Surface incomplete inventories and refresh progress,
  retain explicitly last-known branches only for the same target, and show
  single-root branches directly. Candidate Vox scan used 171 Git commands in
  0.426 seconds; backend and real-browser verification passed. Deployment is a
  separate authorized Brew/web operation, never a side effect of publication.
