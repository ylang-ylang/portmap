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
