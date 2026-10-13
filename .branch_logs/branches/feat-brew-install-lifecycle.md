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
- Add Catalog and Settings page tabs. Keep ConnectionManager and Download portmap
  together in Settings; catalog loading/errors/service empty states and machine
  actions remain exclusively in Catalog. Keep both panels mounted so navigation
  preserves machine selection and unsaved connection forms; follow existing
  keyboard tab navigation and public/private connection-control permissions.
- Document automatic Brew setup against the user's existing Docker/Podman API,
  preferring Docker when available, without installing or starting another engine.
- Isolate runtime-selection tests from the controller's rootless Podman socket
  with a temporary XDG runtime directory; exercise Docker preference with both
  real test sockets listening.
- Build the 0.13.0 Linux amd64 frozen archive including the new Settings UI;
  the actual CLI reports `portmap 0.13.0` as an ordinary UID in a read-only,
  network-disabled container. Archive SHA-256:
  `82e7dc22afa8e5bea24fcc20f5e7a1e13a2284582223476d48c655b29f73553a`.
