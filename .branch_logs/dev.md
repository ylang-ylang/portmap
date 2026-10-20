# dev

<!-- git-guard: ref=refs/heads/dev -->

- Merge `feat/container-http-url-dns` so portmap-managed containers can resolve
  and consume HTTP-like portmap URLs from inside Docker bridge networks.
- Include Git Guard runtime/config auto-sync updates required by the installed
  hook during the merge.
- Merge `feat/catalog-compose-restart-action` so the catalog can restart
  portmap-managed compose projects from the web UI.
- Merge `feat/uv-tool-short-commands` so new repos can use the installed
  `uv tool` command, current-directory defaults, and forwarded portmap DNS.
- Merge `feat/compose-readme-rules` so generated project onboarding documents
  include Docker Compose rules while scaffold content lives in package template
  files instead of embedded Python strings.
- Merge `feat/vite-catalog-ui` so the catalog frontend is a Vite/React app
  with a mock compose preview, project/branch panels, compact endpoint tables,
  action feedback logs, and generated onboarding guidance for user-facing
  endpoint ordering.
- Include Git Guard runtime auto-sync updates required by the installed hook
  during the merge.
- Merge `feat/default-compose-name` so the main portmap gateway compose uses
  Docker Compose's default `docker-compose.yml` filename while the mock compose
  remains separate.
- Merge `feat/host-dns-cli` so portmap can install and remove host-level
  systemd-resolved split DNS drop-ins with `portmap dns set/unset`.
- Merge `feat/fronted` so the catalog DNS status keeps red/green state colors
  in dark mode and the browser tab shows the `pM` favicon.
- Merge `feat/catalog-public-static` so the production Python catalog server
  serves Vite public root static assets such as `/favicon.svg`.
- Merge `feat/catalog-worktree-start` so the catalog groups entries by repo
  identity and linked `.git` wt root, exposes running/dead branch controls,
  and keeps startable history as dead branch discovery instead of a separate UI
  level.
- Merge `feat/transparent-compose` so the main gateway compose file remains
  transparent in repos without `.portmap` runtime state and no longer requires
  `PORTMAP_STATE_DIR` just to parse `docker compose down`.
- Merge `feat/host-agent` so the catalog can use a small host-side agent for
  Git worktree discovery and host compose starts, while docs clarify that
  portmap manages port resources and endpoint indexes rather than protocol
  behavior.
- Merge `feat/fronted` so empty running/dead branch controls are fixed-width
  and visually muted when their counts are zero.
- Merge `feat/fronted` spacing follow-up so fixed branch controls do not clip
  icons and restart/down actions have clearer separation.
- Merge `feat/branch-tip-order` so catalog branches are ordered by newest Git
  tip first and endpoint/service tables preserve `.portmap/endpoints.toml`
  declaration order.
- Merge `feat/worktree-status-badges` so running containers with deleted
  worktree directories show red deleted branch badges, and submodule checkouts
  show submodule badges beside branch names.
- Merge `feat/worktree-status-badges` follow-up so host agent worktree metadata
  overrides catalog-container path checks and avoids false deleted badges for
  existing host worktrees.
- Merge `feat/submodule-worktrees` so the host agent can discover startable
  portmap-ready submodules across sibling superproject worktrees.
- Merge `feat/submodule-display-group` so submodule instances group by parent
  repo path and show the superproject branch as the primary branch label.
- Merge `feat/submodule-branch-display` so submodule branch labels show the
  submodule repo branch while the superproject branch remains contextual
  metadata.
- Merge `feat/submodule-worktree-path-display` so synthetic submodule grouping
  keys stay internal and worktree headers show human-readable submodule paths.
- Merge `feat/submodule-worktree-icon` so submodule worktree headers include a
  compact submodule badge and icon for clearer hierarchy.
- Merge `feat/infra-http-port-80` so equal gateway HTTP/catalog ports publish
  only Traefik, with the catalog as the lowest-priority Host fallback. Preserve
  the separate default ports and document migration, DNS limits, and rollback.
- Merge `feat/docker-and-podman` so each host can run portmap on docker
  or podman through runtime socket resolution, and so portmap installs
  as a plain package (brew/pip/wheel) with packaged gateway assets,
  installed-mode shim, and an MIT license.
- Merge `feat/demo-command` so portmap ships a self-contained
  `portmap demo up|down` verification project and derives the default
  DNS domain from the machine hostname (`<hostname>.portmap`), letting
  multiple portmap hosts coexist in one client resolver.
- Merge `feat/client-connect` for workstation-side direct/SSH discovery,
  isolated split DNS, a shared native HTTP gateway, and client-aware catalog
  links. Verify simultaneous Docker/Podman Debian VM access, automatic SSH
  fallback, independent disconnect, browser routing and cleanup boundaries.
- Merge the client listener follow-up so immediate same-port restarts tolerate
  TCP TIME_WAIT without taking over an active listener; installed-package
  restart cycles and all 105 focused regressions passed.
- Merge `feat/standalone-client-download`: deliver a separate frozen
  `portmap-client` with bundled Python/CoreDNS/Traefik and manifest-backed web
  downloads. Remove client commands from the server CLI and document migration.
  Verify the real archive, isolated installer, browser UI, SSH/DNS lifecycle,
  installed server wheel, and 147 focused regressions.
- Merge `feat/client-download-redirect`: allow GitHub's signed asset-CDN
  redirects without weakening canonical URL or redirect trust checks. Verify
  real empty-cache downloads in the catalog image and a causal regression
  (old predicate fails, fixed predicate passes); 99 focused tests pass.
  Prepare matching 0.8.0 server and standalone client artifacts.
- Merge `feat/hostname-domain-default`: remove the source config's fixed
  legacy DNS domain so the server CLI inherits the per-host `.portmap` default.
  Use installed server ownership and user configuration for the deployment;
  refresh application endpoints and replace the legacy DNS rule with the
  independent client's split DNS.
- Merge `feat/client-download-progress`: show download percentage, bytes,
  rate and remaining time on terminal stderr, preserving clean redirected logs
  and visible errors. Add verification/extraction stages; verify piped-input
  and independent output descriptors with 66 regressions and real CLI installs.
- Merge `feat/unified-portmap`: one command for standalone remote access and
  local publishing, direct remote DNS without a local HTTP listener, and a
  shared private port-80 entry for SSH/local routes with separate LAN ingress.
  Deliver the unified frozen archive, installer, and optional Homebrew web
  commands; retain scoped resolver/process ownership and legacy-state migration.
- Integrate the `V0.10` release ledger from the synchronized feature branch;
  keep all build artifacts outside the clean publication history.
- Merge `feat/reinstall-dns-lifecycle`: move the invoking CLI out of a
  disposable logind session before spawning persistent workers, preserving
  ordinary-user ownership and explicit no-sudo refusal/rollback.
- Publish `V0.11` with 391 passing non-integration tests, frozen-bundle smoke,
  real creator-SSH-session termination and running same-version reinstall
  acceptance. The notebook ends in direct mode without a local HTTP listener.
- Merge `feat/default-local-ingress` for unified Docker/rootless Podman DNS,
  HTTP, catalog and SSH relay infrastructure, with a persistent private port-80
  entry, explicit local/LAN publication and shared CLI/Web remote controls.
- Verify 590 source tests plus genuine legacy rollback/resume, immutable
  installed upgrades, host/container access, logout survival and isolated
  teardown on PVE VM101/102. Restore NSS/dependency state and remove only owned
  fixtures; preserve original workloads, DNS and running 4096 MiB VM settings.
- Integrate `feat/brew-install-lifecycle`: reconcile replaced source bind mounts
  on Brew reinstall, preserve ordinary-user workers outside user-manager cleanup,
  and preflight scope permissions before retiring an existing host agent.
- Append Settings to the machine tab row for Downloads and Connections; keep
  service empty states within machine views, remove the technical metadata
  row, and use canonical machine URLs for Web access checks.
- Verify 595 source cases, real notebook Brew install/reinstall and scoped
  ordinary-user agent/state preservation. The final native UI/download check
  remains blocked when the notebook subsequently leaves the reachable network.
- Confirm the final compiled UI in a read-only recorded-data Chromium preview,
  including the local-only `本机` badge, retained drafts and keyboard/remote
  navigation; keep that evidence separate from blocked native notebook checks.
- Integrate `feat/portable-runtime` for V0.14: runtime-published HTTP, safe
  resolver-file DNS, foreground ownership and standard-entry Podman compatibility.
- Unify Compose client discovery, preserve cold-restored socket binds, and remove
  deployment-only preparation. Independent regression: 842 source cases passed,
  2 integration cases deselected; 9 tap cases passed.
- Integrate V0.15 cold-remount recovery of owned state beneath a private
  ancestor, with descriptor identity and fail-closed foreign/shared checks.
  Preserve state contents and connections; 870 non-integration tests passed.
- Integrate `feat/catalog-auto-refresh`: visible-only automatic catalog updates,
  foreground refresh, explicit error Retry, and hostname-only machine tabs.
  Hot-update mj's JS/CSS without service restarts; real browser before/after
  verifies polling, hidden-tab pause, foreground recovery, retained selection,
  compact tabs and Settings details. All three existing connections remain reachable.
- Retain SSH/direct badges beside compact machine hostnames per user clarification,
  while keeping full connection addresses hidden. Real mj browser confirms two SSH
  badges, one DIRECT badge, no local badge/address rows, and no top-level Refresh.
- Integrate `feat/web-managed-updates` for V0.16: Settings version/build identity,
  authenticated target-owned updates, durable per-machine/batch jobs and manager-last
  ordering; preserve DNS, roles and connections during explicit restart recovery.
- Independent non-integration regression: 988 passed, 2 deselected. Real isolated
  rootless Podman deployment serves the candidate API and existing demo correctly.
- Integrate final unchecked-release UI correction, verified in real Chromium.
  Frozen V0.16 CLI and safe missing-job update-worker entry smoke passed.
- Integrate V0.17: allow cold release checks to finish across SSH/IPC, retain
  fast cached reads, and include latest version in private unpaired SSH status.
- Real target checks exceeding the old 8-second limit now succeed through owned
  SSH forwards; signatures and same-version refusal verified on both targets.
  Independent final regression: 990 passed, 2 integration cases deselected.
- Integrate V0.18: consistently qualify official Brew queries and probe existing
  sudo execution instead of credential validation. Real collision/permission
  scenarios verified; no extra privileges granted. Regression: 991 passed,
  2 integration cases deselected; remove incidental argv/env echo assertions.
- Integrate requested frontend hotfix: unobtrusive serving-version footer and
  accurate SSH auto-pairing/unavailable labels. mj's 0.18 footer was deployed
  and visually verified live; label corrections were verified with recorded
  real responses but their mj transfer is blocked by lost SSH connectivity.
- Integrate `feat/reconnect-snapshot`: preserve historical compiled SSH backend
  URLs when upgrading rollback probe metadata, even after relay allocations retire.
  Affected regression: 18 passed; real mj reconnects now succeed without dropping
  saved profiles. Complete previously blocked UI deployment and live verification:
  all four machines show 0.18.0 / UP TO DATE, footer correct, three project URLs200.
- Integrate `feat/machine-connection-settings`: one Settings Machines area combines
  connection controls and update/version facts per domain; duplicate profiles stay
  editable within one card. Known failed remotes move behind an unlink icon beside
  Settings and into a collapsed Settings group.
- Real mj deployment verifies desktop/mobile same-row icon, disclosure actions,
  four machine cards, duplicate dexgem profile preservation, Add/Cancel and mujoco
  project access. Browser-only injections verify failure/recovery transitions and
  recovery from an initial list502 through shared health data without manual Retry.
- Restore official0.18 on mujoco and its mj connection; dexgem deployment remains
  externally blocked by failed Coder workspace startup/no agents. No persistence
  template changes or deferred Tailscale DNS redesign included.
- Integrate `feat/http-project-skill` as the 0.19.0 release candidate: serve
  versioned Markdown from `/skills/project-integration/SKILL.md`, maintain its
  sole source under `skills/`, and expose a guide link/copyable agent prompt.
- Flatten machine cards while preserving update/connection/error controls and
  duplicate profiles. Candidate browser, public/private HTTP, wheel and actual
  frozen-binary smoke passed; installed machines were not hot-patched.
- Add push-to-GitHub-dev immutable build/release automation and checked Brew
  formula PR distribution, including protected merge graph, retry identity,
  artifact/manifest verification, source trust and no user-machine upgrades.
- Corrected release-tool regression: 139 passed; Actionlint clean; the actual
  frozen0.19 HTTP smoke passed. Initial full-suite failures were confined to the
  new release tools/fixtures; the automatic release reruns the full suite.
- Bootstrap tap CI merged only after its real GitHub check passed (tap PR1).
  Dedicated `HOMEBREW_TAP_TOKEN` is persisted in source-repository Actions Secrets.
  Formal publication is complete only after the automatic release and tap jobs.
- Integrate `feat/hotfix-release-environment`: the first automatic release run
  stopped before tests/tagging because live Bullseye security packages returned
  HTTP404. Run source tests on the unprivileged Ubuntu runner and pin frozen
  builder APT dependencies to the signed 2025-07-01 Debian snapshot. Keep the
  Python image/glibc baseline and all publication gates unchanged.
- Synchronize V0.18 main ancestry and its release logs via
  `case/release/history-sync` -> `feat/hotfix-release-environment` -> dev.
  Full CI tests/build/HTTP gates passed in run39237869253; the sync resolves
  the subsequent pre-push merge obstruction without altering existing tags.
- Release V0.19 (portmap 0.19.0) from dev 9d5262a8d8 via the automated pipeline: compiled catalog UI/skill rebuilt, frozen linux-amd64 archive, checksum and matching manifest published to the immutable GitHub release; non-integration suite green.
- Correct the release contract in `feat/hotfix-tag-release`: maintainers prepare
  the protected main merge and matching version tag; CI only validates, builds
  and distributes the existing tag. Remove dev-triggered publication and all
  CI version allocation, source commits, branch merges and tag creation.
- Build-generated manifests bind the exact source identity and artifact checksums
  without post-build source writes. Preserve published V0.19, checked formula-only
  Brew distribution, explicit-tag retries and user-controlled machine upgrades.
- Tag-only repair acceptance: 1179 tests passed, 2 integration tests deselected;
  both workflows passed Actionlint. Real frozen candidate CLI and private/public
  Skill HTTP passed; real V0.19 was verified read-only without artifacts/rebuild.
  Sync the existing main/V0.19 refs without changing their objects. Integrating
  this repair into dev does not publish: only an existing main-history version
  tag push (or explicit-tag manual retry) enters the release pipeline.
- The isolated old-policy experiment rejected V0.20.0 and V0.20.1 with
  TAG_TARGET_TAG_PATTERN_MISMATCH. At the user's subsequent request, migrate to
  dev-feat-release-hotfix-case: release/* uses vX.Y.0; hotfix/* uses v=.=.#.
- Prepare v0.20.0 (package 0.20.0), preserve historical V0.19 read-only retries,
  and extend tag/source/manifest/updater validation to exact patch versions.
  Publication remains tag-driven with checked Brew PRs and no machine upgrades.
- Integrate release/v0.20.0 before its main merge, as required by the new graph.
  Frozen candidate/Skill HTTP and both workflow lint gates passed; the exact
  full regression result is 1337 passed, 2 deselected. No live deployment.
- Correct draft discovery after run39280508837: a published-tag API404 does not
  mean no draft exists. Use complete release-list discovery and isolate draft-aware
  planning from the read-only source build job. Retry the existing v0.20.0 draft
  and uploaded attachments without rebuilding, retagging or upgrading machines.
- hotfix/reconnect-after-network-loss: maintain saved owned SSH connections from
  the existing host agent, withdraw dead routes, and restore with bounded backoff
  after network recovery. Preserve explicit disconnects, healthy peers and remote
  identity checks; no background infrastructure bootstrap or host DNS repair.
- Diagnose mj's hibernation-triggered NetworkManager/Tailscale direct-DNS conflict.
  Persistent host configuration delegates both to resolved; reloading NM preserved
  the stub, resolved PID and working voxmani portal. Document this admin-only policy.
- hotfix/catalog-host-path-status: stop using the catalog filesystem to classify
  host project paths. Unknown/untracked stays distinct from host-confirmed deletion;
  matching agent metadata remains authoritative. Preserve running-service controls.
