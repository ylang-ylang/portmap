# feat/hotfix-release-environment

- Release run 39236892394 stopped before tests or publication: live Bullseye security package URLs returned HTTP 404.
- Pin builder APT sources to the signed 2025-07-01 Debian snapshot; preserve the pinned Python image and glibc baseline.
- Run the full source suite as the Ubuntu 24.04 runner in a dedicated Python 3.12 virtual environment, using its installed Git.
- Actual Docker builder image build passed, including dependency downloads and PyInstaller installation; Actionlint passed. The next push runs the complete publication gates again.
