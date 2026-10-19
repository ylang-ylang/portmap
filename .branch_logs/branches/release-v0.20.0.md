# release/v0.20.0

- Branch from integrated dev 6645291 under the requested release/hotfix policy.
- Release package version 0.20.0 with tag v0.20.0; no runtime code changes during release hardening.
- Source validation: 1337 non-integration tests passed, two integration cases deselected; both workflows passed Actionlint; real frozen candidate version/provenance/private+public Skill HTTP passed.
- Merge this branch into dev before the tagged main merge. CI alone builds and distributes the immutable tagged artifact and checked Brew formula PR; user machines are not upgraded.
