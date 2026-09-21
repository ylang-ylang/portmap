# Podman Compatibility Spike

Date: 2026-09-21. Branch: `feat/docker-and-podman`.

Goal: verify the two load-bearing assumptions for per-machine runtime
selection (`runtime = docker | podman`, one runtime per host) before any
abstraction work starts.

## Environment

- Host: debian-vm-1 (`192.168.201.188`), Debian 13 (trixie), kernel 6.12
- Docker 29.6.2, Docker Compose v5.3.1 (pre-existing)
- Podman 5.4.2 (installed via apt for this spike), rootful
  `podman.socket` at `/run/podman/podman.sock`
- `192.168.201.142` was unreachable (no authorized credential for root or
  ylang) and played no part in this spike.

## Hypothesis 1: Traefik works against the Podman compat socket

Script: `tests/spike/01-traefik-podman-socket.sh`

Traefik v3.6 runs as a Podman container with the rootful Podman socket
mounted at `/var/run/docker.sock`, stock
`--providers.docker=true --providers.docker.exposedbydefault=false`.

Result: **3/3 PASS**

- label discovery: labeled container appears as `web-a@docker` router
- events: a second labeled container is routed live without Traefik restart
- events: removing a container deregisters its route (404 afterwards)

Conclusion: Traefik's Docker provider talks to the Podman compat API
socket without code changes. The gateway data plane does not need to be
replaced for Podman.

## Hypothesis 2: docker compose v2 CLI works against the Podman socket

Script: `tests/spike/02-compose-cli-podman.sh`

`DOCKER_HOST=unix:///run/podman/podman.sock docker compose ...` with a
base file and an override using `!reset []`, extra labels, and an
external network.

Result: **10/10 PASS**

- `compose config --format json` renders against the Podman socket
  (this is exactly what `compose.py` does for inspection)
- `!reset []` removes the base port mapping in the merged config
  (client-side merge semantics, runtime-independent)
- `up -d` / `ps` / `down -v` lifecycle works; down is clean
- base + override labels merge onto the container
- `com.docker.compose.project` labels are present, so portmap's
  label-derived catalog keeps working
- external network (`podman network create`, referenced as
  `external: true`) attaches correctly
- undeclared port stays unpublished after `!reset`; declared direct TCP
  mapping (`18091:80/tcp`) publishes and serves

Conclusion: keeping the docker compose v2 CLI binary with `DOCKER_HOST`
pointed at the Podman socket preserves portmap's generated-override
semantics, including `!reset` which the single-port gateway mode needs.

## Incidental findings

- `nginx:alpine` hangs in its entrypoint under rootful Podman on this
  host: `10-listen-on-ipv6-by-default.sh` blocks in `apk manifest nginx`
  and nginx never starts (connection refused on every interface). Use a
  static-binary test image (`traefik/whoami`) for spike work. This is an
  image/apk quirk, not a Podman networking problem.
- Docker and Podman coexist on this host without interference:
  Docker's iptables `FORWARD` policy is `DROP` with its own chains,
  netavark programs a separate `inet netavark` nft table; same-bridge
  container-to-container traffic is L2-switched and unaffected.
- Rootful Podman containers use `10.89.0.0/24`-style netavark subnets;
  inter-container and host-to-container reachability works out of the box.

## Impact on the dual-runtime plan

Both spike assumptions hold, so the plan stays at "parameterize, do not
redesign":

- `settings.py`: add `runtime = auto | docker | podman` with socket
  discovery (`DOCKER_HOST`, `/var/run/docker.sock`,
  `/run/podman/podman.sock`, `$XDG_RUNTIME_DIR/podman/podman.sock`)
- `compose.py`, `cli.py`, `compose_takeover.py`, `catalog.py`, `agent.py`:
  replace hardcoded `["docker", "compose", ...]` and
  `/var/run/docker.sock` with the resolved runtime command + socket env
- gateway compose: parameterize the two socket mounts
- broker shim: same file can shadow `podman compose` too because Podman's
  provider search path includes `$HOME/.docker/cli-plugins/docker-compose`;
  the shim must export the right `DOCKER_HOST` per caller
- rootless Podman (privileged ports 53/80, socket under `XDG_RUNTIME_DIR`)
  was NOT covered by this spike and remains an open item
