# feat/hostname-domain-default

<!-- git-guard: ref=refs/heads/feat/hostname-domain-default -->

- Remove the fixed `debug.lan` override from the checked-in gateway config.
  Source checkouts now inherit the same hostname-based DNS default as installed
  server packages instead of silently selecting the legacy shared domain.
- Keep machine-local listener settings in user configuration. Migrate the
  active deployment and generated application endpoints through the installed
  CLI, with standalone client DNS/access replacing the old global DNS rule.
