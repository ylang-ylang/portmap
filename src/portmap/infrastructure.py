"""Owned container infrastructure, staged before resolver and HTTP cutover."""
from __future__ import annotations

import fcntl
import copy
import http.client
import ipaddress
import json
import os
import secrets
import socket
import stat
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Mapping

from .client_dns import DNS_ADDRESS, DNS_PORT, prepare_resolver, probe_marker, render_corefile, resolver_status, setup_resolver, teardown_resolver, verify_resolution
from .client_discovery import probe_gateway
from .client_tunnel import http_socket_dir, tunnel_alive
from .container_runtime import ContainerRuntime, container_address
from .downloads import download_cache_dir
from .errors import PortmapError
from .ingress import ensure_ingress, ingress_status, stop_ingress
from .infrastructure_config import HEALTH_HOST, IMAGES, access_snapshot, compose_config, dns_config, json_bytes, relay_config, relay_ports, resource_labels, routes_config, static_config
from .legacy_native import capture_native_workers, release_native_http, restore_native_http, retire_native_workers
from .settings import PortmapSettings, load_portmap_settings

_START_TIMEOUT = 25.0
_MAX_JSON = 4 * 1024 * 1024


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PortmapError(f"infrastructure directory is not private and owned by this user: {path}")


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
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PortmapError("unsafe infrastructure ownership record")
    try:
        state = json.loads(path.read_text())
        if (state["version"] != 1 or state["uid"] != os.getuid() or state["state_dir"] != str(state_dir.resolve())
                or len(state["owner"]) != 32 or any(character not in "0123456789abcdef" for character in state["owner"])
                or Path(state["instance_dir"]) != state_dir.resolve() / "infrastructure" / state["owner"]):
            raise ValueError("owner mismatch")
        ContainerRuntime.from_record(state["runtime"])
        for key in ("http_port", "listen_port", "public_backend_port", "management_port", "dns_port"):
            if type(state[key]) is not int or not 1 <= state[key] <= 65535:
                raise ValueError("invalid listener port")
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


def _dns_port(native: dict[str, Any]) -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            tcp.bind((DNS_ADDRESS, DNS_PORT))
            udp.bind((DNS_ADDRESS, DNS_PORT))
        return DNS_PORT
    except OSError as exc:
        if not native.get("dns") or native["dns"].get("state") != "owned":
            raise PortmapError(f"DNS listener {DNS_ADDRESS}:{DNS_PORT} is already occupied; refusing to replace an unrelated service") from exc
        # An owned native server stays live until the replacement proves its
        # own high-port listener and the suffix route is switched explicitly.
        return _free_port(DNS_ADDRESS, udp=True)


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


def _new_state(state_dir: Path, settings: PortmapSettings, runtime: ContainerRuntime, http_port: int, native: dict[str, Any]) -> dict[str, Any]:
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
    state = {
        "version": 1, "phase": "starting", "owner": owner, "uid": os.getuid(), "state_dir": str(state_dir),
        "instance_dir": str(directory), "runtime": runtime.record(), "project": prefix,
        "names": {role: f"{prefix}-{role}" for role in IMAGES}, "containers": {}, "addresses": {},
        "domain": settings.dns_domain, "http_port": http_port, "dns_port": _dns_port(native),
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


def _seed(state: dict[str, Any], profiles: Mapping[str, dict[str, Any]], local: dict[str, Any] | None, runtime: ContainerRuntime) -> None:
    directory = Path(state["instance_dir"])
    dormant = {domain: {**profile, "available": False} for domain, profile in profiles.items()}
    snapshot = access_snapshot(state, dormant, local)
    state["revision"], routes = routes_config(state, snapshot)
    # HAProxy can ignore reload signals while its first worker initializes.
    # Start that worker with the final private relay endpoints, not an empty set.
    state["relay_ports"] = relay_ports(profiles, state["relay_ports"])
    state["relay_revision"], bridge = relay_config(state, profiles)
    marker = f"{state['owner']}:starting"
    # No DNS route points at this staging listener yet.
    content = render_corefile({}, {}, marker, forward=state["dns_forward"])
    paths = _configuration_paths(state)
    for key, value in {"routes": json_bytes(routes), "dns": content.encode(), "access": json_bytes(snapshot), "relay": bridge.encode()}.items():
        _atomic(paths[key], value, mode=0o644)
    _atomic(directory / "config" / "traefik.yaml", json_bytes(static_config(state)), mode=0o644)
    _atomic(directory / "compose.yaml", json_bytes(compose_config(state, runtime)))


def _observe_containers(state: dict[str, Any], runtime: ContainerRuntime) -> None:
    labels = resource_labels(state)
    containers, addresses = {}, {}
    for role, name in state["names"].items():
        container = runtime.require_owned(name, labels)
        if container is None or not container.get("State", {}).get("Running"):
            raise PortmapError(f"the owned {role} container is not running; inspect portmap gateway logs")
        recorded = state["containers"].get(role)
        if recorded is not None and recorded != container["Id"]:
            raise PortmapError(f"the owned {role} container was replaced outside this controller; rerun setup to reconcile it")
        containers[role] = container["Id"]
        addresses[role] = container_address(container, state["network"]["name"])
    state["containers"], state["addresses"] = containers, addresses


def _reconcile_containers(state: dict[str, Any], runtime: ContainerRuntime) -> None:
    # Create the bind source as the invoking user, never via the engine.
    Path(state["download_cache_dir"]).mkdir(parents=True, exist_ok=True)
    directory = Path(state["instance_dir"])
    desired = json_bytes(compose_config(state, runtime))
    path = directory / "compose.yaml"
    current = path.read_bytes()
    inactive = False
    for role, name in state["names"].items():
        container = runtime.require_owned(name, resource_labels(state))
        if container is None or not container.get("State", {}).get("Running"):
            inactive = True
        elif container_address(container, state["network"]["name"]) != state["addresses"][role]:
            raise PortmapError(f"the owned {role} container moved off its reserved address; refusing to change business DNS targets")
    if current != desired or inactive:
        _atomic(directory / "config" / "traefik.yaml", json_bytes(static_config(state)), mode=0o644)
        _atomic(path, desired)
        runtime.compose(path, ["up", "-d"], capture=True)
        state["containers"] = {}
    _observe_containers(state, runtime)


def _verify_host_view(state: dict[str, Any], profiles: Mapping[str, dict[str, Any]]) -> None:
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
    if probe_marker(DNS_ADDRESS, state["dns_port"], timeout=0.4) != state["dns_marker"]:
        raise PortmapError("container DNS has not loaded the requested hostname mappings")
    if not _routes_loaded(_json_get(state["management_port"], "/api/rawdata"), routes):
        raise PortmapError("Traefik has not enabled the requested route revision")
    target = state["listen_port"] if port is None else port
    if _json_get(target, f"/__portmap_ready/{state['owner']}/{state['revision']}") != {"ok": True, "owner": state["owner"]}:
        raise PortmapError("private catalog ownership readiness failed")
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


def _publish(state: dict[str, Any], profiles: Mapping[str, dict[str, Any]], local: dict[str, Any] | None, runtime: ContainerRuntime) -> dict[str, Any]:
    if any(profile.get("via") == "direct" and ipaddress.ip_address(profile["address"]).version == 6 for profile in profiles.values()):
        network = runtime.inspect_network(state["network"]["id"])
        if not network or not network.get("EnableIPv6"):
            raise PortmapError(
                f"IPv6 direct access requires an IPv6-enabled gateway bridge; existing network "
                f"{state['network']['name']!r} was left unchanged. Use an IPv6-enabled gateway network "
                "or connect explicitly via SSH."
            )
    candidate = {**state, "relay_ports": relay_ports(profiles, state["relay_ports"])}
    snapshot = access_snapshot(candidate, profiles, local)
    candidate["relay_revision"], bridge = relay_config(candidate, profiles)
    candidate["dns_marker"], corefile = dns_config(candidate, profiles)
    candidate["revision"], routes = routes_config(candidate, snapshot)
    paths = _configuration_paths(state)
    previous = {key: path.read_bytes() for key, path in paths.items()}
    payloads = {"access": json_bytes(snapshot), "relay": bridge.encode(), "routes": json_bytes(routes), "dns": corefile.encode()}
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


def sync_infrastructure(
    state_dir: Path, profiles: Mapping[str, dict[str, Any]], local: dict[str, Any] | None, *,
    settings: PortmapSettings | None = None, http_port: int | None = None,
    use_sudo: bool = True, legacy_state_dir: Path | None = None,
) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    effective = load_portmap_settings() if settings is None else settings
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
                agent_runtime_dir=Path(previous["agent_runtime_dir"]),
            )
        port = http_port if http_port is not None else (previous["http_port"] if previous else preferred_http_port(state_dir)) or effective.http_port
        if type(port) is not int or not 1 <= port <= 65535:
            raise PortmapError("local HTTP port must be an integer between 1 and 65535")
        runtime = ContainerRuntime.connect(effective)
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
        source_dir = (legacy_state_dir or state_dir).expanduser().resolve()
        if previous and previous.get("legacy_state_dir"):
            source_dir = Path(previous["legacy_state_dir"])
        native = capture_native_workers(source_dir)
        views = _profile_views(profiles)
        if previous is not None:
            # Upgrade existing version-1 records through normal Compose reconciliation.
            previous["download_cache_dir"] = str(download_cache_dir(effective.state_dir).resolve())
        if previous and previous.get("phase") == "ready":
            prepare_resolver(state_dir, use_sudo=use_sudo)
            _reconcile_containers(previous, runtime)
            # A recreated relay must finish startup before receiving a reload.
            _wait_ready(previous, json.loads(_configuration_paths(previous)["routes"].read_bytes()))
            ensure_ingress(state_dir, name="private", bind_address="127.0.0.1", port=port, target_port=previous["listen_port"], use_sudo=use_sudo)
            setup_resolver(state_dir, dns_port=previous["dns_port"], use_sudo=use_sudo)
            state = _publish(previous, views, local, runtime)
            _save(state_dir, state)
            retire_native_workers(source_dir, native)
            return _status(state, runtime, check=True)
        old_resolver = resolver_status(source_dir)
        old_ingress = ingress_status(source_dir, name="private")
        transfer = source_dir != state_dir and not resolver_status(state_dir)["installed"]
        prepared = prepare_resolver(source_dir if transfer else state_dir, use_sudo=use_sudo)
        state = previous
        switched_http = switched_dns = removed_old_link = removed_old_ingress = False
        committed = False
        try:
            if state is None:
                state = _new_state(state_dir, effective, runtime, port, native)
                state["legacy_state_dir"] = str(source_dir)
                _seed(state, views, local, runtime)
            elif state.get("phase") == "stopped":
                state["phase"] = "starting"
                state["relay_ports"] = {}
                _seed(state, views, local, runtime)
            _save(state_dir, state)
            _reconcile_containers(state, runtime)
            state = _publish(state, views, local, runtime)
            _verify_pending_ssh_http(views, port=state["listen_port"])
            if transfer:
                teardown_resolver(source_dir, use_sudo=use_sudo)
                removed_old_link = True
                prepare_resolver(state_dir, use_sudo=use_sudo)
                if old_ingress.get("owned"):
                    stop_ingress(source_dir, name="private", use_sudo=use_sudo)
                    removed_old_ingress = True
            release_native_http(source_dir, native, port, use_sudo=use_sudo)
            ensure_ingress(state_dir, name="private", bind_address="127.0.0.1", port=port, target_port=state["listen_port"], use_sudo=use_sudo)
            switched_http = True
            setup_resolver(state_dir, dns_port=state["dns_port"], use_sudo=use_sudo)
            switched_dns = True
            _wait_ready(state, json.loads(_configuration_paths(state)["routes"].read_bytes()), port=port)
            _verify_host_view(state, views)
            state["phase"] = "ready"
            _save(state_dir, state)
            committed = True
            retire_native_workers(source_dir, native)
            return _status(state, runtime, check=True)
        except BaseException as exc:
            if committed:
                raise PortmapError(f"container infrastructure is active, but legacy worker cleanup needs attention: {exc}") from exc
            failures = []
            if switched_dns or removed_old_link:
                try:
                    if transfer:
                        teardown_resolver(state_dir, use_sudo=use_sudo)
                        prepare_resolver(source_dir, use_sudo=use_sudo)
                    if old_resolver.get("installed"):
                        setup_resolver(source_dir, dns_port=old_resolver["port"], use_sudo=use_sudo)
                    elif not transfer:
                        teardown_resolver(state_dir, use_sudo=use_sudo)
                except Exception as cleanup:
                    failures.append(str(cleanup))
            if switched_http or removed_old_ingress:
                try:
                    if transfer or not old_ingress.get("owned"):
                        stop_ingress(state_dir, name="private", use_sudo=use_sudo)
                    if old_ingress.get("owned"):
                        ensure_ingress(source_dir, name="private", bind_address="127.0.0.1", port=old_ingress["port"], target_port=old_ingress["target_port"], use_sudo=use_sudo)
                except Exception as cleanup:
                    failures.append(str(cleanup))
            try:
                restore_native_http(source_dir, native)
            except Exception as cleanup:
                failures.append(str(cleanup))
            if state is not None:
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
            if prepared.get("created") and not old_resolver.get("installed") and not transfer:
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
        profiles = json.loads(paths["access"].read_bytes())["machines"]
        candidate["dns_marker"], content = dns_config(candidate, profiles)
        compose_path = Path(live["instance_dir"]) / "compose.yaml"
        old_compose, old_corefile = compose_path.read_bytes(), paths["dns"].read_bytes()
        desired_compose = json.loads(old_compose)
        desired_compose["services"]["dns"] = compose_config(candidate, runtime)["services"]["dns"]
        labels = resource_labels(live)
        current = runtime.require_owned(live["names"]["dns"], labels)
        if current is None or current["Id"] != live["containers"]["dns"]:
            raise PortmapError("DNS container identity changed before the publication handoff")

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
            _atomic(paths["dns"], content.encode(), mode=0o644)
            _atomic(compose_path, json_bytes(desired_compose))
            recreate(candidate)
            _save(state_dir, candidate)
        except BaseException as exc:
            _atomic(paths["dns"], old_corefile, mode=0o644)
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


def stop_infrastructure(state_dir: Path, *, use_sudo: bool = True) -> None:
    state_dir = state_dir.expanduser().resolve()
    if not state_dir.exists():
        return
    with _lock(state_dir):
        state = load_infrastructure(state_dir)
        if state is None:
            native = capture_native_workers(state_dir)
            teardown_resolver(state_dir, use_sudo=use_sudo)
            stop_ingress(state_dir, name="private", use_sudo=use_sudo)
            retire_native_workers(state_dir, native)
            return
        runtime = ContainerRuntime.from_record(state["runtime"])
        # Failed resolver removal must retain its live DNS target.
        teardown_resolver(state_dir, use_sudo=use_sudo)
        stop_ingress(state_dir, name="private", use_sudo=use_sudo)
        _remove_containers(state, runtime)
        removed = state["network"]["created"] and runtime.remove_empty_network(state["network"]["id"], resource_labels(state))
        if removed:
            (state_dir / "infrastructure.json").unlink()
        else:
            # Retain fixed DNS addresses for running AND stopped business
            # containers; teardown never removes their network underneath them.
            state.update(phase="stopped", containers={})
            _save(state_dir, state)


def _status(state: dict[str, Any], runtime: ContainerRuntime, *, check: bool) -> dict[str, Any]:
    states = {}
    error = None
    try:
        for role, identity in state["containers"].items():
            container = runtime.require_owned(identity, resource_labels(state))
            states[role] = bool(container and container.get("State", {}).get("Running"))
        running = set(states) == set(IMAGES) and all(states.values()) and state.get("phase") == "ready"
        if check and running:
            _health(state, json.loads(_configuration_paths(state)["routes"].read_bytes()), port=state["http_port"])
            snapshot = json.loads(_configuration_paths(state)["access"].read_bytes())
            _verify_host_view(state, snapshot["machines"])
    except (PortmapError, OSError, ValueError, http.client.HTTPException) as exc:
        running, error = False, str(exc)
    dns_running = states.get("dns", False) and probe_marker(DNS_ADDRESS, state["dns_port"], timeout=0.3) == state.get("dns_marker")
    result = {
        "configured": state["phase"] != "stopped", "running": running, "runtime": state["runtime"], "state_dir": state["state_dir"],
        "domain": state["domain"], "owner": state["owner"], "phase": state["phase"], "instance_dir": state["instance_dir"],
        "containers": {role: {"id": identity, "running": states.get(role, False)} for role, identity in state["containers"].items()},
        "dns": {"running": dns_running, "address": DNS_ADDRESS, "port": state["dns_port"], "container_address": state["addresses"].get("dns")},
        "gateway": {"running": running, "address": "127.0.0.1", "http_port": state["http_port"], "listen_port": state["listen_port"], "public_backend_port": state["public_backend_port"], "management_port": state["management_port"], "revision": state.get("revision"), "routes_loaded": running, "ingress": ingress_status(Path(state["state_dir"]), name="private")},
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
            "dns": {"running": False, "address": DNS_ADDRESS, "port": DNS_PORT},
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
