# feat/vite-catalog-ui

<!-- git-guard: ref=refs/heads/feat/vite-catalog-ui -->

- Move the catalog frontend into a Vite app with dev, build, and preview
  scripts.
- Build Vite output into `src/portmap/catalog_static/` so the Python catalog
  server can continue serving packaged static assets.
- Change the catalog hierarchy to collapsible project groups containing
  collapsible branch groups.
- Merge Split DNS setup and unset commands into one utility panel.
- Open catalog links in a new browser tab with `noopener noreferrer`.
- Add a Vite mock catalog mode for quickly previewing the project/branch UI
  without running Docker services.
