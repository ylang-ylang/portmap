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
