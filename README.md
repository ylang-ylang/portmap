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

Every machine running `portmap setup` needs a working local Docker or rootless
Podman API socket and the Docker Compose CLI, including remote-only clients.
CoreDNS, Traefik, the catalog, and the SSH socket relay run in one shared
container stack; there is no native DNS/HTTP fallback. Standalone installation
alone starts nothing; Homebrew also runs setup. See [Unified CLI](#unified-cli).

For local publishing, your project should use Compose, and managed services
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

## Unified CLI

One `portmap` command can access remote machines, publish this machine's
projects, or do both. The standalone installer only places the command;
Homebrew also initializes the access role. Neither publishes projects or
enables boot services automatically.

Open a trusted server's catalog and select **Settings → Download portmap**,
or run its installer below. **Settings** is the last tab after the machines;
in the private portal it also contains **Connections**. Service empty states
stay inside machine tabs.

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
its Python runtime, CLI, agent, frontend, and infrastructure configuration.
It does not bundle host CoreDNS or Traefik binaries. Setup requires a running
Docker or rootless Podman runtime, Docker Compose, and access to their socket;
OpenSSH is also needed for SSH connections. Linux DNS integration needs
`systemd-resolved` and `iproute2`; HTTP 80 ingress needs a systemd system manager.
Applications must actually use systemd-resolved, through its NSS integration
(for example `libnss-resolve` on Debian) or its configured DNS stub.
A running resolver service alone is insufficient when `/etc/nsswitch.conf`
and `/etc/resolv.conf` still bypass it. Only machines with that bypass need
the one-time OS prerequisite; it is not repeated per project, connection, or
upgrade, and is also necessary for native DNS servers. Portmap does not rewrite
those global files; enable the OS resolver integration separately.

On Linux, Homebrew installs the command and runs `portmap setup` in the same
operation. A successful install leaves `http://127.0.0.1/` usable; no separate
setup command is required:

```bash
brew install ylang-ylang/tap/portmap
brew upgrade ylang-ylang/tap/portmap
```

The container runtime is a separate prerequisite. Portmap reuses an accessible
local Docker or Podman API, preferring Docker when both are available and
honoring explicit runtime configuration. It does not install, start, enable,
or reconfigure a container engine. A missing/inaccessible API or failed
initialization makes the Brew command fail; installing the CLI alone is not
reported as a ready gateway. The login user needs a user systemd manager and
sudo authorization for the existing scoped DNS/HTTP integration. Owned old
standalone launchers are retired, but their bundles and saved connections
are retained.

Source installations are also supported:

```bash
uv tool install --editable /path/to/portmap --force
```

Source installations use the same container stack; host CoreDNS and Traefik
executables are not needed. Standalone downloads for macOS, Windows, ARM64,
and musl/Alpine are not published. The Linux workflow below is the supported
default-port workflow; other resolver adapters do not imply verified port-80
support on those platforms.

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
http_bind = "127.0.0.1"
```

This publishes locally only. To enable LAN HTTP separately, set `http_bind`
to this machine's exact LAN IPv4 or IPv6 address (without a zone identifier)
and run `portmap up`. IPv6 publication binds only that IPv6 address.
Unspecified/wildcard values (`0.0.0.0` or `::`) do not implicitly open LAN
ingress. The default domain is `<hostname>.portmap`; `dns_domain` can select
another host zone under `.portmap`.

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
entry and DNS integration. Standalone installation alone performs none of
these steps; the Brew formula initializes access automatically.

The private ingress forwards to the shared container Traefik's access
entrypoint. Traefik routes each SSH domain through a container relay to that
connection's private host Unix socket; local projects use its separate web
entrypoint. No container treats the host's `127.0.0.1` as a remote endpoint.
Adding another SSH machine does not add another browser-facing port.

The LAN ingress forwards **only to the local web entrypoint**, never to the
private access entrypoint. Its catalog lists only local projects and omits
connection controls, imported remote catalogs, and SSH target names.

Systemd owns the exact `127.0.0.1:80` and, when enabled, `<LAN-IP>:80` sockets.
An unprivileged `systemd-socket-proxyd` service forwards each socket to its
fixed loopback backend. Portmap installs owned units with narrowly scoped
sudo commands; it does not run the entire CLI as root, modify sudoers, apply
binary capabilities, or lower `net.ipv4.ip_unprivileged_port_start`.
Units are not enabled at boot. Rerun `setup` and `up` as needed after reboot.

Host-side SSH masters and the agent must outlive the terminal or SSH login
that started them. Inside a logind `session-*.scope` or a per-user systemd
manager cgroup, portmap moves its own CLI process into a transient
system-manager scope through one exact privileged `busctl StartTransientUnit`
call before starting workers. Their credentials do not change. This also keeps
workers alive after a Brew installer service exits or the user manager stops.
`--no-sudo` refuses a new worker if this escape needs elevation. No linger,
logind policy, or boot setting is changed.
The container runtime/API service must independently remain available after
logout; Portmap does not change its service policy. Replacing a standalone
bundle or source checkout alone does not restart existing workers. Brew runs
setup, but compatible live workers can still be reused rather than restarted.

Port 80 is exclusive per address. A competing `docker -p 80:80` wildcard
mapping is a conflict, not another independent virtual host. Portmap never
stops unrelated listeners or silently falls back to `18080`.
`setup --http-port N` explicitly selects an alternative private HTTP port;
local publishing must use the same configured `http_port`.

Traefik's access, web, and management host bindings are private loopback high
ports. The catalog and SSH relay have no host-port mappings. Use `portmap up`,
not a raw gateway `docker compose up`, to publish projects and maintain ingress.
`portmap gateway config` remains available for configuration inspection.

`portmap down` stops the local publishing role and its public ingress while
preserving remote connections and the prepared private HTTP entry.
Only `portmap teardown` releases that entry and the split DNS integration.
Direct DNS mappings remain independent of the local HTTP data path.

Only HTTP is multiplexed here. HTTPS, DNS, raw TCP/UDP, and port ranges are not
tunneled through port 80. CoreDNS has separate host, container, and preserved
legacy-public views: host clients use split DNS; project containers use its
gateway-network address, not the host's loopback address. SSH/direct clients
do not require remote port 53. Public project controls remain unauthenticated:
publish only to trusted networks, not the public Internet.

### Container Runtime: Docker or Podman

Each host runs one container runtime, selected per machine:

```toml
# portmap.toml
[runtime]
backend = "auto"    # auto | docker | podman
# socket = "/run/user/1000/podman/podman.sock"  # optional explicit override
```

`auto` checks an explicit `DOCKER_HOST`, then a **live** `/var/run/docker.sock`,
then the rootless Podman socket at `$XDG_RUNTIME_DIR/podman/podman.sock`
(or `/run/user/<uid>/podman/podman.sock`), then `/run/podman/podman.sock`.
Automatic probes connect to sockets rather than trusting stale filesystem
entries. `PORTMAP_RUNTIME` / `PORTMAP_RUNTIME_SOCKET` override the file.
Infrastructure requires a local Unix socket; remote machines use `connect`.

Podman hosts still use the Docker Compose CLI as the client: portmap points it
at the Podman API socket through `DOCKER_HOST`. The user running portmap must
already have permission to access that socket, just as with Docker. Generated
project overrides need Compose `!reset` support.

HTTP 80 is owned by the restricted systemd ingress, not a rootless host-port
mapping. The host DNS listener uses `169.254.254.53:1053`; managed containers
query CoreDNS on the shared bridge at port 53. No low-port sysctl, binary
capability, sudoers, or linger change is needed. Podman's actual user-namespace
mode is read from its native API rather than assumed from Docker compatibility.

New Portmap bridge networks are dual-stack. Existing networks are preserved,
not silently recreated or reconfigured. IPv6 direct access requires an
IPv6-enabled gateway network and working host IPv6 connectivity; an existing
IPv4-only bridge is rejected before publishing that connection. Direct DNS
still returns the remote IPv6 address, never a local proxy address.

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

### Installing and Migrating Legacy Installations

The standalone installer requires `curl`, `tar`, `awk`, `grep`, `readlink`, and either
`sha256sum` or `shasum`. It verifies the archive using the trusted server's
manifest, not an independent signature. Use HTTPS or a trusted network.
Interactive stderr shows download progress; redirected logs retain stage
messages and failures without the progress meter.

Standalone installation places each verified bundle in a new immutable directory under
`${XDG_DATA_HOME:-$HOME/.local/share}/portmap/` and atomically switches
`~/.local/bin/portmap`. Reinstalling the same version also keeps previous
bundles intact: running workers and pending migrations may still need their
adjacent `_internal` assets. No processes, DNS routes, or HTTP sockets are
started by installation. Old bundles are not automatically garbage-collected.
Unrelated launchers, unsafe archives, and checksum mismatches are rejected.

After replacing `portmap-client` with `portmap`, run `portmap setup` as the same
login user. Before the default access state has saved its first connections
file, setup imports `~/.local/state/portmap-client` profiles. The old directory
is kept; explicit isolated state overrides do not trigger this import.
An empty saved connections file is intentional: disconnected or torn-down legacy
profiles are not imported again by a later setup or local-server start.

Imported profiles and existing native installations use the same migration.
A verified live SSH master is retained, including its authentication, custom
remote port and raw TCP forwards. Setup adds and probes its Unix-socket HTTP
forward, proves the replacement container route, then retires only the exact
obsolete HTTP forward. A failed cutover keeps the old forward and records the
proved replacement for a later setup attempt. Dead masters get one restoration
attempt using their saved identity; individual failures are reported.

Native DNS and HTTP workers are retired only after the replacement is ready.
If a verified native worker directly holds the requested HTTP port, setup
releases that captured worker for the handover; a pre-commit interruption
restarts its recorded command and restores its ownership records. An unrelated
listener is never stopped or silently bypassed with another port.

The installer retires only a positively identified legacy launcher, keeping
old bundles available while their running workers are migrated. There is no
`portmap-client` command alias. If a machine had a direct connection to its
own hostname, disconnect that profile before enabling its local publishing
role. If legacy publishing is already active, `setup` also migrates that server
after proving the shared private chain. Failure before the server commit
restores the old stack. The commit is saved before old containers are removed;
rerun `setup` to resume an interrupted retirement. Existing project containers
are not recreated. Use `portmap up` separately when local publishing is not
already enabled.

### Building and Hosting the Unified Package

The pinned builder targets Debian 11's glibc 2.31 baseline. Docker is used for
this build; the installed command can use Docker or rootless Podman at runtime:

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

`tools/build_portmap.py` freezes the unified CLI, host agent, access/publishing
modules, infrastructure/demo/scaffold assets, and frontend. DNS and HTTP
engines come from the pinned container images, not host binaries. The builder
emits the archive, SHA-256 sidecar, and `release.json`. Only after real runtime
acceptance, copy that manifest to `src/portmap/release.json` for source/Brew
publication and upload the archive and manifest to the versioned release.

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
Only that cache directory is mounted into the catalog; the rest of the
publishing state is not exposed. Teardown retains cached release archives.

### Project Catalog and Compose Integration

The private catalog has a machine selector for this machine and connected
remotes. Each selection reads that machine's actual projects, branches,
status, and endpoints; project actions target the selected machine rather
than silently falling back to local. The existing project/worktree UI is
shared across local, direct, and SSH views.

The private connection list accepts a host/IP/URL or SSH alias, with direct
or SSH mode. Add, edit, connect, disconnect, delete, and check use the same
saved configuration and serialized operations as the CLI. Failed connection
attempts remain editable and show their reason and last check time. Latency
is measured through the actual direct or SSH HTTP path, not ICMP; unavailable
measurements stay unavailable. No bandwidth or speed statistics are collected.
The public catalog excludes this connection manager and imported machines.

When the selected machine's agent is running, stopped worktrees from the same
Git repo are startable from the UI. `Down` removes the selected managed Compose
project's containers and networks, not the access infrastructure. JSON is at:

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
