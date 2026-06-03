# feat/multi-instance-compose-runtime

## 2026-06-03

- Move new development into its own `portmap@feat-multi-instance-compose-runtime` worktree.
- Add broker auto-generation for managed compose projects that contain `.portmap/endpoints.toml`.
- Add branch/worktree-scoped compose project metadata and inject it through the docker-compose broker unless `-p/--project-name` is explicit.
- Keep `.portmap/state.json` as worktree-local generated state.
- Add a separate host-wide raw/range allocation pool through `PORTMAP_ALLOCATION_STATE_FILE` or `PORTMAP_STATE_DIR`.
- Switch TCP and UDP endpoints from Traefik TCP/UDP entrypoints to Docker direct port mappings.
- Keep `portmap_gateway` network attachment only for services with HTTP endpoints.
- Add ignored `test_repo/` integration fixture generation with git worktree-style mock compose branches.
- Add optional Docker integration test that verifies real HTTP, TCP, UDP, and range responses.
- Verify with `uv run --with pytest pytest`.
- Verify real responses with `PORTMAP_RUN_INTEGRATION=1 uv run --with pytest pytest tests/test_integration_test_repo.py -q`.
