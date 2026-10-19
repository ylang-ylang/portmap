# feat/http-project-skill

- Add the version-bound `/skills/project-integration/SKILL.md` HTTP document on private and public catalog listeners, with UTF-8 Markdown, HEAD support, no-cache and nosniff. It needs no agent/access snapshot and includes no request-specific/private state.
- Keep the sole hand-maintained guide at `skills/project-integration/SKILL.md` (not the repository root); link it prominently from README. Frontend builds generate the catalog copy; wheels force-include the canonical source and the standalone build stages it without modifying read-only source mounts.
- Add Settings guide link and copyable agent prompt using the current portal origin. Correct the invalid `portmap compose` example and explicitly document that user-supplied Compose `-f` bypasses broker injection and requires the generated override/project identity.
- Flatten machine cards to one identity/transport/status/version/actions summary. Keep technical data in Details, omit duplicate installed version and repetitive SSH authorization prose, retain real errors/direct authorization/update jobs and independently actionable duplicate connection profiles.
- Prepare version0.19.0 and move future releases/hotfixes to push-triggered immutable build/publication plus checked Brew formula PR distribution. No installed host files were hot-patched for this work and no user machine upgrades were performed.
- Add release/hotfix constraints to `.git-guard/contribution.md`; hotfix branches remain `feat/hotfix-*` under the existing protected graph.
- Candidate evidence: real private/public HTTP GET+HEAD, exact canonical rendering, built-wheel extraction/HTTP, and actual glibc2.31 frozen0.19 binary all passed. Browser copy-to-clipboard, flat cards, Details toggles,390px no-overflow and duplicate SSH/auto profile controls exercised.
- Bootstrap tap CI only: https://github.com/ylang-ylang/homebrew-tap/pull/1 merged after actual `tap-checks` success (run39229171637), without changing the formula version.
- Distribution readiness is gated on a dedicated `HOMEBREW_TAP_TOKEN` Actions secret; it was absent during implementation. Do not report V0.19 published/distributed until the real workflow completes.
- The user subsequently supplied and authorized persistent storage of the
  dedicated tap credential. `HOMEBREW_TAP_TOKEN` is now saved in GitHub Actions
  Secrets on the source repository; its presence and target-tap API access were
  verified without printing the credential or placing it in code/arguments.
- Final corrected release-tool gate:139 passed; Actionlint had no findings;
  the actual frozen0.19 binary passed the workflow's `smoke-build` CLI gate.
  The production push-triggered workflow performs the full suite again before
  publication. Initial failing fixtures and their corrected results are retained
  in the external verification receipts rather than relabeled as successes.
