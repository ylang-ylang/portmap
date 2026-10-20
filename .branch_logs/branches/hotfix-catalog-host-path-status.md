# hotfix/catalog-host-path-status

- Based on main v0.20.1; that version still contained the user-reproduced namespace bug. Prepare patch version 0.20.2 without changing voxmani's installed 0.18.0 or running infrastructure.
- Root cause: container_to_service used worktree_status_from_raw/Path.exists and Git-root discovery against Docker-label host paths in the catalog namespace; absent agent records left the false deleted state intact.
- Seed the existing unknown state (worktree_exists=null, empty status and root fields), then reuse existing authoritative agent enrichment. Remove obsolete container-side Git-root probing. Host-side status inspection remains available and confirmed deletion/submodule metadata still propagates.
- Behavioral regression gate: 1423 passed, 2 deselected, rc0; tests cover invisible non-Git host paths, visible non-Git paths, unavailable/unmatched/partial agent records, host-confirmed deletion, and authoritative host submodule/root metadata.
- Real local application smoke: discovered an owned non-Git project with unknown state and HTTP200; Restart restarted exactly its one container and retained HTTP200; Down removed exactly that container and the endpoint stopped. Pre-existing projects were untouched; owned fixture resources were removed.
- No frontend label suppression, protocol expansion, host-directory mounts, Git initialization, DNS/Tailscale changes or remote package replacement.
