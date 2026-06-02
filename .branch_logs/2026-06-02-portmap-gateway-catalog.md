portmap gateway and catalog work:

- add shared Traefik/CoreDNS gateway compose
- add HTTP catalog UI on port 80
- generate minimal project overrides from endpoint declarations
- default HTTP upstream Host rewrite with preserve_host opt-out
- query running endpoints from the shared catalog instead of project-local registry files
