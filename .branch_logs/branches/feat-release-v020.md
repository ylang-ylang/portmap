# feat/release-v020

- Confirm the old policy rejects three-part tags in an isolated actual-hook experiment; no production tag was created by that experiment.
- At the user's request, adopt the bundled dev-feat-release-hotfix-case graph, retaining tag-only CI, immutable artifacts, checked formula-only Brew distribution and no host upgrades.
- Prepare package version 0.20.0 for the new normal tag v0.20.0; dedicated hotfix/* branches will publish exact patch tags such as v0.20.1.
- Migrate release, source-manifest and updater tag handling while preserving existing V0.19 read-only retry validation.
- Document that old installed web updaters require an explicit first migration through Brew rather than hotpatching.
- Independent exact regression gate: 1337 passed, 2 deselected, exit 0, with
  JUnit counts preserved; both workflows passed checksum-verified Actionlint.
- Actual frozen v0.20.0 candidate build, source/checksum verification, CLI version
  and private/public Skill HTTP passed. Candidate artifacts were not published.
