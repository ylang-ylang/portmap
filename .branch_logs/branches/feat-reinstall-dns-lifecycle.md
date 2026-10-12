# feat/reinstall-dns-lifecycle

<!-- git-guard: ref=refs/heads/feat/reinstall-dns-lifecycle -->

- Keep real unified portmap 0.10 background workers alive when the launching
  SSH session ends: the journal at 2026-10-12 10:40:21 showed session-5.scope
  killing process 2438373 (coredns) with SIGTERM at disconnect under logind
  `KillUserProcesses=yes`. The 11:22 reinstall was not the cause.
- Add a shared `client_runtime.ensure_persistent_scope(use_sudo=...)` lifecycle
  helper: on Linux it reads `/proc/self/cgroup`, and only when the invoking
  process sits in a disposable logind `session-<id>.scope` (including legacy
  non-numeric ids such as `session-c2.scope`) does it adopt its own PID into a
  transient system-manager scope through the proven StartTransientUnit call
  (mode `fail`, `CollectMode=inactive-or-failed`, no unit files, no boot
  enablement), then waits a bounded 5s/50ms for the new cgroup.
- Only the caller's own PID moves. CoreDNS, Traefik, the catalog view, SSH
  masters and the host agent keep running as the same unprivileged UID with
  unchanged groups, environments, recorded Popen PIDs and exact argv
  ownership; scope creation does not elevate workers, and the scope collects
  itself once its processes exit. One CLI process requests at most one scope;
  outside a login session, and on non-Linux systems, nothing is attempted.
- Call it before the first persistent spawn in all four spawn sites
  (`sync_dns` Popen, gateway `_launch`, `start_tunnel` master, agent
  `ensure_agent_started`) and propagate `use_sudo` through `setup_client`,
  `_publish`, `_open_ssh_upstream`, restore/connect dispatch, gateway
  launches, `start_server`, and the `agent start` CLI (new `--no-sudo`),
  instead of adding an environment toggle.
- Honor `--no-sudo` end-to-end: root calls busctl directly; an ordinary
  caller that declined elevation fails explicitly before any killable worker
  exists, and scope creation/adoption failures surface captured busctl output
  while keeping the existing rollback and ownership semantics (JSON CLI
  stdout stays clean; no silent fallback to a session-bound worker).
- Installation or upgrade alone neither restarts already-running old-version
  workers nor changes their lifetime; rollout stops and relaunches owned
  workers explicitly, preserving profiles and proving the launching SSH
  session closes (documented in README and docs/architecture.md).
- Regressions cover numeric and legacy session ids, no-op outside sessions
  and off Linux, adoption-once idempotence, bounded adoption acknowledgement,
  and real no-sudo refusal/rollback across each persistent worker type.
  Unit tests never move the test runner's login session or require privilege.
- Verification: 391 non-integration tests passed, one integration test
  deselected; frozen 0.11.0 CLI starts in a clean Debian container without
  installed Python, Docker client, or Brew. Real SSH-disconnect acceptance
  is verified separately from those tests.
- Real notebook acceptance passed after the actual creating sessions became
  inactive: DNS PID 3883943 survived session-11.scope; SSH PID 3890046,
  catalog-view PID 3891232 and Traefik PID 3891233 survived session-16.scope.
  All workers retained UID 1000 in transient system-manager scopes.
- Reinstalling 0.11.0 preserved the live DNS PID and successful real NSS/HTTP
  access. Restore direct access to 192.168.201.165 after the temporary SSH
  scenario; the final client has no local HTTP gateway and Lushu returns 200.
