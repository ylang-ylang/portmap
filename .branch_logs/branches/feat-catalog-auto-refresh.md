# feat/catalog-auto-refresh

<!-- git-guard: ref=refs/heads/feat/catalog-auto-refresh -->

- Replace the catalog's top-level Refresh button with completion-paced 15-second polling while visible, plus focus/visibility refresh; pause in hidden tabs and Settings, coalesce foreground events, and preserve machine selection.
- Keep explicit Retry actions for catalog/machine-list errors. Avoid aborting an initial catalog load when clicking its already-selected tab.
- Simplify machine tabs to hostname only; keep transport/address details in Settings, offline styling and hover description, and keyboard tab navigation. Remove obsolete target/offline-badge CSS.
- Build the shipped JS/CSS and hot-update only those two assets in mj's existing 0.14.0 installation without restarting services or changing DNS/connections. Original assets backed up at /home/ylang/portmap-catalog-static-backup-0.14.0-20261016/.
- Real Chromium before/after through mj: Refresh 1→0; zero automatic registry requests before, one during the 18-second visible after window; hidden window zero, foreground restore one; selected machine unchanged. All four tab texts are single hostnames; Settings retains full connection targets; all three existing connections remain reachable.
- Browser Settings extraction hit a verifier-only document-context error; its captured actual DOM still demonstrated all three connection details. No UI source-text tests added; real browser behavior and served-asset hashes provide smoke evidence.
