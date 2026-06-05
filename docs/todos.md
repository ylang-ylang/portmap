# portmap TODOs

This list tracks missing pieces and design follow-ups for `portmap`.

## Boundary

`portmap` should stay focused on port resources and how those resources are
indexed by repo, worktree root, branch, service, and endpoint.

For range-like protocols, `portmap` only manages:

- one external entry port
- one allocated host/container port range
- branch/worktree-safe allocation
- catalog metadata for humans, scripts, and agents

It does not implement TURN, RTP, FTP, SIP, RTSP, WebRTC, credentials, media
negotiation, or protocol-specific application config. The managed project owns
those details.

The host agent should remain a small host-side component. It exists because Git
worktree scanning and host-side `docker compose` starts are host-only actions.
Keep its API narrow enough that the agent can be embedded into another local
service, reused as a submodule, or run as part of the standalone `portmap`
gateway.

## Planned Work

| Item | Why It Matters | Status |
|---|---|---|
| HTTPS endpoint support | Some apps need HTTPS URLs even in local branch debugging. This should be Traefik/gateway plumbing, not app-specific logic. | Planned |
| `doctor` / setup diagnostics | DNS, gateway, agent, Docker socket, split DNS, and generated override failures need one obvious diagnostic command. | Planned |
| `prune` / stale cleanup | Moved worktrees, deleted repos, old compose projects, stale history, and unused raw/range allocations need explicit cleanup. | Planned |
| Current-repo worktree CLI | A simple host-side `portmap worktrees` / `start` / `stop` view would make debugging easier when the catalog is unavailable. | Planned |
| Endpoint discovery hints | `portmap init` should inspect rendered Compose `ports` and `expose` entries and suggest candidate endpoint declarations. | Planned |
| Computed endpoint references | Services may need another endpoint's assigned URL or host port. Add a clear, generic way to expose those values without app-specific logic. | Planned |
| Catalog action safety | Web start/down/restart actions need explicit trust-boundary docs, and may need optional token or local-only policy. | Planned |
| Agent integration API | Keep `/worktrees` and `/compose-up` stable and document how another local service can host or call the agent. | Planned |
| Status table UX | CLI and web should show repo, worktree root, branch, endpoint, external address, and compose project in a compact table. | In progress |
| Raw/range allocation visibility | The catalog should make single raw ports and allocated ranges easy to audit and copy. | In progress |

## Comparison

Scores are `1-5`; higher is better. For operational risk, higher means lower
risk. HTTPS is intentionally not scored here because it is straightforward
gateway plumbing.

| Dimension | Outport | worktree-compose | portmap |
|---|---:|---:|---:|
| Fit for Docker multi-worktree endpoint management | 3 | 2 | 5 |
| Implementation simplicity | 4 | 5 | 2 |
| Endpoint coverage | 2 | 2 | 5 |
| Git worktree support | 3 | 4 | 5 |
| Multi-repo catalog | 4 | 1 | 5 |
| Operational risk | 3 | 4 | 2 |
| Maturity | 5 | 3 | 3 |
| Developer UX | 5 | 4 | 4 |

Outport is strongest for env-driven local app URLs. worktree-compose is
strongest for a small current-repo Git worktree workflow. `portmap` is the
better fit when the resource being managed is the external network entrypoint:
HTTP URL, raw TCP port, raw UDP port, or entry port plus allocated range.
