# feat/web-managed-updates

## 2026-10-16

- Add Settings versions/build fingerprints, installation readiness, per-machine updates and remote-first batches with the manager last.
- Execute installs in target-owned durable workers through authenticated HTTP APIs, not SSH installation commands; persist jobs, code snapshots and restart recovery.
- Separate private token pairing and batch management from signed public agent APIs; bind SSH IPC to owned forwards, local starts to the served state directory, and worker ownership to UID/PID start identity.
- Preserve server/client roles, DNS and connections during recovery; verify actual agent and catalog versions. Support Homebrew upgrades and standalone bundles, including activation of newer already-installed code.
- Keep prior completion-paced catalog polling and hostname/transport machine tabs.
- Verification: independent full non-integration suite: 988 passed, 2 deselected. Real isolated rootless Podman candidate: HTTP API reports Brew 0.15.0 with candidate build identity, eligible updater and existing demo/catalog intact. Release upgrade verification is recorded separately after publication.
- Real Chromium Settings/API smoke passed. Correct initial unknown-release status to “not checked” instead of claiming “up to date”; rebuild UI and Linux amd64 archive.
