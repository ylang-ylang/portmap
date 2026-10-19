"""Owned container infrastructure, staged before resolver and HTTP cutover."""
from __future__ import annotations

import fcntl
import copy
import errno
import http.client
import ipaddress
import json
import os
import secrets
import socket
import sys
import stat
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Mapping

from .client_dns import DNS_ADDRESS, DNS_PORT, prepare_resolver, probe_marker, render_corefile, resolver_environment, resolver_plan, resolver_status, setup_resolver, teardown_resolver, validate_resolver_plan, verify_resolution
from .client_discovery import probe_gateway
from .client_tunnel import http_socket_dir, tunnel_alive
from .container_runtime import ContainerRuntime, container_address
from .downloads import download_cache_dir
from .errors import PortmapError
from .ingress import ensure_ingress, ingress_status, stop_ingress
from .permissions import ensure_private_directory, ensure_private_file
from .infrastructure_config import HEALTH_HOST, IMAGES, access_snapshot, compose_config, dns_config, json_bytes, relay_config, relay_ports, resource_labels, routes_config, static_config
from .legacy_native import capture_native_workers, release_native_http, restore_native_http, retire_native_workers
from .runtime_compat import prepare_runtime
from .settings import PortmapSettings, load_portmap_settings

_START_TIMEOUT = 25.0
_MAX_JSON = 4 * 1024 * 1024


def _private_directory(path: Path) -> None:
    ensure_private_directory(
        path,
        message=f"infrastructure directory is not private and owned by this user: {path}",
    )


def _atomic(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".portmap-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            os.fchmod(output.fileno(), mode)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _lock(state_dir: Path) -> Iterator[None]:
    _private_directory(state_dir)
    fd = os.open(state_dir / "infrastructure.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_infrastructure(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / "infrastructure.json"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    ensure_private_file(path, message="unsafe infrastructure ownership record")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PortmapError("unsafe infrastructure ownership record")
    try:
        state = json.loads(path.read_text())
        if (state["version"] != 1 or state["uid"] != os.getuid() or state["state_dir"] != str(state_dir.resolve())
                or len(state["owner"]) != 32 or any(character not in "0123456789abcdef" for character in state["owner"])
                or Path(state["instance_dir"]) != state_dir.resolve() / "infrastructure" / state["owner"]):
            raise ValueError("owner mismatch")
        ContainerRuntime.from_record(state["runtime"])
        for key in ("http_port", "listen_port", "public_backend_port", "management_port"):
            if type(state[key]) is not int or not 1 <= state[key] <= 65535:
                raise ValueError("invalid listener port")
        legacy_roles = "host_dns_enabled" not in state
        state.setdefault("host_dns_enabled", state["phase"] != "stopped")
        if type(state["host_dns_enabled"]) is not bool:
            raise ValueError("invalid host DNS role")
        if legacy_roles and not state["host_dns_enabled"]:
            state["resolver"], state["dns_port"] = None, None
        elif "resolver" not in state:
            state["resolver"] = {
                "backend": "resolved" if sys.platform.startswith("linux") else "darwin",
                "address": DNS_ADDRESS, "port": state["dns_port"], "forwarders": [],
            }
        if state["resolver"] is not None:
            state["resolver"] = validate_resolver_plan(state["resolver"])
            if type(state["dns_port"]) is not int or state["resolver"]["port"] != state["dns_port"]:
                raise ValueError("resolver port disagrees with the published DNS port")
        elif state["host_dns_enabled"] or state["dns_port"] is not None:
            raise ValueError("host DNS requires a published resolver")
        plan = state["resolver"]
        state.setdefault("container_upstreams", list(plan["forwarders"]) if plan and plan["backend"] == "forward" else [])
        if not isinstance(state["container_upstreams"], list):
            raise ValueError("invalid container DNS upstreams")
        if state["container_upstreams"]:
            validate_resolver_plan({
                "backend": "forward", "address": "127.0.0.1", "port": 53,
                "forwarders": state["container_upstreams"],
            })
        state.setdefault("environment", None)
        if state["environment"] is not None and (
            not isinstance(state["environment"], dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in state["environment"].items())
        ):
            raise ValueError("invalid infrastructure environment identity")
        state.setdefault("ingress_backend", "auto")
        if state["ingress_backend"] not in {"auto", "container", "systemd"}:
            raise ValueError("invalid ingress selection")
        if set(state["names"]) != set(IMAGES) or not isinstance(state["containers"], dict):
            raise ValueError("invalid container roles")
        _private_directory(Path(state["instance_dir"]))
        return state
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise PortmapError(f"invalid infrastructure ownership record: {exc}") from exc


def _save(state_dir: Path, state: dict[str, Any]) -> None:
    _atomic(state_dir / "infrastructure.json", json_bytes(state))


def preferred_http_port(state_dir: Path) -> int | None:
    try:
        values = json.loads((state_dir / "settings.json").read_text())
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        raise PortmapError("invalid local HTTP port settings") from exc
    port = values.get("public_http_port")
    if values.get("version") != 1 or type(port) is not int or not 1 <= port <= 65535:
        raise PortmapError("invalid local HTTP port settings")
    return port


def save_public_http_port(state_dir: Path, port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise PortmapError("local HTTP port must be an integer between 1 and 65535")
    _private_directory(state_dir)
    _atomic(state_dir / "settings.json", json_bytes({"version": 1, "public_http_port": port}))


def _free_port(address: str = "127.0.0.1", *, udp: bool = False) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
        tcp.bind((address, 0))
        port = tcp.getsockname()[1]
        if udp:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
                datagram.bind((address, port))
        return port


def _dns_port(native: dict[str, Any], plan: Mapping[str, Any]) -> int:
    address, port = plan["address"], plan["port"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            tcp.bind((address, port))
            udp.bind((address, port))
        return port
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            # A rootful engine can publish a port the invoking user cannot bind.
            # Its actual publish operation remains the permission authority.
            return port
        if exc.errno != errno.EADDRINUSE:
            raise PortmapError(f"cannot use DNS listener {address}:{port}: {exc}") from exc
        if plan["backend"] not in {"resolved", "darwin"} or not native.get("dns") or native["dns"].get("state") != "owned":
            raise PortmapError(f"DNS listener {address}:{port} is already occupied; refusing to replace an unrelated service") from exc
        # Keep an owned native listener until the replacement route is proved.
        return _free_port(address, udp=True)


def _profile_views(profiles: Mapping[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for domain, profile in profiles.items():
        available = profile.get("via") == "direct"
        if profile.get("via") == "ssh" and profile.get("tunnel") and profile.get("http_socket"):
            path = Path(profile["http_socket"])
            try:
                metadata = path.lstat()
                available = stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid() and tunnel_alive(profile["tunnel"])
            except OSError:
                available = False
        result[domain] = {**profile, "available": available}
    return result


def _private_ports(public: int) -> tuple[int, int, int]:
    ports = []
    with ExitStack() as handles:
        while len(ports) < 3:
            listener = handles.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            if port != public:
                ports.append(port)
    return tuple(ports)


def _new_state(state_dir: Path, settings: PortmapSettings, runtime: ContainerRuntime, http_port: int, native: dict[str, Any], plan: Mapping[str, Any] | None) -> dict[str, Any]:
    owner = secrets.token_hex(16)
    parent = state_dir / "infrastructure"
    _private_directory(parent)
    directory = parent / owner
    _private_directory(directory)
    config = directory / "config"
    config.mkdir(mode=0o755)
    (config / "dynamic").mkdir(mode=0o755)
    _private_directory(directory / "catalog")
    settings.agent_runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    prefix = f"portmap-{os.getuid()}-{owner[:12]}"
    listen, publishing, management = _private_ports(http_port)
    dns_port = _dns_port(native, plan) if plan is not None else None
    state = {
        "version": 1, "phase": "starting", "owner": owner, "uid": os.getuid(), "state_dir": str(state_dir),
        "instance_dir": str(directory), "runtime": runtime.record(), "project": prefix,
        "names": {role: f"{prefix}-{role}" for role in IMAGES}, "containers": {}, "addresses": {},
        "domain": settings.dns_domain, "http_port": http_port, "dns_port": dns_port,
        "resolver": {**plan, "port": dns_port} if plan is not None else None,
        "host_dns_enabled": plan is not None, "ingress_backend": settings.ingress_backend,
        "container_upstreams": list(plan["forwarders"]) if plan and plan["backend"] == "forward" else [],
        "environment": resolver_environment(),
        "listen_port": listen, "public_backend_port": publishing, "management_port": management,
        "agent_runtime_dir": str(settings.agent_runtime_dir.resolve()), "dns_forward": settings.dns_forward,
        "socket_dir": str(http_socket_dir(state_dir)), "relay_ports": {},
        "publishing_state_dir": str(settings.state_dir.resolve()), "public_dns": None,
        "download_cache_dir": str(download_cache_dir(settings.state_dir).resolve()),
    }
    network, created = runtime.ensure_network(settings.gateway_network, resource_labels(state))
    state["network"] = {"name": settings.gateway_network, "id": network["Id"], "created": created}
    try:
        state["addresses"] = runtime.reserve_addresses(network, IMAGES)
        state["default_dns_address"] = state["addresses"]["dns"]
    except BaseException:
        _save(state_dir, state)
        if created and runtime.remove_empty_network(network["Id"], resource_labels(state)):
            (state_dir / "infrastructure.json").unlink(missing_ok=True)
        raise
    return state


def _configuration_paths(state: dict[str, Any]) -> dict[str, Path]:
    config = Path(state["instance_dir"]) / "config"
    return {"routes": config / "dynamic" / "routes.yaml", "dns": config / "Corefile", "access": config / "catalog-access.json", "relay": config / "relay.cfg"}


def _prepare_bind_sources(state: dict[str, Any]) -> None:
    # /tmp may have disappeared since the last setup. Restore ownership before
    # Compose can auto-create the relay's bind source as a root-owned directory.
    if str(http_socket_dir(Path(state["state_dir"]))) != state["socket_dir"]:
        raise PortmapError("the recorded SSH socket directory does not match this infrastructure")
    Path(state["download_cache_dir"]).mkdir(parents=True, exist_ok=True)


def _seed(state: dict[str, Any], profiles: Mapping[str, dict[str, Any]], local: dict[str, Any] | None, runtime: ContainerRuntime) -> None:
    _prepare_bind_sources(state)
    directory = Path(state["instance_dir"])
    dormant = {domain: {**profile, "available": False} for domain, profile in profiles.items()}
    marker = f"{state['owner']}:starting"
    state["dns_marker"] = marker
    snapshot = access_snapshot(state, dormant, local)
    state["revision"], routes = routes_config(state, snapshot)
    # HAProxy can ignore reload signals while its first worker initializes.
    # Start that worker with the final private relay endpoints, not an empty set.
    state["relay_ports"] = relay_ports(profiles, state["relay_ports"])
    state["relay_revision"], bridge = relay_config(state, profiles)
    # No DNS route points at this staging listener yet.
    content = render_corefile(
        {}, {}, marker, forward=state["dns_forward"],
        host_forward=state["resolver"]["forwarders"] if state["resolver"] and state["resolver"]["backend"] == "forward" else None,
        container_forward=state["container_upstreams"] if state["dns_forward"] == "/etc/resolv.conf" and state["container_upstreams"] else None,
    )
    paths = _configuration_paths(state)
    for key, value in {"routes": json_bytes(routes), "dns": content.encode(), "access": json_bytes(snapshot), "relay": bridge.encode()}.items():
        _atomic(paths[key], value, mode=0o644)
    _atomic(directory / "config" / "traefik.yaml", json_bytes(static_config(state)), mode=0o644)
    _atomic(directory / "compose.yaml", json_bytes(compose_config(state, runtime)))


def _container_mac(container: Mapping[str, Any], network: str) -> str | None:
    endpoint = container.get("NetworkSettings", {}).get("Networks", {}).get(network, {})
    address = endpoint.get("MacAddress")
    return address.lower() if isinstance(address, str) and address else None


def _observe_containers(state: dict[str, Any], runtime: ContainerRuntime) -> None:
    labels = resource_labels(state)
    containers, addresses = {}, {}
    services = json.loads((Path(state["instance_dir"]) / "compose.yaml").read_bytes())["services"]
    for role, name in state["names"].items():
        container = runtime.require_owned(name, labels)
        if container is None or not container.get("State", {}).get("Running"):
            raise PortmapError(f"the owned {role} container is not running; inspect portmap gateway logs")
        recorded = state["containers"].get(role)
        if recorded is not None and recorded != container["Id"]:
            raise PortmapError(f"the owned {role} container was replaced outside this controller; rerun setup to reconcile it")
        expected_mac = services[role]["networks"]["gateway"].get("mac_address") or services[role].get("mac_address")
        if expected_mac and _container_mac(container, state["network"]["name"]) != expected_mac:
            raise PortmapError(f"the runtime did not retain the owned {role} network MAC address")
        containers[role] = container["Id"]
        addresses[role] = container_address(container, state["network"]["name"])
    state["containers"], state["addresses"] = containers, addresses


def _reconcile_containers(state: dict[str, Any], runtime: ContainerRuntime) -> None:
    _prepare_bind_sources(state)
    directory = Path(state["instance_dir"])
    path = directory / "compose.yaml"
    current = path.read_bytes()
    current_services = json.loads(current)["services"]
    macs: dict[str, str] = {}
    inactive: set[str] = set()
    for role, name in state["names"].items():
        container = runtime.require_owned(name, resource_labels(state))
        if container is None or not container.get("State", {}).get("Running"):
            inactive.add(role)
        elif container_address(container, state["network"]["name"]) != state["addresses"][role]:
            raise PortmapError(f"the owned {role} container moved off its reserved address; refusing to change business DNS targets")
        service = current_services.get(role, {})
        endpoint = service.get("networks", {}).get("gateway", {})
        actual_mac = _container_mac(container, state["network"]["name"]) if container else None
        if actual_mac:
            macs[role] = actual_mac
        elif endpoint.get("mac_address") or service.get("mac_address"):
            macs[role] = endpoint.get("mac_address") or service["mac_address"]
    desired_config = compose_config(state, runtime, mac_addresses=macs)
    for role, mac in macs.items():
        service = current_services.get(role)
        if not service:
            continue
        # API spelling changes and adoption of an existing MAC do not alter
        # the live endpoint. Normalize them before deciding to replace a role.
        endpoint = service["networks"]["gateway"]
        endpoint.pop("mac_address", None)
        service.pop("mac_address", None)
        if "mac_address" in desired_config["services"][role]:
            service["mac_address"] = mac
        else:
            endpoint["mac_address"] = mac
    desired = json_bytes(desired_config)
    changed = inactive | {
        role for role in state["names"]
        if current_services.get(role) != desired_config["services"][role]
    }
    if current != desired or changed:
        _atomic(directory / "config" / "traefik.yaml", json_bytes(static_config(state)), mode=0o644)
        _atomic(path, desired)
        if changed:
            # A whole-project up may recreate unchanged services on Podman.
            # Host-access changes must not replace consumers' live DNS server.
            runtime.compose(path, ["up", "-d", "--no-deps", *sorted(changed)], capture=True)
            state["containers"] = {}
    _observe_containers(state, runtime)


def _verify_host_view(state: dict[str, Any], profiles: Mapping[str, dict[str, Any]]) -> None:
    if not state["host_dns_enabled"]:
        return
    verify_resolution(state["domain"], "127.0.0.1")
    for domain, profile in profiles.items():
        if profile.get("via") == "direct":
            verify_resolution(domain, profile["address"])
        elif profile.get("available"):
            verify_resolution(domain, "127.0.0.1")
    _verify_pending_ssh_http(profiles, port=state["http_port"])


def _verify_pending_ssh_http(profiles: Mapping[str, dict[str, Any]], *, port: int) -> None:
    for domain, profile in profiles.items():
        if profile.get("via") == "ssh" and "legacy_http_forward" in profile:
            if not profile.get("available"):
                raise PortmapError(f"legacy SSH replacement for {domain} is not available")
            # Probe the exact replacement listener, never DNS that can still
            # lead to the native gateway or the independently proved socket.
            probe_gateway(
                f"http://127.0.0.1:{port}", http_host=domain,
                expected_domain=domain, timeout=5,
            )


def _json_get(port: int, path: str, *, host: str = HEALTH_HOST, timeout: float = 0.5) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Host": host, "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(_MAX_JSON + 1)
        if response.status != 200 or len(raw) > _MAX_JSON:
            raise ValueError(f"readiness returned HTTP {response.status} or an oversized response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("readiness returned invalid JSON")
        return result
    finally:
        connection.close()


def _routes_loaded(actual: dict[str, Any], desired: dict[str, Any]) -> bool:
    routers, services = actual.get("routers"), actual.get("services")
    if not isinstance(routers, dict) or not isinstance(services, dict):
        return False
    expected = desired["http"]
    if {name for name in routers if name.endswith("@file")} != {f"{name}@file" for name in expected["routers"]}:
        return False
    for name, wanted in expected["routers"].items():
        found = routers.get(f"{name}@file", {})
        if found.get("status") != "enabled" or any(found.get(key) != wanted[key] for key in ("rule", "priority", "entryPoints")):
            return False
        if found.get("service") not in (wanted["service"], wanted["service"] + "@file"):
            return False
    for name, wanted in expected["services"].items():
        found = services.get(f"{name}@file", {})
        if found.get("status") != "enabled" or any(found.get("loadBalancer", {}).get(key) != wanted["loadBalancer"][key] for key in ("servers", "passHostHeader")):
            return False
    return True


def _health(state: dict[str, Any], routes: dict[str, Any], *, port: int | None = None) -> None:
    if state["host_dns_enabled"] and probe_marker(state["resolver"]["address"], state["dns_port"], timeout=0.4) != state["dns_marker"]:
        raise PortmapError("container DNS has not loaded the requested hostname mappings")
    if not _routes_loaded(_json_get(state["management_port"], "/api/rawdata"), routes):
        raise PortmapError("Traefik has not enabled the requested route revision")
    target = state["listen_port"] if port is None else port
    expected = {"ok": True, "owner": state["owner"], "dns_marker": state["dns_marker"]}
    if _json_get(target, f"/__portmap_ready/{state['owner']}/{state['revision']}") != expected:
        raise PortmapError("private catalog or container DNS ownership readiness failed")
    if _json_get(target, f"/__portmap_relay/{state['owner']}") != {"owner": state["owner"], "revision": state["relay_revision"]}:
        raise PortmapError("SSH Unix-socket relay has not loaded the requested revision")


def _wait_ready(state: dict[str, Any], routes: dict[str, Any], *, port: int | None = None) -> None:
    deadline = time.monotonic() + _START_TIMEOUT
    error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _health(state, routes, port=port)
            return
        except (PortmapError, OSError, ValueError, http.client.HTTPException) as exc:
            error = exc
        time.sleep(0.1)
    raise PortmapError(f"container infrastructure did not become ready: {error}")


def _check_profile_network(state: dict[str, Any], profiles: Mapping[str, dict[str, Any]], runtime: ContainerRuntime) -> None:
    if any(profile.get("via") == "direct" and ipaddress.ip_address(profile["address"]).version == 6 for profile in profiles.values()):
        network = runtime.inspect_network(state["network"]["id"])
        if not network or not network.get("EnableIPv6"):
            raise PortmapError(
                f"IPv6 direct access requires an IPv6-enabled gateway bridge; existing network "
                f"{state['network']['name']!r} was left unchanged. Use an IPv6-enabled gateway network "
                "or connect explicitly via SSH."
            )


def _dynamic_configuration(
    state: dict[str, Any], profiles: Mapping[str, dict[str, Any]], local: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    candidate = {**state, "relay_ports": relay_ports(profiles, state["relay_ports"])}
    candidate["relay_revision"], bridge = relay_config(candidate, profiles)
    candidate["dns_marker"], corefile = dns_config(candidate, profiles)
    snapshot = access_snapshot(candidate, profiles, local)
    candidate["revision"], routes = routes_config(candidate, snapshot)
    payloads = {"access": json_bytes(snapshot), "relay": bridge.encode(), "routes": json_bytes(routes), "dns": corefile.encode()}
    return candidate, routes, payloads


def _publish(state: dict[str, Any], profiles: Mapping[str, dict[str, Any]], local: dict[str, Any] | None, runtime: ContainerRuntime) -> dict[str, Any]:
    _check_profile_network(state, profiles, runtime)
    candidate, routes, payloads = _dynamic_configuration(state, profiles, local)
    paths = _configuration_paths(state)
    previous = {key: path.read_bytes() for key, path in paths.items()}
    bridge_changed = previous["relay"] != payloads["relay"]
    try:
        # Endpoints first, routes next, DNS last. Readers see atomic snapshots.
        for key in ("access", "relay", "routes", "dns"):
            if previous[key] != payloads[key]:
                _atomic(paths[key], payloads[key], mode=0o644)
        if bridge_changed:
            runtime.signal_owned(state["containers"]["relay"], resource_labels(state), "SIGUSR2")
        _wait_ready(candidate, routes)
        if state.get("phase") == "ready":
            _wait_ready(candidate, routes, port=candidate["http_port"])
            _verify_host_view(candidate, profiles)
    except BaseException as exc:
        for key, content in previous.items():
            _atomic(paths[key], content, mode=0o644)
        if bridge_changed:
            try:
                runtime.signal_owned(state["containers"]["relay"], resource_labels(state), "SIGUSR2")
            except PortmapError as rollback:
                raise PortmapError(f"configuration update failed ({exc}); relay rollback also failed: {rollback}") from exc
        if state.get("phase") == "ready":
            try:
                _wait_ready(state, json.loads(previous["routes"]))
            except PortmapError as rollback:
                raise PortmapError(f"configuration update failed ({exc}); previous configuration restored on disk but readiness failed: {rollback}") from exc
        raise
    return candidate


def _sync_existing(
    previous: dict[str, Any], state_dir: Path, runtime: ContainerRuntime,
    profiles: Mapping[str, dict[str, Any]], local: dict[str, Any] | None,
    plan: Mapping[str, Any] | None, *, host_dns: bool, use_sudo: bool,
    native: dict[str, Any], source_dir: Path,
) -> dict[str, Any]:
    """Reconfigure owned roles without tearing down their shared consumers."""
    _check_profile_network(previous, profiles, runtime)
    paths = _configuration_paths(previous)
    before = {key: path.read_bytes() for key, path in paths.items()}
    rollback = copy.deepcopy(previous)
    # Upgrade probe metadata on rollback as well: new catalog code must be
    # able to prove the restored revision of an older owned snapshot.
    old_snapshot = json.loads(before["access"])
    old_local = old_snapshot["local"] if old_snapshot["local"]["enabled"] else None
    # Compiled machine rows already contain their historical backend URLs.
    # They are not live profiles and must not be rebound through a newer
    # relay-port allocation while preparing rollback.
    old_snapshot = {
        **access_snapshot(rollback, {}, old_local),
        "machines": old_snapshot["machines"],
    }
    rollback["revision"], old_routes = routes_config(rollback, old_snapshot)
    before["access"], before["routes"] = json_bytes(old_snapshot), json_bytes(old_routes)
    old_resolver = resolver_status(state_dir)
    old_plan = resolver_plan(state_dir, use_sudo=use_sudo) if old_resolver.get("installed") else None
    current_environment = resolver_environment()
    dns = runtime.require_owned(previous["names"]["dns"], resource_labels(previous))
    dns_live = bool(dns and dns.get("State", {}).get("Running"))
    state = copy.deepcopy(previous)
    state.update(phase="starting", host_dns_enabled=host_dns, http_address=local.get("http_address") if local else None)
    if current_environment is not None:
        state["environment"] = current_environment
    committed = False
    try:
        if host_dns:
            if plan is None:
                raise PortmapError("host DNS initialization requires a resolver plan")
            prepare_resolver(state_dir, use_sudo=use_sudo, plan=plan)
            old_binding = previous["resolver"]
            same_binding = old_binding and (old_binding["address"], old_binding["port"]) == (plan["address"], plan["port"])
            port = plan["port"] if dns_live and same_binding else _dns_port(native, plan)
            state["resolver"], state["dns_port"] = {**plan, "port": port}, port
            state["container_upstreams"] = list(plan["forwarders"]) if plan["backend"] == "forward" else []
        else:
            teardown_resolver(state_dir, use_sudo=use_sudo)
            if not dns_live:
                # A cold server-only restart needs no old host publication.
                # A live DNS instance keeps its bindings until its consumers
                # stop: removing OCI bindings would recreate their DNS server.
                state["resolver"], state["dns_port"] = None, None
            if (previous["environment"] is not None and current_environment is not None
                    and current_environment != previous["environment"] and previous["container_upstreams"]):
                # Re-read the restored/current resolver without taking it over.
                # Corefile reload preserves the live DNS container and bindings.
                fresh = resolver_plan(state_dir, backend="forward", use_sudo=use_sudo)
                state["container_upstreams"] = list(fresh["forwarders"])
                if state["resolver"] and state["resolver"]["backend"] == "forward":
                    state["resolver"] = {**state["resolver"], "forwarders": list(fresh["forwarders"])}
        state, routes, payloads = _dynamic_configuration(state, profiles, local)
        for key in ("access", "relay", "routes", "dns"):
            if paths[key].read_bytes() != payloads[key]:
                _atomic(paths[key], payloads[key], mode=0o644)
        _reconcile_containers(state, runtime)
        if before["relay"] != payloads["relay"]:
            runtime.signal_owned(state["containers"]["relay"], resource_labels(state), "SIGUSR2")
        _wait_ready(state, routes)
        ensure_ingress(
            state_dir, name="private", bind_address="127.0.0.1", port=state["http_port"],
            target_port=state["listen_port"], use_sudo=use_sudo,
            backend=state["ingress_backend"], infrastructure=state,
        )
        if host_dns:
            setup_resolver(state_dir, dns_port=state["dns_port"], use_sudo=use_sudo, plan=state["resolver"])
        _wait_ready(state, routes, port=state["http_port"])
        _verify_host_view(state, profiles)
        state["phase"] = "ready"
        _save(state_dir, state)
        committed = True
        retire_native_workers(source_dir, native if host_dns else {key: value for key, value in native.items() if key != "dns"})
        return _status(state, runtime, check=True)
    except BaseException as exc:
        if committed:
            raise PortmapError(f"shared infrastructure is active, but legacy worker cleanup needs attention: {exc}") from exc
        failures: list[str] = []
        if not old_resolver.get("installed") and host_dns:
            try:
                teardown_resolver(state_dir, use_sudo=use_sudo, plan=plan)
            except Exception as cleanup:
                # Do not remove the only live target of a resolver we could
                # not restore. Its exact resources and journal stay owned.
                _save(state_dir, state)
                raise PortmapError(f"{exc}; DNS restoration failed: {cleanup}; the owned container stack was retained") from exc
        try:
            for key, content in before.items():
                _atomic(paths[key], content, mode=0o644)
            # Leave compose.yaml at the attempted spec so reconciliation
            # restores the actual containers, not merely the file on disk.
            rollback["containers"] = {}
            _reconcile_containers(rollback, runtime)
            runtime.signal_owned(rollback["containers"]["relay"], resource_labels(rollback), "SIGUSR2")
            _wait_ready(rollback, old_routes)
            if old_resolver.get("installed"):
                prepare_resolver(state_dir, use_sudo=use_sudo, plan=old_plan)
                setup_resolver(state_dir, dns_port=old_plan["port"], use_sudo=use_sudo, plan=old_plan)
            elif rollback["host_dns_enabled"]:
                rollback["phase"] = "starting"
            _save(state_dir, rollback)
        except Exception as cleanup:
            failures.append(str(cleanup))
            state["phase"], state["containers"] = "starting", {}
            _save(state_dir, state)
        if failures:
            raise PortmapError(f"{exc}; shared configuration rollback needs attention: {'; '.join(failures)}") from exc
        raise


def sync_infrastructure(
    state_dir: Path, profiles: Mapping[str, dict[str, Any]], local: dict[str, Any] | None, *,
    settings: PortmapSettings | None = None, http_port: int | None = None,
    use_sudo: bool = True, legacy_state_dir: Path | None = None,
    host_dns: bool | None = None,
) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    effective = load_portmap_settings() if settings is None else settings
    if host_dns is not None and type(host_dns) is not bool:
        raise PortmapError("host DNS role must be explicitly enabled, disabled, or preserved")
    with _lock(state_dir):
        previous = load_infrastructure(state_dir)
        if previous is not None and settings is None:
            # Connection mutations use the established infrastructure identity,
            # not whichever project or mutable shell configuration invoked them.
            effective = replace(
                effective, runtime=previous["runtime"]["backend"],
                runtime_socket=previous["runtime"]["socket_path"],
                gateway_network=previous["network"]["name"], dns_domain=previous["domain"],
                http_port=previous["http_port"], dns_forward=previous["dns_forward"],
                state_dir=Path(previous["publishing_state_dir"]),
                resolver_backend=previous["resolver"]["backend"] if previous["resolver"] else effective.resolver_backend,
                ingress_backend=previous["ingress_backend"],
                agent_runtime_dir=Path(previous["agent_runtime_dir"]),
            )
        port = http_port if http_port is not None else (previous["http_port"] if previous else preferred_http_port(state_dir)) or effective.http_port
        if type(port) is not int or not 1 <= port <= 65535:
            raise PortmapError("local HTTP port must be an integer between 1 and 65535")
        runtime = ContainerRuntime.connect(effective)
        # Verify (and when needed repair) the runtime before any portmap-owned
        # container or network exists; every standard entry (setup, run, up)
        # converges here, so a rebuilt workspace that swapped the API service
        # back to incompatible helpers is repaired again -- never bypassed.
        prepare_runtime(runtime, state_dir)
        preserved_host_dns = bool(previous and previous["host_dns_enabled"])
        cold_public_dns = None
        changed = previous and (
            previous["runtime"] != runtime.record() or previous["network"]["name"] != effective.gateway_network
            or previous["domain"] != effective.dns_domain or previous["http_port"] != port
            or previous["agent_runtime_dir"] != str(effective.agent_runtime_dir.resolve())
            or previous["publishing_state_dir"] != str(effective.state_dir.resolve())
            or previous["dns_forward"] != effective.dns_forward
        )
        if changed:
            old_runtime = ContainerRuntime.from_record(previous["runtime"])
            if previous.get("phase") != "stopped" or old_runtime.network_containers(previous["network"]["id"]):
                raise PortmapError("existing infrastructure or business containers still depend on the saved runtime, network, domain and HTTP port; stop publishing and its dependent projects before changing these settings")
            if previous["network"]["created"]:
                old_runtime.remove_empty_network(previous["network"]["id"], resource_labels(previous))
            (state_dir / "infrastructure.json").unlink()
            previous = None
        if previous is not None:
            environment = resolver_environment()
            if (previous["environment"] is not None and environment is not None
                    and previous["environment"] != environment
                    and runtime.inspect_network(previous["network"]["id"]) is None):
                inventory = runtime.request("GET", "/containers/json?all=1")
                if not isinstance(inventory, list):
                    raise PortmapError("the runtime returned an invalid container inventory")
                labels = resource_labels(previous)
                for container in inventory:
                    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
                    if (all((container.get("Labels") or {}).get(key) == value for key, value in labels.items())
                            or previous["network"]["name"] in networks
                            or any(endpoint.get("NetworkID") == previous["network"]["id"] for endpoint in networks.values())):
                        raise PortmapError("the saved gateway network is missing but containers still reference this infrastructure; refusing to change their DNS targets")
                # The workspace generation and engine graph are both gone.
                # Keep old recovery metadata until a fresh stack is staged.
                cold_public_dns = copy.deepcopy(previous.get("public_dns"))
                previous = None
        source_dir = (legacy_state_dir or state_dir).expanduser().resolve()
        if previous and previous.get("legacy_state_dir"):
            source_dir = Path(previous["legacy_state_dir"])
        native = capture_native_workers(source_dir)
        views = _profile_views(profiles)
        old_resolver = resolver_status(source_dir)
        needs_host_dns = host_dns if host_dns is not None else bool(
            profiles or preserved_host_dns or old_resolver.get("installed")
        )
        transfer = source_dir != state_dir and needs_host_dns and not resolver_status(state_dir)["installed"]
        plan = resolver_plan(
            source_dir if transfer else state_dir, backend=effective.resolver_backend, use_sudo=use_sudo,
        ) if needs_host_dns else None
        if previous is not None:
            # Upgrade existing version-1 records through normal Compose reconciliation.
            previous["download_cache_dir"] = str(download_cache_dir(effective.state_dir).resolve())
            if effective.ingress_backend != "auto":
                previous["ingress_backend"] = effective.ingress_backend
        if previous and previous.get("phase") != "stopped":
            return _sync_existing(
                previous, state_dir, runtime, views, local, plan,
                host_dns=needs_host_dns, use_sudo=use_sudo, native=native, source_dir=source_dir,
            )
        old_plan = resolver_plan(source_dir, use_sudo=use_sudo) if old_resolver.get("installed") else None
        old_ingress = ingress_status(source_dir, name="private")
        prepared = prepare_resolver(source_dir if transfer else state_dir, use_sudo=use_sudo, plan=plan) if plan else {}
        state = previous
        switched_http = switched_dns = removed_old_link = removed_old_ingress = False
        committed = False
        try:
            if state is None:
                state = _new_state(state_dir, effective, runtime, port, native, plan)
                state["legacy_state_dir"] = str(source_dir)
                state["http_address"] = local.get("http_address") if local else None
                if cold_public_dns is not None:
                    state["public_dns"] = {**cold_public_dns, "address": state["addresses"]["dns"]}
                if source_dir != state_dir and effective.ingress_backend == "auto" and old_ingress.get("owned"):
                    state["ingress_backend"] = old_ingress.get("backend", "systemd")
                _seed(state, views, local, runtime)
            elif state.get("phase") == "stopped":
                state["phase"] = "starting"
                state["relay_ports"] = {}
                state["dns_port"] = _dns_port(native, plan) if plan else None
                state["resolver"] = {**plan, "port": state["dns_port"]} if plan else None
                state["host_dns_enabled"] = needs_host_dns
                state["container_upstreams"] = list(plan["forwarders"]) if plan and plan["backend"] == "forward" else []
                state["environment"] = resolver_environment()
                state["http_address"] = local.get("http_address") if local else None
                _seed(state, views, local, runtime)
            _save(state_dir, state)
            _reconcile_containers(state, runtime)
            state = _publish(state, views, local, runtime)
            _verify_pending_ssh_http(views, port=state["listen_port"])
            if transfer:
                teardown_resolver(source_dir, use_sudo=use_sudo)
                removed_old_link = True
                prepare_resolver(state_dir, use_sudo=use_sudo, plan=state["resolver"])
            if source_dir != state_dir and old_ingress.get("owned"):
                stop_ingress(source_dir, name="private", use_sudo=use_sudo)
                removed_old_ingress = True
            release_native_http(source_dir, native, port, use_sudo=use_sudo)
            ensure_ingress(
                state_dir, name="private", bind_address="127.0.0.1", port=port,
                target_port=state["listen_port"], use_sudo=use_sudo,
                backend=state["ingress_backend"], infrastructure=state,
            )
            switched_http = True
            if needs_host_dns:
                setup_resolver(state_dir, dns_port=state["dns_port"], use_sudo=use_sudo, plan=state["resolver"])
                switched_dns = True
            _wait_ready(state, json.loads(_configuration_paths(state)["routes"].read_bytes()), port=port)
            _verify_host_view(state, views)
            state["phase"] = "ready"
            _save(state_dir, state)
            committed = True
            retire_native_workers(source_dir, native if needs_host_dns else {key: value for key, value in native.items() if key != "dns"})
            return _status(state, runtime, check=True)
        except BaseException as exc:
            if committed:
                raise PortmapError(f"container infrastructure is active, but legacy worker cleanup needs attention: {exc}") from exc
            failures = []
            dns_cleanup_safe = True
            if switched_dns or removed_old_link or (needs_host_dns and state is not None and state["resolver"] and state["resolver"]["backend"] == "forward"):
                try:
                    if transfer:
                        teardown_resolver(state_dir, use_sudo=use_sudo)
                        if old_plan is not None:
                            prepare_resolver(source_dir, use_sudo=use_sudo, plan=old_plan)
                    if old_resolver.get("installed"):
                        setup_resolver(source_dir, dns_port=old_resolver["port"], use_sudo=use_sudo, plan=old_plan)
                    elif not transfer:
                        teardown_resolver(state_dir, use_sudo=use_sudo)
                except Exception as cleanup:
                    failures.append(str(cleanup))
                    dns_cleanup_safe = False
            if switched_http or removed_old_ingress:
                try:
                    if transfer or not old_ingress.get("owned"):
                        stop_ingress(state_dir, name="private", use_sudo=use_sudo)
                    if old_ingress.get("owned"):
                        ensure_ingress(
                            source_dir, name="private", bind_address="127.0.0.1",
                            port=old_ingress["port"], target_port=old_ingress["target_port"],
                            use_sudo=use_sudo, backend=old_ingress.get("backend", "auto"),
                        )
                except Exception as cleanup:
                    failures.append(str(cleanup))
            try:
                restore_native_http(source_dir, native)
            except Exception as cleanup:
                failures.append(str(cleanup))
            if state is not None and not dns_cleanup_safe:
                # Keep the DNS target alive if restoring the host resolver failed.
                # Removing it here could break unrelated name resolution.
                _save(state_dir, state)
                failures.append("DNS restoration failed; the owned container stack was retained")
            if state is not None and dns_cleanup_safe:
                try:
                    _remove_containers(state, runtime)
                    removed_network = state["network"]["created"] and runtime.remove_empty_network(state["network"]["id"], resource_labels(state))
                    if not failures:
                        if removed_network:
                            (state_dir / "infrastructure.json").unlink(missing_ok=True)
                        else:
                            state.update(phase="stopped", containers={})
                            _save(state_dir, state)
                except Exception as cleanup:
                    failures.append(str(cleanup))
            if dns_cleanup_safe and prepared.get("created") and not old_resolver.get("installed") and not transfer:
                try:
                    teardown_resolver(state_dir, use_sudo=use_sudo)
                except Exception as cleanup:
                    failures.append(str(cleanup))
            if failures:
                raise PortmapError(f"{exc}; owned infrastructure cleanup needs attention: {'; '.join(failures)}") from exc
            raise


def _remove_containers(state: dict[str, Any], runtime: ContainerRuntime) -> None:
    labels = resource_labels(state)
    # Includes partial Compose startup, but only exact owner+uid+state labels.
    for container in runtime.owned_containers(labels):
        runtime.remove_owned(container["Id"], labels)


def set_public_dns_bindings(state: dict[str, Any], bindings: dict[str, Any] | None) -> dict[str, Any]:
    """Transfer only legacy DNS consumers, keeping all other containers intact."""
    state_dir = Path(state["state_dir"])
    with _lock(state_dir):
        live = load_infrastructure(state_dir)
        if live is None or live["owner"] != state["owner"] or live["phase"] != "ready":
            raise PortmapError("DNS publication requires the current ready infrastructure owner")
        runtime = ContainerRuntime.from_record(live["runtime"])
        candidate = copy.deepcopy(live)
        candidate["public_dns"] = copy.deepcopy(bindings)
        desired_address = live["default_dns_address"]
        if bindings is not None:
            if bindings.get("version") != 1 or bindings.get("listen_port") != 1054:
                raise PortmapError("invalid legacy DNS publication descriptor")
            desired_address = bindings.get("address") or desired_address
        try:
            address = ipaddress.IPv4Address(desired_address)
        except ipaddress.AddressValueError as exc:
            raise PortmapError("legacy DNS container address is invalid") from exc
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise PortmapError("legacy DNS container address is not bridge-reachable")
        if str(address) in {value for role, value in live["addresses"].items() if role != "dns"}:
            raise PortmapError("legacy DNS address conflicts with another owned infrastructure role")
        candidate["addresses"]["dns"] = str(address)
        if candidate["public_dns"] == live.get("public_dns") and candidate["addresses"] == live["addresses"]:
            return live
        paths = _configuration_paths(live)
        snapshot = json.loads(paths["access"].read_bytes())
        local = snapshot["local"] if snapshot["local"]["enabled"] else None
        profiles = snapshot["machines"]
        candidate, _, payloads = _dynamic_configuration(candidate, profiles, local)
        compose_path = Path(live["instance_dir"]) / "compose.yaml"
        old_compose = compose_path.read_bytes()
        before = {key: path.read_bytes() for key, path in paths.items()}
        desired_compose = json.loads(old_compose)
        labels = resource_labels(live)
        current = runtime.require_owned(live["names"]["dns"], labels)
        if current is None or current["Id"] != live["containers"]["dns"]:
            raise PortmapError("DNS container identity changed before the publication handoff")
        mac = _container_mac(current, live["network"]["name"])
        desired_compose["services"]["dns"] = compose_config(
            candidate, runtime, mac_addresses={"dns": mac} if mac else None,
        )["services"]["dns"]

        def recreate(record: dict[str, Any]) -> None:
            existing = runtime.require_owned(record["names"]["dns"], labels)
            if existing is not None:
                runtime.remove_owned(existing["Id"], labels)
            runtime.compose(compose_path, ["up", "-d", "--no-deps", "dns"], capture=True)
            replacement = runtime.require_owned(record["names"]["dns"], labels)
            if replacement is None or not replacement.get("State", {}).get("Running"):
                raise PortmapError("replacement DNS container did not start")
            if container_address(replacement, record["network"]["name"]) != record["addresses"]["dns"]:
                raise PortmapError("replacement DNS did not retain the required business resolver address")
            record["containers"]["dns"] = replacement["Id"]
            _wait_ready(record, json.loads(paths["routes"].read_bytes()), port=record["http_port"])
            _verify_host_view(record, profiles)

        try:
            for key in ("access", "routes", "dns"):
                _atomic(paths[key], payloads[key], mode=0o644)
            _atomic(compose_path, json_bytes(desired_compose))
            recreate(candidate)
            _save(state_dir, candidate)
        except BaseException as exc:
            for key, content in before.items():
                _atomic(paths[key], content, mode=0o644)
            _atomic(compose_path, old_compose)
            try:
                recreate(live)
                _save(state_dir, live)
            except Exception as rollback:
                live["phase"] = "starting"
                _save(state_dir, live)
                raise PortmapError(f"DNS publication failed ({exc}); restoring the previous owned DNS container also failed: {rollback}") from exc
            raise
        return candidate


def stop_infrastructure(
    state_dir: Path, *, use_sudo: bool = True, local: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    if not state_dir.exists():
        return {"retained": False, "consumers": []}
    with _lock(state_dir):
        state = load_infrastructure(state_dir)
        if state is None:
            native = capture_native_workers(state_dir)
            teardown_resolver(state_dir, use_sudo=use_sudo)
            stop_ingress(state_dir, name="private", use_sudo=use_sudo)
            retire_native_workers(state_dir, native)
            return {"retained": False, "consumers": []}
        runtime = ContainerRuntime.from_record(state["runtime"])
        owned_ids = {entry["Id"] for entry in runtime.owned_containers(resource_labels(state))}
        for role in ("private", "public"):
            entry = ingress_status(state_dir, name=role)
            if entry.get("owned") and entry.get("container_id"):
                owned_ids.add(entry["container_id"])
        consumers = [
            entry["Id"] for entry in runtime.network_containers(state["network"]["id"])
            if entry["Id"] not in owned_ids
        ]
        if local is not None or consumers:
            shared = _sync_existing(
                state, state_dir, runtime, {}, local, None,
                host_dns=False, use_sudo=use_sudo,
                native=capture_native_workers(state_dir), source_dir=state_dir,
            )
            return {"retained": True, "consumers": consumers, "infrastructure": shared}
        # Failed resolver removal must retain its live DNS target.
        teardown_resolver(state_dir, use_sudo=use_sudo)
        stop_ingress(state_dir, name="private", use_sudo=use_sudo)
        _remove_containers(state, runtime)
        removed = state["network"]["created"] and runtime.remove_empty_network(state["network"]["id"], resource_labels(state))
        if removed:
            (state_dir / "infrastructure.json").unlink()
        else:
            # The network itself can be externally owned; do not remove it.
            state.update(phase="stopped", containers={}, host_dns_enabled=False, resolver=None, dns_port=None)
            _save(state_dir, state)
        return {"retained": False, "consumers": []}


def _status(state: dict[str, Any], runtime: ContainerRuntime, *, check: bool) -> dict[str, Any]:
    states = {}
    error = None
    private_ingress: dict[str, Any] = {"running": False}
    try:
        for role, identity in state["containers"].items():
            container = runtime.require_owned(identity, resource_labels(state))
            states[role] = bool(container and container.get("State", {}).get("Running"))
        private_ingress = ingress_status(Path(state["state_dir"]), name="private")
        running = (
            set(states) == set(IMAGES) and all(states.values())
            and state.get("phase") == "ready" and bool(private_ingress.get("running"))
        )
        if check and running:
            _health(state, json.loads(_configuration_paths(state)["routes"].read_bytes()), port=state["http_port"])
            snapshot = json.loads(_configuration_paths(state)["access"].read_bytes())
            _verify_host_view(state, snapshot["machines"])
    except (PortmapError, OSError, ValueError, http.client.HTTPException) as exc:
        running, error = False, str(exc)
    dns_running = False
    if states.get("dns", False) and state.get("phase") == "ready":
        try:
            expected = {"ok": True, "owner": state["owner"], "dns_marker": state["dns_marker"]}
            dns_running = _json_get(
                state["listen_port"], f"/__portmap_ready/{state['owner']}/{state['revision']}",
            ) == expected
        except (PortmapError, OSError, ValueError, http.client.HTTPException):
            pass
    host_enabled = state["host_dns_enabled"]
    plan = state["resolver"] if host_enabled else None
    result = {
        "configured": state["phase"] != "stopped", "running": running, "runtime": state["runtime"], "state_dir": state["state_dir"],
        "domain": state["domain"], "owner": state["owner"], "phase": state["phase"], "instance_dir": state["instance_dir"],
        "containers": {role: {"id": identity, "running": states.get(role, False)} for role, identity in state["containers"].items()},
        "dns": {
            "running": dns_running, "host_enabled": host_enabled,
            "address": plan["address"] if plan else None, "port": state["dns_port"] if plan else None,
            "container_address": state["addresses"].get("dns"), "backend": plan["backend"] if plan else None,
        },
        "gateway": {"running": running, "address": "127.0.0.1", "http_port": state["http_port"], "listen_port": state["listen_port"], "public_backend_port": state["public_backend_port"], "management_port": state["management_port"], "revision": state.get("revision"), "routes_loaded": running, "ingress": private_ingress},
        "resolver": resolver_status(Path(state["state_dir"])),
    }
    if error:
        result["error"] = error
    return result


def infrastructure_status(state_dir: Path, *, check: bool = False) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    try:
        state = load_infrastructure(state_dir)
        if state is not None:
            return _status(state, ContainerRuntime.from_record(state["runtime"]), check=check)
        return {"configured": False, "running": False, "state_dir": str(state_dir),
            "dns": {"running": False, "address": "127.0.0.1", "port": DNS_PORT},
            "gateway": {"running": False, "address": "127.0.0.1", "http_port": None},
            "resolver": resolver_status(state_dir),
            "migration_required": (state_dir / "gateway.json").exists() or (state_dir / "dns.json").exists()}
    except (PortmapError, OSError, ValueError) as exc:
        return {"configured": True, "running": False, "state_dir": str(state_dir), "error": str(exc),
            "dns": {"running": False}, "gateway": {"running": False}, "resolver": {"installed": False}}


def container_dns_address(state_dir: Path) -> str:
    state = load_infrastructure(state_dir.expanduser().resolve())
    if state is None or state.get("phase") != "ready":
        raise PortmapError("container DNS is not configured; run portmap setup before generating project overrides")
    publication_record = Path(state["publishing_state_dir"]) / "server" / "instance.json"
    try:
        publication = json.loads(publication_record.read_bytes())
    except FileNotFoundError:
        publication = None
    if publication and publication.get("version") == 1:
        raise PortmapError("local publishing migration is not complete; finish portmap setup before generating new project overrides")
    return state["addresses"]["dns"]
