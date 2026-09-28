# feat/client-download-progress

<!-- git-guard: ref=refs/heads/feat/client-download-progress -->

- Show curl's full download meter when stderr is a terminal, even when the
  installer is piped into `sh`; keep redirected output quiet without hiding
  HTTP failures.
- Announce verification and extraction stages so successful download completion
  is not followed by unexplained silence.
- Defend the stdin/stdout/stderr distinction with real PTY regression cases.
  Verify 66 focused tests, actual changing progress with the real archive,
  redirected logs, HTTP failure cleanup, and an installed server wheel.
- Publish matching 0.9.0 server/client artifacts without changing client runtime
  behavior or existing user connections.
