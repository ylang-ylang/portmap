# feat/release-draft-lookup

- Run 39280508837 passed full source tests, frozen build and actual HTTP smoke for immutable tag v0.20.0, but publication misclassified the complete draft as absent through GitHub's published-tag lookup.
- Discover drafts through the paginated release list after a genuine published-tag 404, without treating authentication/network errors as absence.
- Separate trusted draft-aware planning (Contents write needed for draft visibility) from all source tests/build/smoke (Contents read only).
- Recover existing draft id417475653 and its original uploaded assets; never rebuild or replace this version, move its tag, or upgrade user machines.
- Independent gate: 295 release-controller/Brew tests passed and both workflows
  passed Actionlint. Real GET/download-only planning/inspection found the existing
  draft, mode=verify, missing=[], original run/attempt preserved, no artifact recovery.
- Remove the unnecessary download overwrite flag for a fresh temporary directory.
- The installed git-guard auto-sync regenerated its managed Python >=3.11
  interpreter selector during commit; include that required managed-file update
  without disabling or bypassing any hook.
