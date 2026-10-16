# feat/portable-runtime

<!-- git-guard: ref=refs/heads/feat/portable-runtime -->

- Default to runtime-published private HTTP and resolver-file DNS without requiring systemd, systemd-resolved or host CAP_NET_ADMIN. Preserve original DNS upstreams, search/options, file identity and external edits.
- Keep private/public ingress isolated, preserve project-container DNS consumers, and provide ordinary setup plus foreground run lifecycle ownership and saved SSH recovery.
- Move verified rootless Podman network-helper compatibility into standard setup/run/up before infrastructure changes. Preserve existing engines and configuration, refuse unsafe workloads/process identities, and keep the original API alive when its working directory is unavailable.
- Select a working Docker Compose plugin or standalone Compose client inside the product; no Brew-only plugin wiring or deployment-only repair is required.
- Restore private socket bind sources before container reconciliation and recreate only mounts whose source generation changed.
- Keep source-snapshot deployment separate from public installation, with transactional cross-tap reinstall and no independent runtime preparation.
- Remove pre-patched network helpers from the nested-workspace fixture so standard startup must handle the original distribution stack.
- Final independent source regression: 842 passed, 2 integration cases deselected. Tap regression: 9 passed. Real public-Brew workspace acceptance is recorded separately after publication.
- Build Linux amd64 0.14.0 with pinned PyInstaller 6.14.1 on the glibc-2.31 baseline. Archive SHA256: 42d41062f8e42d42e60da26a4e3f68f962f56ffaeca31eaad6a125d2a00398dd; 8119841 bytes.
- Sync the V0.14 integration before fixing the real cold-rebuild failure:
  volume remounts restore group-write bits on persistent state beneath a
  still-private owned home. Standard startup must recover safely without
  manual permission edits or weakening foreign-state checks.
- V0.15 restores permission-reset state only beneath a verified pre-existing
  private owned ancestor. Descriptor identity, no-symlink traversal, single-link
  files and rejection of parent traversal prevent adopting shared/foreign data.
- Cover client, server, ingress, legacy and persisted-agent state without
  recursive chmod or changes to contents, saved connections or project data.
- Independent final regression: 870 passed, 2 integration cases deselected;
  focused read-only security review found no blocking issue.
- Frozen 0.15.0 Linux amd64 archive: 8128689 bytes; SHA256
  `41dd2d75fc86f66b72021c68bc7b1c684a8181d0427cd23d1920c6a4a58c5fdd`.
