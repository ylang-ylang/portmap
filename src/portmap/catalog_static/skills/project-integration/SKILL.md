---
name: portmap-project-integration
description: Connect a Compose project or Git worktree to an existing Portmap gateway and verify its real development URLs.
metadata:
  portmap-version: "{{PORTMAP_VERSION}}"
---

# Project integration with Portmap

This guide ships with the serving Portmap catalog version **{{PORTMAP_VERSION}}**.
It describes that version, not necessarily the version installed on a selected remote machine.
Run `portmap --version` on the machine hosting the project; if versions differ, consult
that installation's `--help` and its own guide before using unsupported commands.

## Authority and scope

This is reference documentation, not a script or a grant of permission. Follow the
user's instructions and the project's agent rules first. Read the document; never
pipe this Markdown into a shell. Do not expose tokens, change DNS policy, replace
Coder templates, grant privileges, or delete unrelated resources to make setup pass.
Do not run global `portmap down` or `portmap teardown` for project cleanup.

Work in the requested repository/worktree **on the machine where the application
will run**. A gateway URL copied from a browser does not authorize deployment to a
different machine. A local or SSH-connected client is only the viewing/access side.
If this guide's URL is unreachable, report the network prerequisite rather than
changing system DNS or installing a second gateway without permission.

## 1. Inspect before changing anything

- Read existing project instructions and `.portmap/README.md`, if present.
- Locate the existing Compose file(s), service names, internal listening ports,
  health checks and Git worktree. Preserve application-specific devices, mounts,
  environment and production deployment arrangements.
- If no Compose setup exists, explain the required containerization changes and
  obtain approval before changing the project's deployment architecture.
- Run `portmap --version` and `portmap status --check` on the project host.
  A working Docker or Podman API and a Compose client are prerequisites.
- Do not reinstall a working installation. If the existing gateway is stopped,
  and starting it is authorized, recover with `portmap setup` then `portmap up`.
  Surface failures; do not change runtime policy or silently switch DNS backends.
- Installation persistence and automatic service startup are separate. These
  commands do not install a Coder startup hook or guarantee recovery after reboot.

## 2. Initialize the project contract

From the repository/worktree root, run `portmap init` only when the project has not
already been initialized. For a non-default Compose file use
`portmap init --compose-file <actual-compose-file>`. Replace angle-bracket values
with paths discovered in the repository. Never use `--force` to overwrite an
existing contract without reviewing and obtaining approval for the changes.

Read the generated `.portmap/README.md`. Declare only the services the user wants
exposed in `.portmap/endpoints.toml`. For example, if the actual Compose service is
`web` and its container listens on port 3000:

```toml
[endpoints.web]
kind = "http"
service = "web"
container_port = 3000
```

Use the real service name and **container-internal** port, not a guessed or newly
allocated host port. Put primary user-facing endpoints first.

For managed services:

- Use bridge networking, not `network_mode: host`.
- Bind the application to `0.0.0.0` inside its container, not only `127.0.0.1`.
- Prefer `expose` over fixed host `ports` for HTTP services. Do not remove a mapping
  that serves a separate required purpose without checking its consumers.
- Avoid fixed `container_name` and fixed Compose project names. Portmap scopes
  instances to branches/worktrees; overriding that identity can cause collisions.
- Reuse the shared gateway. Do not add project-local Traefik/CoreDNS services or
  hand-edit generated networks, routing labels or allocation files.

Commit the project contract (`endpoints.toml`, project guidance and ignore rules).
Do not commit generated overrides, `state.json`, `registry.json` or allocation state.
Keep each session's edits and generated state in its own worktree.

## 3. Start the current project instance

For an auto-discovered standard Compose file, use the explicit broker entry,
independent of shell/plugin integration:

```bash
portmap docker-compose -- up -d
```

**Explicit `-f` options bypass automatic Portmap injection.** For a non-default
Compose file, generate the override first and include it yourself:

```bash
portmap generate --compose-file <actual-compose-file> --config .portmap/endpoints.toml --out-dir .portmap
portmap docker-compose -- -p <generated-compose-project> -f <actual-compose-file> -f .portmap/docker-compose.override.generated.yml up -d
```

Read `<generated-compose-project>` from `compose_project` in `.portmap/state.json`;
do not invent a fixed shared name. For multi-file setups, generate from the effective
merged Compose configuration and preserve the same base `-f` options, followed by
the generated override. Rendered configurations can contain secrets: keep them
local and git-ignored. Verify the final merged config includes Portmap routes.

Do not use `portmap compose`: it is not an alias for the explicit broker command.
For later project operations use the same worktree. These short examples apply to
the auto-discovered case; explicit-file setups must repeat the same generated `-p`
and complete `-f` options used at startup:

```bash
portmap docker-compose -- ps
portmap docker-compose -- logs --tail 100
```

To stop/remove **this project's** instance when requested:

```bash
portmap docker-compose -- down
```

Do not add `--volumes`, prune the engine, or stop other projects. Portmap's global
`down`/`teardown` commands are not substitutes for project-scoped Compose operations.

## 4. Obtain real URLs and verify the application

```bash
portmap list
portmap status --check
portmap endpoints <actual-repo-name> <actual-instance-name>
```

Take repository/instance identities and URLs from the actual catalog/CLI output;
do not fabricate hostnames from a branch name. The private local catalog is also
available at `http://127.0.0.1/registry.json` on a standard-port installation.
Respect an existing custom HTTP port instead of assuming port 80 there.

Verify both the application and the intended client path:

1. Inspect service state/logs and exercise the application's real HTTP/API endpoint.
2. From the user's connected client, open the assigned URL and verify the expected
   page or response. The gateway homepage returning 200 proves only the gateway.
3. Confirm the selected catalog machine and project/worktree are the intended ones.
4. If the client is not connected to the project host, report that prerequisite;
   connecting it requires the appropriate target address/SSH access and authorization.

HTTP, WebSocket, SSE and CDP use HTTP routing. Raw TCP needs its own declared
endpoint; SSH clients must explicitly enable TCP forwarding when required. UDP
is not forwarded through the ordinary SSH connection. WebRTC signaling may use
HTTP, but media/TURN/UDP reachability must be designed and verified separately.
Never claim a non-HTTP path works merely because its endpoint was declared.

## 5. Handoff to the next session

Record the project-specific workflow in the project's existing agent instructions
or README, pointing to `.portmap/README.md` and `.portmap/endpoints.toml`. Return:

- The actual project-host identity and repository/worktree.
- Exact project-scoped start, inspect and stop commands.
- Actual assigned URLs and the application responses tested from the intended client.
- Any remaining access, protocol, runtime or restart-recovery limitations.

Do not claim deployment success from generated files, a running container alone,
a mock page, or an untested URL.
