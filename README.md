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
http://frontend.dev.myrepo.ylang-u22.portmap
http://frontend.feat-a.myrepo.ylang-u22.portmap
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

Remote-only access needs no local container runtime. The standalone download
includes Python, CoreDNS, and Traefik; see [Unified CLI](#unified-cli).

For local publishing, your project should use Docker Compose with Docker or
Podman, and managed services should have:

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

## Unified CLI

One `portmap` command can access remote machines, publish this machine's
projects, or do both. Installing it does not enable either role.

Open a trusted server's catalog and use **Download portmap**, or run its
installer:

```bash
SERVER=http://192.0.2.10
curl -fsSL "$SERVER/install.sh" | sh -s -- "$SERVER"
export PATH="$HOME/.local/bin:$PATH"  # if not already configured
portmap --version
```

Use an address reachable before `.portmap` DNS is configured. When the Web page
is opened through a `.portmap` name, its install command uses the advertised
server IP and retains the current HTTP port instead of requiring that name.

The standalone package supports **Linux amd64 with glibc 2.31+** and bundles
its Python runtime, CoreDNS, and Traefik. Client-only operation does not need
Python, Homebrew, or Docker installed separately. OpenSSH is needed for SSH
connections. Linux DNS integration needs `systemd-resolved` and `iproute2`;
the default HTTP 80 ingress needs a systemd system manager.
Applications must actually use systemd-resolved, through its NSS integration
(for example `libnss-resolve` on Debian) or its configured DNS stub.
A running resolver service alone is insufficient when `/etc/nsswitch.conf`
and `/etc/resolv.conf` still bypass it. Portmap does not rewrite those global
files; enable the OS resolver integration separately.

The Web page also offers Homebrew installation and upgrade:

```bash
brew install ylang-ylang/tap/portmap
brew upgrade ylang-ylang/tap/portmap
```

The formula installs Python and the native DNS/HTTP dependencies, not Docker.
Source installations are also supported:

```bash
uv tool install --editable /path/to/portmap --force
```

Source installs need CoreDNS and Traefik on `PATH`. Standalone downloads for
macOS, Windows, ARM64, and musl/Alpine are not published. The Linux workflow
below is the supported default-port workflow; the presence of other resolver
adapters does not imply a verified port-80 implementation on those platforms.

### Publish Local Projects

Run as your normal login user, with access to a local Docker or Podman socket:

```bash
portmap up
portmap status --check
portmap demo up
```

`up` configures the isolated access resolver, starts the host-side agent and
container-network gateway, then publishes the local role. The catalog and
HTTP projects share port **80**. `demo up` starts a separate verification
project, not your existing application.

Gateway assets are packaged with the command. Settings come from the gateway
root's `portmap.toml`, or `~/.config/portmap/portmap.toml` for an installed
package. `PORTMAP_ROOT` explicitly selects another asset root. Example:

```toml
[gateway]
http_port = 80
http_bind = "0.0.0.0"
dns_bind = "127.0.0.1"
```

`http_bind = "0.0.0.0"` selects the detected LAN IPv4 address for the public
ingress; it does **not** create a wildcard HTTP listener. Set
`http_bind = "127.0.0.1"` to publish locally only. The default domain is
`<hostname>.portmap`; `dns_domain` can select another host zone under
`.portmap`.

### HTTP 80 Ownership and Isolation

| Access path | DNS result | Browser port | Local HTTP listener |
|---|---|---|---|
| Direct remote | Verified remote IP | Remote gateway's port, normally 80 | Prepared by setup, bypassed by direct traffic |
| SSH remote | `127.0.0.1` | 80 | Shared private ingress |
| Local project | `127.0.0.1` | 80 | Same private ingress |
| LAN client of this server | This server's LAN IP | 80 | Separate public ingress |

`portmap setup` configures split DNS and reserves the private
`127.0.0.1:80` entry, including when there are no profiles or only direct
connections. Disconnecting the last remote does not release the entry.
After stopping local publishing, `portmap teardown` releases the owned
entry and DNS integration. Installation alone performs none of these steps.

The private ingress forwards to native Traefik on a private high port. Traefik
routes each SSH domain to its own private SSH forward, and the local domain to
the container gateway's loopback backend. Adding another SSH machine does not
add another browser-facing port.

The LAN ingress forwards **directly to the local container backend**, never to
the private SSH gateway. Remote SSH routes therefore do not become available
to other LAN machines when local publishing is enabled.

Systemd owns the exact `127.0.0.1:80` and, when enabled, `<LAN-IP>:80` sockets.
An unprivileged `systemd-socket-proxyd` service forwards each socket to its
fixed loopback backend. Portmap installs owned units with narrowly scoped
sudo commands; it does not run the entire CLI as root, modify sudoers, apply
binary capabilities, or lower `net.ipv4.ip_unprivileged_port_start`.
Units are not enabled at boot. Rerun `setup` and `up` as needed after reboot.

The background workers (client CoreDNS, native Traefik with its catalog view,
SSH masters, and the host agent) must outlive the terminal or SSH login that
started them. When portmap detects it is running inside a disposable logind
`session-*.scope`, it first moves its own process into a transient systemd
scope through one exact privileged `busctl StartTransientUnit` call, before
the first worker starts. Workers then survive the launching session's
disconnect while still running as the same unprivileged user, with no unit
files and no boot enablement; outside such a login session (and on non-Linux
systems) nothing is attempted. `--no-sudo` refuses a new worker if this escape
needs elevation. Installing or upgrading portmap alone neither restarts
already-running workers nor changes their lifetime. Existing workers must
stop and start again before the new launch rule applies; an idempotent
`setup`, `up`, `connect`, or `agent start` may reuse a running worker.

Port 80 is exclusive per address. A competing `docker -p 80:80` wildcard
mapping is a conflict, not another independent virtual host. Portmap never
stops unrelated listeners or silently falls back to `18080`.
`setup --http-port N` explicitly selects an alternative private HTTP port;
local publishing must use the same configured `http_port`.

The container Traefik binds only a private loopback high port. The catalog has
no separate host-port mapping; `catalog_port`, `catalog_bind`, and the former
single-port overlay are no longer used. Use `portmap up`, not a raw gateway
`docker compose up`, to allocate and maintain the backend port and ingress.
`portmap gateway config` remains available for configuration inspection.

`portmap down` stops the local publishing role and its public ingress while
preserving remote connections and the prepared private HTTP entry.
Only `portmap teardown` releases that entry and the split DNS integration.
Direct DNS mappings remain independent of the local HTTP data path.

Only HTTP is multiplexed here. HTTPS, DNS, raw TCP/UDP, and port ranges are not
tunneled through port 80. Server CoreDNS remains separately configurable for
container/external DNS consumers; unified access clients use their own split
DNS and do not require remote port 53. The catalog and control actions remain
unauthenticated: restrict publishing to trusted networks, not the public
Internet.

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

Podman hosts still use the Docker Compose CLI as the client: portmap points it
at the Podman API socket through `DOCKER_HOST`. The user running portmap must
already have permission to access that socket, just as with Docker. Generated
project overrides need Compose `!reset` support.

HTTP 80 is owned by the restricted systemd ingress, not the container runtime.
For rootless Podman, configure an unprivileged server DNS port such as
`[gateway] dns_port = 1053` rather than globally permitting low-port binds.
That server DNS listener is separate from the client resolver at
`169.254.254.53:1053`.

The compose takeover shim also covers `podman compose`: Podman's provider
search includes `~/.docker/cli-plugins/docker-compose`. On Podman-only hosts
install the Docker CLI and Compose plugin as client-only packages; no dockerd
is required. Historical runtime comparison evidence is in
[docs/podman-spike.md](docs/podman-spike.md).

### Remote Access: Direct IP or SSH

Install the same `portmap` command on the computer running your browser:

```bash
# Configure split DNS and keep the private 127.0.0.1:80 entry ready.
portmap setup

# Inspect configured SSH aliases or probe one explicit address.
portmap discover
portmap discover 192.0.2.10

# Direct access uses the verified remote IP and remote HTTP port.
portmap connect 192.0.2.10 --via direct

# SSH access uses a private tunnel behind the shared local HTTP 80 ingress.
portmap connect devbox --via ssh
portmap connect main.workspace.owner.coder --via ssh

portmap connections --check
portmap status --check
portmap disconnect devbox
```

Without `--via`, `auto` tries direct access and may use a concrete configured
SSH alias as fallback. It does not invent usernames, keys, workspace names, or
unknown hosts. Interactive use can prompt for an SSH target; scripts should
supply `--via ssh` or `--ssh-target`. Existing OpenSSH configuration,
`ProxyCommand`, `ProxyJump`, and credentials remain authoritative.

Bootstrap defaults to port 80. Supply an explicit address port for a
non-default direct gateway, or `--remote-port N` for SSH. Direct URLs retain
the remote port; SSH URLs use the shared local public port. These are separate
from the private high ports allocated for tunnels and backend listeners.

`192.0.2.10`, `192.0.2.10:80`, and `http://192.0.2.10` identify the same direct
target, so reconnecting does not force a disconnect. A different target with
the same hostname zone is rejected rather than overwriting its DNS mapping.
The remote catalog cannot choose your workstation's reachable IP.

`connect --tcp` additionally forwards advertised raw TCP ports. UDP and port
ranges do not travel over ordinary SSH forwards and are marked unsupported.
SSH apex catalogs project client-visible URLs; application subdomains and
catalog control actions preserve the remote Host-routing path.

### Split DNS and State

`portmap setup` creates an owned dummy interface on Linux and routes only
`~portmap` to local CoreDNS at `169.254.254.53:1053`. Direct records use remote
IP addresses; SSH and local records use loopback. Unconnected zones return
NXDOMAIN. Physical-interface DNS and `/etc/resolv.conf` are not rewritten.
Do not combine this setup with the former per-interface Web DNS commands.

The unified state root defaults to
`${XDG_STATE_HOME:-$HOME/.local/state}/portmap`; access state is in `client/`
and local publishing ownership is in `server/`.
`PORTMAP_STATE_DIR` overrides the shared root; `PORTMAP_CLIENT_STATE_DIR`
selects an isolated access state for advanced use. Private ownership records
prevent cleanup from signaling unrelated processes or removing foreign units.

```bash
# Stop local publishing first, if enabled; remote connections survive down.
portmap down
# Remove the access resolver, owned workers and remote connections.
portmap teardown
```

Teardown refuses while the local publishing role is active. Disconnecting a
remote does not stop its containers. Use `--no-sudo` on setup, teardown,
connect, disconnect, up, down, or agent start when privilege elevation must
not be invoked; operations that need an administrative change fail explicitly
instead. This includes the login-session escape above: with `--no-sudo` a
background worker that cannot be kept alive past the session is never started.
The older `portmap dns set/unset` commands manage an explicit server-DNS
drop-in, not this access resolver, and are not part of the unified setup path.

### Installing and Migrating from 0.9

The installer requires `curl`, `tar`, `awk`, `grep`, `readlink`, and either
`sha256sum` or `shasum`. It verifies the archive using the trusted server's
manifest, not an independent signature. Use HTTPS or a trusted network.
Interactive stderr shows download progress; redirected logs retain stage
messages and failures without the progress meter.

Installation only places the versioned bundle at
`${XDG_DATA_HOME:-$HOME/.local/share}/portmap/<version>` and atomically manages
`~/.local/bin/portmap`. Keep the adjacent `_internal` directory.
No processes, DNS routes, or HTTP sockets are started by installation.
Unrelated launchers, unsafe archives, and checksum mismatches are rejected.

After replacing `portmap-client` with `portmap`, run `portmap setup` as the same
login user. Before the new default access state has saved its first connections
file, setup imports `~/.local/state/portmap-client` profiles, stops only verified owned
legacy workers/tunnels, and rebuilds the resolver. The old directory is kept;
explicit isolated state overrides do not trigger this import.
An empty saved connections file is intentional: disconnected or torn-down legacy
profiles are not imported again by a later setup or local-server start.
Saved SSH identities and custom remote ports are retained. Setup attempts to
restore each saved SSH connection and reports individual restoration failures;
reconnecting the same saved target can revive it without a forced disconnect.

The installer retires only a positively identified legacy launcher, keeping
old bundles available while their running workers are migrated. There is no
`portmap-client` command alias. If a machine had a direct connection to its
own hostname, disconnect that profile before enabling its local publishing
role. Run `portmap up` to move the existing gateway to its private backend and
controlled public ingress; existing project containers are not recreated by
this operation.

### Building and Hosting the Unified Package

The pinned builder targets Debian 11's glibc 2.31 baseline. Docker is a build
dependency, not a remote-access dependency:

```bash
npm ci
npm run build
BUILD_DIR=$(mktemp -d)
docker build -f packaging/portmap.Dockerfile -t portmap-builder packaging
docker run --rm --cpus=1 --memory=1g \
  --user "$(id -u):$(id -g)" -e PYINSTALLER_CONFIG_DIR=/tmp/pyinstaller \
  -v "$PWD:/src:ro" -v "$BUILD_DIR:/out" \
  portmap-builder --out-dir /out
```

`tools/build_portmap.py` verifies pinned native dependencies and freezes the
unified CLI, host agent, server/client modules, gateway/demo/scaffold assets,
frontend, and redistribution notices. It emits the real archive, SHA-256
sidecar, and `release.json`. Copy that manifest to `src/portmap/release.json`
for source/Brew publication and upload both the archive and `release.json`
to the indicated versioned GitHub release.

The archive deliberately cannot contain its own checksum manifest. The
builder writes a sidecar next to the unpacked executable after hashing; the
installer writes one from the server metadata before activating the bundle.
A raw extracted bundle without a sidecar can retrieve the bounded, version-
pinned manifest from the trusted release. An invalid present manifest is an
error, not permission to use a different release.

The catalog serves `/api/downloads`, `/install.sh`, and
`/downloads/portmap/<filename>`. The old client-only routes are removed.
Seed `<server-state-dir>/downloads/<filename>` or set `PORTMAP_DOWNLOAD_DIR`
to a verified artifact directory for offline distribution; otherwise the
server fetches and checks the pinned upstream archive once. Clients download
from the catalog's own origin.

### Project Catalog and Compose Integration

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
