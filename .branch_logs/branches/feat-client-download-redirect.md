# feat/client-download-redirect

<!-- git-guard: ref=refs/heads/feat/client-download-redirect -->

- Fix real empty-cache download failure on GitHub's signed asset-CDN redirect.
  Permit query strings only on trusted CDN hosts; retain query-free canonical
  manifest URLs and all HTTPS/host/credential/port/fragment checks.
- Add a regression through urllib's redirect handling and verified archive
  publication, replacing only the external HTTPS transport.
- Verify a real public release download in the catalog's Python Alpine image
  with an empty cache, no monkeypatches, exact size/SHA-256, and no partial files.
  Confirm negative redirect hops still fail closed.
- Keep the published V0.7 immutable. Align package/client versions at 0.8.0
  for the V0.8 correction release; client runtime behavior remains unchanged.
