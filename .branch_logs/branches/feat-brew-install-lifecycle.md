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
  `518ad70f07174c23de16114a81a200efe943019fd101a9293656dae84303563c`.
- Real notebook `brew install ylang-ylang/tap/portmap` upgrades 0.12.0_2 to
  0.13.0 and completes setup successfully in the same operation: setup 12.739s,
  package build 27s, followed by normal cleanup of the old keg.
