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
- Make running/dead branch count buttons fixed-width and visually mute zero
  states with explicit empty styling.
- Widen the fixed running/dead branch controls so their icons, count badge, and
  label fit without overflow.
- Increase spacing between restart/down action buttons so branch controls do
  not feel visually cramped.
- Sync the current `dev` merge commit before merging the spacing follow-up back
  to `dev`.
