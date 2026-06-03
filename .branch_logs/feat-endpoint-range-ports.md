# feat/endpoint-range-ports

## 2026-06-03

- Add `range` endpoints for services with one entry port plus a dynamic data/media port range.
- Generate direct Docker port mappings and `PORTMAP_<ENDPOINT>_*` variables for range endpoints.
- Add `portmap init` to create `.portmap/endpoints.toml`, `.portmap/README.md`, and `.portmap/.gitignore` for managed compose repos.
- Add optional host `docker compose` takeover through `portmap shell-hook` and explicit `portmap docker-compose -- ...`.
- Document the managed compose repo shape, generated artifacts, range endpoint model, and shell wrapper switch.
- Verify with `uv run --with pytest pytest`.
