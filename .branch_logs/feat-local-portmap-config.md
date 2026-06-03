# feat/local-portmap-config

## Summary

- Added root-level `portmap.toml` settings for the shared gateway and port allocation state.
- Added runtime settings loading so gateway environment values come from the portmap repo root.
- Added static catalog frontend assets with client-side registry rendering and DNS probe feedback.
- Added compose takeover and broker/shim support for generated portmap overrides.
- Added automatic DNS bind resolution from wildcard config to the detected LAN host IP.

## Verification

- `uv run --with pytest pytest`
