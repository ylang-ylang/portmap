# feat/fronted

<!-- git-guard: ref=refs/heads/feat/fronted -->

- Keep DNS status colors explicit in dark mode so failed probes stay red and
  successful probes stay green.
- Show successful DNS probes as `succ` in the catalog header status badge.
- Add a `pM` SVG favicon to the catalog browser tab and package it with the
  static frontend build.
- Extend static frontend tests to cover the favicon, DNS success label, and DNS
  status CSS classes.
- Sync the current `dev` branch into `feat/fronted` before additional catalog
  frontend changes.
