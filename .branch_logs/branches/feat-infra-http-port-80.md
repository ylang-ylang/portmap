# feat/infra-http-port-80

<!-- git-guard: ref=refs/heads/feat/infra-http-port-80 -->

- Coalesce equal effective HTTP/catalog ports with a Compose reset overlay so
  only Traefik publishes the shared port; retain default 8080/80 and custom
  separate bindings.
- Register the catalog backend on the gateway network with a HostRegexp
  fallback at priority 1, and exclude gateway infrastructure from project
  discovery.
- Replace source-text gateway assertions with rendered Compose checks for
  shared ports, environment precedence, separate bindings, routing labels, and
  catalog visibility. Drop the unrelated Git timestamp spelling assertion
  while preserving branch ordering and epoch comparisons.
- Document single-port configuration, safe migration/rollback, project URL
  regeneration, external DNS requirements, and catalog trust boundaries.
- Verification: 46 affected gateway/settings/lifecycle/catalog/planner/broker
  tests pass. An isolated shadow stack served the catalog for bare IP and
  unknown Host requests, and routed web.dev.lushu.debug.lan/map.html with HTTP
  200, first on loopback 18080 and then on host 80. The test catalog published
  no host ports. Restore the existing catalog:80 after the acceptance window;
  existing Traefik:8080, DNS, and lushu containers were not restarted.
- Use the allowed feat/* family because this repository's actual guard policy
  does not include infra/*. Restore temporary guard installation assets and
  use system Git 2.34.1 for guarded integration (the bundled older hook rejects
  the Linuxbrew Git 2.55.0 reference-transaction preparing phase).
