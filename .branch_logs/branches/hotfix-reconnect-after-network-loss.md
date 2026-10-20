# hotfix/reconnect-after-network-loss

- Base: main v0.20.0. Prepare patch version 0.20.1 through hotfix -> dev -> main, with immutable tag/Brew distribution.
- mj emergency recovery: restore resolved and its stub consumer path, reconnect the saved voxmani SSH transport, and verify real portal Access OK.
- Root cause: hibernation resume triggered NetworkManager resolver-file writes; Tailscale's boot-selected direct manager repeatedly reapplied its resolver file and restarted resolved until start-limit-hit. Portmap's resolved adapter did not perform those writes/restarts.
- Persistent host fix: NetworkManager dns=systemd-resolved/rc-manager=unmanaged, owned stub link and controlled Tailscale restart over LAN. New Tailscale PID selected resolvedManager; NM reload preserved resolver PID, split routes and portal HTTP200. Preserve rollback backups; no forced hibernation or physical link disruption.
- Add saved SSH maintenance to the existing agent lifetime, with cancellation, per-profile backoff, ownership/identity checks, and disconnect-wins serialization. Runtime publication never bootstraps infrastructure or changes host DNS.
- Carry forward already-verified draft discovery and planning/build permission separation from dev; do not rewrite existing release tags or artifacts.
- Candidate 0e9074a verification: full non-integration gate exited 0 with 1418 passed, 2 deselected (180.33s); all changed-area tests included.
- Independent frozen build exited 0; manifest/checksum validation passed and the executable reported portmap 0.20.1. Canonical project Skill returned HTTP 200 Markdown on both private and public owned loopback listeners.
- Real isolated Docker/SSH acceptance exited 0: saved voxmani profile connected within 2s; killing only the smoke-owned SSH master produced HTTP 502, then withdrawn route 404, then a new master and genuine remote registry HTTP 200 about 8s later. Explicit disconnect remained disconnected for 12s with maintenance active.
- Smoke used container ingress and host_dns=False, without changing host DNS or existing connections. Its maintenance thread, SSH masters, containers, network and temporary script/state were cleaned up.
