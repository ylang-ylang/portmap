# Dev Feat Case Flow

```mermaid
gitGraph TB:
    commit id:"init"

    checkout main
    branch dev order: 1
    checkout main
    commit id:"main after dev fork"

    checkout dev
    commit id:"dev branch history"
    branch "feat/*" order: 2
    checkout main
    commit id:"main after feat fork"

    checkout main
    branch "case/*/*" order: 3
    checkout main
    commit id:"main after case fork"

    checkout "case/*/*"
    commit id:"case work"

    checkout "feat/*"
    merge "case/*/*" id:"case/*/* to feat/*"
    commit id:"feature work"

    checkout dev
    commit id:"dev branch sync point"
    checkout "feat/*"
    merge dev id:"dev to feat/* sync"

    checkout dev
    merge "feat/*" id:"feat/* to dev"

    checkout main
    merge dev id:"dev to main" tag:"V#.#"
```

## Rules

- `case/*/*` means `case/<context>/<topic>`, where `<context>` is a real project, customer, dataset, robot, deployment, or reproducible scenario.
- `case/*/*` branches from `main` and may merge only into `feat/*`.
- `case/*/*` must not merge directly into `dev` or `main`; reusable work must be distilled through `feat/*`.
- `feat/*` branches from `dev`.
- `feat/*` work must absorb the current `dev` and merge back to `dev`.
- `dev` is the integration branch and must not receive direct commits after the policy is installed.
- `main` may only receive tagged merges from `dev`.
- `main` release merge results must use a `V#.#` tag, where `#` means one or more decimal digits.
- `main` must not receive direct commits.
- Ad hoc tags are not allowed; release tags are allowed only when they satisfy the `dev` to `main` rule.

## Release and hotfix policy

- Every shipped change, including a small hotfix, must use the immutable release
  and Homebrew distribution workflow. Use `feat/hotfix-*` for small fixes under
  the same protected branch graph; “hotfix” does not mean live patching.
- Never overwrite Python modules, JavaScript, CSS, or other files inside users'
  installed Portmap packages as a deployment method.
- A release is complete only after its artifacts are published and the tap formula
  PR passes checks and merges. Failure must remain visible and retries must not
  rewrite published tags or assets.
- Publishing only builds/distributes packages. User machines upgrade explicitly
  through Homebrew or the authenticated web updater; CI must not upgrade them.
