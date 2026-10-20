# portmap Architecture

`portmap` is a branch-scoped network control plane for local
docker-compose projects. It does not proxy traffic itself. It translates
project/branch rules into Docker Compose overlays, Traefik HTTP labels, Docker
direct port mappings, and a local CLI-queryable endpoint registry.

The runtime is one shared container stack plus a small ordinary-user host
agent. CoreDNS, Traefik, the catalog, and an SSH Unix-socket relay run in
containers, including on remote-only clients. The host retains the CLI,
worktree/Compose agent, OpenSSH workers, and narrowly scoped DNS/ingress
integration. There is no native DNS or HTTP fallback.
Every managed service consuming Portmap DNS joins the gateway bridge through
the generated Compose override. HTTP routes use Traefik; raw TCP/UDP/ranges
still use direct runtime port mappings, not the HTTP gateway.

## Container Runtime Abstraction

`portmap` supports one container runtime per host: Docker or Podman,
selected by `[runtime] backend` in `portmap.toml` (`auto` by default) or
`PORTMAP_RUNTIME` / `PORTMAP_RUNTIME_SOCKET`.

The common seam is the runtime API socket. The controller uses it for
resource inspection and ownership-checked operations; Docker Compose applies
the generated stack with `DOCKER_HOST` pointing at the same endpoint.
Traefik and the catalog mount that socket at `/var/run/docker.sock`.
Docker Compose remains the client on Podman hosts because it preserves
generated-override semantics (`!reset`, label merge, external networks) that
`podman-compose` does not.

Owned infrastructure endpoints have fixed IPv4 and MAC addresses. New MACs
derive from the infrastructure owner and role; existing owned endpoints keep
their observed MAC without a restart just to add the explicit configuration.
This prevents stale neighbor caches from sending traffic to a removed catalog
instance after a same-IP replacement. The controller verifies the runtime's
actual MAC rather than flushing host neighbor tables or requesting capabilities.
Role changes apply only changed or stopped services with `up --no-deps`, not
the whole stack: an unchanged CoreDNS must retain its container identity while
project consumers still use it.
The engine's actual Docker API version selects the Compose MAC encoding:
per-network at 1.44+, service-level on older APIs (each core role has one network).
Saved declarations are normalized before role comparison, so a change of
encoding alone neither replaces a container nor changes its live MAC.
Endpoint MAC pinning does not fix a runtime that changes the bridge's own MAC.
Podman/Netavark must also include the upstream stable-bridge fix documented in
the README requirements; Portmap never changes host ARP policy to compensate.

Auto-detection respects an explicit `DOCKER_HOST`, then probes a live Docker
socket, then rootless Podman (`$XDG_RUNTIME_DIR/podman/podman.sock`, defaulting
to `/run/user/<uid>/podman/podman.sock`), then the rootful Podman socket.
Explicit backend/socket configuration takes precedence. Infrastructure requires
a local Unix socket; remote machine access goes through direct or SSH profiles.

The takeover shim sends managed projects through the unified CLI and doubles
as Podman's Compose provider: its provider search includes
`$HOME/.docker/cli-plugins/docker-compose`.

Controller Compose invocations select their CLI once per process: the
`docker compose` plugin when usable, else an installed standalone
`docker-compose` client. No user-level docker configuration is written to
make Compose runnable, and a selected command's real failures are reported,
never retried on the other command.

The planner, endpoint model, labels, DNS/route generation, ownership and
rollback rules are shared. Compatibility APIs are not treated as equivalent
networking or privilege behavior: the controller reads Podman's native
rootless flag and maps container file ownership accordingly. New ingress always
asks the runtime to publish the exact requested binding. Only an actually owned
legacy entry preserves the systemd socket adapter; runtime failures do not
select it as a fallback. No low-port sysctl, host capability, linger or logind
policy change is part of setup.
[podman-spike.md](podman-spike.md) is historical evidence, not current acceptance.

## Unified Local and Remote Access

The public executable is `portmap`. Local publishing and remote access are
optional roles of the same command, not separate installations. The frozen
one-directory bundle includes Python, the CLI/agent, access and publishing
modules, infrastructure/demo/scaffold assets, and frontend. It does not contain
host CoreDNS or Traefik executables. Setup always requires a working runtime
and Docker Compose. Standalone installation alone starts nothing. The updated
Brew helper activates through an available user service manager; otherwise it
reports installation without attempted activation and gives manual commands.

The frozen executable re-executes itself for the host agent and inside the
catalog container. Native/OS subprocesses receive an environment without
bundle-specific dynamic-library paths. The shared state root is
`$XDG_STATE_HOME/portmap` (default `~/.local/state/portmap`); `client/` contains
profiles, desired connection configuration, resolver and infrastructure
ownership, while `server/` records the optional local publishing role.

Exactly two remote transports are supported: direct IP reachability,
including existing LAN/Tailscale routing, and ordinary OpenSSH forwarding.
No Coder API, VPN implementation, or custom HTTP data plane is introduced.
Discovery inventories literal SSH aliases and static Include globs, not
unknown users, keys, or workspace names. Existing authentication,
ProxyCommand and ProxyJump remain authoritative.

Direct profiles resolve to the reachable IP verified on the workstation.
They retain the remote HTTP port and bypass the local HTTP gateway. Equivalent
default-port address spellings are normalized without weakening hostname-zone
collision checks.

`portmap run` provides an explicit foreground lifetime without a systemd
scope escape. It owns a supervised agent worker process group in the caller's
session/cgroup; host controls, Compose children and SSH masters started there
share that lifetime. Shutdown stops the group with a bounded grace period,
kills remaining owned members before reaping the leader, and then withdraws
dead transports while retaining saved connections and shared containers.
Keeping the leader unreaped prevents its process-group ID from being reused
during cleanup. Blocking request handlers cannot spawn workers after cleanup.
Initialization signals use the existing BaseException rollback path. A healthy
preexisting agent is reused rather than claimed or stopped. Ordinary background
commands retain their existing persistence integration where the OS requires it.


`setup` and `run` enable host DNS and prepare the private HTTP entry, normally
`127.0.0.1:80`. Empty profiles, direct-only access and the last disconnect retain
this entry. Fresh `up` initializes server/container roles without host DNS
takeover; an existing host-access role remains enabled. Setup neither starts
application projects nor enables LAN ingress or boot units.

SSH and local host profiles resolve to loopback and share private HTTP port 80.
Each SSH remote has a private host Unix-socket forward mounted read-only into
the container relay. Container clients resolve those zones to Traefik's bridge
address instead of unusable host loopback. Routing uses HTTP Host, not TLS SNI.

```mermaid
flowchart LR
  Browser[Host browser] --> Direct[Verified direct remote IP]
  Browser --> Private[Exact loopback HTTP entry]
  Private --> Access[Traefik access entrypoint]
  Access --> Relay[Container Unix-socket relay]
  Relay --> SSH[Ordinary-user host SSH forwards]
  SSH --> Remotes[Remote HTTP gateways]
  Access --> PrivateCatalog[Private catalog and machine controls]
  Access --> Web[Same Traefik: local web entrypoint]
  LAN[LAN client] --> Public[Exact LAN HTTP entry]
  Public --> Web
  Web --> Projects[Managed local projects]
  Web --> Catalog[Catalog: private self-hop or public local-only scope]
```

The LAN ingress never targets the private access entrypoint. The public
catalog does not serve imported machines, connection controls, or SSH names.
The container backend uses a separate HAProxy container per role, publishing
the exact requested address/port to a high container port on the existing
bridge. Private ingress targets Traefik `access:80`; public ingress targets
`web:8080`. It uses neither host networking nor wildcard publication, and does
not recreate the gateway or SSH relay. Separate labels and version-2 per-role
records keep ingress out of the four-role core inventory; recorded container
IDs prevent adopting or stopping external replacements.

The optional systemd backend retains exact-address sockets and unprivileged
DynamicUser `systemd-socket-proxyd` services with empty capabilities. Its targets
are the container-published loopback high ports; rootless DNAT is supported.
Unit changes use scoped sudo operations, not root execution of user-writable
portmap code. Neither backend changes sudoers, global low-port sysctls, binary
capabilities, or boot enablement. Conflicting listeners produce an error, never
a silent high-port fallback or a foreign-process shutdown.

CoreDNS separates host, container and legacy-public views. Infrastructure state
records `host_dns_enabled`, an optional resolver plan, captured container
upstreams and a generic boot/namespace/init-process identity. PID 1's start
time distinguishes recreated containers even when namespace inodes are reused.
A new server-only stack has no host DNS publication, but still supplies its fixed
bridge resolver to managed containers. Catalog readiness probes that bridge
resolver's exact ownership/revision marker from inside the network; it does
not need a host DNS listener or a remote server's DNS endpoint.

The Linux default captures ordinary non-loopback upstreams, publishes CoreDNS
at `127.0.0.1:53` over UDP and TCP, and explicitly initializes the system
resolver file. `.portmap` answers remain local; ordinary names are forwarded
to the original servers in order. Captured upstreams belong in the Corefile,
so forwarding can hot-reload without recreating consumer-needed CoreDNS.
An explicit custom container-forwarding configuration retains its meaning.
A real forwarding probe precedes takeover; actual system resolution must
succeed before host access is reported ready.

The recovery journal saves original content, file kind, symlink target,
device/inode, mode and owner before writing, then saves the actual active file
identity. An untouched original can resume interrupted setup. Identical
redirect bytes alone cannot prove ownership of a replaced file. Unreadable
or externally replaced files fail closed; recovery information and the live
DNS target remain available. Bind-mount writes are in-place and may need manual
recovery after forced interruption.

On a new environment, a genuinely fresh resolver is captured as the new
original; an old backup is never restored over it. A surviving owned file
must still pass identity checks. A lost engine graph can be rebuilt only after
proving the environment changed and that neither the saved network nor any
owned containers or business references survive.

Loopback/VPN stubs and mixed upstream policies are not flattened. A usable
resolved adapter can preserve the existing `~portmap` route; its owned dummy
interface is the only adapter requiring host NET_ADMIN. Saved legacy ports
remain compatible. The advanced `external` adapter retains a preconfigured
suffix route without writing host DNS. No adapter edits `/etc/nsswitch.conf`,
invents a public DNS server, or pretends unavailable system resolution is ready.

Client and server DNS are independent: direct clients resolve remote names to
a verified reachable IP; SSH clients resolve them to their own private entry.
HTTP preserves `Host`, and the server routes to internal project ports.
Remote discovery requires HTTP identity, not remote DNS access. Host DNS
metadata is omitted for server-only catalogs; download bootstrap uses the
separate HTTP address, never `dns_server`.

Managed containers use CoreDNS at the fixed bridge address on port 53;
their SSH/local answers point at Traefik's bridge address. The legacy-public
view never inherits private SSH routes. Dormant SSH profiles gain active
DNS mappings only after successful restoration.

The private catalog selects local or remote machines explicitly. Remote
registries are fetched through the selected transport and projected into
client-visible links; `/actions/*` targets that same machine. Unknown or
unavailable selections fail rather than falling back to local. Application
subdomains preserve Host and do not have `/registry.json` intercepted.
Connection mutations require a same-origin JSON request, including a matching
HTTP port; the public catalog cannot invoke them.

Connection state is private, serialized and atomically published. Failed
connect publication restores previous routes and removes the newly owned
tunnel. Disconnect leaves remote containers untouched. Stopping local
publishing preserves remote access. Teardown restores host DNS and removes
remote transports but retains the shared data plane while local publishing or
running/stopped business containers still consume it. The same live CoreDNS
and its existing publications stay until final consumer removal; keeping only
an empty network while killing its resolver is not sufficient. Default-state
setup can import the old `portmap-client` layout.
Both imported and in-place native profiles preserve a verified live SSH master:
the Unix HTTP forward and published route are proved before the exact old HTTP
forward is cancelled. Saved authentication, custom remote ports and raw TCP
forwards remain intact. Dead masters receive one explicit restoration attempt;
failures are reported without inventing a replacement identity.

Relay startup renders the desired private Unix-socket endpoints before HAProxy
loads its first worker; startup does not depend on an early reload signal.
Recreated infrastructure must load its saved revision before a live update.
DNS and public HTTP publication remain gated on the desired revision.
A native worker holding the requested private HTTP port is released only after
the replacement route is proved. Its captured command and paired ownership
records are restored on a pre-commit interruption; unrelated listeners remain
outside the transaction.

Distribution uses `/api/downloads`, `/install.sh`, and
`/downloads/portmap/<filename>`. The Web distinguishes installation from
readiness and documents foreground startup and explicit DNS forwarding.
The updated Homebrew helper waits for setup/page readiness when activation is
available, but reports unattempted activation on machines without a usable user
manager. Neither path installs or starts a container engine. Published V0.13
assets and resource pins remain unchanged until a separate release.

`release.json` pins real versioned assets, size and SHA-256, and its generated
`source` block records the repository, release tag and exact source commit.
Frozen installs resolve an adjacent sidecar first; source/Brew installs and
raw bundles without a sidecar fetch the bounded official manifest pinned to
their exact version from the trusted release and cache it. The archive never
embeds its own checksum manifest: build and installer write the sidecar
separately. Invalid local metadata fails
verified before serving. Canonical HTTPS GitHub URLs and trusted signed-CDN
redirects remain constrained; arbitrary hosts and credential-bearing URLs
are rejected. Standalone installation itself changes no DNS, processes or
listeners. Brew uses the ordinary user's available service manager without
escaping the package-install sandbox. Attempted activation failures propagate;
unattempted activation leaves existing services untouched and claims no readiness.

Exact-version release lookup preserves the immutable historical `V0.0` through
`V0.19` tags for versions `0.0.0` through `0.19.0`. All newer normal releases
and hotfixes use lowercase full-version `vX.Y.Z` tags; patch manifests and their
generated source identity retain the complete version. New release versions
must be greater than `0.19.0`; lowercase aliases for historical versions and
invented older patch tags are rejected. Release builds accept only the paired
full lowercase tag and exact HEAD commit and never write a manifest into
source. The current updater understands both historical and new
tags. Already-installed old binaries only recognize the old `V#.#` latest-tag
format and cannot discover the first new-format release themselves: Brew users
must explicitly run `brew update` and `brew upgrade ylang-ylang/tap/portmap`
(standalone users must explicitly reinstall from the new official release).
There are no compatibility aliases or bridge tags, and publishing a release
does not automatically upgrade any machine.

Source-snapshot SSH deployment is a separate controller operation:
`tools/deploy_portmap.py` builds with the pinned builder and stages a pristine
source snapshot (no generated manifest inside), the built release manifest,
and tap helpers. `deploy_apply.py` installs an owned
local tap and explicitly runs the setup helper outside the Brew sandbox, then
checks actual system resolution and HTTP. The installed `portmap setup`
itself verifies (and when needed repairs) the container runtime, so the
deploy flow adds no separate pre-repair step. An identical snapshot skips
rebuild and reinstall. The staged manifest and its verified archive are
seeded into the target's download cache, so the deployed snapshot serves
exactly its own build offline; public release pins are not rewritten.
Existing packages use Homebrew's reinstall transaction even for a version
upgrade: plain install refuses a switch from a published tap to the snapshot
tap. Reinstall preserves the old keg for rollback instead of requiring a
manual uninstall. Fresh packages still use install.


`portmap.runtime_compat` runs inside every standard entry (setup, run, up)
through the infrastructure controller before any portmap-owned container or
network exists. No separate repair command or deployment helper is needed.
It preserves the selected engine and only repairs
the known old rootless Netavark helper incompatibility when the exact
user-owned API service is idle and safely identifiable. Helpers and the
configuration override are user-local; the owned header plus the leading
helper directory marks files an earlier deployment of the same transaction
wrote, and those are reused or refreshed in place instead of colliding
forever after a workspace rebuild swapped the API service. Restart/rollback
uses process start identities and socket peer credentials; an unavailable
service working directory is refused before terminating the live API. Foreign
replacement listeners or external configuration edits are not overwritten,
running workloads and unknown service processes are refused explicitly, and
any failure after the first owned mutation rolls the transaction back.
Compatible engines -- including every Docker backend -- are no-ops. Private
state directories still require the current owner and mode 0700 permission
bits; inherited setgid does not grant group access and is not mistaken for
an unsafe directory.

Restoring a persistent home volume can relax those bits again (directories
to 2770/2775, records to 0660) while the private home itself keeps blocking
every other user. `portmap.permissions` centralizes the recovery every gate
shares: an `O_NOFOLLOW` descriptor walk proves no symlink participates and
that one owned ancestor still denies group and other access, and only then
the verified descriptor is tightened back to 0700/0600 -- setgid preserved
where the kernel allows it, content untouched, nothing recursed. File
descriptors must match the leaf reached through the verified parent and have
only one hard link. Paths without a private ancestor, foreign-owned hops,
symlinks and stripped owner bits remain refused. Persisted agent metadata
uses the same proof; its old readable metadata-only runtime compatibility
cannot authorize recovery of writable files from an untrusted shared path.


## Fixed Model

The default data plane is split by endpoint shape:

```text
HTTP / WebSocket / SSE / CDP / WebRTC signaling
  -> Traefik HTTP routers
  -> branch service:container_port

raw TCP / raw UDP
  -> Docker direct host port mapping
  -> branch service:container_port

dynamic port-range protocols
  -> project-owned service
  -> Docker direct mapping for one entry host port plus one branch-scoped host port range
```

`portmap` owns only the control-plane pieces:

- branch/project naming rules
- shared gateway network name
- wildcard debug DNS domain
- compose inspection
- endpoint discovery or declaration
- generated compose overlays
- generated Traefik labels for HTTP endpoints
- generated direct Docker port mappings for TCP, UDP, and range endpoints
- raw TCP/UDP host-port allocation
- range endpoint host-port allocation
- host Git worktree discovery through the host agent
- local endpoint registry
- status and cleanup metadata

It does not own:

- HTTP proxy implementation
- TCP/UDP proxy implementation
- range-like protocol implementation
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

## Host Agent And Shared Gateway

```bash
portmap up
```

Local publishing uses the already prepared container stack and host agent.
The agent scans Git worktrees and performs host-side Compose operations;
Traefik, CoreDNS and the catalog retain gateway-network access on both Docker
and rootless Podman.

Docker labels contain host paths, not paths in the catalog container. The
catalog must not stat those paths or run Git against them. A discovered
running service starts with `worktree_exists: null` and an empty
`worktree_status` (unknown/untracked); missing Git metadata, an unavailable
agent, or an unmatched/partial agent record does not mean deletion.
Matching host-agent metadata is authoritative for existence, Git roots and
submodule state. Only host-confirmed absence yields `worktree_exists: false`
and `worktree_status: "deleted"`. Unknown directory state does not suppress
service discovery, endpoints, Restart or Down.

The gateway publishes three distinct loopback high ports: private access,
local web, and management/readiness. The catalog's private/public listeners
have no independent host publication. On the web entrypoint, application Host
routes outrank catalog fallbacks. A private self-hop from Traefik's own
container loopback selects the private catalog; public ingress cannot supply
that TCP source address. Other web traffic selects the local-only public
catalog. The `portmap.gateway=true` label excludes infrastructure from project
lists and lifecycle controls.

The public HTTP port is advertised separately from these backend ports.
Only an explicit non-loopback IPv4/IPv6 `http_bind` creates LAN ingress;
wildcards and loopback-only configuration create none. The LAN socket targets
the web backend, never the private access backend.

`server/instance.json` records uid, effective settings, backend port and
access-state directory under a lock. Stop uses saved settings, not a changed
shell environment. Up prepares/reuses infrastructure, publishes the local
role and optional LAN ingress, then commits ownership. A failed first startup
cleans only newly owned resources; existing publishing state stays recoverable.
The public backend must return a valid catalog within the same bounded startup
budget as the shared stack. It does not use the shorter interactive remote
discovery deadline: first catalog generation can include runtime and host-agent
worktree discovery. Static HTML or a listening socket alone is not readiness.
Down removes the local role and public ingress, not the shared stack or agent.
SSH/direct profiles and private HTTP/DNS survive. Teardown releases access
infrastructure only after local publishing has stopped, and retains networks
still consumed by business containers.

Background portmap workers inherit the invoking CLI user's credentials;
scope creation does not elevate them or enable boot services. On Linux, a CLI
in a logind `session-*.scope` or a per-user manager cgroup adopts only its own
PID into a transient system-manager scope (one exact privileged `busctl
StartTransientUnit`, mode `fail`, `CollectMode=inactive-or-failed`). Workers
therefore survive both the caller's session and an installer/user service
ending, without enabling linger. Processes already in a system service need
no move. Replacing a standalone bundle or source tree alone does not rescue
old workers; Brew runs setup, which can still reuse compatible live workers.

Source/Brew catalog containers mount packaged Python modules read-only.
The catalog's Compose configuration includes the bind-source device/inode
identity. Replacing a keg at the same pathname therefore recreates only the
catalog before Brew removes the old directory; unchanged mounts do not cause
recreation. The access indicator uses the selected machine's canonical catalog
URL rather than inventing a publishing-only hostname for setup-only clients.
Frozen mode uses the same generated Compose model, mounting the complete
bundle read-only and executing `portmap _serve-catalog` in a compatible image.
Host agent workers re-execute that executable rather than assuming an external
Python installation. There is no alternate frozen Compose stack.

Cold recovery prepares the private SSH socket directory before generating or
starting Compose, both for an existing stack and for a stopped stack being
seeded again. The directory may have disappeared with `/tmp` at reboot; letting
the engine recreate this bind source produces the wrong owner or permissions.
Preparation uses the same owner record and private-mode checks as SSH forwards.
Existing foreign or root-owned directories are refused, not silently adopted
or chmodded.

The relay's Compose specification also records the socket directory's
device/inode identity. Replacing that directory at the same pathname therefore
remounts only the relay, rather than retaining a bind to the removed object.
An unchanged CoreDNS container keeps its identity. Recovery from an already
mis-owned legacy directory requires an ownership-proven operator repair first;
normal startup then keeps the directory private without engine-side creation.


DNS and raw TCP/UDP/ranges remain separate from HTTP; unified access needs no
remote port 53. Existing business containers, their endpoint labels and DNS
addresses survive migration. Generated overrides change only when the project
is explicitly regenerated; infrastructure cutover does not recreate projects.

The controller creates or validates the shared bridge network (default):

```text
portmap_gateway
```

Newly owned bridges enable IPv4 and IPv6 through the runtime's native IPAM.
Existing bridges retain their configuration and consumers. An IPv6 direct
profile requires an IPv6-enabled bridge before routes or DNS are published;
the controller does not rebuild an existing IPv4-only network behind running
projects. Direct traffic remains native IP traffic, not a relay workaround.

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

Workstations use `portmap setup` and `portmap connect`, not physical-interface
DNS rewrites or per-endpoint hosts entries. Containers use the shared bridge
DNS address. A migrated server's existing external DNS binding is preserved
only for its prior consumers, not for the unified client transport.
The advanced `portmap dns set/unset` drop-in utility is independent of the
access resolver and is not invoked by setup.

## Endpoint Classes

`portmap` should classify exposed services into four endpoint classes.

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

Internal router, service, and middleware names include the repository ID,
branch, and worktree identity so equally named projects do not collide in
Traefik. Public hostnames keep their human-readable names. Example labels:

```yaml
services:
  frontend:
    labels:
      - traefik.enable=true
      - traefik.http.routers.c8120ac634d1-feat-a-e19f736c-frontend.rule=Host(`frontend.feat-a.comap.debug.lan`)
      - traefik.http.routers.c8120ac634d1-feat-a-e19f736c-frontend.entrypoints=web
      - traefik.http.services.c8120ac634d1-feat-a-e19f736c-frontend.loadbalancer.server.port=5173
      - traefik.http.routers.c8120ac634d1-feat-a-e19f736c-frontend.middlewares=c8120ac634d1-feat-a-e19f736c-frontend-host
      - traefik.http.middlewares.c8120ac634d1-feat-a-e19f736c-frontend-host.headers.customrequestheaders.Host=127.0.0.1:5173
      - traefik.docker.network=portmap_gateway
```

Request path:

```text
browser
  -> http://frontend.feat-a.comap.example-host.portmap
  -> Traefik web entrypoint
  -> portmap_gateway
  -> frontend container:5173
```

The browser needs the URL, not the container port.

### `range`

Examples:

- TURN/coturn
- SIP plus RTP
- RTSP plus RTP/RTCP
- FTP passive mode

Range endpoints model protocols with one entry/control port and a later
runtime-selected data/media port range. `portmap` does not understand the
protocol; it only allocates the external entry port and the contiguous host port
range, then writes both into the generated compose override.

The important resource is the port pool and its index:

```text
repo + worktree root + branch + endpoint -> entry port + range
```

Protocol-specific behavior stays outside `portmap`.

Example declaration:

```toml
[endpoints.turn]
kind = "range"
service = "coturn"
container_port = 3478
protocol = "udp"
range_size = 40
```

Example generated compose shape:

```yaml
services:
  coturn:
    ports:
      - "34781:3478/udp"
      - "49160-49199:49160-49199/udp"
    environment:
      PORTMAP_TURN_HOST: "<detected-host-ip>"
      PORTMAP_TURN_PORT: "34781"
      PORTMAP_TURN_PROTOCOL: "udp"
      PORTMAP_TURN_RANGE_MIN_PORT: "49160"
      PORTMAP_TURN_RANGE_MAX_PORT: "49199"
      PORTMAP_TURN_RANGE_SIZE: "40"
```

The range uses same-number host/container mapping. This is deliberate because
protocols such as TURN advertise selected relay ports to external clients; the
project service must be configured to listen on the injected range.

All services referenced by `endpoints.toml` receive the generated
`PORTMAP_<ENDPOINT>_*` variables. This stays generic while allowing one service
to consume another endpoint's externally assigned address, for example a browser
service reading `PORTMAP_TURN_*` to publish TURN details.

### `tcp`

Examples:

- MQTT
- Postgres
- Redis
- MySQL
- custom TCP services

Raw TCP usually cannot share one entrypoint by hostname because it has no HTTP
`Host` header. TLS TCP with SNI is an exception, but v1 should not rely on it.

For raw TCP, `portmap` allocates a host port and writes a Docker direct port
mapping.

Example generated override:

```yaml
services:
  mqtt:
    ports:
      - "18831:1883/tcp"
    environment:
      PORTMAP_MQTT_HOST: "<detected-host-ip>"
      PORTMAP_MQTT_PORT: "18831"
      PORTMAP_MQTT_PROTOCOL: "tcp"
    labels:
      - portmap.managed=true
      - portmap.endpoints.comap-feat-a-mqtt.kind=tcp
      - portmap.endpoints.comap-feat-a-mqtt.host=<detected-host-ip>
      - portmap.endpoints.comap-feat-a-mqtt.host_port=18831
```

Request path:

```text
mqtt client
  -> 127.0.0.1:18831
  -> mqtt container:1883
```

The service container usually does not need to know the external host port. The
client does, so the port must be written to the registry.

### `udp`

Examples:

- custom UDP services
- coturn/TURN UDP ports when the project owns coturn

Raw UDP also needs allocated host ports. It uses Docker direct port mapping in
v1.

Example generated override:

```yaml
services:
  udp-service:
    ports:
      - "19991:9999/udp"
    environment:
      PORTMAP_UDP_HOST: "<detected-host-ip>"
      PORTMAP_UDP_PORT: "19991"
      PORTMAP_UDP_PROTOCOL: "udp"
    labels:
      - portmap.managed=true
      - portmap.endpoints.comap-feat-a-udp.kind=udp
      - portmap.endpoints.comap-feat-a-udp.host=<detected-host-ip>
      - portmap.endpoints.comap-feat-a-udp.host_port=19991
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
  README.md
  .gitignore
  docker-compose.override.generated.yml
  state.json
```

`endpoints.toml` is the project-local source of truth. The generated compose
override is the only project-local artifact required for HTTP endpoints because
Traefik and the catalog both read Docker labels from running containers.
`state.json` is branch/worktree-local generated state. It records the generated
compose project name and endpoint identity for the broker.

`portmap init` creates `.portmap/endpoints.toml`, `.portmap/README.md`, and
`.portmap/.gitignore`. The README is deliberately project-local: it documents
how this compose repo should be shaped for portmap to manage it, how endpoint
kinds map to HTTP/raw/range routing, how to generate the branch-specific
override, and how to query the shared catalog. `portmap generate` should also
add the README and `.gitignore` when they are missing, but it should not
overwrite existing project-local docs.

Raw TCP/UDP endpoints may need additional generated state because they consume
host ports:

```text
~/.local/state/portmap/allocations.json
```

This allocation pool is host-wide, not branch state. It prevents two worktrees
from picking the same raw/range host port before Docker has bound either port.
If a caller explicitly chooses project-local allocation state, that file may be
stored as `.portmap/allocations.json`.

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

The catalog is derived from Docker/Podman labels on running containers. It has
no host-port mapping of its own; the backend Traefik fallback serves it through
the controlled public or private HTTP entry:

```text
http://example-host.portmap/
http://example-host.portmap/registry.json
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
          "url": "http://frontend.feat-a.comap.example-host.portmap"
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

Open implementation items are tracked in [todos.md](todos.md).
