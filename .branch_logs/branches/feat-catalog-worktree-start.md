# feat/catalog-worktree-start

<!-- git-guard: ref=refs/heads/feat/catalog-worktree-start -->

- Add catalog worktree discovery from running service labels, Git worktree lists, configured roots, and compact startable history.
- Add a compose-up action for startable worktrees.
- Add repo/wt-root/branch catalog UI hierarchy, grouped by repo first-commit identity and linked `.git` root.
- Surface running/dead branch controls as compact wt-root dropdowns while keeping branch-level restart/down controls.
- Treat history as a discovery source for startable dead branches instead of an independent UI level.
- Keep built catalog CSS human-readable by disabling CSS minification.
