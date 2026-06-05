# feat/container-http-url-dns

<!-- git-guard: ref=refs/heads/feat/container-http-url-dns -->

- Add runtime-derived DNS injection to generated compose overrides so
  portmap-managed containers can resolve HTTP-like debug URLs.
- Keep the DNS server source tied to portmap root settings and automatic host
  detection instead of fixed machine IPs.
- Verify generated override behavior with unit tests and a real Docker
  container-to-portmap-URL smoke test.
