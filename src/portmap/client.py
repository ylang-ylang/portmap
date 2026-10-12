"""Client access to remote portmap gateways and the local server role.

Direct connections publish a DNS record for the locally verified reachable
remote IP and keep using the remote HTTP port: no local HTTP gateway is
involved. SSH connections share one browser-facing local HTTP gateway whose
public port is separated from its unprivileged private listener. The local
server role is persisted separately from user remote connections and proxies
its whole domain to the local backend.
"""
from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import socket
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .client_discovery import GatewayInfo, discover_ssh_hosts, discover_targets, probe_gateway, resolve_ssh_host
from .client_dns import dns_status, resolver_status, setup_resolver, stop_dns, sync_dns, teardown_resolver, verify_resolution
from .client_gateway import (
    gateway_status,
    preferred_http_port,
    save_public_http_port,
    stop_gateway,
    sync_gateway,
)
from .client_runtime import legacy_client_state_dir
from .client_tunnel import add_forward, cancel_forward, start_tunnel, stop_tunnel, tunnel_alive
from .errors import PortmapError


class SSHSelectionRequired(PortmapError):
    """No authoritative local SSH target was supplied or discovered."""


@contextmanager
def client_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = state_dir.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PortmapError(f"client state must be an owned private directory with mode 0700: {state_dir}")
    with (state_dir / "client.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load(state_dir: Path) -> dict[str, dict[str, Any]]:
    path = state_dir / "connections.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise PortmapError(f"invalid connection state: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("connections"), dict):
        raise PortmapError(f"invalid connection state: {path}")
    profiles = payload["connections"]
    if any(not isinstance(profile, dict) or domain != profile.get("domain") for domain, profile in profiles.items()):
        raise PortmapError(f"invalid connection profiles: {path}")
    return profiles


def _save(state_dir: Path, profiles: dict[str, dict[str, Any]]) -> None:
    fd, name = tempfile.mkstemp(prefix=".connections-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "connections": profiles}, handle, indent=2, sort_keys=True)
            handle.write("\n")
        Path(name).replace(state_dir / "connections.json")
    finally:
        Path(name).unlink(missing_ok=True)


def _load_local(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / "local-server.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PortmapError(f"invalid local server state: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1 or "local" not in payload:
        raise PortmapError(f"invalid local server state: {path}")
    profile = payload["local"]
    if profile is not None and (
        not isinstance(profile, dict)
        or profile.get("via") != "local"
        or not isinstance(profile.get("domain"), str)
        or not profile["domain"]
    ):
        raise PortmapError(f"invalid local server profile: {path}")
    return profile


def _save_local(state_dir: Path, profile: dict[str, Any] | None) -> None:
    fd, name = tempfile.mkstemp(prefix=".local-server-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "local": profile}, handle, indent=2, sort_keys=True)
            handle.write("\n")
        Path(name).replace(state_dir / "local-server.json")
    finally:
        Path(name).unlink(missing_ok=True)


def _records(profiles: dict[str, dict[str, Any]]) -> dict[str, str]:
    """DNS records for the published domains.

    Direct domains resolve to their verified remote IP; the local server and
    routed SSH connections resolve to loopback behind the shared gateway. A
    dormant SSH profile has no owned gateway route, so it gets no record at
    all: a loopback record without our listener could send the domain to an
    unrelated local service, while the honest answer is that the name is not
    currently served.
    """
    records = {}
    for domain, profile in profiles.items():
        if profile.get("via") == "direct":
            address = profile.get("address")
            if not isinstance(address, str) or not address:
                raise PortmapError(f"direct connection {domain} has no verified remote address")
            records[domain] = address
        elif profile.get("via") == "ssh":
            if profile.get("tunnel") is not None:
                records[domain] = "127.0.0.1"
        else:
            records[domain] = "127.0.0.1"
    return records


def _published_profiles(state_dir: Path, profiles: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every domain that needs a DNS record: remote connections plus the local role."""
    local = _load_local(state_dir)
    return {**profiles, **({local["domain"]: local} if local else {})}


def _gateway_profiles(profiles: dict[str, dict[str, Any]], local: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Only live SSH tunnels and the local role are routed through the gateway.

    A migrated SSH profile whose tunnel was stopped stays saved but dormant
    until the user reconnects it; direct connections never appear here.
    """
    managed = {
        domain: profile
        for domain, profile in profiles.items()
        if profile.get("via") == "ssh" and profile.get("tunnel") is not None
    }
    if local is not None:
        managed[local["domain"]] = local
    return managed


def _port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise PortmapError(f"invalid port: {port!r}")
    return port


def _url(address: str, port: int) -> str:
    host = f"[{address}]" if ":" in address else address
    return f"http://{host}" + (f":{_port(port)}" if port != 80 else "")


def _direct_target_key(target: str) -> str:
    """Collapse equivalent direct HTTP target spellings (IP, IP:80, http://IP).

    Hostname spellings and non-default ports stay distinct, so connecting a
    different hostname for the same gateway still reaches the domain collision
    guard instead of silently reusing the saved mapping.
    """
    value = target
    if value.lower().startswith("http://"):
        value = value[7:]
    if value.startswith("[") and "]" in value:
        host, _, suffix = value.partition("]")
        return host + "]" if suffix in ("", ":80") else value
    host, separator, port = value.rpartition(":")
    if separator and port == "80" and host and ":" not in host:
        return host
    return value


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _tcp_loopback(domain: str, profiles: dict[str, dict[str, Any]]) -> str:
    if sys.platform == "darwin":
        return "127.0.0.1"
    used = {profile.get("tcp_address") for profile in profiles.values()}
    seed = int.from_bytes(hashlib.sha256(domain.encode()).digest()[:2], "big")
    for offset in range(65024):
        value = (seed + offset) % 65024
        candidate = f"127.77.{value // 254}.{value % 254 + 1}"
        if candidate not in used:
            return candidate
    raise PortmapError("no free loopback address for raw TCP forwarding")


def _require_domain(domain: str) -> None:
    if not domain.endswith(".portmap"):
        raise PortmapError(
            f"remote DNS domain is {domain!r}; client DNS manages only <hostname>.portmap. "
            "Configure that domain on the remote host and regenerate its project overrides first."
        )


def _gateway(info: GatewayInfo, timeout: float) -> GatewayInfo:
    if info.scheme != "http":
        raise PortmapError("client aggregation targets the plain HTTP portmap gateway; use its IP HTTP listener or SSH")
    base = urlsplit(info.catalog_url)
    if (base.port or 80) == info.http_port:
        return info
    # A successful request to the separate catalog port is not proof of the
    # actual data-plane listener. Follow its port, never its advertised IP.
    return probe_gateway(_url(info.address, info.http_port), timeout=timeout, expected_domain=info.domain)


def _open_ssh_upstream(
    state_dir: Path,
    chosen_ssh: str,
    *,
    ssh_config: Path | None,
    remote_port: int | None,
    timeout: float,
    expected_domain: str | None = None,
    use_sudo: bool = True,
) -> tuple[dict[str, Any], GatewayInfo, str]:
    """Start an SSH master and forward the remote HTTP gateway to loopback.

    Returns the tunnel descriptor, the verified gateway info, and the local
    backend URL. A tunnel that cannot prove a catalog is stopped again before
    the failure surfaces, so the caller only ever owns a live transport.
    """
    key = hashlib.sha256(chosen_ssh.encode()).hexdigest()[:20]
    tunnel = start_tunnel(chosen_ssh, state_dir=state_dir, key=key, config_file=ssh_config, timeout=max(timeout, 30), use_sudo=use_sudo)
    bootstrap_port = _free_port()
    try:
        candidate = _port(remote_port) if remote_port is not None else 80
        added = False
        try:
            add_forward(tunnel, local_address="127.0.0.1", local_port=bootstrap_port, remote_port=candidate)
            added = True
            info = probe_gateway(_url("127.0.0.1", bootstrap_port), timeout=timeout)
        except PortmapError as exc:
            raise PortmapError(
                "SSH connected but no portmap catalog was reachable; use --remote-port for a custom gateway: "
                + str(exc)
            ) from exc
        finally:
            if added:
                cancel_forward(tunnel, local_address="127.0.0.1", local_port=bootstrap_port, remote_port=candidate)
        domain = info.domain
        _require_domain(domain)
        if expected_domain is not None and domain != expected_domain:
            raise PortmapError(
                f"the SSH target now serves {domain!r}, not the saved {expected_domain!r}; "
                "refusing to replace the saved mapping"
            )
        upstream_port = _free_port()
        add_forward(tunnel, local_address="127.0.0.1", local_port=upstream_port, remote_port=info.http_port)
        backend_url = _url("127.0.0.1", upstream_port)
        info = probe_gateway(backend_url, timeout=timeout, expected_domain=domain)
    except BaseException:
        stop_tunnel(tunnel)
        raise
    return tunnel, info, backend_url


def _forward_tcp_ports(tunnel: dict[str, Any], info: GatewayInfo, tcp_address: str) -> list[int]:
    """Forward every catalogued raw TCP port onto the connection's address."""
    ports: list[int] = []
    for service in info.catalog["services"]:
        for endpoint in service["endpoints"]:
            if endpoint.get("kind") != "tcp":
                continue
            host_port = _port(endpoint.get("host_port"))
            if host_port in ports:
                continue
            add_forward(tunnel, local_address=tcp_address, local_port=host_port, remote_port=host_port)
            ports.append(host_port)
    return ports


def _public_gateway_port(state_dir: Path) -> int:
    """Choose URLs before publication; a first SSH backend does not exist yet."""
    status = gateway_status(state_dir)
    if status.get("version") == 2 and status.get("running"):
        return _port(status["http_port"])
    return (
        preferred_http_port(state_dir)
        or (status.get("http_port") if status.get("version") == 2 else None)
        or 80
    )


def _endpoints(catalog: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
    from .client_view import project_catalog

    # Direct connections keep the remote port in their URLs; SSH connections
    # present the shared public local port.
    port = profile["remote_http_port"] if profile.get("via") == "direct" else profile["http_port"]
    view = project_catalog(catalog, profile, http_port=port)
    return [
        {**endpoint, "repo_name": service.get("repo_name"), "branch": service.get("branch")}
        for service in view["services"]
        for endpoint in service["endpoints"]
    ]


def _public(profile: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in profile.items() if key not in {"tunnel", "backend_url"}}


def _origin_parts(url: object) -> tuple[str, int]:
    if not isinstance(url, str):
        raise PortmapError("legacy direct profile has no backend URL")
    parsed = urlsplit(url)
    try:
        address = str(ipaddress.ip_address(parsed.hostname or ""))
    except ValueError as exc:
        raise PortmapError("legacy direct profile has no numeric backend address") from exc
    return address, parsed.port if parsed.port is not None else 80


def _migrated_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Convert a saved standalone portmap-client profile to the unified model.

    Direct profiles republish against their verified remote address and port.
    SSH profiles keep their exact saved target and SSH config identity for
    restoration; their stopped tunnel and dead loopback upstream are dropped,
    leaving the profile dormant until setup or connect restores it.
    """
    domain = profile.get("domain")
    if not isinstance(domain, str) or not domain:
        raise PortmapError("legacy connection profile has no domain")
    if profile.get("via") == "direct":
        address, remote_port = _origin_parts(profile.get("backend_url"))
        return {
            "domain": domain,
            "target": profile.get("target"),
            "ssh_target": None,
            "via": "direct",
            "address": address,
            "remote_http_port": profile.get("remote_http_port") or remote_port,
            "scheme": "http",
            "backend_url": _url(address, remote_port),
            "catalog_url": _url(domain, profile.get("remote_http_port") or remote_port) + "/",
            "tcp_address": address,
            "tcp_ports": [],
            "include_tcp": bool(profile.get("include_tcp")),
        }
    tunnel = profile.get("tunnel")
    saved_config = tunnel.get("config_file") if isinstance(tunnel, dict) else None
    migrated = {
        "domain": domain,
        "target": profile.get("target"),
        "ssh_target": profile.get("ssh_target"),
        "via": "ssh",
        "address": "127.0.0.1",
        "scheme": "http",
        "tcp_address": profile.get("tcp_address"),
        "tcp_ports": [],
        "include_tcp": bool(profile.get("include_tcp")),
    }
    if isinstance(saved_config, str) and saved_config:
        # Preserve the exact configuration identity the saved connection used,
        # never an invented alias, so restoration reconnects as it did before.
        migrated["ssh_config"] = saved_config
    return migrated


def _import_legacy_state(state_dir: Path, *, use_sudo: bool = True) -> None:
    """Safely import the standalone portmap-client state directory once.

    Import only before the first saved connections file. An empty saved mapping
    records an intentional disconnect or teardown and must not resurrect old
    targets. Explicit shared or role-specific state overrides never import.
    Every legacy worker, tunnel, DNS server, and resolver route is stopped
    through its ownership-verified path; nothing unrelated is touched and the
    legacy directory itself is retained.
    """
    if os.environ.get("PORTMAP_CLIENT_STATE_DIR") or os.environ.get("PORTMAP_STATE_DIR"):
        return
    legacy = legacy_client_state_dir()
    if legacy == state_dir.expanduser().resolve() or not legacy.exists():
        return
    if (state_dir / "connections.json").exists() or _load_local(state_dir) is not None:
        return
    with client_lock(legacy):
        profiles = _load(legacy)
        for label, stop in (
            ("gateway", lambda: stop_gateway(legacy, use_sudo=use_sudo)),
            ("dns", lambda: stop_dns(legacy)),
        ):
            try:
                stop()
            except Exception as exc:
                raise PortmapError(
                    f"legacy portmap-client {label} under {legacy} could not be stopped safely; "
                    f"resolve it before running setup: {exc}"
                ) from exc
        for domain, profile in profiles.items():
            if profile.get("tunnel"):
                try:
                    stop_tunnel(profile["tunnel"])
                except Exception as exc:
                    raise PortmapError(
                        f"legacy SSH tunnel for {domain} under {legacy} could not be stopped; "
                        f"resolve it before running setup: {exc}"
                    ) from exc
        try:
            teardown_resolver(legacy, use_sudo=use_sudo)
        except Exception as exc:
            raise PortmapError(f"legacy resolver route under {legacy} could not be removed safely: {exc}") from exc
        _save(state_dir, {domain: _migrated_profile(profile) for domain, profile in profiles.items()})


def _apply_public_http_port(state_dir: Path, profiles: dict[str, dict[str, Any]], http_port: int) -> None:
    """Persist the preferred public port once publication has succeeded.

    Stored endpoint lists and per-profile ports embed the public port; when it
    changes they are dropped so the saved view stays honest until each
    connection is reconnected and refreshed.
    """
    changed = preferred_http_port(state_dir) != http_port
    save_public_http_port(state_dir, http_port)
    if not changed:
        return
    for profile in profiles.values():
        if profile.get("via") != "direct":
            profile.pop("endpoints", None)
            profile["http_port"] = http_port
    if profiles:
        _save(state_dir, profiles)


def setup_client(state_dir: Path, *, use_sudo: bool = True, http_port: int | None = None) -> dict[str, Any]:
    if http_port is not None:
        _port(http_port)
    with client_lock(state_dir):
        _import_legacy_state(state_dir, use_sudo=use_sudo)
        profiles = _load(state_dir)
        local = _load_local(state_dir)
        had_resolver = resolver_status(state_dir)["installed"]
        had_dns = dns_status(state_dir)["running"]
        had_gateway = gateway_status(state_dir)["running"]
        try:
            # Linux DNS binds the dedicated dummy address, which must exist
            # before CoreDNS starts (also works with systemd-resolved 249).
            resolver = setup_resolver(state_dir, use_sudo=use_sudo)
            dns = sync_dns(state_dir, _records(_published_profiles(state_dir, profiles)), use_sudo=use_sudo)
            # Direct-only setups get DNS with no HTTP gateway at all; the
            # preferred public port is still remembered for later SSH/local use.
            gateway = sync_gateway(state_dir, _gateway_profiles(profiles, local), http_port=http_port, use_sudo=use_sudo)
            if http_port is not None:
                _apply_public_http_port(state_dir, profiles, http_port)
            # Re-establish saved SSH transports whose authentication is still
            # available (exactly one attempt each); remotes that cannot be
            # restored keep their saved configuration and are reported.
            restored, restore_failures = _restore_saved_ssh(state_dir, profiles, use_sudo=use_sudo)
            result = {"dns": dns, "resolver": resolver, "gateway": gateway, "state_dir": str(state_dir)}
            if restored:
                result["restored_connections"] = restored
            if restore_failures:
                result["restore_failures"] = restore_failures
            return result
        except Exception as exc:
            cleanups = []
            if not had_gateway:
                cleanups.append(lambda: stop_gateway(state_dir, use_sudo=use_sudo))
            if not had_resolver:
                cleanups.append(lambda: teardown_resolver(state_dir, use_sudo=use_sudo))
            if not had_dns:
                cleanups.append(lambda: stop_dns(state_dir))
            cleanup_errors = []
            for cleanup in cleanups:
                try:
                    cleanup()
                except Exception as failure:
                    cleanup_errors.append(str(failure))
            if cleanup_errors:
                raise PortmapError(f"{exc}; cleanup failed: {'; '.join(cleanup_errors)}") from exc
            raise


def _publish(state_dir: Path, profiles: dict[str, dict[str, Any]], *, use_sudo: bool = True) -> None:
    # The local catalog view reads connections.json. Publish it before the
    # routes, then DNS last; mutations are serialized by client_lock and rolled
    # back. Direct records keep their verified remote addresses, so a failing
    # SSH publication never disturbs them.
    _save(state_dir, profiles)
    sync_gateway(state_dir, _gateway_profiles(profiles, _load_local(state_dir)), use_sudo=use_sudo)
    sync_dns(state_dir, _records(_published_profiles(state_dir, profiles)), use_sudo=use_sudo)


def _restore_ssh_profile(
    state_dir: Path,
    profiles: dict[str, dict[str, Any]],
    domain: str,
    *,
    use_sudo: bool = True,
    timeout: float = 5,
) -> dict[str, Any]:
    """Revive one dormant or dead saved SSH connection under its saved identity.

    The caller holds client_lock. The exact saved SSH target and config file
    are reused; a remote that now serves a different domain is refused rather
    than silently replacing the mapping. On failure the saved profile keeps
    its configuration, its (dead) transport record is dropped so the domain
    never resolves to loopback without an owned route, and the error states
    the reconnect requirement instead of claiming the connection is reachable.
    """
    profile = profiles.get(domain)
    if not isinstance(profile, dict) or profile.get("via") != "ssh":
        raise PortmapError(f"no saved SSH connection for {domain}")
    identity = profile.get("ssh_target") or profile.get("target")
    if not isinstance(identity, str) or not identity:
        raise PortmapError(f"{domain} has no saved SSH target to restore; disconnect it and connect again")
    saved_config = profile.get("ssh_config")
    ssh_config = Path(saved_config) if isinstance(saved_config, str) and saved_config else None
    stale = profile.get("tunnel")
    original = dict(profile)
    tunnel = None
    mutated = False
    try:
        if stale is not None:
            profile.pop("tunnel", None)
            mutated = True
            stop_tunnel(stale)
        public = _public_gateway_port(state_dir)
        tunnel, info, backend_url = _open_ssh_upstream(
            state_dir, identity, ssh_config=ssh_config, remote_port=profile.get("remote_http_port"),
            timeout=timeout, expected_domain=domain, use_sudo=use_sudo,
        )
        mutated = True
        tcp_address = profile.get("tcp_address") or _tcp_loopback(domain, profiles)
        tcp_ports = _forward_tcp_ports(tunnel, info, tcp_address) if profile.get("include_tcp") else []
        profile.update({
            "backend_url": backend_url,
            "remote_http_port": info.http_port,
            "http_port": public,
            "tcp_address": tcp_address,
            "tcp_ports": tcp_ports,
            "catalog_url": _url(domain, public) + "/",
        })
        profile["endpoints"] = _endpoints(info.catalog, profile)
        profile["tunnel"] = tunnel
        _publish(state_dir, profiles, use_sudo=use_sudo)
        verify_resolution(domain, "127.0.0.1")
        probe_gateway(_url(domain, public), timeout=timeout, expected_domain=domain)
        return _public(profile)
    except BaseException as exc:
        rollback_errors = []
        if mutated:
            profile.clear()
            profile.update(original)
            profile.pop("tunnel", None)
            try:
                _publish(state_dir, profiles, use_sudo=use_sudo)
            except Exception as cleanup:
                rollback_errors.append(str(cleanup))
        if tunnel is not None:
            try:
                stop_tunnel(tunnel)
            except Exception as cleanup:
                rollback_errors.append(str(cleanup))
        if isinstance(exc, PortmapError):
            message = (
                f"could not restore saved SSH connection {domain} through {identity}: {exc}. "
                f"The saved connection was kept; run 'portmap connect {identity}' when the host is reachable, "
                f"or 'portmap disconnect {domain}' to drop it"
            )
            if rollback_errors:
                message += "; cleanup failed: " + "; ".join(rollback_errors)
            raise PortmapError(message) from exc
        if rollback_errors:
            raise PortmapError(f"{exc}; cleanup failed: {'; '.join(rollback_errors)}") from exc
        raise


def _restore_saved_ssh(
    state_dir: Path,
    profiles: dict[str, dict[str, Any]],
    *,
    use_sudo: bool = True,
    timeout: float = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One normal restore attempt per dormant or dead saved SSH connection.

    Runs under client_lock during setup, with no extra retry policy: each
    remote gets exactly one attempt using its saved authentication/config.
    Failures keep the saved profile and are reported to the caller; they never
    fail the setup itself.
    """
    restored: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for domain, profile in list(profiles.items()):
        if profile.get("via") != "ssh":
            continue
        if profile.get("tunnel") is not None and tunnel_alive(profile["tunnel"]):
            continue
        try:
            restored.append(_restore_ssh_profile(state_dir, profiles, domain, use_sudo=use_sudo, timeout=timeout))
        except PortmapError as exc:
            failures.append({
                "domain": domain,
                "target": profile.get("ssh_target") or profile.get("target"),
                "error": str(exc),
            })
    return restored, failures


def connect(
    state_dir: Path,
    target: str,
    *,
    via: str = "auto",
    ssh_target: str | None = None,
    ssh_config: Path | None = None,
    remote_port: int | None = None,
    include_tcp: bool = False,
    timeout: float = 5,
    use_sudo: bool = True,
) -> dict[str, Any]:
    if via not in {"auto", "direct", "ssh"}:
        raise PortmapError("connection mode must be auto, direct or ssh")
    if not target and not ssh_target:
        raise PortmapError("specify an IP/URL or SSH target; use portmap discover to list local SSH aliases")
    if timeout <= 0 or timeout > 60:
        raise PortmapError("connection timeout must be between 0 and 60 seconds")
    if remote_port is not None:
        _port(remote_port)
    with client_lock(state_dir):
        if not resolver_status(state_dir)["installed"]:
            raise PortmapError("local split DNS is not configured; run portmap setup first")
        profiles = _load(state_dir)
        local = _load_local(state_dir)
        direct_key = _direct_target_key(target)

        for profile in profiles.values():
            stored = direct_key if profile.get("via") == "direct" else target
            if profile.get("target") != stored:
                continue
            if ssh_target is not None and profile.get("ssh_target") != ssh_target:
                continue
            if via != "auto" and profile.get("via") != via:
                continue
            if profile.get("via") == "ssh":
                if include_tcp and not profile.get("include_tcp"):
                    raise PortmapError(f"TCP forwarding options changed; disconnect {profile['domain']} before reconnecting")
                if not profile.get("tunnel") or not tunnel_alive(profile["tunnel"]):
                    # The verified saved identity matched: revive the saved
                    # transport instead of dead-ending behind a forced
                    # disconnect. A different target still hits the domain
                    # collision guard below.
                    return {
                        **_restore_ssh_profile(state_dir, profiles, profile["domain"], use_sudo=use_sudo, timeout=timeout),
                        "already_connected": True,
                        "restored": True,
                        "dns_ready": True,
                    }
            info = probe_gateway(profile["backend_url"], timeout=timeout, expected_domain=profile["domain"])
            profile["remote_http_port"] = info.http_port
            if profile.get("via") == "ssh":
                profile["http_port"] = _public_gateway_port(state_dir)
            profile["endpoints"] = _endpoints(info.catalog, profile)
            _publish(state_dir, profiles, use_sudo=use_sudo)
            verify_resolution(profile["domain"], profile["address"])
            port = info.http_port if profile.get("via") == "direct" else profile["http_port"]
            probe_gateway(_url(profile["domain"], port), timeout=timeout, expected_domain=profile["domain"])
            return {**_public(profile), "already_connected": True, "dns_ready": True}
        info = None
        direct_error = None
        tunnel = None
        publishing = False
        try:
            if via != "ssh":
                try:
                    info = _gateway(probe_gateway(target, timeout=timeout), timeout)
                except PortmapError as exc:
                    direct_error = str(exc)
                    if via == "direct":
                        raise
                    if target in discover_ssh_hosts(ssh_config):
                        try:
                            configured = resolve_ssh_host(target, config_file=ssh_config, timeout=timeout)
                            if not configured["proxy_command"] and not configured["proxy_jump"]:
                                info = _gateway(probe_gateway(configured["hostname"], timeout=timeout), timeout)
                        except PortmapError:
                            pass
            tcp_ports: list[int] = []
            chosen_ssh = None
            local_gateway_port = None
            if info is not None:
                selected = "direct"
                domain, tcp_address = info.domain, info.address
                backend_url = _url(info.address, info.http_port)
            else:
                selected = "ssh"
                chosen_ssh = ssh_target or (target if via == "ssh" or "@" in target or target in discover_ssh_hosts(ssh_config) else None)
                if not chosen_ssh:
                    raise SSHSelectionRequired(
                        f"direct connection failed: {direct_error}. No configured SSH target could be identified. "
                        "Use --via ssh with a full target (including .coder aliases), or --ssh-target user@host."
                    )
                # Publication starts the gateway only after a verified SSH
                # backend exists; choosing the port does not start a listener.
                local_gateway_port = _public_gateway_port(state_dir)
                tunnel, info, backend_url = _open_ssh_upstream(
                    state_dir, chosen_ssh, ssh_config=ssh_config,
                    remote_port=remote_port, timeout=timeout, use_sudo=use_sudo,
                )
                domain = info.domain
                tcp_address = _tcp_loopback(domain, profiles)
                if include_tcp:
                    tcp_ports = _forward_tcp_ports(tunnel, info, tcp_address)
            _require_domain(domain)
            if domain in profiles or (local is not None and local["domain"] == domain):
                raise PortmapError(f"{domain} is already connected via another target; disconnect it before replacing its DNS mapping")
            profile: dict[str, Any] = {
                "domain": domain,
                "target": direct_key if selected == "direct" else target,
                "ssh_target": chosen_ssh,
                "via": selected,
                "address": info.address if selected == "direct" else "127.0.0.1",
                "remote_http_port": info.http_port,
                "scheme": "http",
                "backend_url": backend_url,
                "catalog_url": _url(domain, info.http_port if selected == "direct" else local_gateway_port) + "/",
                "tcp_address": tcp_address,
                "tcp_ports": tcp_ports,
                "include_tcp": include_tcp,
            }
            if selected == "ssh":
                profile["http_port"] = local_gateway_port
            profile["endpoints"] = _endpoints(info.catalog, profile)
            if tunnel:
                profile["tunnel"] = tunnel
            publishing = True
            _publish(state_dir, {**profiles, domain: profile}, use_sudo=use_sudo)
            verify_resolution(domain, profile["address"])
            # Prove the published path. Direct connections resolve straight to
            # the verified remote IP on the remote port; SSH connections prove
            # the public local gateway with the HTTP Host preserved remotely.
            probe_gateway(_url(domain, info.http_port if selected == "direct" else local_gateway_port), timeout=timeout, expected_domain=domain)
            return {**_public(profile), "already_connected": False, "dns_ready": True}
        except Exception as exc:
            cleanup_errors = []
            if publishing:
                try:
                    _publish(state_dir, profiles, use_sudo=use_sudo)
                except Exception as cleanup:
                    cleanup_errors.append(str(cleanup))
            if tunnel:
                try:
                    stop_tunnel(tunnel)
                except Exception as cleanup:
                    cleanup_errors.append(str(cleanup))
            if cleanup_errors:
                raise PortmapError(f"{exc}; cleanup failed: {'; '.join(cleanup_errors)}") from exc
            raise


def _local_role(state_dir: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Validate a caller-supplied local role into its canonical profile."""
    if not isinstance(spec, dict):
        raise PortmapError("local server profile must be an object")
    domain = spec.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        raise PortmapError("local server profile requires a domain")
    domain = domain.strip().lower()
    _require_domain(domain)
    backend_port = spec.get("backend_port")
    if backend_port is not None:
        backend_url = _url("127.0.0.1", _port(backend_port))
    else:
        backend_url = spec.get("backend_url")
        if not isinstance(backend_url, str):
            raise PortmapError("local server profile requires backend_port or backend_url")
        parsed = urlsplit(backend_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise PortmapError("local backend must be a plain http://127.0.0.1:<port> origin")
        backend_url = _url("127.0.0.1", _port(parsed.port if parsed.port is not None else 80))
    public = spec.get("http_port")
    if public is None:
        public = preferred_http_port(state_dir)
    if public is None:
        public = 80
    _port(public)
    return {
        "domain": domain,
        "via": "local",
        "backend_url": backend_url,
        "remote_http_port": public,
        "http_port": public,
        "address": "127.0.0.1",
        "target": "local",
        "scheme": "http",
        "tcp_address": "127.0.0.1",
        "tcp_ports": [],
        "include_tcp": False,
    }


def set_local_server(state_dir: Path, profile: dict[str, Any] | None, *, use_sudo: bool = True) -> dict[str, Any]:
    """Publish or remove the local server role without touching remote connections."""
    with client_lock(state_dir):
        profiles = _load(state_dir)
        current = _load_local(state_dir)
        if profile is None:
            role = None
            if current is None:
                return {
                    "local": None,
                    "dns": dns_status(state_dir),
                    "resolver": resolver_status(state_dir),
                    "gateway": gateway_status(state_dir),
                }
        else:
            role = _local_role(state_dir, profile)
            if role["domain"] in profiles:
                raise PortmapError(f"{role['domain']} is used by a remote connection; disconnect it before publishing the local server")
        _save_local(state_dir, role)
        try:
            _publish(state_dir, profiles, use_sudo=use_sudo)
        except Exception:
            _save_local(state_dir, current)
            raise
        return {
            "local": _public(role) if role else None,
            "dns": dns_status(state_dir),
            "resolver": resolver_status(state_dir),
            "gateway": gateway_status(state_dir),
        }


def connections(state_dir: Path, *, check: bool = False) -> dict[str, Any]:
    with client_lock(state_dir):
        profiles = _load(state_dir)
        rows = []
        for profile in profiles.values():
            row = _public(profile)
            row["status"] = "configured"
            if check:
                port = profile.get("remote_http_port") if profile.get("via") == "direct" else profile.get("http_port")
                try:
                    if profile.get("tunnel") and not tunnel_alive(profile["tunnel"]):
                        raise PortmapError("SSH tunnel is down")
                    if port is None:
                        identity = profile.get("ssh_target") or profile.get("target")
                        raise PortmapError(f"saved SSH transport is dormant; restore it with 'portmap connect {identity}' or disconnect it")
                    probe_gateway(_url(profile["domain"], port), expected_domain=profile["domain"])
                    row["status"] = "reachable"
                except PortmapError as exc:
                    row["status"] = "unreachable"
                    row["error"] = str(exc)
            rows.append(row)
        local = _load_local(state_dir)
        return {
            "connections": rows,
            "local": _public(local) if local else None,
            "dns": dns_status(state_dir),
            "resolver": resolver_status(state_dir),
            "gateway": gateway_status(state_dir),
        }


def disconnect(state_dir: Path, domain: str, *, use_sudo: bool = True) -> dict[str, Any]:
    with client_lock(state_dir):
        profiles = _load(state_dir)
        key = domain.strip(".").lower()
        if key not in profiles:
            matches = [name for name in profiles if name.removesuffix(".portmap") == key]
            if len(matches) != 1:
                raise PortmapError(f"no connection for {domain!r}")
            key = matches[0]
        profile = profiles[key]
        proposed = {name: item for name, item in profiles.items() if name != key}
        try:
            _publish(state_dir, proposed, use_sudo=use_sudo)
            if profile.get("tunnel"):
                stop_tunnel(profile["tunnel"])
        except Exception:
            _publish(state_dir, profiles, use_sudo=use_sudo)
            raise
        return {"disconnected": key}


def teardown_client(state_dir: Path, *, use_sudo: bool = True) -> dict[str, Any]:
    with client_lock(state_dir):
        # Never silently destroy a live server's local gateway and DNS role.
        if _load_local(state_dir) is not None:
            raise PortmapError("local publishing is active; run portmap down before teardown")
        profiles = _load(state_dir)
        # If removing the resolver route fails, retain its working DNS target.
        teardown_resolver(state_dir, use_sudo=use_sudo)
        errors = []
        remaining = {}
        for domain, profile in profiles.items():
            if profile.get("tunnel"):
                try:
                    stop_tunnel(profile["tunnel"])
                except Exception as exc:
                    errors.append(str(exc))
                    remaining[domain] = profile
        _save(state_dir, remaining)
        for stop in (lambda path: stop_gateway(path, use_sudo=use_sudo), stop_dns):
            try:
                stop(state_dir)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise PortmapError("client teardown incomplete: " + "; ".join(errors))
        return {"stopped": True, "state_dir": str(state_dir)}


def discover(state_dir: Path, target: str | None = None, *, ssh_config: Path | None = None, timeout: float = 3) -> dict[str, Any]:
    result = discover_targets(target, config_file=ssh_config, timeout=timeout)
    with client_lock(state_dir):
        result["saved_connections"] = [_public(profile) for profile in _load(state_dir).values()]
    return result
