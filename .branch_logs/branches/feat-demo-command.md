# feat/demo-command

<!-- git-guard: ref=refs/heads/feat/demo-command -->

- Add `portmap demo up|down`: a self-contained two-endpoint demo project
  (whoami HTTP + raw TCP) under the state directory, driven through the
  real broker pipeline (ensure_generated_override + injected compose
  command). Demo compose/endpoints live as package asset files under
  src/portmap/demo_assets, following the scaffold_templates convention.
- Derive the default DNS domain from the machine hostname: a host named
  `ylang-U22` serves `*.ylang-u22.portmap`, so multiple portmap hosts
  coexist in one client resolver. Explicit `[gateway] dns_domain` config
  and PORTMAP_DNS_DOMAIN still override; unusable hostnames fall back to
  the bare `portmap` zone.
