# hotfix/reconnect-after-network-loss

- Base: main v0.20.0. Prepare patch version 0.20.1 through hotfix -> dev -> main, with immutable tag/Brew distribution.
- mj emergency recovery: restore resolved and its stub consumer path, reconnect the saved voxmani SSH transport, and verify real portal Access OK.
- Root cause: hibernation resume triggered NetworkManager resolver-file writes; Tailscale's boot-selected direct manager repeatedly reapplied its resolver file and restarted resolved until start-limit-hit. Portmap's resolved adapter did not perform those writes/restarts.
- Persistent host fix: NetworkManager dns=systemd-resolved/rc-manager=unmanaged, owned stub link and controlled Tailscale restart over LAN. New Tailscale PID selected resolvedManager; NM reload preserved resolver PID, split routes and portal HTTP200. Preserve rollback backups; no forced hibernation or physical link disruption.
- Add saved SSH maintenance to the existing agent lifetime, with cancellation, per-profile backoff, ownership/identity checks, and disconnect-wins serialization. Runtime publication never bootstraps infrastructure or changes host DNS.
- Carry forward already-verified draft discovery and planning/build permission separation from dev; do not rewrite existing release tags or artifacts.
