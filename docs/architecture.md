# portmap Architecture

`portmap` is a branch-scoped network control plane for local
docker-compose projects. It does not proxy traffic itself. It translates
project/branch rules into Docker Compose overlays, Traefik labels, Traefik
entrypoints, and a local CLI-queryable endpoint registry.

The default runtime shape is a shared gateway hosted by the `portmap` tooling,
not by each managed project. The gateway runs Traefik for HTTP routing and
CoreDNS for wildcard debug-domain resolution. Managed project services join the
shared Docker bridge network `portmap_gateway` through a generated compose
override.

## Fixed Model

The default data plane is Traefik:

```text
HTTP / WebSocket / SSE / CDP / WebRTC signaling
  -> Traefik HTTP routers
  -> branch service:container_port

raw TCP / raw UDP
  -> Traefik TCP/UDP entrypoints or Docker port mappings
  -> branch service:container_port

TURN / coturn
  -> project-owned TCP/UDP service
  -> exposed like any other raw TCP/UDP endpoint
```

`portmap` owns only the control-plane pieces:

- branch/project naming rules
- shared gateway network name
- wildcard debug DNS domain
- compose inspection
- endpoint discovery or declaration
- generated compose overlays
- generated Traefik labels and entrypoints
- raw TCP/UDP host-port allocation
- local endpoint registry
- status and cleanup metadata

It does not own:

- HTTP proxy implementation
- TCP/UDP proxy implementation
- TURN credential generation
- ICE configuration
- WebRTC behavior
- application startup policy
- GPU/browser/MCP/debug workflow behavior

## Why Traefik

Traefik is the default because it can serve as one shared network data plane for:

- HTTP reverse proxy
- WebSocket reverse proxy
- SSE and streaming HTTP
- Docker label discovery
- TCP routers
- UDP routers
- dynamic container lifecycle updates

Caddy can still be a future HTTP/WebSocket-only backend, but v1 should not need
Caddy when Traefik is the default.

## Shared Gateway

Start the gateway from the `portmap` repository:

```bash
cp .env.example .env
docker compose -f docker-compose.gateway.yml up -d
```

Default shape:

```text
host:80 catalog
  -> portmap-catalog
  -> Docker socket labels
  -> current portmap-managed services and endpoints

host:8080 HTTP
  -> portmap-traefik
  -> portmap_gateway Docker network
  -> managed project service:container_port

host:53 DNS
  -> portmap-dns
  -> *.debug.lan A 192.168.201.52
```

The gateway compose creates the named Docker network:

```text
portmap_gateway
```

Generated project overrides declare that network as external and attach managed
services to it:

```yaml
services:
  frontend:
    networks:
      default:
      portmap_gateway:
    labels:
      - traefik.enable=true
      - traefik.docker.network=portmap_gateway

networks:
  portmap_gateway:
    external: true
    name: portmap_gateway
```

This keeps Traefik out of individual project compose files while still allowing
Traefik to reach Docker bridge services without per-service host port mappings.

Development machines should use split DNS:

```text
*.debug.lan -> portmap host DNS, for example 192.168.201.52
all other names -> normal DNS
```

This lets every generated Host-routing URL resolve externally without adding
per-endpoint entries to `/etc/hosts`.

Linux setup command shape:

```bash
DNS_SERVER=192.168.201.52
DNS_DOMAIN=debug.lan
DNS_IFACE="$(ip route get "$DNS_SERVER" | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"

sudo resolvectl dns "$DNS_IFACE" "$DNS_SERVER"
sudo resolvectl domain "$DNS_IFACE" "~$DNS_DOMAIN"
```

## Endpoint Classes

`portmap` should classify exposed services into three endpoint
classes.

### `http`

Examples:

- frontend dev server
- backend HTTP API
- CDP HTTP/WebSocket
- MCP over HTTP/WebSocket/SSE
- WebRTC signaling

HTTP endpoints can share one Traefik HTTP entrypoint because Traefik can route
by HTTP `Host`.

By default, generated HTTP routers rewrite the upstream request `Host` header
to:

```text
127.0.0.1:<container_port>
```

This is a generic HTTP proxy policy, not a CDP-specific rule. It makes local
debug services that reject arbitrary external Host headers work behind the
shared debug domain. Endpoint declarations can opt out when the application
needs to observe the external Host:

```toml
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 5173
preserve_host = true
```

They can also set a specific upstream Host:

```toml
[endpoints.browser_debug]
kind = "http"
service = "browser"
container_port = 19333
upstream_host = "127.0.0.1:19333"
```

Example generated labels:

```yaml
services:
  frontend:
    labels:
      - traefik.enable=true
      - traefik.http.routers.comap-feat-a-frontend.rule=Host(`frontend.feat-a.comap.debug.local`)
      - traefik.http.routers.comap-feat-a-frontend.entrypoints=web
      - traefik.http.services.comap-feat-a-frontend.loadbalancer.server.port=5173
      - traefik.http.routers.comap-feat-a-frontend.middlewares=comap-feat-a-frontend-host
      - traefik.http.middlewares.comap-feat-a-frontend-host.headers.customrequestheaders.Host=127.0.0.1:5173
      - traefik.docker.network=portmap_gateway
```

Request path:

```text
browser
  -> http://frontend.feat-a.comap.debug.local:8080
  -> Traefik web entrypoint
  -> portmap_gateway
  -> frontend container:5173
```

The browser needs the URL, not the container port.

### `tcp`

Examples:

- MQTT
- Postgres
- Redis
- MySQL
- custom TCP services

Raw TCP usually cannot share one entrypoint by hostname because it has no HTTP
`Host` header. TLS TCP with SNI is an exception, but v1 should not rely on it.

For raw TCP, `portmap` allocates a host port and generates a Traefik
TCP entrypoint plus labels.

Example generated Traefik entrypoint:

```yaml
entryPoints:
  mqtt-comap-feat-a:
    address: ":18831"
```

Example generated labels:

```yaml
services:
  mqtt:
    labels:
      - traefik.enable=true
      - traefik.tcp.routers.comap-feat-a-mqtt.entrypoints=mqtt-comap-feat-a
      - traefik.tcp.routers.comap-feat-a-mqtt.rule=HostSNI(`*`)
      - traefik.tcp.services.comap-feat-a-mqtt.loadbalancer.server.port=1883
      - traefik.docker.network=portmap_gateway
```

Request path:

```text
mqtt client
  -> 127.0.0.1:18831
  -> Traefik mqtt-comap-feat-a entrypoint
  -> mqtt container:1883
```

The service container usually does not need to know the external host port. The
client does, so the port must be written to the registry.

### `udp`

Examples:

- custom UDP services
- coturn/TURN UDP ports when the project owns coturn

Raw UDP also usually needs allocated host ports. Traefik can route UDP by
entrypoint, not by HTTP-style hostname.

Example generated Traefik entrypoint:

```yaml
entryPoints:
  udp-comap-feat-a:
    address: ":19999/udp"
```

Example generated labels:

```yaml
services:
  udp-service:
    labels:
      - traefik.enable=true
      - traefik.udp.routers.comap-feat-a-udp.entrypoints=udp-comap-feat-a
      - traefik.udp.services.comap-feat-a-udp.loadbalancer.server.port=9999
      - traefik.docker.network=portmap_gateway
```

## TURN Boundary

TURN is not a core `portmap` concept.

`portmap` should not generate TURN credentials, ICE configs, or
WebRTC application settings. If a project runs coturn, the tool treats it like
ordinary TCP/UDP endpoints.

The WebRTC application remains responsible for using and exposing its own
`iceServers` configuration, for example through env vars, config files, or a
`/turn`/`/rtc-config` endpoint.

Two valid project-level patterns:

```text
shared coturn:
  all branches use turn:<host>:3478

per-branch coturn:
  branch-a uses turn:<host>:34781
  branch-b uses turn:<host>:34782
```

`portmap` only records and exposes whichever external TCP/UDP ports
the project chooses.

## Compose Inspection

The tool should inspect rendered Compose output, preferably via:

```bash
docker compose config --format json
```

It should read:

- services
- networks
- `network_mode`
- `ports`
- `expose`
- existing labels
- compose project name

Rules:

- reject or warn on `network_mode: host` for managed endpoints
- allow normal Docker bridge networks
- prefer explicit endpoint declarations over guessing
- treat `ports` and `expose` as endpoint candidates
- generate explicit `loadbalancer.server.port` labels instead of relying on Traefik port guessing

## Repository Identity

`portmap` should distinguish repositories by Git history, not by
directory name, branch name, or current commit.

Default repo identity:

```text
repo_id = hash(first_commit_hash)
```

This means these are treated as the same repo:

- different branches of the same Git history
- different worktrees of the same Git history
- different clones of the same Git history

This is intentional. For this tool, "same repo" means "same code history root",
not necessarily "same hosting-provider project".

Human display names can still come from the directory name or remote basename:

```text
repo_id = hash(first_commit_hash)
display_name = basename(remote_url or git_root)
```

Users may override the identity explicitly:

```toml
# .portmap/repo.toml
repo_id = "comap"
display_name = "comap"
```

Resolution order:

```text
1. explicit .portmap/repo.toml repo_id
2. hash(first_commit_hash)
3. hash(git_root_path) as fallback when the first commit is unavailable
```

Known boundaries:

- shallow clones may not contain the first commit
- history rewriting with tools such as filter-repo can change the first commit
- forks with the same root commit are intentionally grouped unless `repo_id` is
  explicitly overridden

## Generated Artifacts

Generated files should live under:

```text
.portmap/
  endpoints.toml
  docker-compose.override.generated.yml
```

`endpoints.toml` is the project-local source of truth. The generated compose
override is the only project-local artifact required for HTTP endpoints because
Traefik and the catalog both read Docker labels from running containers.

Raw TCP/UDP endpoints may need additional generated state because they consume
host ports:

```text
.portmap/state.json
```

Extra Traefik static config should only exist when a runtime mode needs it. It
should not be generated as an empty file for HTTP-only projects.

The original project files should not be modified in v1:

- do not rewrite `docker-compose.yml`
- do not rewrite `.env`
- do not remove existing `ports`

## Catalog And Query

The shared gateway catalog is a core feature. It answers:

```text
which repos are running
which branch/worktree instances exist
which endpoints each instance exposes
which URL or host:port each endpoint uses
```

The catalog is derived from Docker labels on running containers. It is exposed
on host port `80`:

```text
http://portmap.debug.lan/
http://portmap.debug.lan/registry.json
```

Minimal JSON shape:

```json
{
  "services": [
    {
      "repo_name": "comap",
      "branch": "feat-a",
      "worktree": "/home/ylang/ylangs_ws/comap@worktree/comap@feat-a",
      "compose_project": "comap_feat_a",
      "compose_service": "frontend",
      "endpoints": [
        {
          "name": "frontend",
          "kind": "http",
          "url": "http://frontend.feat-a.comap.debug.lan:8080"
        },
        {
          "name": "mqtt",
          "kind": "tcp",
          "host": "127.0.0.1",
          "host_port": 18831
        }
      ]
    }
  ]
}
```

Project-local `.portmap/registry.json` is intentionally not required in the
default model. It duplicates Docker labels and can become stale when containers
are stopped or recreated.

Future CLI query commands should read the shared catalog by default:

```bash
portmap list
portmap status
portmap endpoints <repo> <instance>
```

## Design Rule

If a concern can be expressed as:

```text
branch instance + endpoint kind + container port -> external URL or host port
```

it belongs in `portmap`.

If a concern depends on WebRTC ICE behavior, TURN credentials, business flows,
GPU/browser runtime, MCP tool semantics, or application internals, it belongs
to the project or a higher-level debug workflow.
