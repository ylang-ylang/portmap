# feat/compose-readme-rules

<!-- git-guard: ref=refs/heads/feat/compose-readme-rules -->

- Expand the generated `.portmap/README.md` with Docker Compose rules for
  portmap-managed services.
- Document binding to `0.0.0.0`, using `expose` over fixed host ports, avoiding
  fixed container names, and keeping gateway services out of project compose.
- Cover range endpoint environment variables in the generated onboarding docs.
- Move scaffold README, endpoint examples, and gitignore content into package
  template files instead of embedding large generated text in Python code.
