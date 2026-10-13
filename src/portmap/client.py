"""Client access to remote portmap gateways and the local server role.

Direct connections keep using the verified numeric remote IP and its remote
HTTP port. SSH connections start one private OpenSSH master per remote and
route its HTTP catalog through an owned Unix-socket forward inside the
shared container infrastructure; no host-loopback upstream is exposed to the
container. DNS records, the gateway routes and the resolver are owned by the
one infrastructure controller, so every client mutation republishes through
it. The local server role is persisted separately from user remote
connections; it needs no native backend because the shared infrastructure
routes the local domain to its own Docker-provider web entry.
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
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .agent import ensure_agent_started
from .client_discovery import (
    GatewayInfo,
    discover_ssh_hosts,
    discover_targets,
    probe_gateway,
    probe_gateway_socket,
    resolve_ssh_host,
)
from .client_runtime import legacy_client_state_dir
from .client_tunnel import (
    add_forward,
    add_http_forward,
    cancel_forward,
    cancel_http_forward,
    start_tunnel,
    stop_tunnel,
    tunnel_alive,
)
from .infrastructure import (
    infrastructure_status,
    preferred_http_port,
    save_public_http_port,
    stop_infrastructure,
    sync_infrastructure,
)
from .errors import PortmapError
from .settings import PortmapSettings, load_portmap_settings


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


def _load_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "connections.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "connections": {}}
    except (OSError, ValueError) as exc:
        raise PortmapError(f"invalid connection state: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("connections"), dict):
        raise PortmapError(f"invalid connection state: {path}")
    profiles = payload["connections"]
    if any(not isinstance(profile, dict) or domain != profile.get("domain") for domain, profile in profiles.items()):
        raise PortmapError(f"invalid connection profiles: {path}")
    legacy = payload.get("legacy_state_dir")
    if legacy is not None and (not isinstance(legacy, str) or not Path(legacy).is_absolute()):
        raise PortmapError(f"invalid legacy source state: {path}")
    return payload


def _load(state_dir: Path) -> dict[str, dict[str, Any]]:
    return _load_state(state_dir)["connections"]


def _save(state_dir: Path, profiles: dict[str, dict[str, Any]], *, legacy_state_dir: Path | None = None) -> None:
    # Keep import ownership in the same atomic record as the connections. A
    # failed cutover, or a later disconnect, must not lose the source or cause
    # the old directory's profiles to be imported again.
    payload = _load_state(state_dir)
    payload["connections"] = profiles
    if legacy_state_dir is not None:
        payload["legacy_state_dir"] = str(legacy_state_dir.expanduser().resolve())
    fd, name = tempfile.mkstemp(prefix=".connections-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
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
    """Start an SSH master and forward the remote HTTP gateway to a Unix socket.

    A short loopback TCP bootstrap proves the remote catalog and its domain,
    then is cancelled immediately. The persistent upstream is the parent's
    owned stream-local forward, so the container reaches the remote through
    its private socket directory and no host-loopback HTTP listener remains.
    Returns the tunnel descriptor, the verified gateway info, and the socket
    path. A tunnel that cannot prove a catalog is stopped again before the
    failure surfaces, so the caller only ever owns a live transport.
    """
    key = hashlib.sha256(chosen_ssh.encode()).hexdigest()[:20]
    tunnel = start_tunnel(chosen_ssh, state_dir=state_dir, key=key, config_file=ssh_config, timeout=max(timeout, 30), use_sudo=use_sudo)
    socket_path: str | None = None
    try:
        bootstrap_port = _free_port()
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
        upstream_port = info.http_port
        socket_path = add_http_forward(tunnel, state_dir=state_dir, domain=domain, remote_port=upstream_port, timeout=max(timeout, 5))
        try:
            info = probe_gateway_socket(socket_path, timeout=timeout, expected_domain=domain)
        except BaseException as exc:
            failure = str(exc)
            try:
                cancel_http_forward(tunnel, socket_path=socket_path, remote_port=upstream_port)
            except Exception as cleanup:
                failure = f"{failure}; HTTP forward cleanup needs attention: {cleanup}"
            raise PortmapError(failure) from exc
    except BaseException:
        stop_tunnel(tunnel)
        raise
    return tunnel, info, socket_path


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


def _cancel_http_forward_quietly(tunnel: dict[str, Any], profile: dict[str, Any]) -> None:
    """Retire a saved profile's socket forward through its ownership checks."""
    socket_path = profile.get("http_socket")
    if profile.get("tunnel") and isinstance(socket_path, str) and socket_path:
        cancel_http_forward(tunnel, socket_path=socket_path, remote_port=_port(profile.get("remote_http_port") or 80))


def _public_gateway_port(state_dir: Path) -> int:
    """Choose URLs before publication; a first SSH backend does not exist yet."""
    status = infrastructure_status(state_dir)
    gateway = status.get("gateway") or {}
    if status.get("running") and gateway.get("http_port"):
        return _port(gateway["http_port"])
    return preferred_http_port(state_dir) or gateway.get("http_port") or 80


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
    return {key: value for key, value in profile.items() if key not in {"tunnel", "backend_url", "http_socket", "ssh_config", "legacy_http_forward"}}


def _probe_transport(profile: dict[str, Any], timeout: float) -> GatewayInfo:
    """Probe the actual remote HTTP transport of one saved profile.

    Direct profiles request their verified numeric remote origin; SSH
    profiles request the remote catalog through the owned Unix-socket
    forward, never through the local aggregate portal.
    """
    if profile.get("via") == "ssh":
        if profile.get("tunnel") and not tunnel_alive(profile["tunnel"]):
            raise PortmapError("SSH tunnel is down")
        socket_path = profile.get("http_socket")
        if not isinstance(socket_path, str) or not socket_path:
            identity = profile.get("ssh_target") or profile.get("target")
            raise PortmapError(f"saved SSH transport is dormant; restore it with 'portmap connect {identity}' or disconnect it")
        return probe_gateway_socket(socket_path, timeout=timeout, expected_domain=profile["domain"])
    backend_url = profile.get("backend_url")
    if not isinstance(backend_url, str) or not backend_url:
        raise PortmapError(f"direct connection {profile.get('domain')} has no verified remote address")
    return probe_gateway(backend_url, timeout=timeout, expected_domain=profile["domain"])


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
    """Normalize a standalone profile without discarding its saved transport.

    Direct profiles retain their verified numeric origin. SSH profiles retain
    the exact master, authentication, custom port and raw TCP forwards until
    the common live-master adoption or normal dead-master restoration runs.
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
    migrated = dict(profile)
    if not migrated.get("ssh_config") and isinstance(saved_config, str) and saved_config:
        migrated["ssh_config"] = saved_config
    return migrated


def _import_legacy_state(state_dir: Path) -> None:
    """Import only an unused, non-isolated standalone state directory.

    Existing connections, including an intentionally empty mapping, are never
    reimported. Source ownership survives failed/interrupted cutover in the
    destination's connections record; the source itself remains untouched.
    In-place migration does not import anything and is handled by _publish.
    """
    if (state_dir / "connections.json").exists() or _load_local(state_dir) is not None:
        return
    if os.environ.get("PORTMAP_CLIENT_STATE_DIR") or os.environ.get("PORTMAP_STATE_DIR"):
        return
    legacy = legacy_client_state_dir()
    if legacy == state_dir.expanduser().resolve() or not legacy.exists():
        return
    with client_lock(legacy):
        profiles = {domain: _migrated_profile(profile) for domain, profile in _load(legacy).items()}
        _save(state_dir, profiles, legacy_state_dir=legacy)


def _adopt_live_ssh(
    state_dir: Path, profile: dict[str, Any], *, public: int, timeout: float,
) -> dict[str, Any]:
    """Attach the saved master, leaving its old HTTP and raw TCP forwards live."""
    domain, tunnel = profile["domain"], profile["tunnel"]
    remote_port = _port(profile.get("remote_http_port") or 80)
    try:
        socket_path = add_http_forward(tunnel, state_dir=state_dir, domain=domain, remote_port=remote_port, timeout=max(timeout, 5))
    except Exception as exc:
        raise PortmapError(f"legacy SSH connection {domain} could not be attached to the shared infrastructure: {exc}") from exc
    try:
        info = probe_gateway_socket(socket_path, timeout=timeout, expected_domain=domain)
        migrated = _migrated_profile(profile)
        obsolete = {}
        try:
            address, local_port = _origin_parts(profile.get("backend_url"))
        except PortmapError:
            pass  # Without an exact tuple, the old forward stays on its master.
        else:
            shared_tcp = (
                address == profile.get("tcp_address")
                and local_port in (profile.get("tcp_ports") or [])
                and local_port == remote_port
            )
            if ipaddress.ip_address(address).is_loopback and not shared_tcp:
                obsolete = {"local_address": address, "local_port": local_port, "remote_port": remote_port}
        migrated.update({
            "http_socket": socket_path,
            "remote_http_port": remote_port,
            "http_port": public,
            "catalog_url": _url(domain, public) + "/",
            # Even an unknown obsolete tuple needs published-route proof before
            # native retirement. Empty means there is nothing safe to cancel.
            "legacy_http_forward": obsolete,
        })
        # Keep the original URL usable by native readers until cutover commits.
        migrated["endpoints"] = _endpoints(info.catalog, migrated)
        return migrated
    except BaseException as exc:
        try:
            cancel_http_forward(tunnel, socket_path=socket_path, remote_port=remote_port)
        except Exception as cleanup:
            raise PortmapError(f"{exc}; HTTP forward cleanup needs attention: {cleanup}") from exc
        raise


def _prepare_live_ssh(
    state_dir: Path, profiles: dict[str, dict[str, Any]], *, http_port: int | None = None, timeout: float = 5,
) -> None:
    """Persist each proved socket and its retirement tuple before publication.

    The same operation handles standalone imports, in-place native state and
    matching connects. A proved socket is retained if infrastructure cutover
    fails: the old TCP listener still serves and the durable tuple permits a
    retry without opening another master or losing source ownership.
    """
    public = http_port if http_port is not None else _public_gateway_port(state_dir)
    for profile in profiles.values():
        if profile.get("via") != "ssh" or not profile.get("tunnel"):
            continue
        if profile.get("http_socket") and "legacy_http_forward" not in profile:
            continue
        if not tunnel_alive(profile["tunnel"]):
            # A dead master has no live HTTP listener to cut over. Keep its
            # descriptor/config for normal restoration after publication.
            profile.pop("legacy_http_forward", None)
            continue
        if profile.get("http_socket"):
            if "legacy_http_forward" in profile:
                _probe_transport(profile, timeout)
            continue
        migrated = _adopt_live_ssh(state_dir, profile, public=public, timeout=timeout)
        original = dict(profile)
        profile.clear()
        profile.update(migrated)
        try:
            _save(state_dir, profiles)
        except BaseException:
            profile.clear()
            profile.update(original)
            _cancel_http_forward_quietly(migrated["tunnel"], migrated)
            raise


def _retire_obsolete_forwards(state_dir: Path, profiles: dict[str, dict[str, Any]]) -> list[str]:
    """Retire exact old HTTP tuples only after shared-route readiness.

    Save after each retirement. If interrupted between cancellation and save,
    a refused cancellation is complete only when that exact listener is gone;
    an accepting or indeterminate listener keeps its ownership record.
    """
    failures = []
    for domain, profile in profiles.items():
        if "legacy_http_forward" not in profile:
            continue
        forward = profile["legacy_http_forward"]
        if forward:
            try:
                cancel_forward(profile["tunnel"], **forward)
            except Exception as exc:
                try:
                    with socket.create_connection((forward["local_address"], forward["local_port"]), timeout=1):
                        pass
                except ConnectionRefusedError:
                    pass
                except OSError:
                    failures.append(f"{domain}: {exc}")
                    continue
                else:
                    failures.append(f"{domain}: {exc}")
                    continue
        del profile["legacy_http_forward"]
        profile.pop("backend_url", None)
        _save(state_dir, profiles)
    return failures


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
            if isinstance(profile.get("domain"), str):
                profile["catalog_url"] = _url(profile["domain"], http_port) + "/"
    if profiles:
        _save(state_dir, profiles)


def setup_client(
    state_dir: Path,
    *,
    use_sudo: bool = True,
    http_port: int | None = None,
    settings: PortmapSettings | None = None,
) -> dict[str, Any]:
    if http_port is not None:
        _port(http_port)
    with client_lock(state_dir):
        _import_legacy_state(state_dir)
        profiles = _load(state_dir)
        _prepare_live_ssh(state_dir, profiles, http_port=http_port)
        effective = settings if settings is not None else load_portmap_settings()
        # Portal controls need the host agent even for remote-only users.
        agent = ensure_agent_started(effective, use_sudo=use_sudo, access_state_dir=state_dir)
        if not agent.running:
            raise PortmapError(agent.message)
        # _publish adopts live native masters before the controller can prove
        # or retire anything. The controller owns transactional rollback; a
        # blanket stop here could destroy the old route or a committed cutover.
        shared = _publish(state_dir, profiles, settings=settings, http_port=http_port, use_sudo=use_sudo)
        if http_port is not None:
            _apply_public_http_port(state_dir, profiles, http_port)
        restored, restore_failures = _restore_saved_ssh(state_dir, profiles, use_sudo=use_sudo)
        result = {"dns": shared["dns"], "resolver": shared["resolver"], "gateway": shared["gateway"], "state_dir": str(state_dir)}
        if restored:
            result["restored_connections"] = restored
        if restore_failures:
            result["restore_failures"] = restore_failures
        if shared.get("legacy_forward_failures"):
            result["legacy_forward_failures"] = shared["legacy_forward_failures"]

    # The shared infrastructure is ready and the client lock is released, so
    # an already-publishing legacy server can migrate onto it now instead of
    # keeping a second stack until the next `up`. Server -> client ->
    # infrastructure is the canonical lock order; the client lock is never
    # held while the server role takes its own.
    migration = _migrate_legacy_publishing(effective, use_sudo=use_sudo)
    if migration:
        result["legacy_server_migration"] = migration
    return result


def _migrate_legacy_publishing(settings: PortmapSettings, *, use_sudo: bool = True) -> dict[str, Any] | None:
    """Move an already-publishing legacy server onto the shared infrastructure.

    Runs only after setup proved the shared infrastructure and released the
    client lock, respecting the canonical server -> client -> infrastructure
    lock order; the server role never recurses into client setup itself.
    """
    from .server import migrate_legacy_server

    return migrate_legacy_server(settings, use_sudo=use_sudo)


def _publish(
    state_dir: Path, profiles: dict[str, dict[str, Any]], *, use_sudo: bool = True,
    settings: PortmapSettings | None = None, http_port: int | None = None, timeout: float = 5,
) -> dict[str, Any]:
    # The local catalog view reads connections.json. Persist both the proposed
    # routes and their pending legacy ownership before the controller stages
    # them; every publication path must adopt live native SSH first.
    _prepare_live_ssh(state_dir, profiles, http_port=http_port, timeout=timeout)
    _save(state_dir, profiles)
    source = _load_state(state_dir).get("legacy_state_dir")
    shared = sync_infrastructure(
        state_dir, profiles, _load_local(state_dir), use_sudo=use_sudo, settings=settings,
        http_port=http_port, legacy_state_dir=Path(source) if source else None,
    )
    failures = _retire_obsolete_forwards(state_dir, profiles)
    if failures:
        shared["legacy_forward_failures"] = failures
    return shared


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
    stale = profile.get("tunnel")
    if stale is not None and tunnel_alive(stale):
        # A matching native connection needs adoption, never authentication or
        # reconnect. _publish proves its socket and the shared public route.
        shared = _publish(state_dir, profiles, use_sudo=use_sudo, timeout=timeout)
        result = _public(profiles[domain])
        if shared.get("legacy_forward_failures"):
            result["legacy_forward_failures"] = shared["legacy_forward_failures"]
        return result
    identity = profile.get("ssh_target") or profile.get("target")
    if not isinstance(identity, str) or not identity:
        raise PortmapError(f"{domain} has no saved SSH target to restore; disconnect it and connect again")
    saved_config = profile.get("ssh_config") or (stale.get("config_file") if isinstance(stale, dict) else None)
    ssh_config = Path(saved_config) if isinstance(saved_config, str) and saved_config else None
    original = dict(profile)
    tunnel = None
    socket_path: str | None = None
    forward_port: int | None = None
    mutated = False
    try:
        if stale is not None:
            mutated = True
            _cancel_http_forward_quietly(stale, profile)
            profile.pop("tunnel", None)
            stop_tunnel(stale)
        public = _public_gateway_port(state_dir)
        tunnel, info, socket_path = _open_ssh_upstream(
            state_dir, identity, ssh_config=ssh_config, remote_port=profile.get("remote_http_port"),
            timeout=timeout, expected_domain=domain, use_sudo=use_sudo,
        )
        mutated = True
        forward_port = info.http_port
        tcp_address = profile.get("tcp_address") or _tcp_loopback(domain, profiles)
        tcp_ports = _forward_tcp_ports(tunnel, info, tcp_address) if profile.get("include_tcp") else []
        profile.update({
            "http_socket": socket_path,
            "remote_http_port": info.http_port,
            "http_port": public,
            "tcp_address": tcp_address,
            "tcp_ports": tcp_ports,
            "catalog_url": _url(domain, public) + "/",
        })
        profile.pop("backend_url", None)
        profile.pop("legacy_http_forward", None)
        profile["endpoints"] = _endpoints(info.catalog, profile)
        profile["tunnel"] = tunnel
        _publish(state_dir, profiles, use_sudo=use_sudo)
        probe_gateway(_url(domain, public), timeout=timeout, expected_domain=domain)
        return _public(profile)
    except BaseException as exc:
        rollback_errors = []
        if mutated:
            profile.clear()
            profile.update(original)
            profile.pop("tunnel", None)
            profile.pop("http_socket", None)
            profile.pop("legacy_http_forward", None)
            try:
                _publish(state_dir, profiles, use_sudo=use_sudo)
            except Exception as cleanup:
                rollback_errors.append(str(cleanup))
        if tunnel is not None:
            if socket_path is not None and forward_port is not None:
                try:
                    cancel_http_forward(tunnel, socket_path=socket_path, remote_port=forward_port)
                except Exception as cleanup:
                    rollback_errors.append(str(cleanup))
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
        if profile.get("http_socket") and profile.get("tunnel") is not None and tunnel_alive(profile["tunnel"]):
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
        if not infrastructure_status(state_dir).get("resolver", {}).get("installed"):
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
                if "legacy_http_forward" in profile or not profile.get("http_socket") or not profile.get("tunnel") or not tunnel_alive(profile["tunnel"]):
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
            info = _probe_transport(profile, timeout)
            profile["remote_http_port"] = info.http_port
            if profile.get("via") == "ssh":
                profile["http_port"] = _public_gateway_port(state_dir)
            profile["endpoints"] = _endpoints(info.catalog, profile)
            _publish(state_dir, profiles, use_sudo=use_sudo)
            port = info.http_port if profile.get("via") == "direct" else profile["http_port"]
            probe_gateway(_url(profile["domain"], port), timeout=timeout, expected_domain=profile["domain"])
            return {**_public(profile), "already_connected": True, "dns_ready": True}
        info = None
        direct_error = None
        tunnel = None
        socket_path: str | None = None
        upstream_port: int | None = None
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
            backend_url = None
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
                # Publication starts the shared gateway only after a verified
                # SSH backend exists; choosing the port does not start anything.
                local_gateway_port = _public_gateway_port(state_dir)
                tunnel, info, socket_path = _open_ssh_upstream(
                    state_dir, chosen_ssh, ssh_config=ssh_config,
                    remote_port=remote_port, timeout=timeout, use_sudo=use_sudo,
                )
                upstream_port = info.http_port
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
                "catalog_url": _url(domain, info.http_port if selected == "direct" else local_gateway_port) + "/",
                "tcp_address": tcp_address,
                "tcp_ports": tcp_ports,
                "include_tcp": include_tcp,
            }
            if selected == "direct":
                profile["backend_url"] = backend_url
            else:
                profile["http_port"] = local_gateway_port
                profile["http_socket"] = socket_path
            profile["endpoints"] = _endpoints(info.catalog, profile)
            if tunnel:
                profile["tunnel"] = tunnel
            publishing = True
            _publish(state_dir, {**profiles, domain: profile}, use_sudo=use_sudo)
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
                if socket_path is not None and upstream_port is not None:
                    try:
                        cancel_http_forward(tunnel, socket_path=socket_path, remote_port=upstream_port)
                    except Exception as cleanup:
                        cleanup_errors.append(str(cleanup))
                try:
                    stop_tunnel(tunnel)
                except Exception as cleanup:
                    cleanup_errors.append(str(cleanup))
            if cleanup_errors:
                raise PortmapError(f"{exc}; cleanup failed: {'; '.join(cleanup_errors)}") from exc
            raise


def _local_role(state_dir: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Validate a caller-supplied local role into its canonical profile.

    The local role needs no native backend: the shared infrastructure routes
    the local domain to its own Docker-provider web entry.
    """
    if not isinstance(spec, dict):
        raise PortmapError("local server profile must be an object")
    domain = spec.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        raise PortmapError("local server profile requires a domain")
    domain = domain.strip().lower()
    _require_domain(domain)
    public = spec.get("http_port")
    if public is None:
        public = preferred_http_port(state_dir)
    if public is None:
        public = 80
    _port(public)
    return {
        "domain": domain,
        "via": "local",
        "remote_http_port": public,
        "http_port": public,
        "address": "127.0.0.1",
        "target": "local",
        "scheme": "http",
        "tcp_address": "127.0.0.1",
        "tcp_ports": [],
        "include_tcp": False,
    }


def set_local_server(state_dir: Path, profile: dict[str, Any] | None, *, use_sudo: bool = True, settings: PortmapSettings | None = None) -> dict[str, Any]:
    """Publish or remove the local server role without touching remote connections."""
    with client_lock(state_dir):
        profiles = _load(state_dir)
        current = _load_local(state_dir)
        if profile is None:
            role = None
            if current is None:
                status = infrastructure_status(state_dir)
                return {"local": None, **status}
        else:
            role = _local_role(state_dir, profile)
            if role["domain"] in profiles:
                raise PortmapError(f"{role['domain']} is used by a remote connection; disconnect it before publishing the local server")
        _save_local(state_dir, role)
        try:
            status = _publish(state_dir, profiles, use_sudo=use_sudo, settings=settings)
        except Exception:
            _save_local(state_dir, current)
            raise
        return {"local": _public(role) if role else None, **status}


def connections(state_dir: Path, *, check: bool = False) -> dict[str, Any]:
    with client_lock(state_dir):
        profiles = _load(state_dir)
        rows = []
        for profile in profiles.values():
            row = _public(profile)
            row["status"] = "configured"
            if check:
                # One bounded request against the actual remote transport:
                # direct profiles hit their numeric remote origin, SSH
                # profiles the owned Unix-socket forward. The elapsed time is
                # an HTTP catalog round trip, never a ping or the local
                # aggregate portal, and a failure carries no number.
                checked_at = datetime.now(timezone.utc).isoformat()
                try:
                    started = time.monotonic()
                    _probe_transport(profile, 5)
                    elapsed = time.monotonic() - started
                except PortmapError as exc:
                    row["status"] = "unreachable"
                    row["error"] = str(exc)
                else:
                    row["status"] = "reachable"
                    row["latency_ms"] = round(elapsed * 1000, 2)
                row["last_checked_at"] = checked_at
            rows.append(row)
        local = _load_local(state_dir)
        status = infrastructure_status(state_dir)
        return {
            "connections": rows,
            "local": _public(local) if local else None,
            "dns": status["dns"],
            "resolver": status["resolver"],
            "gateway": status["gateway"],
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
                _cancel_http_forward_quietly(profile["tunnel"], profile)
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
        errors = []
        remaining = {}
        for domain, profile in profiles.items():
            tunnel = profile.get("tunnel")
            if not tunnel:
                continue
            try:
                _cancel_http_forward_quietly(tunnel, profile)
                stop_tunnel(tunnel)
            except Exception as exc:
                errors.append(str(exc))
                remaining[domain] = profile
        _save(state_dir, remaining)
        try:
            stop_infrastructure(state_dir, use_sudo=use_sudo)
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
