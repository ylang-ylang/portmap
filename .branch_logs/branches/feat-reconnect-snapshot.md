# feat/reconnect-snapshot

## 2026-10-19

- Reproduce mj reconnect failure: rebuilding compiled rollback machine rows through a newer relay-port allocation raises KeyError for an unrelated saved SSH machine.
- Refresh rollback probe metadata without rebinding historical compiled backend URLs. Preserve existing connection records and routes rather than deleting the blocked profile.
- Add a rollback regression with a retired relay allocation and failed candidate readiness, asserting the historical route and relay configuration survive.
- Independent affected-module regression: 18 passed. Checksum-guarded atomic hotfix deployed to mj; agent restarted and both saved SSH connections restored through normal connect commands.
- Live Chromium verification: mj, dexgem, mujoco and ylang-u22 all show 0.18.0 / UP TO DATE; subtle footer shows Portmap v0.18.0. SSH machines no longer show a manual authorization warning. All three existing project HTTP routes returned200.
- dexgem again lacked its installed CLI before this recovery; restored official Brew package and standard setup using cached public downloads. This is not a claim that the environment's installation-persistence problem has been resolved.
