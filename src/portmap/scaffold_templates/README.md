# Portmap For This Compose Repo

This directory is the project-local portmap contract. It tells the shared
portmap gateway which docker-compose services may be exposed for each
branch/worktree instance.

## Required Repo Shape

The repo should provide a normal Docker Compose file:

```text
{{COMPOSE_FILE}}
```

Managed services must use Docker bridge networking. Do not use
`network_mode: host` for services declared in `{{ENDPOINT_CONFIG}}`.

## Docker Compose Rules For Portmap

Use these rules when editing `{{COMPOSE_FILE}}` for services declared in
`{{ENDPOINT_CONFIG}}`.

### Listen On The Container Interface

The application process must listen on `0.0.0.0:<container_port>`, not only
`127.0.0.1`. Traefik and other containers cannot reach a service that binds
only to loopback inside its own container.

Examples:

```yaml
services:
  frontend:
    command: npm run dev -- --host 0.0.0.0
    expose:
      - "5173"

  backend:
    command: uvicorn app:app --host 0.0.0.0 --port 8000
    expose:
      - "8000"
```

### Prefer expose Over Fixed Host Ports

HTTP/WebSocket/CDP/SSE endpoints do not need host `ports`; they share the
portmap Traefik HTTP port and are routed by generated Host names. Prefer:

```yaml
services:
  frontend:
    expose:
      - "5173"
```

Do not add fixed host ports just for portmap-managed HTTP endpoints:

```yaml
services:
  frontend:
    ports:
      - "5173:5173"
```

Raw TCP/UDP/range endpoints should also avoid fixed host ports unless the
project has a separate non-portmap reason. Portmap injects branch-scoped host
ports for `tcp`, `udp`, and `range` endpoints to avoid conflicts between
worktrees.

### Avoid Global Names

Do not set fixed `container_name` for portmap-managed services:

```yaml
services:
  backend:
    container_name: backend
```

Multiple branches/worktrees can run at the same time, so fixed container names
will collide. Let Docker Compose derive names from the portmap-generated compose
project name.

Do not rely on a fixed compose project name for portmap-managed instances.
Portmap injects a branch/worktree-scoped compose project name during
`docker compose ...` takeover.

### Do Not Define The Gateway In The Project

The project compose file should not define its own Traefik or CoreDNS services
for portmap routing. The shared portmap gateway owns those. The generated
override attaches HTTP-like services to the external `portmap_gateway` network
when needed, so the project usually does not need to declare that network by
hand.

### Keep Runtime-Specific Capabilities In The Project

Project runtime requirements such as GPU devices, privileged mode, bind mounts,
browser image contents, or application-specific environment variables still
belong in the project compose file. Portmap only manages endpoint exposure,
branch-scoped ports, generated DNS, and routing labels.

### Configure Range Services From PORTMAP Environment Variables

For `range` endpoints such as TURN, RTP, passive FTP, or similar protocols, the
service must read the injected `PORTMAP_<ENDPOINT>_*` variables and advertise
the assigned external host and port range.

For this declaration:

```toml
[endpoints.turn]
kind = "range"
service = "coturn"
container_port = 3478
protocol = "udp"
range_size = 40
```

the service can read variables such as:

```text
PORTMAP_TURN_HOST
PORTMAP_TURN_PORT
PORTMAP_TURN_RANGE_MIN_PORT
PORTMAP_TURN_RANGE_MAX_PORT
```

## Endpoint Declaration

Edit:

```text
{{ENDPOINT_CONFIG}}
```

Declare only the services that portmap should expose:

```toml
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 5173

[endpoints.mqtt]
kind = "tcp"
service = "mqtt"
container_port = 1883

[endpoints.turn]
kind = "range"
service = "coturn"
container_port = 3478
protocol = "udp"
range_size = 40
```

Use `http` for HTTP, WebSocket, SSE, CDP, and WebRTC signaling. These share the
gateway HTTP port and are routed by generated Host names.

Use `tcp` or `udp` for raw fixed-port protocols. These receive branch-scoped
host ports; clients should read the assigned port from the catalog or CLI.

Use `range` for protocols that publish one entry port plus a dynamic data/media
port range, such as coturn/TURN, SIP/RTP, RTSP/RTP, or FTP passive mode. The
project service must read the injected `PORTMAP_<ENDPOINT>_*` environment
variables and configure itself to advertise the assigned host and port range.

## Generate And Start A Branch Instance

Start the shared portmap gateway once from the portmap repo:

```bash
portmap gateway up -d
```

Gateway/domain/port defaults are read directly from the tracked
`portmap.toml` in the portmap repo root. They are not copied into this
project's `.portmap` directory.

Then, from this repo/worktree:

```bash
BRANCH="$(git branch --show-current)"

portmap generate \
  --compose-file {{COMPOSE_FILE}} \
  --config {{ENDPOINT_CONFIG}} \
  --out-dir {{OUT_DIR}} \
  --branch "$BRANCH"

docker compose \
  -f {{COMPOSE_FILE}} \
  -f {{OVERRIDE_FILE}} \
  up -d
```

Each worktree/branch runs the same source compose file plus its own generated
override. Portmap uses the repo identity and branch/worktree instance to assign
different compose project names, Host names, and raw/range ports.

For transparent `docker compose ...` takeover, install the Docker Compose
plugin shim once after installing `portmap` as a uv tool:

```bash
portmap broker install --method docker-plugin
docker compose up -d
docker compose ps
```

The shim does not rely on shell functions, so it also works for non-interactive
agents and scripts. It intercepts only `docker compose` while
`PORTMAP_COMPOSE_TAKEOVER` is enabled. Disable takeover with:

```bash
PORTMAP_COMPOSE_TAKEOVER=0 docker compose up -d
```

Without the plugin shim, call the broker explicitly:

```bash
portmap docker-compose -- up -d
```

## Query Endpoints

```bash
portmap endpoints <repo-name-or-id> "$(git branch --show-current | tr '/' '-')"
curl http://portmap.debug.lan/registry.json
```

The catalog tells external tools and agents which URL or host port to use.
HTTP-like endpoint URLs also work from portmap-managed Docker containers.
During compose override generation, portmap injects the shared portmap DNS
server into services declared in `{{ENDPOINT_CONFIG}}` so those containers can
resolve sibling repo/branch hosts such as:

```text
http://frontend.dev.other-repo.debug.lan:8080
```

This DNS injection is only applied to services listed in `{{ENDPOINT_CONFIG}}`;
ordinary compose services that are not declared as endpoints are left untouched.

## Tracked And Generated Files

Track these files:

```text
{{ENDPOINT_CONFIG}}
{{OUT_DIR}}/README.md
{{OUT_DIR}}/.gitignore
```

Do not edit generated files by hand:

```text
{{OVERRIDE_FILE}}
{{OUT_DIR}}/state.json
{{OUT_DIR}}/allocations.json
{{OUT_DIR}}/registry.json
```

`{{OUT_DIR}}/state.json` is the generated state for this worktree/branch. It
records the compose project name and generated endpoint identity.

Raw/range host-port allocations use a separate allocation pool. The broker
defaults to the shared host file `~/.local/state/portmap/allocations.json` so
different worktrees cannot pick the same host port before containers are
started. `{{OUT_DIR}}/allocations.json` is only used when a caller explicitly
uses project-local allocation state.
