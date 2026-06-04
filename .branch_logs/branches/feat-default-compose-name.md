# feat/default-compose-name

<!-- git-guard: ref=refs/heads/feat/default-compose-name -->

- Rename the main gateway compose file to Docker Compose's default
  `docker-compose.yml` while keeping the mock compose file separate.
- Update `portmap gateway` and tests to use the default compose filename.
- Let direct default compose rendering use the same DNS forward default as
  portmap settings.
