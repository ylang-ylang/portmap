"""Unprivileged HAProxy container ingress published on an exact host address.

One independently owned container per ingress role (``private`` or ``public``)
runs the same haproxy image as the SSH relay, joins the existing gateway
bridge network, and forwards a single engine-published ``<host-ip>:<port>``
listener to the Traefik gateway container (``:80`` for private/access,
``:8080`` for public/web). No host networking, no wildcard bind, no
capabilities, and no systemd/sudo dependency: host port ownership is the
container engine's published-port machinery.

Ownership is expressed with dedicated ``portmap.ingress.*`` labels plus a
per-role state record, so the shared infrastructure inventory (which matches
the ``portmap.infrastructure.*`` labels) never adopts or removes these
containers, and foreign resources are never touched. A failed replacement
restores the previously owned container before raising.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import stat
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .container_runtime import ContainerRuntime
from .errors import PortAllocationError, PortmapError
from .permissions import ensure_private_file
from .infrastructure_config import IMAGES
from .ingress import _bind_address, _clear_state, _load_state, _role, _save_state

BACKEND = "container"
RECORD_VERSION = 2
INGRESS_LABEL_NAMESPACE = "portmap.ingress"
RELAY_IMAGE_KEY = "relay"
CONTAINER_LISTEN_PORT = 19080  # high container-side port; the host port stays authoritative
CONTAINER_CONFIG_PATH = "/usr/local/etc/haproxy/haproxy.cfg"

GATEWAY_TARGET_PORTS = {"private": 80, "public": 8080}

_START_TIMEOUT = 15.0
_CONNECT_TIMEOUT = 1.0
_POLL_INTERVAL = 0.1
_CONFLICT_PATTERN = re.compile(r"address already in use|port is already allocated", re.IGNORECASE)


# --- naming, labels, and configuration ---------------------------------------------


def container_name(state_dir: Path, role: str) -> str:
    """Deterministic container name for this user, state directory, and role."""
    digest = hashlib.sha256(str(Path(state_dir).resolve()).encode("utf-8")).hexdigest()[:8]
    return f"portmap-ingress-{_role(role)}-{os.getuid()}-{digest}"


def ingress_labels(state_dir: Path, role: str) -> dict[str, str]:
    """Ingress-specific ownership labels, disjoint from the infrastructure set."""
    return {
        INGRESS_LABEL_NAMESPACE: "true",
        f"{INGRESS_LABEL_NAMESPACE}.uid": str(os.getuid()),
        f"{INGRESS_LABEL_NAMESPACE}.role": _role(role),
        f"{INGRESS_LABEL_NAMESPACE}.state": str(Path(state_dir).resolve()),
    }


def haproxy_config(gateway: str, target_port: int, listen_port: int = CONTAINER_LISTEN_PORT) -> str:
    """Deterministic pure-TCP forward of the container port to the gateway."""
    return (
        "global\n"
        "    master-worker\n"
        "    log stdout format raw local0\n"
        "defaults\n"
        "    mode tcp\n"
        "    log global\n"
        "    option tcplog\n"
        "    timeout connect 5s\n"
        "    timeout client 0s\n"
        "    timeout server 0s\n"
        "frontend ingress\n"
        f"    bind *:{listen_port}\n"
        "    default_backend gateway\n"
        "backend gateway\n"
        f"    server gateway {gateway}:{target_port}\n"
    )


def _config_path(state_dir: Path, role: str) -> Path:
    return Path(state_dir) / "ingress" / f"{_role(role)}.cfg"


def _role_state_dir(state_dir: Path) -> str:
    return str(Path(state_dir).resolve())


def _engine(infrastructure: Mapping[str, Any] | None) -> tuple[ContainerRuntime, str, str, str]:
    if not isinstance(infrastructure, Mapping):
        raise PortmapError(
            "container ingress requires the shared container infrastructure state; "
            "run portmap setup first"
        )
    try:
        runtime = ContainerRuntime.from_record(infrastructure["runtime"])
        network = infrastructure["network"]["name"]
        gateway = infrastructure["addresses"]["gateway"]
    except (KeyError, TypeError, PortmapError) as exc:
        raise PortmapError("the infrastructure state does not describe the gateway network required by container ingress") from exc
    if not isinstance(network, str) or not network:
        raise PortmapError("the infrastructure state has no gateway network name for container ingress")
    try:
        address = ipaddress.ip_address(gateway)
    except ValueError as exc:
        raise PortmapError("the infrastructure gateway address is not a literal IP address") from exc
    if not isinstance(address, ipaddress.IPv4Address) or address.is_loopback or address.is_unspecified or address.is_multicast:
        raise PortmapError("the infrastructure gateway address is unusable for container ingress")
    image = IMAGES.get(RELAY_IMAGE_KEY)
    if not isinstance(image, str) or not image:
        raise PortmapError("the relay image required for container ingress is not configured")
    return runtime, network, str(address), image


def load_infrastructure_state(state_dir: Path) -> dict[str, Any] | None:
    """Validated infrastructure state for this directory, or None when absent."""
    if not (Path(state_dir) / "infrastructure.json").exists():
        return None
    from .infrastructure import load_infrastructure

    return load_infrastructure(Path(state_dir))


# --- persisted record ---------------------------------------------------------------


def _require_owned_record(state_dir: Path, role: str, record: Mapping[str, Any]) -> None:
    resolved = _role_state_dir(state_dir)
    problems: list[str] = []
    if record.get("backend") != BACKEND or record.get("version") != RECORD_VERSION:
        problems.append("not a container ingress record")
    if record.get("uid") != os.getuid():
        problems.append(f"owned by uid {record.get('uid')!r}, not this user")
    if record.get("state") != resolved:
        problems.append("recorded for a different state directory")
    if record.get("name") != role:
        problems.append(f"recorded for the {record.get('name')!r} role")
    if record.get("container") != container_name(state_dir, role):
        problems.append("recorded container name does not match this state directory")
    if record.get("labels") != ingress_labels(state_dir, role):
        problems.append("recorded labels do not match this state directory")
    if not isinstance(record.get("bind_address"), str):
        problems.append("invalid recorded bind_address")
    for key in ("port", "target_port", "container_port", "gateway_port"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            problems.append(f"invalid recorded {key}")
    if record.get("container_port") != CONTAINER_LISTEN_PORT or record.get("gateway_port") != GATEWAY_TARGET_PORTS[role]:
        problems.append("recorded listener/target does not match this ingress role")
    if not isinstance(record.get("container_id"), str) or not record["container_id"]:
        problems.append("invalid recorded container identity")
    if record.get("config") != str(_config_path(state_dir, role)):
        problems.append("recorded configuration path does not match this ingress role")
    try:
        _bind_address(role, record.get("bind_address"))
        address = ipaddress.ip_address(record.get("gateway_address"))
        if not isinstance(address, ipaddress.IPv4Address) or address.is_loopback or address.is_unspecified or address.is_multicast:
            problems.append("invalid recorded gateway address")
    except (ValueError, PortmapError):
        problems.append("invalid recorded ingress addresses")
    if not isinstance(record.get("gateway_address"), str) or not isinstance(record.get("network"), str):
        problems.append("invalid recorded forwarding target")
    if problems:
        raise PortmapError("refusing to use the recorded ingress: " + "; ".join(problems))


def _record_matches(
    record: Mapping[str, Any],
    *,
    container_id: str,
    bind: str,
    port: int,
    gateway: str,
    gateway_port: int,
    network: str,
    image: str,
    config_sha256: str,
) -> bool:
    return (
        record.get("container_id") == container_id
        and record.get("bind_address") == bind
        and record.get("port") == port
        and record.get("gateway_address") == gateway
        and record.get("gateway_port") == gateway_port
        and record.get("network") == network
        and record.get("image") == image
        and record.get("container_port") == CONTAINER_LISTEN_PORT
        and record.get("config_sha256") == config_sha256
    )


# --- host configuration file ----------------------------------------------------------


def _write_config(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        try:
            info = path.lstat()
        except OSError as exc:
            raise PortmapError(f"cannot inspect ingress configuration {path}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PortmapError(f"refusing to replace {path}: not a regular portmap ingress configuration file")
        if info.st_uid != os.getuid():
            raise PortmapError(f"refusing to replace {path}: owned by uid {info.st_uid}, not this user")
        if stat.S_IMODE(info.st_mode) & 0o022:
            ensure_private_file(path, message=f"refusing to replace {path}: group- or world-writable", strip=0o022)
    data = content.encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return hashlib.sha256(data).hexdigest()


def _config_intact(path: Path, content: str) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return False
    if stat.S_IMODE(info.st_mode) & 0o022:
        return False
    try:
        return path.read_bytes() == content.encode("utf-8")
    except OSError:
        return False


# --- engine operations -----------------------------------------------------------------


def _create_container(
    runtime: ContainerRuntime,
    *,
    name: str,
    image: str,
    labels: Mapping[str, str],
    bind: str,
    port: int,
    config_path: Path,
    network: str,
) -> str:
    payload = {
        "Image": image,
        "Cmd": ["haproxy", "-W", "-db", "-f", CONTAINER_CONFIG_PATH],
        "User": runtime.file_user,
        "Labels": dict(labels),
        "ExposedPorts": {f"{CONTAINER_LISTEN_PORT}/tcp": {}},
        "HostConfig": {
            "NetworkMode": network,
            # Exact host-IP publication to the high container port; the
            # engine owns the port, this process never binds it.
            "PortBindings": {f"{CONTAINER_LISTEN_PORT}/tcp": [{"HostIp": bind, "HostPort": str(port)}]},
            "Binds": [f"{config_path}:{CONTAINER_CONFIG_PATH}:ro"],
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "RestartPolicy": {"Name": "no"},
        },
        # Bridge attachment to the existing gateway network; never host mode.
        "NetworkingConfig": {"EndpointsConfig": {network: {}}},
    }
    result = runtime.request("POST", f"/containers/create?name={quote(name, safe='')}", payload, statuses=(201,))
    try:
        return result["Id"]
    except (TypeError, KeyError) as exc:
        raise PortmapError("the container runtime returned an invalid container creation response") from exc


def _start_container(runtime: ContainerRuntime, container_id: str, *, bind: str, port: int) -> None:
    try:
        runtime.request("POST", f"/containers/{quote(container_id, safe='')}/start", None, statuses=(204, 304))
    except PortmapError as exc:
        if _CONFLICT_PATTERN.search(str(exc)):
            raise PortAllocationError(
                f"another listener already uses {bind}:{port}; portmap never stops unrelated services "
                "or moves the requested port. Stop the current owner of that port or choose a different port."
            ) from exc
        raise


def _connectable(bind: str, port: int) -> bool:
    try:
        with socket.create_connection((bind, port), timeout=_CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def _wait_ready(runtime: ContainerRuntime, container_id: str, *, bind: str, port: int) -> None:
    deadline = time.monotonic() + _START_TIMEOUT
    while True:
        container = runtime.inspect_container(container_id)
        if container is None:
            raise PortmapError(f"the owned ingress container {container_id[:12]} disappeared while starting")
        state = container.get("State") or {}
        if state.get("Running"):
            if _connectable(bind, port):
                return
            detail = "the published port does not accept connections yet"
        elif state.get("Status") in {"exited", "dead"} or state.get("ExitCode") not in (None, 0):
            detail = f"the container exited during startup (status={state.get('Status')}, exit={state.get('ExitCode')}"
            if state.get("Error"):
                detail += f", error={state['Error']}"
            detail += "); inspect it with the container runtime logs"
        else:
            detail = f"the container is {state.get('Status') or 'not running'}"
        if time.monotonic() >= deadline:
            raise PortmapError(f"container ingress did not become ready on {bind}:{port}: {detail}")
        time.sleep(_POLL_INTERVAL)


def _published(container: Mapping[str, Any], *, bind: str, port: int, container_port: int) -> bool:
    bindings = ((container.get("NetworkSettings") or {}).get("Ports") or {}).get(f"{container_port}/tcp")
    return isinstance(bindings, list) and {"HostIp": bind, "HostPort": str(port)} in bindings


# --- rollback ---------------------------------------------------------------------------


def _restore_previous_container(
    state_dir: Path, role: str, previous: Mapping[str, Any], runtime: ContainerRuntime, notes: list[str]
) -> None:
    try:
        labels = ingress_labels(state_dir, role)
        name = container_name(state_dir, role)
        stale = runtime.require_owned(name, labels)
        if stale is not None:
            runtime.remove_owned(stale["Id"], labels)
        config_path = _config_path(state_dir, role)
        _write_config(
            config_path, haproxy_config(previous["gateway_address"], previous["gateway_port"], previous["container_port"])
        )
        container_id = _create_container(
            runtime, name=name, image=previous["image"], labels=labels,
            bind=previous["bind_address"], port=previous["port"],
            config_path=config_path, network=previous["network"],
        )
        _start_container(runtime, container_id, bind=previous["bind_address"], port=previous["port"])
        _wait_ready(runtime, container_id, bind=previous["bind_address"], port=previous["port"])
        restored = dict(previous)
        restored["container_id"] = container_id
        restored["runtime"] = runtime.record()
        _save_state(state_dir, role, restored)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never masked
        notes.append(f"could not restore the previous ingress container: {exc}")


def _remove_created(state_dir: Path, role: str, runtime: ContainerRuntime, notes: list[str]) -> None:
    labels = ingress_labels(state_dir, role)
    try:
        stale = runtime.require_owned(container_name(state_dir, role), labels)
        if stale is not None:
            runtime.remove_owned(stale["Id"], labels)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"remove container: {exc}")
    try:
        _config_path(state_dir, role).unlink(missing_ok=True)
    except OSError as exc:
        notes.append(f"remove configuration: {exc}")
    _clear_state(state_dir, role)


# --- public interface --------------------------------------------------------------------


def ensure_ingress_container(
    state_dir: Path,
    *,
    name: str,
    bind_address: str,
    port: int,
    target_port: int,
    infrastructure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update the owned HAProxy ingress container and verify it serves.

    Never invokes sudo, systemctl, or host networking. A failed replacement
    restores the previously owned container before raising; foreign resources
    are always refused, never stopped.
    """
    role = _role(name)
    state_dir = Path(state_dir)
    if infrastructure is None:
        infrastructure = load_infrastructure_state(state_dir)
    runtime, network, gateway, image = _engine(infrastructure)
    gateway_port = GATEWAY_TARGET_PORTS[role]
    labels = ingress_labels(state_dir, role)
    container = container_name(state_dir, role)
    record = _load_state(state_dir, role)
    previous: dict[str, Any] | None = None
    if record is not None and record.get("backend") == BACKEND:
        _require_owned_record(state_dir, role, record)
        previous = record
    config_path = _config_path(state_dir, role)
    config = haproxy_config(gateway, gateway_port)
    config_sha256 = hashlib.sha256(config.encode("utf-8")).hexdigest()
    existing = runtime.require_owned(container, labels)
    if existing is not None and (previous is None or existing.get("Id") != previous.get("container_id")):
        raise PortmapError("refusing to replace an ingress container without its matching ownership record")
    owns_binding = bool(
        existing is not None and (existing.get("State") or {}).get("Running")
        and _published(existing, bind=bind_address, port=port, container_port=CONTAINER_LISTEN_PORT)
    )
    if not owns_binding and _connectable(bind_address, port):
        raise PortAllocationError(
            f"{bind_address}:{port} is already served by another listener; portmap never stops unrelated services"
        )
    if (
        previous is not None
        and existing is not None
        and existing.get("Id") == previous.get("container_id")
        and (existing.get("State") or {}).get("Running")
        and _published(existing, bind=bind_address, port=port, container_port=CONTAINER_LISTEN_PORT)
        and _record_matches(
            previous, container_id=existing["Id"], bind=bind_address, port=port,
            gateway=gateway, gateway_port=gateway_port, network=network,
            image=image, config_sha256=config_sha256,
        )
        and _config_intact(config_path, config)
        and _connectable(bind_address, port)
    ):
        status = ingress_status_container(state_dir, name=role)
        status["changed"] = False
        return status
    notes: list[str] = []
    try:
        if existing is not None:
            runtime.remove_owned(existing["Id"], labels)
        _write_config(config_path, config)
        container_id = _create_container(
            runtime, name=container, image=image, labels=labels,
            bind=bind_address, port=port, config_path=config_path, network=network,
        )
        _start_container(runtime, container_id, bind=bind_address, port=port)
        _wait_ready(runtime, container_id, bind=bind_address, port=port)
        _save_state(
            state_dir,
            role,
            {
                "version": RECORD_VERSION,
                "backend": BACKEND,
                "name": role,
                "bind_address": bind_address,
                "port": port,
                "target_port": target_port,
                "container_port": CONTAINER_LISTEN_PORT,
                "container": container,
                "container_id": container_id,
                "image": image,
                "labels": labels,
                "config": str(config_path),
                "config_sha256": config_sha256,
                "network": network,
                "gateway_address": gateway,
                "gateway_port": gateway_port,
                "runtime": runtime.record(),
                "uid": os.getuid(),
                "state": _role_state_dir(state_dir),
            },
        )
    except BaseException as exc:
        if previous is not None:
            _restore_previous_container(state_dir, role, previous, runtime, notes)
        else:
            _remove_created(state_dir, role, runtime, notes)
        if notes:
            raise PortmapError(f"{exc}; ingress rollback also failed: {'; '.join(notes)}") from exc
        raise
    status = ingress_status_container(state_dir, name=role)
    status["changed"] = True
    return status


def ingress_status_container(state_dir: Path, *, name: str) -> dict[str, Any]:
    """Read-only report of the container ingress for this state directory and role."""
    role = _role(name)
    status: dict[str, Any] = {
        "name": role,
        "backend": BACKEND,
        "socket_unit": None,
        "service_unit": None,
        "installed": False,
        "owned": False,
        "running": False,
        "bind_address": None,
        "port": None,
        "target_port": None,
        "socket_state": None,
        "service_state": None,
        "enabled": False,
        "container": None,
        "container_state": None,
        "error": None,
    }
    record = _load_state(Path(state_dir), role)
    if record is None or record.get("backend") != BACKEND:
        return status
    status["bind_address"] = record.get("bind_address")
    status["port"] = record.get("port")
    status["target_port"] = record.get("target_port")
    status["container"] = record.get("container")
    try:
        _require_owned_record(Path(state_dir), role, record)
    except PortmapError as exc:
        status["error"] = str(exc)
        return status
    runtime = ContainerRuntime.from_record(record["runtime"])
    container = runtime.require_owned(record["container"], record["labels"])
    if container is None:
        status["error"] = f"the recorded ingress container {record['container']} is missing"
        return status
    if container.get("Id") != record["container_id"]:
        status["error"] = "the recorded ingress container was replaced outside portmap"
        return status
    status["installed"] = True
    status["container_id"] = container["Id"]
    status["owned"] = True
    state = container.get("State") or {}
    status["container_state"] = state.get("Status")
    status["running"] = bool(
        state.get("Running")
        and _published(
            container,
            bind=record["bind_address"],
            port=record["port"],
            container_port=record["container_port"],
        )
    )
    return status


def stop_ingress_container(state_dir: Path, *, name: str) -> dict[str, Any]:
    """Remove only the ingress container validated as owned by this state directory."""
    role = _role(name)
    state_dir = Path(state_dir)
    result: dict[str, Any] = {
        "name": role,
        "backend": BACKEND,
        "socket_unit": None,
        "service_unit": None,
        "installed": False,
        "running": False,
        "stopped": True,
        "removed": False,
        "container": None,
    }
    record = _load_state(state_dir, role)
    if record is None or record.get("backend") != BACKEND:
        return result
    _require_owned_record(state_dir, role, record)
    result["container"] = record.get("container")
    runtime = ContainerRuntime.from_record(record["runtime"])
    labels = ingress_labels(state_dir, role)
    container = runtime.require_owned(record["container"], labels)
    if container is not None:
        if container.get("Id") != record["container_id"]:
            raise PortmapError("refusing to remove an ingress container replaced outside portmap")
        result["installed"] = True
        runtime.remove_owned(container["Id"], labels)
    try:
        _config_path(state_dir, role).unlink(missing_ok=True)
    except OSError as exc:
        raise PortmapError(f"could not remove the ingress configuration: {exc}") from exc
    _clear_state(state_dir, role)
    result["removed"] = True
    return result
