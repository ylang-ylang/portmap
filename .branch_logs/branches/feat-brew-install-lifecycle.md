# feat/brew-install-lifecycle

<!-- git-guard: ref=refs/heads/feat/brew-install-lifecycle -->

- Make source-backed catalog Compose configuration include the source directory's
  device/inode generation. Replacing a Homebrew keg at the same pathname must
  recreate the catalog before cleanup retires its old bind mount; immutable
  frozen bundles retain their existing configuration.
- Treat both logind session scopes and user-manager services as disposable worker
  parents. Adopt only the invoking ordinary-user CLI PID into the existing
  transient system-manager scope before spawning persistent workers, without
  changing UID/groups, linger, login policy or boot enablement.
- Perform scope preflight before stopping an incompatible live host agent, so
  refused elevation does not retire a working installation.
- Probe Web access through the selected machine DTO's canonical catalog URL,
  rather than inventing a publishing-only hostname for a private access client.
- Append Settings to the existing machine tab row, without an extra Catalog tab.
  Keep ConnectionManager and Download portmap together in Settings, including
  after visiting a remote machine; catalog loading/errors/service empty states
  and machine actions remain in machine views. Keep both panels mounted to
  preserve selection and unsaved forms, retaining keyboard navigation and
  public/private connection-control permissions.
- Document automatic Brew setup against the user's existing Docker/Podman API,
  preferring Docker when available, without installing or starting another engine.
- Isolate runtime-selection tests from the controller's rootless Podman socket
  with a temporary XDG runtime directory; exercise Docker preference with both
  real test sockets listening.
- Verify 595 non-integration tests, with one integration case deselected.
  The initial two failures exposed ambient rootless-socket leakage in old tests;
  fixing their environment isolation made both pass without production changes.
- Build the 0.13.0 Linux amd64 archive with the final single-row Settings UI.
  Archive SHA-256:
  `fd7ebee77c36594cae326045be294c81bdb13f3a2b86a7bd0185fefda8e36d22`.
- Real notebook `brew install ylang-ylang/tap/portmap` upgrades 0.12.0_2 to
  0.13.0 and completes setup successfully in the same operation: setup 12.739s,
  package build 27s, followed by normal cleanup of the old keg.
- Same-version 0.13.0 Brew reinstall also succeeds (setup 12.385s, package
  build 27s). After the command exits, root/JS/CSS/APIs return 200 and the
  catalog's mounted source device/inode matches the current keg generation;
  DNS/gateway/relay and unrelated business container identities remain unchanged.
- The host agent remains UID 1000 in a system-manager scope after the installer
  unit is collected. Saved direct profiles, NSS/resolv.conf, volumes, disabled
  Docker/ingress boot state and Linger=no are preserved. An inaccessible-API
  child exits nonzero without modifying those state files or the working portal.
- Remove the technical Generated-at/HTTP/DNS/JSON row and keep Settings
  independently appended and right-aligned after all machine tabs. Deliver
  these small UI changes by updating only frontend assets, without another
  notebook Brew reinstall or container restart.
- The final rebuilt frozen archive is 8,020,291 bytes and the actual executable
  reports `portmap 0.13.0` as UID 1000 in a read-only, network-disabled container.
- The notebook subsequently becomes unreachable (`No route to host`), closing
  both owned SSH/CDP tunnels. Final native browser and advertised-download
  checks are blocked by that external network prerequisite, not counted as
  passes; the earlier wrong-path 404 was a verifier URL error.
- A read-only Chromium preview using recorded notebook API responses verifies
  the actual compiled UI: Settings stays last/right-aligned, panels and empty
  states remain correctly separated, the diagnostic row is gone, draft state
  survives navigation, Home/End select the expected tabs, and Downloads remain
  available after visiting the remote machine. This is not native notebook proof.
- Add a small `本机` badge only for `machine.via === "local"`; the final preview
  confirms the local label and unchanged remote label. This last badge delta
  and the matching final release manifest still need notebook synchronization
  after its network connection returns.
