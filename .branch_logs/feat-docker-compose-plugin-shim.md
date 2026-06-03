# feat/docker-compose-plugin-shim

## 2026-06-03

- Add Docker Compose plugin shim support so `docker compose ...` can be brokered outside interactive shell functions.
- Add `portmap broker install`, `portmap broker status`, `portmap broker doctor`, and `portmap broker uninstall`.
- Remove the old `portmap shell-hook` shell-function takeover path.
- Generate a user-level `docker-compose` plugin shim that forwards metadata to the real Docker Compose plugin.
- Avoid recursive broker calls with `PORTMAP_BROKER_BYPASS=1`.
- Strip Docker CLI plugin environment variables before delegating to the real Compose plugin.
- Verify with unit tests and a temporary `DOCKER_CONFIG` integration check.
