# feat/web-managed-updates

## 2026-10-16

- Add Settings versions/build fingerprints, installation readiness, per-machine updates and remote-first batches with the manager last.
- Execute installs in target-owned durable workers through authenticated HTTP APIs, not SSH installation commands; persist jobs, code snapshots and restart recovery.
- Separate private token pairing and batch management from signed public agent APIs; bind SSH IPC to owned forwards, local starts to the served state directory, and worker ownership to UID/PID start identity.
- Preserve server/client roles, DNS and connections during recovery; verify actual agent and catalog versions. Support Homebrew upgrades and standalone bundles, including activation of newer already-installed code.
- Keep prior completion-paced catalog polling and hostname/transport machine tabs.
- Verification: independent full non-integration suite: 988 passed, 2 deselected. Real isolated rootless Podman candidate: HTTP API reports Brew 0.15.0 with candidate build identity, eligible updater and existing demo/catalog intact. Release upgrade verification is recorded separately after publication.
- Real Chromium Settings/API smoke passed. Correct initial unknown-release status to “not checked” instead of claiming “up to date”; rebuild UI and Linux amd64 archive.
- Synchronize dev integration before final release. Independent real-browser check confirms NOT CHECKED for unknown latest version; frozen 0.16 CLI and missing-job worker entry both execute correctly.

## 2026-10-17

- Real browser-triggered Brew update 0.15 → 0.16 succeeded after closing the page; CLI, agent and catalog reported 0.16, demo HTTP and system DNS remained healthy. Standard teardown restored exact resolver contents/dev/inode, preserved 5 original containers and 7 networks, and removed isolated runtime scaffolding.
- Bootstrapped official 0.16 on mj, mujoco, dexgem and ylang-u22; mj's three connections and three existing project HTTP routes passed. Direct management capability was verified without exposing its token.
- Found actual SSH cold checks taking 9.4–11.0 seconds against an 8-second caller budget. Separate explicit-check budgets from cached probes and align target IPC, start and aggregate deadlines for V0.17.
- Include latest_version in unsigned private SSH display status; retain the exclusion of jobs and logs. Add real-socket regressions for paired and unpaired slow checks; explicitly model the old running version in the activation regression.
- Candidate real SSH smoke passed for both Coder targets: 9.856/9.452-second checks, signed status verification and same-version HTTP409 refusal, without SSH installation commands or elevated test scope.
- mj's final UI verification is pending this development host's Tailscale browser reauthorization; do not confuse that connectivity prerequisite with target installation readiness.
- Synchronize the completed V0.16 dev integration before merging the V0.17 corrections.
- V0.18 follow-through: dexgem completed a signed-API 0.16 → 0.17 update;
  mujoco exposed ambiguous bare Brew queries in the presence of the obsolete
  snapshot tap. Qualify info/install/prefix consistently; real qualified queries
  succeeded while both taps existed, and a package-manager behavior regression
  now covers the collision without pinning command logs.
- Fix sudo preflight's credential-validation false negative: existing policy
  already permits NOPASSWD ALL, while sudo -v can still require validation.
  Harmless noninteractive execution succeeds; no privilege policy was changed.
- Independent final V0.18 regression: 991 passed, 2 integration cases deselected.
- Synchronize the V0.17 integration before merging the final preflight corrections.
- Complete real API upgrades to 0.18 on mujoco, dexgem and ylang-u22; the
  notebook subsequently upgraded to official 0.18 during its online window.
- Add a subtle 12px right-aligned page footer using the serving catalog version,
  refreshed on foreground return and completed local updates. Hot-deploy mj
  JS/CSS without restarting services; real browser verifies “Portmap v0.18.0”
  in catalog and Settings.
- Correct misleading update badges: unavailable services are not labelled
  unsupported; SSH auto-pairing does not require manual authorization. Verify
  against recorded real mj responses in Chromium. Its follow-up asset transfer
  is blocked by mj SSH becoming unreachable; do not claim this label fix live.
