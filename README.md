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

Every machine running `portmap setup` or `portmap run` needs a working local
Docker or Podman API socket and the Docker Compose CLI, including remote-only
clients. CoreDNS, Traefik, the catalog, and the SSH socket relay remain a shared
container stack; there is no native DNS/HTTP fallback. Systemd, resolved and
host `CAP_NET_ADMIN` are no longer universal requirements.
An answering API socket alone is insufficient: the engine must be able to
create its own containers and networks.
Use Compose 2.23.2+ with support for the generated override format.
For Docker API 1.44+, fixed MACs use per-network `mac_address`; older APIs
use the service-level field on each single-network infrastructure container.
Portmap checks the actual API version rather than forcing a newer one.
For Podman using Netavark, the network stack needs the
[stable bridge-MAC fix](https://github.com/containers/netavark/releases/tag/v1.9.0).
On Linux/amd64, `setup` and `run` detect an affected, idle, user-owned rootless
Podman API and prepare checksum-verified network helpers in the user's data
directory before starting Portmap containers. They preserve the engine's base
configuration and restart only the verified API process with the owned override.
Already-compatible engines are unchanged. Active workloads, foreign configuration
or an unsupported repair environment fail explicitly instead of being modified.
Portmap does not install or upgrade the container engine, change host neighbor
tables, or require a separate deployment repair command.

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
projects, or do both. Standalone installation only places the command.
The updated Homebrew helper also initializes access when a usable user service
manager exists; otherwise it explicitly leaves automatic setup unattempted.
Neither publishes projects nor enables boot services automatically.

Open a trusted server's catalog and select **Settings → Download portmap**,
or run its installer below. **Settings** is the last tab after the machines;
in the private portal it also contains **Connections**. Service empty states
stay inside machine tabs. The portal's own machine carries a small **本机** badge.

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
Docker or Podman API, Docker Compose, and permission to access the socket.
OpenSSH is also needed for SSH connections. DNS and HTTP integration are
selected independently; see [Portable Startup](#portable-startup).
The historical `resolved` adapter still requires resolved, iproute2, an
application resolver path through resolved, and permission to create its
owned dummy interface. Those requirements do not apply to the other adapters.

With a usable user service manager, the updated Homebrew activation helper runs
`portmap setup` in the same operation and checks the loopback page. Without one,
installation succeeds but explicitly reports that automatic setup was not run:

```bash
brew install ylang-ylang/tap/portmap
brew upgrade ylang-ylang/tap/portmap
```

The container runtime is a separate prerequisite. Portmap reuses an accessible
local Docker or Podman API, preferring Docker when both are available and
honoring explicit runtime configuration. It does not install, start, enable,
or reconfigure a container engine. When activation is attempted, an unavailable
API or failed initialization makes the Brew command fail. When it is not
attempted, existing services/configuration remain unchanged and readiness is
not claimed: run `portmap run` or `portmap setup`, then `portmap status --check`.
Owned old standalone launchers are retired, but bundles and saved connections
are retained. Homebrew supplies the Docker CLI and the Compose client as
client-only packages; `portmap setup` locates the installed Compose client by
itself, so no plugin wiring or `cliPluginsExtraDirectories` step is needed,
and no container engine or user-group change is part of the installation.

Source installations are also supported:

```bash
uv tool install --editable /path/to/portmap --force
```

Source installations use the same container stack; host CoreDNS and Traefik
executables are not needed. Standalone downloads for macOS, Windows, ARM64,
and musl/Alpine are not published. The Linux workflow below is the supported
default-port workflow; other resolver adapters do not imply verified port-80
support on those platforms.

### One-command Source Snapshot Deployment

Deploy the current checkout over ordinary SSH, including workspaces without
systemd. This path does not depend on Coder or Kubernetes:

```bash
python3 tools/deploy_portmap.py SSH_TARGET \
  --tap-source /path/to/homebrew-tap
```

The controller needs Python 3.11+, the matching Portmap/tap checkouts, and
a working Docker or Podman engine for the pinned Linux amd64 builder.
The Linux amd64 target needs SSH, Python 3.11+, Homebrew, and its own usable
Docker or Podman API. The tap installs the Compose client. Ordinary runtime
use and standalone installation do not require this deployment tool or Brew.

The command builds the real standalone archive, stages checksummed source
and Brew helpers, installs the private `portmap-local/snapshot` formula, then
runs setup outside Homebrew's sandbox. DNS initialization explains its scope
and requests sudo when necessary; run from an interactive terminal if sudo
requires a password. Success requires real system DNS, port-80 HTTP, frontend
assets, and the catalog API—not merely a completed package installation.
Unchanged inputs reuse the build and installed package and recheck readiness.

For an old Linux amd64 rootless Podman/Netavark installation, the deploy tool
can install pinned Netavark/Aardvark 1.9 helpers under the user's data directory.
It preserves the existing containers configuration and uses an owned override.
Only an identified, user-owned, idle standalone API service can be restarted
for this repair; active workloads or uncertain ownership fail explicitly.
It does not replace the engine, distro binaries, global policies or capabilities.
Already compatible runtimes are left alone.

This is source-snapshot deployment, not a public release. The packaged manifest
describes the archive actually built; repository release metadata and published
tap pins remain unchanged. On the target, use `portmap up` and `portmap demo up`
to show a separate demo project, or start your own configured project normally.


### Portable Startup

Run in the foreground from a terminal, an existing supervisor, or a project
startup command. No specific init system, workspace product, or orchestration
platform is required:

```bash
portmap run
```

`run` supervises its agent worker and owned SSH processes. Normal SIGINT,
SIGTERM and SIGHUP stop owned workers, remove their live routes, and retain
saved connections, shared infrastructure and business containers. An already
healthy agent is reused and is not stopped by this runner. Forced termination
still follows the caller's process supervisor/cgroup policy. Only one runner
may own a client state directory. Background `setup` remains available.

For one-time background initialization, run `portmap setup`. Any project can
adopt the same standard entry in its own startup path - a Makefile target, a
dev-container service command, or a wrapper script:

```bash
portmap setup && portmap compose up -d
```

`setup` reports success only when the container infrastructure, DNS and the
HTTP entry are actually working, so a project script fails fast instead of
serving a broken entry; repeated invocations are safe and re-verify the
environment first. An existing supervisor or container entrypoint can instead
own `portmap run`; Portmap does not install a particular init system. Normal
use needs no `--resolver` or `--ingress` selection.

New HTTP entries are published by Docker or Podman at the exact requested
address and port: private access defaults to `127.0.0.1:80`; public access
requires an explicitly configured non-loopback address. Rootless runtimes are
not rejected in advance. A real runtime bind failure reports the cause rather
than switching to systemd, another port, or a different address. Existing owned
systemd entries retain migration/recovery compatibility.

On Linux with ordinary, non-loopback upstream nameservers, `setup` and `run`
publish CoreDNS on `127.0.0.1:53` over UDP and TCP. Initialization explains the
scope before changing the current `/etc/resolv.conf`, requesting sudo only
when needed. `.portmap` is answered locally; ordinary queries go to the
captured original DNS, not substituted public servers. Search domains and
options are preserved. Port 53 is necessary because a `nameserver` entry
cannot specify another port or route only one suffix.

The recovery journal records original content, file kind, symlink target,
device/inode, permissions and owner, plus the actual post-write identity.
Teardown restores only a file it can still prove it owns. External edits or
an interrupted write with uncertain ownership fail closed and retain recovery
information. Bind mounts are updated in place, so forced termination during
that write can require manual restoration from the journal. Readiness checks
the system's actual resolution, not merely the CoreDNS listener.

Local proxies such as `127.0.0.53`, mixed stub/upstream lists, and VPN split-DNS
policies are not flattened into guessed upstreams. A usable policy-preserving
resolved adapter may retain compatibility; otherwise initialization reports
the limitation. The existing `resolved` and `external` adapters remain
advanced/compatibility options, not mandatory choices for new users.

Host/client DNS and project-container DNS are separate roles. Fresh
`portmap up` can publish projects for remote HTTP or SSH access without
taking over the server's host DNS or opening DNS to remote clients. The
accessing client resolves the name and preserves its HTTP `Host`; only the
server needs to know the project's internal port. Managed containers continue
using CoreDNS on the private bridge.

`portmap teardown` removes active remote connections and restores owned host
DNS. While local publishing or running/stopped project containers still depend
on the bridge, it retains shared infrastructure, including the same CoreDNS
instance and its existing publications. After stopping publishing and removing
those project containers, teardown releases the remaining owned resources.

Before uninstalling the executable, run `portmap teardown` while the current
container API is still available. If it reports retained project consumers,
stop/remove those projects through their normal Compose workflow, run
`portmap down`, then run teardown again. Do not delete the state directory
first: it contains the resolver ownership journal needed for safe restoration.

In a workspace/container, the resolver change applies only to the file visible
there, which may be shared with sibling containers. It does not change a node's
or cluster's global DNS. A rebuilt workspace rechecks its current resolver
identity and environment instead of restoring a dead instance's backup.

The same recheck discipline applies to the container API itself. A rebuilt
workspace can lose the environment an existing rootless Podman API service was
started with (for example its `CONTAINERS_CONF` override), and a regressed
network stack can break bridge forwarding. Before creating or changing any
portmap-owned container or network, `setup` and `run` re-verify the connected
API - its configuration, network-stack compatibility and ownership - and
repair only an identified, user-owned, idle mismatch. Running workloads,
foreign services and uncertain ownership are refused with the concrete reason
instead of being bypassed. Docker APIs pass this check unchanged. Re-run the
same standard entry after any workspace rebuild; no separate repair command
or source checkout is required.

Permission boundaries remain real: the runtime must be able to publish the
requested ports, the resolver file must be writable through an authorized
operation, and the original DNS must be reachable. Portmap does not change
global low-port policy, grant host capabilities, or stop unrelated listeners.
An explicitly configured high HTTP port is supported; resolver-file DNS still
requires port 53. Actual owned selections are persisted for subsequent commands.


### Publish Local Projects

Run as your normal login user, with access to a local Docker or Podman socket:

```bash
portmap up
portmap status --check
portmap demo up
```

`up` starts the host-side agent and container-network gateway, then publishes
the local role. Fresh server-only use does not configure host DNS or require
port 53. The catalog and HTTP projects share port **80**. Run `setup` or `run`
when this host's own programs also need `.portmap` resolution. `demo up` starts
a separate verification project, not your existing application.

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

`portmap setup` and `portmap run` prepare the selected DNS integration and the
private `127.0.0.1:80` entry, including empty or direct-only profiles.
Disconnecting the last remote does not release the entry. Teardown restores
owned host DNS immediately; shared entries and CoreDNS remain while local
publishing or project containers still depend on them. Installation alone
is not a readiness guarantee.

The private ingress forwards to the shared container Traefik's access
entrypoint. Traefik routes each SSH domain through a container relay to that
connection's private host Unix socket; local projects use its separate web
entrypoint. No container treats the host's `127.0.0.1` as a remote endpoint.
Adding another SSH machine does not add another browser-facing port.

The LAN ingress forwards **only to the local web entrypoint**, never to the
private access entrypoint. Its catalog lists only local projects and omits
connection controls, imported remote catalogs, and SSH target names.

The container ingress backend publishes only the exact requested address and
port and forwards over the existing bridge: private to Traefik access, public
to Traefik web. Each role has its own container and ownership record; changing
one does not recreate the gateway or SSH relay. No host networking is used.
The optional systemd backend retains its exact-address socket and unprivileged
`systemd-socket-proxyd` units with scoped sudo operations.
Neither backend modifies sudoers, binary capabilities, global low-port sysctls,
linger, logind policy, or boot enablement.

In foreground mode the runner provides worker lifetime without a systemd
scope escape. In background mode, workers that need to outlive a logind
`session-*.scope` or per-user manager still use the existing narrowly scoped
`busctl StartTransientUnit` integration; `--no-sudo` refuses required elevation.
The runtime/API service must independently remain available. Replacing a
source checkout or bundle does not restart existing workers; compatible live
workers can be reused by setup.

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
Only explicit teardown releases host DNS and, once consumers are gone, the entry.
Direct DNS mappings remain independent of the local HTTP data path.

Only HTTP is multiplexed here. HTTPS, DNS, raw TCP/UDP, and port ranges are not
tunneled through port 80. CoreDNS has separate host, container, and preserved
legacy-public views: host clients use their configured system resolver, while
project containers use CoreDNS's gateway-network address rather than host
loopback. SSH/direct clients do not require remote port 53.
Public project controls remain unauthenticated: publish only to trusted
networks, not the public Internet.

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

Rootful runtimes can publish the requested low ports on behalf of an ordinary
user. Rootless status alone says nothing about whether a requested low port
will work: the runtime's actual bind result decides. There is no automatic
systemd fallback or policy change. Managed containers query CoreDNS on the
shared bridge at port 53; its host publication is a separate role. Podman's
actual user-namespace mode is read from its native API rather than assumed
from Docker compatibility.

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
# Initialize system DNS access and the private 127.0.0.1:80 entry.
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

The default Linux resolver integration is the takeover described in
[Portable Startup](#portable-startup): `/etc/resolv.conf` is rewritten to use
CoreDNS at `127.0.0.1:53`, ordinary queries are forwarded to the captured
original upstreams with search domains and options preserved, and `.portmap`
zones are answered locally. Direct records use remote IP addresses; SSH and
local records use loopback. Unconnected zones return NXDOMAIN. The advanced
`--resolver resolved` adapter instead creates an owned dummy interface and
routes only `~portmap` to CoreDNS at `169.254.254.53:1053` without rewriting
`/etc/resolv.conf`; it still requires systemd-resolved, iproute2, an
application resolver path through resolved, and permission to create that
interface. Do not combine either mode with the former per-interface Web DNS
commands.

The unified state root defaults to
`${XDG_STATE_HOME:-$HOME/.local/state}/portmap`; access state is in `client/`
and local publishing ownership is in `server/`.
`PORTMAP_STATE_DIR` overrides the shared root; `PORTMAP_CLIENT_STATE_DIR`
selects an isolated access state for advanced use. Private ownership records
prevent cleanup from signaling unrelated processes or removing foreign units.

```bash
# Stop local publishing first, if enabled; remote connections survive down.
portmap down
# Restore host DNS and disconnect remotes; retain any remaining project dependencies.
portmap teardown
```

Teardown can run while publishing remains active: it preserves the shared
data plane needed by that role and by running/stopped project containers.
Disconnecting a remote does not stop its containers. Use `--no-sudo` on setup, teardown,
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

### Nested Workspace Portability

The opt-in nested test uses a **dedicated verification VM**, not a production
workspace. Run it as an ordinary operator with working rootless Podman,
subuid/subgid mappings, user namespaces, `/dev/fuse`, `/dev/net/tun`, a private
`XDG_RUNTIME_DIR`, and pasta or slirp4netns. The VM also needs OpenSSH, the
Docker CLI and Compose plugin, image/package download access, and a reachable
non-loopback original DNS server. These are fixture prerequisites, not
additional Portmap dependencies.

```bash
PORTMAP_RUN_NESTED_INTEGRATION=1 PORTMAP_NESTED_OPERATOR="$USER" \
  uv run --with pytest pytest -m integration tests/test_integration_nested_workspace.py -q
```

The fixture creates its own outer rootless store, network, and labeled
workspace/peer/DNS containers. Each workspace runs its **own real inner
rootless Podman API** on a writable workspace bind; no host engine socket is
mounted. PID 1 is tini, systemd/resolved are absent, and ordinary and sudo
processes both lack `CAP_NET_ADMIN`. Ordinary sessions explicitly drop inherited
and ambient capabilities; sudo is still bounded by the same recorded profile.
The namespace-local low-port policy, proc view and FUSE/TUN devices reproduce
the observed workspace constraints;
the VM's low-port policy, forwarding, DNS, and subid configuration are not
relaxed. It never uses `--privileged`.

The current Linux/amd64 fixture keeps Ubuntu 24.04, Podman 4.9 and crun,
but installs SHA256-pinned official Netavark/Aardvark 1.9 helpers in the
disposable image to include that upstream fix. Receipts record the selected
network helper versions and paths; this is not the unchanged stock 1.4 stack.

Acceptance exercises plain `run`/`setup`, system `.portmap` and ordinary DNS
on port 53, private/public HTTP on port 80, container access, real SSH
connections, foreground shutdown/restart, teardown with live consumers, and
workspace recreation with a fresh resolver and engine graph. JSON receipts
remain under a unique `portmap-nested-pytest-*` directory in the operator's
home. Cleanup uses recorded IDs and ownership labels, not prune or name-prefix
sweeps. The driver module also exposes bounded `baseline`, `prepare`,
`exercise`, `cleanup`, and `status` commands for diagnosing a failed run.

This is source-mode acceptance in a controlled environment. It does not
establish acceptance of a published package or an actual Coder deployment.
