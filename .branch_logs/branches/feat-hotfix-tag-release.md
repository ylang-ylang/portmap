# feat/hotfix-tag-release

- Synchronize the existing upstream dev V0.19 release preparation history without rewriting published refs.
- Replace dev-triggered version allocation/merge/tag automation with validation and distribution of an existing main-history version tag.
- This repair does not create a formal release tag, publish a new version, or upgrade user machines.
- Cut over orchestration to tag validation and release-state-driven build/retry;
  published assets are reused, and interrupted drafts recover authenticated bytes
  from their original retained build attempt. No CI branch/tag writes remain.
- Generate source-bound manifests after archive hashing; source/Brew installations
  bootstrap the exact version's official manifest instead of a stale source file.
  Preserve V0.19 through its historical tag-committed manifest anchor.
- Keep Brew formula-only updates and exact-head checks, and independently verify
  tagged source versions/main history before distribution.
- Update contributor/release documentation and retain existing UI/Skill behavior.
  The provenance-capable Docker image built successfully; independent final
  regression and actual frozen-binary validation follow before integration.
