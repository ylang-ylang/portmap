# feat/machine-connection-settings

- Combine private Settings connection controls and authenticated update controls into one Machines panel, with one card per domain and retained controls for duplicate saved profiles.
- Move known failed/disconnected remotes into native, initially collapsed Disconnected machines groups in navigation and Settings. Keep unknown/unprobed state distinct from failure and keep the serving host available.
- Use serialized visible-only connection probes independently of Settings visits; share per-profile probe status with connection rows. Preserve active-tab gating for update loading, token visibility and durable batch polling.
- Retain existing connection mutations, update authorization, manager-last ordering, jobs, footer and public/private API boundaries; no backend API changes.
- Remove proposed mock mutation handlers that did not actually model state changes; acceptance uses the real mj deployment instead.
- Build: npm run build passed. CSS and JS deployed atomically per file with preimage/staging SHA checks to mj's existing official0.18 installation, without service restart. Actual browser confirms only mj/mujoco/u22 top-level tabs and dexgem inside a closed Disconnected machines (1) group.
- Operational recovery: official Brew0.18 restored on mujoco, existing mj SSH connection restored using supported --timeout60 for the observed ~48-second Coder handshake. dexgem remains blocked by failed Coder workspace start/no agents; no workspace lifecycle or persistence changes performed.
- Tailscale/Portmap DNS redesign remains explicitly deferred.
- Final user refinement: replace the separate navigation disclosure row with a 34px unlink icon beside Settings; healthy tabs scroll independently on narrow screens. Verified icon and Settings share y=66 at1280px and y=93 at390px, no page overflow; click opens the list, Escape closes it.
- Lift connection listings into the same shared snapshot as health checks so a successful probe restores Settings after an initial list502 without manual Retry. Isolated browser injection verified initial502 followed by successful health response renders all four cards automatically.
- Move the local card ahead of remote groups. Actual final Settings has local/mujoco/u22 plus closed dexgem group; dexgem keeps two profile controls and exactly one update block. Add/Cancel and real mujoco project HTTP200 verified; synthetic unreachable→reachable transition returns the target to normal tabs.
