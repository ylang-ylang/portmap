# feat/catalog-compose-down-action

## 2026-06-03

- Add a catalog web action for stopping a visible portmap-managed Docker Compose project.
- Render a `Down` button per compose project in the branch section of the catalog UI.
- Execute the action through the Docker API using `com.docker.compose.project` labels, so stale projects can be stopped even if the original worktree path moved.
- Validate that the target compose project has at least one `portmap.managed=true` container before removing project containers and networks.
- Add catalog tests for the button HTML and Docker API action behavior.
