# portmap

Are you tired of changing, checking, and remembering local ports every time you
run more than one Docker Compose branch?

For example:

```text
myrepo@dev
myrepo@feat-a
myrepo@feat-b
```

They all want to use `5173`, `8000`, or `9333`, so you start assigning ports by
hand. After a while, it gets hard to remember:

```text
Which port belongs to which branch?
Which URL points at which worktree?
Is that old container still running?
```

`portmap` is for that local development mess.

It does not replace Docker Compose. It adds the missing layer for running
multiple branches or worktrees at the same time:

```text
Docker Compose starts the containers.
portmap keeps the branch network entrypoints organized.
```

HTTP, WebSocket, and CDP-like services get stable URLs:

```text
http://frontend.dev.myrepo.debug.lan:8080
http://frontend.feat-a.myrepo.debug.lan:8080
```

TCP, UDP, TURN, and other raw-port services get non-conflicting host ports.

The catalog shows which repo, worktree, and branch are running, and how each
service should be reached.

`portmap` is intentionally narrow. It manages port resources and the index for
those resources:

```text
repo + worktree root + branch + endpoint -> URL / host port / host port range
```

It does not manage your application runtime. It does not know how your frontend
builds, how your database migrates, how TURN credentials are created, or how
WebRTC media behaves.

It manages the external network entrypoints of Docker Compose services:

```text
HTTP / WebSocket / SSE / CDP / WebRTC signaling
raw TCP
raw UDP
entry port + dynamic port range
```

Compared with env-based port allocators, `portmap` goes deeper into Docker
Compose networking. It models the endpoint shape explicitly, then generates the
Compose override, Traefik labels, port mappings, and catalog metadata needed to
reach that endpoint from outside the container.

`portmap` also avoids reimplementing the network data plane. HTTP routing is
delegated to Traefik, debug-domain DNS is delegated to CoreDNS, and raw TCP/UDP
or range exposure is delegated to Docker Compose port mappings.

## Requirements

Your project should use Docker Compose, and services managed by `portmap`
should have:

```text
Docker bridge network
stable service names
known internal container ports
```

For example, the compose file should have stable service names:

```yaml
services:
  frontend:
  backend:
```

Then declare their entrypoints in `.portmap/endpoints.toml`:

```toml
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 5173
```

Services should listen on `0.0.0.0` inside the container so the gateway can
reach them through the Docker bridge network.

If you only run one branch at a time, you may not need `portmap`. If you often
debug several worktrees or branches in parallel, it removes a lot of manual
port bookkeeping.

See [docs/todos.md](docs/todos.md) for planned work, missing pieces, and the
comparison with adjacent tools.

## Scope

`portmap` manages external endpoint resources between branches:

- inspect rendered Docker Compose services, ports, expose entries, and labels
- generate branch-scoped Docker Compose override files
- generate Traefik labels for HTTP endpoints
- generate Docker direct port mappings for raw TCP/UDP and range endpoints
- allocate non-conflicting raw TCP/UDP host ports
- keep per-worktree generated state in `.portmap/state.json`
- keep host-wide raw/range port allocation state in an allocation pool
- write a local endpoint registry for CLI queries
- clean up generated routes, ports, and instance state

`portmap` also provides a shared gateway compose file with Traefik and CoreDNS.
Projects do not need to define their own Traefik service when they use the
generated override.

The project only declares endpoint names, compose service names, and container
internal ports. Existing `ports` and `expose` entries can be treated as endpoint
candidates.

## Design Principles

`portmap` follows two hard boundaries:

```text
Manage port resources and their repo/worktree/branch index.
Do not manage protocol behavior.

Use Traefik, CoreDNS, and Docker Compose as the network data plane.
Do not implement an HTTP/TCP/UDP proxy inside portmap.
```

For example, a TURN-like endpoint is just an entry port plus an allocated port
range in `portmap`. The project still owns coturn config, credentials, ICE
server JSON, and WebRTC behavior.

## Non-goals

- no host-network mode selection
- no GPU or desktop runtime management
- no HTTP/TCP/UDP proxy implementation
- no TURN credential or ICE config management
- no range-like protocol implementation beyond port allocation and cataloging
- no business-flow orchestration
- no test selector or performance-analysis logic

## Minimal Endpoint Declaration

```toml
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 5173

# Optional. HTTP endpoints rewrite upstream Host by default.
# Set this when the service must see the external Host header.
preserve_host = true

[endpoints.mqtt]
kind = "tcp"
service = "mqtt"
container_port = 1883

[endpoints.turn_udp]
kind = "udp"
service = "coturn"
container_port = 3478
```

From that declaration the tool can generate:

- `docker-compose.override.generated.yml`
- `state.json` for this branch/worktree's generated compose project and endpoints
- raw TCP/UDP allocation state only when raw endpoints need host ports

For HTTP-only projects, `.portmap/` can stay minimal:

```text
.portmap/
  endpoints.toml
  docker-compose.override.generated.yml
  state.json
```

The running service catalog is derived from Docker labels at the shared
gateway, so project-local registry files are not required for HTTP endpoints.

HTTP endpoints default to rewriting the upstream `Host` header to
`127.0.0.1:<container_port>`. This makes strict local debug services such as
Chromium CDP accept requests that entered through external debug domains. If an
application needs the original external Host, set `preserve_host = true` for
that endpoint. If a service needs a specific upstream Host, set
`upstream_host = "host:port"`.

## CLI

Install the local checkout as a `uv` tool so other repos can call the short
`portmap` command:

```bash
uv tool install --editable /home/ylang/ylangs_ws/portmap@wt/portmap@dev --force
```

Start portmap once:

```bash
portmap up
```

This starts the host-side agent, then starts the shared gateway containers. The
agent scans host Git worktrees and handles host-side compose starts for the
catalog page; the gateway containers provide Traefik, CoreDNS, and the catalog
UI.

This creates the `portmap_gateway` Docker network and exposes:

```text
Catalog: http://<detected-host-ip>
Traefik: http://<detected-host-ip>:8080
DNS:     <detected-host-ip>:53
```

CoreDNS answers every `*.debug.lan` A record with the detected host LAN IP.
Other DNS queries are forwarded to the configured upstream resolver, defaulting
to `/etc/resolv.conf`, so portmap-managed containers can still resolve public
domains after their DNS is pointed at portmap. Configure development machines
with split DNS so only `debug.lan` queries go to this DNS server.

Gateway runtime settings are tracked in the portmap repo root:

```text
portmap.toml
```

`portmap up` and the lower-level `portmap gateway ...` command read that file
directly and detect the current host LAN IP at runtime. The detected IP is used
for DNS answers and raw/range endpoint advertisement, so the LAN IP does not
need to be stored in config.

Configure split DNS on a Linux development machine without manually looking up
the network interface:

```bash
DNS_SERVER=<detected-host-ip>
DNS_DOMAIN=debug.lan
DNS_IFACE="$(ip route get "$DNS_SERVER" | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"

sudo resolvectl dns "$DNS_IFACE" "$DNS_SERVER"
sudo resolvectl domain "$DNS_IFACE" "~$DNS_DOMAIN"
resolvectl query "portmap.$DNS_DOMAIN"
curl -I "http://portmap.$DNS_DOMAIN/"
```

On machines where the active network stack is not managed by
`systemd-networkd`, the per-link `resolvectl dns` and `resolvectl domain`
commands may fail with:

```text
Failed to set DNS configuration: Unit dbus-org.freedesktop.network1.service not found.
Failed to set domain configuration: Unit dbus-org.freedesktop.network1.service not found.
```

That failure does not mean portmap DNS is broken. It means the host cannot
accept per-link DNS settings through `resolvectl`. Do not install or enable
`systemd-networkd` solely for portmap on a machine already managed by another
network stack, such as NetworkManager. Configure a host-level
`systemd-resolved` drop-in instead. The CLI can install and remove this drop-in:

```bash
portmap dns set
portmap dns unset
```

The equivalent manual setup is:

```bash
DNS_SERVER=<detected-host-ip>
DNS_DOMAIN=debug.lan

sudo mkdir -p /etc/systemd/resolved.conf.d

sudo tee /etc/systemd/resolved.conf.d/90-portmap-debug-lan.conf >/dev/null <<EOF
[Resolve]
DNS=$DNS_SERVER
Domains=~$DNS_DOMAIN
EOF

sudo systemctl restart systemd-resolved
resolvectl query "portmap.$DNS_DOMAIN"
curl -I "http://portmap.$DNS_DOMAIN/"
```

The catalog page lists currently visible `portmap`/Traefik-managed services and
their generated endpoints. When the host agent is running, the catalog also
lists startable stopped worktrees from the same Git repo so dead branches can be
started from the UI. Each visible compose project also has a `Down` button that
removes its portmap-managed Docker Compose containers and networks by compose
project label, which is useful when a worktree/repo path has moved.
The JSON form is available at:

```text
http://<detected-host-ip>/registry.json
```

Generate files for an external compose project:

```bash
cd /path/to/project
portmap init
```

This creates:

```text
.portmap/
  endpoints.toml
  README.md
  .gitignore
```

`.portmap/README.md` explains how to make a docker-compose repo portmap-managed:
use Docker bridge networking for managed services, declare endpoint kinds in
`endpoints.toml`, generate a branch-specific compose override, start compose
with that override, and query the shared catalog for the assigned URLs and raw
ports.

After editing `.portmap/endpoints.toml`, the recommended path is to let the
Docker Compose broker regenerate files automatically:

```bash
docker compose up -d
```

The broker can also be called explicitly without shell integration:

```bash
portmap docker-compose -- up -d
```

For manual generation, `portmap generate` defaults to the current directory
when paths are omitted:

```bash
portmap generate \
  --compose-file docker-compose.yml \
  --config .portmap/endpoints.toml \
  --out-dir .portmap \
  --branch "$(git branch --show-current)"
```

For transparent `docker compose ...` takeover, install the Docker Compose
plugin shim. This works for interactive shells, scripts, and non-interactive
agents such as Codex because it does not rely on `.zshrc` or shell functions:

```bash
portmap broker install --method docker-plugin
docker compose up -d
docker compose ps
```

The shim is installed at:

```text
~/.docker/cli-plugins/docker-compose
```

Docker CLI will call that plugin for `docker compose ...`. The shim forwards
Docker plugin metadata to the real Compose plugin, strips Docker's plugin
environment before forwarding, and calls `portmap docker-compose -- ...` only
when takeover is enabled and the current directory contains `.portmap`.

Inspect or remove the shim:

```bash
portmap broker status
portmap broker uninstall
```

Safety switches:

```text
PORTMAP_COMPOSE_TAKEOVER=0  # disable takeover, forward to real compose
PORTMAP_BROKER_BYPASS=1     # internal bypass to avoid recursive shim calls
```

The broker auto-generates `.portmap/docker-compose.override.generated.yml` and
`.portmap/state.json` when `.portmap/endpoints.toml` exists. It also injects a
branch/worktree-scoped compose project name unless the command already provides
`-p/--project-name`.

`state.json` is local to this worktree. The host-wide raw/range port pool is a
separate file, defaulting to:

```text
~/.local/state/portmap/allocations.json
```

That allocation pool is shared so two branches cannot automatically pick the
same host port before either container has bound it.

The generated override adds managed services to `portmap_gateway` and writes
Traefik Docker labels. The shared Traefik container discovers those labels
through the Docker socket.

Query the shared catalog:

```bash
curl http://<detected-host-ip>/registry.json
portmap list
portmap status
portmap endpoints my-repo dev
```

## Catalog Frontend

The catalog UI is a Vite + React app. Source files live under:

```text
frontend/
```

Install frontend dependencies once:

```bash
npm install
```

Run the frontend dev server with hot reload:

```bash
npm run dev
```

Run the frontend with built-in mock catalog data:

```bash
PORTMAP_CATALOG_MOCK=1 npm run dev
```

Run the same mock UI from Docker Compose on host port `81`:

```bash
docker compose -f docker-compose.mock.yml up
```

Then open:

```text
http://<host-ip>:81/
```

By default the Vite server proxies `/registry.json`, `/actions/*`, `/healthz`,
and `/readyz` to the catalog service at `http://127.0.0.1:80`. Override that
target when the catalog is exposed somewhere else:

```bash
PORTMAP_CATALOG_TARGET=http://127.0.0.1:8081 npm run dev
```

Build the frontend into the Python package static directory:

```bash
npm run build
```

The build output is tracked in:

```text
src/portmap/catalog_static/
```

Mock mode serves `frontend/public/*` through Vite. Production mode serves the
built files from `src/portmap/catalog_static/` through the Python catalog
server. Root-level public assets such as `/favicon.svg`,
`/manifest.webmanifest`, and `/robots.txt` must therefore be present in the
built static directory and served by the Python catalog handler as root static
assets, not only by the Vite dev server.

## Integration Test Repo

The repo includes a Python-managed integration fixture that creates a real Git
worktree-style compose repo under the ignored `test_repo/` directory:

```text
test_repo/
  mock-compose-app@wt/
    mock-compose-app@main
    mock-compose-app@dev
    mock-compose-app@feat-all-endpoints
  gateway/
```

The generated mock app has separate branches/worktrees and endpoint kinds:

```text
dev                -> HTTP endpoint
feat/all-endpoints -> HTTP, TCP, UDP, and range endpoints
```

Run ordinary unit tests:

```bash
uv run --with pytest pytest
```

Run the real Docker response test:

```bash
PORTMAP_RUN_INTEGRATION=1 uv run --with pytest pytest tests/test_integration_test_repo.py -q
```

That integration test starts a temporary Traefik gateway and both worktree
instances, then verifies actual HTTP, TCP, UDP, and range UDP responses with
Python clients. The generated `test_repo/` directory is ignored by git and can
be deleted at any time; the test will recreate it.
