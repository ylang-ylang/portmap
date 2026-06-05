# feat/catalog-public-static

<!-- git-guard: ref=refs/heads/feat/catalog-public-static -->

- Serve Vite public root assets from the production Python catalog server so
  `/favicon.svg` and similar root-level static files behave like mock mode.
- Document the mock Vite versus production Python static-serving contract in
  the catalog frontend README section.
- Add HTTP route tests that verify `/favicon.svg` returns `image/svg+xml` and
  missing root public static assets return 404.
