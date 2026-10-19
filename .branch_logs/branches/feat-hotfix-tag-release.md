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
- Independent runtime verification confirmed real V0.19 tag/manifest/assets and
  no-artifact retry inspection. It exposed pinned Git's rejection of root reading
  runner-owned source: run the release container as the runner UID/GID, matching
  the documented/local deployment build, and remove ineffective safe.directory
  command-line configuration. Actual frozen build verification resumes with this fix.
- Final independent gate: 1179 passed, 2 integration tests deselected; both release
  workflows passed checksum-verified Actionlint 1.7.12. Restore the accidentally
  omitted fake tap routes and verification step id; retain the initial failing
  gate as historical evidence rather than relabeling it.
- Actual frozen candidate build and provenance/checksum gate passed as host UID;
  CLI version and private/public Skill HTTP 200 matched canonical rendered bytes.
  Read-only V0.19 validation/inspection/Brew verification confirmed the original
  commit and published archive digest, with no retained artifact needed for retry.
  No official version tag, Release, tap write or machine upgrade was performed.
