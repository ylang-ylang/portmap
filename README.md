# docker-branch-net

Branch-scoped network manager for local docker-compose debug environments.

The tool has one fixed architecture:

- Docker bridge networking
- host nginx as the single browser/agent entrypoint
- shared TURN service for WebRTC media reachability
- local agent running on the host
- per-branch localhost port allocation

It deliberately does not manage project runtime capability such as GPU,
browser image contents, Playwright workflows, MCP business tools, frontend
performance logic, or application startup policy. Those belong to the project
or the agent workflow.

## Scope

`docker-branch-net` manages network variation between branches:

- allocate non-conflicting localhost ports
- generate docker compose override files with `127.0.0.1` port mappings
- generate nginx reverse-proxy config
- inject shared TURN settings or generated TURN credentials
- write endpoint URLs for host-side agents
- clean up generated routes and allocations

The project only declares endpoint names, compose service names, and container
internal ports.

## Non-goals

- no host-network mode selection
- no GPU or desktop runtime management
- no service-type decision tree
- no business-flow orchestration
- no test selector or performance-analysis logic
- no branch-specific TURN server configuration

## Minimal Endpoint Declaration

```yaml
endpoints:
  desktop:
    service: browser-debug
    container_port: 8081

  cdp:
    service: browser-debug
    container_port: 9334

  frontend:
    service: frontend
    container_port: 5173

  backend:
    service: backend
    container_port: 8000
```

From that declaration the tool can generate:

- `docker-compose.debug.generated.yml`
- `nginx.debug.generated.conf`
- `agent-urls.generated.json`
- allocation state for cleanup and status
