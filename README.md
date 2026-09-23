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
http://frontend.dev.myrepo.ylang-u22.portmap:8080
http://frontend.feat-a.myrepo.ylang-u22.portmap:8080
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

## Server CLI

Install the **server tooling** with Homebrew (Linux or macOS tap):

```bash
brew install ylang-ylang/tap/portmap
```

Or install a source checkout as a `uv` tool so other repos can call the short
`portmap` command:

```bash
uv tool install --editable /home/ylang/ylangs_ws/portmap@wt/portmap@dev --force
```

Both modes are equivalent. Installed packages read gateway assets
(`docker-compose.yml`, single-port overlay, Corefile) from the Python package
and keep editable settings in `~/.config/portmap/portmap.toml` when the
gateway's own `portmap.toml` is absent; `PORTMAP_ROOT` still overrides the
asset root explicitly.

For workstation access, use the separate **`portmap-client` download** below.
Installing the server CLI is not a client prerequisite.

On a machine that runs your Compose projects, start the server-side gateway:

```bash
portmap up
```

This starts the host-side agent, then starts the shared gateway containers. The
agent scans host Git worktrees and handles host-side compose starts for the
catalog page; the gateway containers provide Traefik, CoreDNS, and the catalog
UI.

This creates the `portmap_gateway` Docker network. The default ports stay
compatible with existing deployments; single-port mode shares the HTTP entrypoint:

| Entry | Default | Single-port mode |
|---|---|---|
| Traefik / managed HTTP services | `http://<host-ip>:8080` | `http://<host-ip>:80` |
| Catalog (bare IP or unmatched Host) | `http://<host-ip>:80`, also via Traefik on `:8080` | Via Traefik on `:80`; no catalog host-port mapping |
| CoreDNS (TCP/UDP) | `<host-ip>:53` | Unchanged; keep local/private if the firewall only permits HTTP |

CoreDNS answers every A record under the host's domain with the
detected host LAN IP. The default domain is the machine hostname — a
host named `ylang-U22` serves `*.ylang-u22.portmap` — so multiple
portmap hosts coexist in one client resolver: delegate each
`<hostname>.portmap` subzone to its own machine. Set
`[gateway] dns_domain` in `portmap.toml` (or `PORTMAP_DNS_DOMAIN`) to pin
a custom suffix such as the legacy shared `debug.lan`.
Other DNS queries are forwarded to the configured upstream resolver, defaulting
to `/etc/resolv.conf`, so portmap-managed containers can still resolve public
domains after their DNS is pointed at portmap. Configure development machines
with split DNS so only the debug domain's queries go to this DNS server.

Gateway runtime settings are tracked in the portmap repo root:

```text
portmap.toml
```

`portmap up` and the lower-level `portmap gateway ...` command read that file
directly and detect the current host LAN IP at runtime. The detected IP is used
for DNS answers and raw/range endpoint advertisement, so the LAN IP does not
need to be stored in config.

### One HTTP Port Per Development Machine

For a machine whose firewall only allows inbound TCP `80`, set this in the
gateway's `portmap.toml`:

```toml
[gateway]
http_bind = "0.0.0.0"
http_port = 80
catalog_port = 80
dns_bind = "0.0.0.0"
dns_port = 53
dns_domain = "debug.lan"
network = "portmap_gateway"
```

When `http_port == catalog_port`, `portmap up` / `portmap gateway` automatically
apply `docker-compose.single-port.yml` to remove the catalog's host-port mapping.
The catalog stays on `portmap_gateway`, listening on container port `8081`.
Traefik's ``HostRegexp(`.+`)`` fallback router has priority `1`, below generated
service Host routes. Bare IPs and unknown Hosts therefore serve the catalog,
while registered Hosts reach their services on the same HTTP port. In this mode
`catalog_bind` is ignored; `http_bind` controls the shared listener. Equal
non-80 ports work the same way. Unequal ports keep the separate catalog mapping,
including a custom `catalog_bind` / `catalog_port`.

Preview before changing running services:

```bash
portmap gateway config
```

For an existing default deployment, first validate a separate stack on an unused
loopback port (use distinct container names and an external `portmap_gateway`
network; `-p` alone does not change the fixed gateway container names). At cutover,
release the old catalog's port `80` before starting Traefik on it:

```bash
portmap gateway stop catalog
portmap gateway up -d
curl http://127.0.0.1/
curl -H 'Host: unknown.debug.lan' http://127.0.0.1/
curl -H 'Host: web.dev.lushu.debug.lan' http://127.0.0.1/map.html
```

Changing the gateway port does not rewrite labels on existing project containers.
Run `portmap docker-compose -- up -d` in each managed worktree (including
`lushu@dev`) with the same gateway settings so generated URLs and catalog links
move from `:8080` to `:80`. `PORTMAP_HTTP_PORT=80` is an explicit override if the
project's broker points at another portmap checkout. To roll back, restore
`http_port = 8080` / `catalog_port = 80`, stop Traefik to free `80`, run
`portmap gateway up -d`, and regenerate project URLs with the restored settings.

Raw `docker compose` does not read `portmap.toml` or choose the overlay. Its
equivalent single-port invocation is:

```bash
PORTMAP_HTTP_PORT=80 PORTMAP_CATALOG_PORT=80 docker compose \
  -f docker-compose.yml -f docker-compose.single-port.yml up -d
```

Use Docker Compose with `!reset` support. This mode combines **HTTP** ports only:
it does not tunnel DNS, raw TCP/UDP, or ranges through port `80`. With external
DNS port `53` closed, clients need a separately managed wildcard DNS record or
hosts entries for individual service names pointing at the development machine.
`dns_bind = "0.0.0.0"` resolves to the detected LAN IP; restrict DNS to trusted
local/container clients instead of opening it publicly. The catalog and its
control actions are unauthenticated: exposing the fallback on `80` does not make
the gateway safe for the public Internet.

### Container Runtime: Docker or Podman

Each host runs one container runtime, selected per machine:

```toml
# portmap.toml
[runtime]
backend = "auto"    # auto | docker | podman
# socket = "/run/podman/podman.sock"   # optional explicit override
```

`auto` resolution order: an explicit `DOCKER_HOST` unix socket, then a
**live** `/var/run/docker.sock`, then the Podman sockets
(`/run/podman/podman.sock`, `$XDG_RUNTIME_DIR/podman/podman.sock`).
Liveness is checked by connecting, so a stale socket file left by a
stopped dockerd does not win the probe. `PORTMAP_RUNTIME` /
`PORTMAP_RUNTIME_SOCKET` environment variables override the file.

Podman hosts still use the docker compose CLI as the client: portmap
points it at the Podman API socket through `DOCKER_HOST`, so generated
override semantics (including `!reset` for single-port mode) are
identical on both runtimes. Traefik and CoreDNS are unchanged — Traefik's
Docker provider talks to whichever socket the gateway mounts. Rootful
Podman needs no extra setup; rootless Podman must allow privileged ports
(`sysctl net.ipv4.ip_unprivileged_port_start=53`) for the gateway's DNS
and HTTP listeners.

The compose takeover shim auto-detects the runtime the same way, and it
also covers `podman compose`: Podman's provider search path includes
`~/.docker/cli-plugins/docker-compose`, so one shim shadows both
`docker compose` and `podman compose`. On Podman-only hosts make sure the
docker CLI and its compose plugin are installed as client-only packages
(no dockerd required).

Runtime support was verified end-to-end on Debian 13 with Docker 29.6 /
compose v5.3 and rootful Podman 5.4; see
[docs/podman-spike.md](docs/podman-spike.md) for the spike evidence.

### Client Access: Direct IP or SSH

`portmap-client` is a separate workstation tool. Open your server's catalog and
use **Download client**, or copy the install command shown there. The downloadable
bundle includes its Python runtime, CoreDNS, and Traefik: **no local Python,
Homebrew, Docker, or portmap server installation is needed**. OpenSSH remains an
OS dependency for SSH connections; existing SSH configuration and credentials
are reused.

The published package currently supports **Linux x86-64 with glibc 2.31+**.
Linux split-DNS setup also requires `systemd-resolved`, `iproute2`, and permission
to make privileged resolver changes. macOS, Windows, ARM64, and musl/Alpine
downloads are not published; the page lists only real release artifacts.

The installer requires ordinary shell tools (`curl`, `tar`, `awk`, `grep`,
`readlink`, and `sha256sum` or `shasum`). Use a catalog you trust: the installer
verifies the archive against that server's manifest, not an independent signature.
Use HTTPS or a trusted network when downloading.

```bash
# Replace this example with the catalog origin shown in your browser.
SERVER=http://192.0.2.10
curl -fsSL "$SERVER/install-client.sh" | sh -s -- "$SERVER"
# If ~/.local/bin is not already on PATH:
export PATH="$HOME/.local/bin:$PATH"
portmap-client --version

# Run as your normal login user. Setup elevates only OS resolver changes.
portmap-client setup

# Inventory literal Host aliases from local SSH config and Include files.
# This does not scan the network, authenticate, or expand Host *.coder.
portmap-client discover

# Probe only the supplied gateway address.
portmap-client discover 192.0.2.10:8080
portmap-client connect 192.0.2.10:8080

# Reuse a configured SSH alias, or supply the full target explicitly.
portmap-client connect devbox
portmap-client connect main.workspace.owner.coder --via ssh

portmap-client connections --check
portmap-client disconnect devbox
portmap-client teardown
```

Installation does not start processes or modify DNS. It places a versioned
bundle under `${XDG_DATA_HOME:-$HOME/.local/share}/portmap-client/<version>` and
an executable symlink at `~/.local/bin/portmap-client`. Keep the whole bundle:
the executable uses its adjacent `_internal` directory. The installer refuses
to replace unrelated files or accept an unsafe or checksum-mismatched archive.

**Migrating from 0.6:** if the old combined CLI has active client connections,
run `portmap client teardown` with that old version before upgrading the server
CLI. Then install the separate client, run setup, and reconnect your targets.
Client commands were removed from the server CLI; there are no compatibility
aliases or automatic imports of old process ownership/state.

Replace documentation addresses and SSH names with your own. In `auto` mode,
the client tries direct access and can use a concrete local SSH alias as a
fallback. It does not guess usernames, keys, workspace names, or unknown
hosts. When no SSH target can be inferred, an interactive terminal prompts
for one; scripts must supply `--via ssh` or `--ssh-target`. Existing
`ProxyCommand`/`ProxyJump` and authentication settings remain authoritative;
Coder needs no special backend.

All connected HTTP gateways share **one local HTTP listener**. Each registered
`<hostname>.portmap` zone resolves to `127.0.0.1`; local Traefik routes by Host
to either a reachable remote IP or an internally allocated SSH forward.
The client never forwards remote DNS servers. Catalogs at each
`http://<hostname>.portmap:<local-port>/` rewrite application links to that
same local port, regardless of the remote gateway's port.

The listener prefers port `80`, then `18080`, then a free high port, without
replacing existing listeners. Select a fixed shared port with
`portmap-client setup --http-port 28080`. Port changes require teardown while
saved connections exist. Individual SSH backend ports are private implementation
details, not ports you need to remember.

On Linux, setup requires `systemd-resolved`, `iproute2`, and administrative
permission. It creates an owned dummy interface and directs only `~portmap`
to CoreDNS at `169.254.254.53:1053`. Using that link-local address also supports
systemd 249; loopback DNS on a separate routing interface does not work there.
Global/physical-interface DNS and `/etc/resolv.conf` are left unchanged.
The source contains a macOS resolver adapter, but there is no published or
verified macOS standalone package. Rerun setup after an OS restart to restore
transient Linux resolver state.

Client state lives under `${XDG_STATE_HOME:-$HOME/.local/state}/portmap-client`,
created with mode `0700`, independently of server settings.
`PORTMAP_CLIENT_STATE_DIR` selects an isolated private directory for testing.
Disconnect removes one local route and its owned tunnel, not remote containers;
teardown removes the local resolver route and client-owned processes.

Boundaries:

- A remote must already run a portmap gateway with a `<hostname>.portmap`
  domain. Discovery reads its existing `/registry.json`; the remote does not
  decide which IP or SSH path your workstation should use.
- Gateway bootstrap tries `8080` and `80`; `connect --remote-port N` supplies
  a custom SSH-side bootstrap port. A listener alone is not readiness: catalog,
  routing, and OS DNS are checked before reporting a successful connection.
- Duplicate hostname zones are rejected instead of overwriting another
  connection. The apex hostname is reserved for the local catalog; application
  endpoints must use subdomains.
- HTTP/WebSocket/SSE use the shared HTTP gateway. `connect --tcp` additionally
  forwards advertised raw TCP ports at separate local addresses. Ordinary SSH
  forwarding does not carry UDP or port ranges; unsupported endpoints are
  explicitly marked in the client catalog.
- Client HTTP listeners bind loopback only. Catalog actions still control
  the connected remote gateway and inherit its trust model. A private suffix
  does not add authentication.

#### Building and hosting the client package

The pinned builder targets Debian 11's glibc 2.31 baseline. Run from a source
checkout (Docker is a **build-time** dependency, not a client dependency):

```bash
BUILD_DIR=$(mktemp -d)
docker build -f packaging/client.Dockerfile -t portmap-client-builder packaging
docker run --rm --cpus=1 --memory=1g \
  --user "$(id -u):$(id -g)" -e HOME=/tmp/portmap-client-build \
  -v "$PWD:/src:ro" -v "$BUILD_DIR:/out" \
  portmap-client-builder --out-dir /out
```

`tools/build_client.py` verifies the pinned native dependencies, freezes only
client modules, and emits the archive, SHA-256 sidecar, and `client_release.json`.
Before a release, copy the verified manifest to
`src/portmap/client_release.json` and upload those exact bytes to the indicated
release URL. The source manifest is not generated or changed at server startup.

The catalog serves `/api/client`, `/install-client.sh`, and
`/downloads/client/<filename>` from its own origin. It validates archive size
and SHA-256 before serving. Seed `<server-state-dir>/client-downloads/<filename>`
to serve offline, or let the server fetch the pinned GitHub release once.
`PORTMAP_CLIENT_DOWNLOAD_DIR` can select another server cache directory.
Clients download from the catalog, not directly from GitHub.

### Split DNS

Configure split DNS on a Linux development machine without manually looking up
the network interface:

```bash
DNS_SERVER=<detected-host-ip>
DNS_DOMAIN=<hostname>.portmap
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
