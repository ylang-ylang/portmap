# feat/real-node-network-facts

<!-- git-guard: ref=refs/heads/feat/real-node-network-facts -->

- Add count-only original/final docker0 observations to successful and failed full-node cleanup receipts. Seven comparison booleans alone cannot prove that a clean CI runner initially lacked the IPv6 address implicated by the earlier cold-build failure.
- Derive presence and IPv6 address count from the existing original and final snapshots; issue no additional host commands, export no addresses/MACs, preserve private-state deletion and all seven acceptance checks. Existing IPv6 configuration remains valid rather than being forced or assumed absent.
- Use a separate feature worktree so the ongoing clean-ed1c8f8 browser acceptance remains source-frozen. Construction/runtime cache keys are intentionally unaffected; fresh CI will exercise the added public proof on real host state.
- Fifteen focused cleanup regressions passed with real locked test dependencies, covering absent/IPv4-only/preconfigured IPv6 bridges, added-address and removed-bridge rejection, persisted privacy-safe facts and unchanged private cleanup. Browser-eight/runtime-ten construction hashes and cache keys remain unchanged. Actual cold/warm CI proof remains the next verification step.
