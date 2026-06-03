# portmap

Branch-scoped network manager for local docker-compose debug environments.

The tool is a control plane, not a traffic proxy. Its default data plane is
Traefik.

It maps branch/worktree instances to generated Docker Compose overlays,
Traefik labels, Traefik entrypoints, and a CLI-queryable endpoint registry.

It deliberately does not manage project runtime capability such as GPU,
browser image contents, Playwright workflows, MCP business tools, frontend
performance logic, TURN credentials, ICE config, or application startup policy.
Those belong to the project or the agent workflow.

## Scope

`portmap` manages network variation between branches:

- inspect rendered Docker Compose services, ports, expose entries, and labels
- generate branch-scoped Docker Compose override files
- generate Traefik labels for HTTP/TCP/UDP endpoints
- generate Traefik entrypoints for raw TCP/UDP endpoints
- allocate non-conflicting raw TCP/UDP host ports
- write a local endpoint registry for CLI queries
- clean up generated routes, ports, and instance state

`portmap` also provides a shared gateway compose file with Traefik and CoreDNS.
Projects do not need to define their own Traefik service when they use the
generated override.

The project only declares endpoint names, compose service names, and container
internal ports. Existing `ports` and `expose` entries can be treated as endpoint
candidates.

## Non-goals

- no host-network mode selection
- no GPU or desktop runtime management
- no HTTP/TCP/UDP proxy implementation
- no TURN credential or ICE config management
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
- raw TCP/UDP allocation state only when raw endpoints need host ports
- extra Traefik static data only when raw TCP/UDP entrypoints are needed

For HTTP-only projects, `.portmap/` can stay minimal:

```text
.portmap/
  endpoints.toml
  docker-compose.override.generated.yml
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

Start the shared gateway once:

```bash
cp .env.example .env
docker compose -f docker-compose.gateway.yml up -d
```

This creates the `portmap_gateway` Docker network and exposes:

```text
Catalog: http://192.168.201.52
Traefik: http://192.168.201.52:8080
DNS:     192.168.201.52:53
```

CoreDNS answers every `*.debug.lan` A record with `192.168.201.52`.
Configure development machines with split DNS so only `debug.lan` queries go
to this DNS server.

Configure split DNS on a Linux development machine without manually looking up
the network interface:

```bash
DNS_SERVER=192.168.201.52
DNS_DOMAIN=debug.lan
DNS_IFACE="$(ip route get "$DNS_SERVER" | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"

sudo resolvectl dns "$DNS_IFACE" "$DNS_SERVER"
sudo resolvectl domain "$DNS_IFACE" "~$DNS_DOMAIN"
resolvectl query "portmap.$DNS_DOMAIN"
curl -I "http://portmap.$DNS_DOMAIN/"
```

The catalog page lists currently visible `portmap`/Traefik-managed services and
their generated endpoints. The JSON form is available at:

```text
http://192.168.201.52/registry.json
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

After editing `.portmap/endpoints.toml`, generate files for the current
branch/worktree:

```bash
portmap generate \
  --project-dir /path/to/project \
  --compose-file /path/to/project/docker-compose.yml \
  --config /path/to/project/.portmap/endpoints.toml \
  --out-dir /path/to/project/.portmap \
  --branch dev \
  --repo-id my-repo \
  --repo-name my-repo \
  --domain-suffix debug.lan \
  --gateway-network portmap_gateway
```

Then start the project with the generated override:

```bash
docker compose \
  -f docker-compose.yml \
  -f .portmap/docker-compose.override.generated.yml \
  up -d
```

Optionally, let portmap broker host `docker compose` calls in the current
shell:

```bash
source <(portmap shell-hook --compose-takeover)
docker compose up -d
docker compose ps
```

The hook only intercepts `docker compose` when `PORTMAP_COMPOSE_TAKEOVER=1`.
It does not affect `docker ps`, `docker run`, or compose commands that already
specify `-f/--file`. Disable it in the current shell with:

```bash
portmap_compose_takeover_off
```

The main repo `.env.example` includes `PORTMAP_COMPOSE_TAKEOVER=0` as the
default switch. When `portmap shell-hook` is run from the portmap repo, it reads
the local `.env` and uses that value. The gateway containers do not consume it;
it is for the optional shell hook only.

Without a shell hook, the same broker can be called explicitly:

```bash
portmap docker-compose -- up -d
```

The generated override adds managed services to `portmap_gateway` and writes
Traefik Docker labels. The shared Traefik container discovers those labels
through the Docker socket.

Query the shared catalog:

```bash
curl http://192.168.201.52/registry.json
portmap list
portmap status
portmap endpoints my-repo dev
```
