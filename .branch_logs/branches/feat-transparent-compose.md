# feat/transparent-compose

<!-- git-guard: ref=refs/heads/feat/transparent-compose -->

- Make the main gateway compose file transparent when no portmap runtime
  environment is present.
- Keep `docker compose config` and `docker compose down` from failing on a
  missing `PORTMAP_STATE_DIR`.
- Add a gateway compose regression test for required interpolation defaults.
