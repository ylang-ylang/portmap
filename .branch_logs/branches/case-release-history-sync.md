# case/release/history-sync

- Preserve main's V0.18 release ancestry and release-log history through the configured main -> case -> feat -> dev graph before automated V0.19 publication.
- The initial dev branch lacked 72 lines of main's release log; the automated release-log append then failed to merge back into main in run 39237869253.
- This branch changes no runtime code or published tags/assets.
