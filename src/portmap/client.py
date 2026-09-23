"""Client-owned IP/SSH connections behind one local DNS and HTTP gateway."""
from __future__ import annotations

import fcntl
import hashlib
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
from .client_gateway import gateway_status, stop_gateway, sync_gateway
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


def _records(profiles: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {domain: "127.0.0.1" for domain in profiles}


def _port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise PortmapError(f"invalid port: {port!r}")
    return port


def _url(address: str, port: int) -> str:
    host = f"[{address}]" if ":" in address else address
    return f"http://{host}" + (f":{_port(port)}" if port != 80 else "")


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


def _endpoints(catalog: dict[str, Any], profile: dict[str, Any], port: int) -> list[dict[str, Any]]:
    from .client_view import project_catalog

    view = project_catalog(catalog, profile, http_port=port)
    return [
        {**endpoint, "repo_name": service.get("repo_name"), "branch": service.get("branch")}
        for service in view["services"]
        for endpoint in service["endpoints"]
    ]


def _public(profile: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in profile.items() if key not in {"tunnel", "backend_url"}}


def setup_client(state_dir: Path, *, use_sudo: bool = True, http_port: int | None = None) -> dict[str, Any]:
    if http_port is not None:
        _port(http_port)
    with client_lock(state_dir):
        profiles = _load(state_dir)
        if http_port is not None and any(profile.get("http_port") != http_port for profile in profiles.values()):
            raise PortmapError("saved connections use another HTTP port; run portmap-client teardown before changing --http-port")
        had_resolver = resolver_status(state_dir)["installed"]
        had_dns = dns_status(state_dir)["running"]
        had_gateway = gateway_status(state_dir)["running"]
        try:
            # Linux DNS binds the dedicated dummy address, which must exist
            # before CoreDNS starts (also works with systemd-resolved 249).
            resolver = setup_resolver(state_dir, use_sudo=use_sudo)
            dns = sync_dns(state_dir, _records(profiles))
            gateway = sync_gateway(state_dir, profiles, http_port=http_port)
            return {"dns": dns, "resolver": resolver, "gateway": gateway, "state_dir": str(state_dir)}
        except Exception as exc:
            cleanups = []
            if not had_gateway:
                cleanups.append(lambda: stop_gateway(state_dir))
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


def _publish(state_dir: Path, profiles: dict[str, dict[str, Any]]) -> None:
    # The local catalog view reads this file. Publish it before the route,
    # then DNS last; mutations are serialized by client_lock and rolled back.
    _save(state_dir, profiles)
    sync_gateway(state_dir, profiles)
    sync_dns(state_dir, _records(profiles))


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
) -> dict[str, Any]:
    if via not in {"auto", "direct", "ssh"}:
        raise PortmapError("connection mode must be auto, direct or ssh")
    if not target and not ssh_target:
        raise PortmapError("specify an IP/URL or SSH target; use portmap-client discover to list local SSH aliases")
    if timeout <= 0 or timeout > 60:
        raise PortmapError("connection timeout must be between 0 and 60 seconds")
    if remote_port is not None:
        _port(remote_port)
    with client_lock(state_dir):
        if not resolver_status(state_dir)["installed"]:
            raise PortmapError("local split DNS is not configured; run portmap-client setup first")
        profiles = _load(state_dir)
        gateway = sync_gateway(state_dir, profiles)
        local_port = gateway["http_port"]
        for profile in profiles.values():
            if profile.get("target") == target and (ssh_target is None or profile.get("ssh_target") == ssh_target) and (via == "auto" or profile.get("via") == via):
                if include_tcp and not profile.get("include_tcp"):
                    raise PortmapError(f"TCP forwarding options changed; disconnect {profile['domain']} before reconnecting")
                if profile.get("tunnel") and not tunnel_alive(profile["tunnel"]):
                    raise PortmapError(f"saved SSH connection is down; disconnect {profile['domain']} before reconnecting")
                info = probe_gateway(profile["backend_url"], timeout=timeout, expected_domain=profile["domain"])
                profile["http_port"] = local_port
                profile["endpoints"] = _endpoints(info.catalog, profile, local_port)
                _publish(state_dir, profiles)
                verify_resolution(profile["domain"], "127.0.0.1")
                probe_gateway(_url(profile["domain"], local_port), timeout=timeout, expected_domain=profile["domain"])
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
                key = hashlib.sha256(chosen_ssh.encode()).hexdigest()[:20]
                tunnel = start_tunnel(chosen_ssh, state_dir=state_dir, key=key, config_file=ssh_config, timeout=max(timeout, 30))
                bootstrap_port = _free_port()
                failures = []
                for candidate in ([remote_port] if remote_port else [8080, 80]):
                    added = False
                    try:
                        add_forward(tunnel, local_address="127.0.0.1", local_port=bootstrap_port, remote_port=candidate)
                        added = True
                        info = probe_gateway(_url("127.0.0.1", bootstrap_port), timeout=timeout)
                    except PortmapError as exc:
                        failures.append(str(exc))
                    finally:
                        if added:
                            cancel_forward(tunnel, local_address="127.0.0.1", local_port=bootstrap_port, remote_port=candidate)
                    if info is not None:
                        break
                if info is None:
                    raise PortmapError("SSH connected but no portmap catalog was reachable; use --remote-port for a custom gateway: " + "; ".join(failures))
                domain = info.domain
                _require_domain(domain)
                tcp_address = _tcp_loopback(domain, profiles)
                upstream_port = _free_port()
                add_forward(tunnel, local_address="127.0.0.1", local_port=upstream_port, remote_port=info.http_port)
                backend_url = _url("127.0.0.1", upstream_port)
                info = probe_gateway(backend_url, timeout=timeout, expected_domain=domain)
                if include_tcp:
                    for service in info.catalog["services"]:
                        for endpoint in service["endpoints"]:
                            if endpoint.get("kind") != "tcp":
                                continue
                            host_port = _port(endpoint.get("host_port"))
                            if host_port in tcp_ports:
                                continue
                            add_forward(tunnel, local_address=tcp_address, local_port=host_port, remote_port=host_port)
                            tcp_ports.append(host_port)
            _require_domain(domain)
            if domain in profiles:
                raise PortmapError(f"{domain} is already connected via another target; disconnect it before replacing its DNS mapping")
            profile = {
                "domain": domain,
                "target": target,
                "ssh_target": chosen_ssh,
                "via": selected,
                "address": "127.0.0.1",
                "http_port": local_port,
                "remote_http_port": info.http_port,
                "scheme": "http",
                "backend_url": backend_url,
                "catalog_url": _url(domain, local_port) + "/",
                "tcp_address": tcp_address,
                "tcp_ports": tcp_ports,
                "include_tcp": include_tcp,
            }
            profile["endpoints"] = _endpoints(info.catalog, profile, local_port)
            if tunnel:
                profile["tunnel"] = tunnel
            publishing = True
            _publish(state_dir, {**profiles, domain: profile})
            verify_resolution(domain, "127.0.0.1")
            # Prove the public local gateway and projected catalog, not merely
            # the SSH listener or backend. Project app traffic keeps its Host.
            probe_gateway(_url(domain, local_port), timeout=timeout, expected_domain=domain)
            return {**_public(profile), "already_connected": False, "dns_ready": True}
        except Exception as exc:
            cleanup_errors = []
            if publishing:
                try:
                    _publish(state_dir, profiles)
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


def connections(state_dir: Path, *, check: bool = False) -> dict[str, Any]:
    with client_lock(state_dir):
        profiles = _load(state_dir)
        rows = []
        for profile in profiles.values():
            row = _public(profile)
            row["status"] = "configured"
            if check:
                try:
                    if profile.get("tunnel") and not tunnel_alive(profile["tunnel"]):
                        raise PortmapError("SSH tunnel is down")
                    probe_gateway(_url(profile["domain"], profile["http_port"]), expected_domain=profile["domain"])
                    row["status"] = "reachable"
                except PortmapError as exc:
                    row["status"] = "unreachable"
                    row["error"] = str(exc)
            rows.append(row)
        return {"connections": rows, "dns": dns_status(state_dir), "resolver": resolver_status(state_dir), "gateway": gateway_status(state_dir)}


def disconnect(state_dir: Path, domain: str) -> dict[str, Any]:
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
            _publish(state_dir, proposed)
            if profile.get("tunnel"):
                stop_tunnel(profile["tunnel"])
        except Exception:
            _publish(state_dir, profiles)
            raise
        return {"disconnected": key}


def teardown_client(state_dir: Path, *, use_sudo: bool = True) -> dict[str, Any]:
    with client_lock(state_dir):
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
        for stop in (stop_gateway, stop_dns):
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
