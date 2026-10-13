"""Ownership-validated capture, cutover and retirement of the legacy server stack.

The legacy local-publishing role ran a second Docker Compose stack next to the
shared container infrastructure: Compose project ``portmap`` with the services
``catalog``/``traefik``/``dns``, container names ``portmap-<service>``, and the
Compose-generated project/service/config-root labels plus the catalog's
``portmap.gateway`` label. This module never starts or restarts that stack as
a fallback. :func:`capture_legacy_server` records the exact identity of the
old resources from the version-1 ``server/instance.json`` ownership record
plus the running containers' label identity -- a familiar container name is
never sufficient -- and the caller proves the replacement backend before
:func:`stop_legacy_stack`, :func:`restore_legacy_stack` and
:func:`retire_legacy_server` change anything.

Business containers, business networks (including the shared gateway network
the legacy stack created), the host agent, and the legacy Compose project's
unexpected services are never removed here.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .container_runtime import ContainerRuntime
from .errors import PortmapError
from .ingress import ingress_status
from .settings import PortmapSettings

LEGACY_VERSION = 1
LEGACY_PROJECT = "portmap"
LEGACY_SERVICES = ("catalog", "traefik", "dns")
_PUBLIC_DNS_LISTEN_PORT = 1054
_MAX_COREFILE = 256 * 1024
_INGRESS_NAME = "public"
_PROJECT_LABEL = "com.docker.compose.project"
_SERVICE_LABEL = "com.docker.compose.service"
_WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
_CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"


def _record_path(settings: PortmapSettings) -> Path:
    return settings.state_dir / "server" / "instance.json"


def _safe_record(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PortmapError(f"cannot inspect legacy server state {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PortmapError(f"unsafe legacy server state file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PortmapError(f"cannot read legacy server state {path}: {exc}") from exc


def load_legacy_record(settings: PortmapSettings) -> dict[str, Any] | None:
    """Return the validated version-1 server ownership record, or None.

    A version-2 record (the shared-infrastructure role) is not legacy state
    and yields None. A present-but-invalid version-1 record raises
    ``PortmapError`` so uncertain ownership is never silently discarded.
    """
    path = _record_path(settings)
    try:
        raw = _safe_record(path)
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise PortmapError(f"invalid legacy server state: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") == 2:
        return None
    try:
        if (data.get("version") != LEGACY_VERSION or data.get("uid") != os.getuid()
                or data.get("state_dir") != str(settings.state_dir.expanduser().resolve())
                or not isinstance(data.get("settings"), dict)):
            raise ValueError("invalid owner or schema")
        port = data["backend_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("invalid backend port")
        if not Path(data["client_state_dir"]).is_absolute():
            raise ValueError("invalid client state directory")
        root = data["settings"].get("root")
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise ValueError("invalid recorded configuration root")
    except (ValueError, TypeError, KeyError) as exc:
        raise PortmapError(f"invalid legacy server state: {path}: {exc}") from exc
    return data


# --- migration bookkeeping ---------------------------------------------------------------


def migration_path(state_dir: Path) -> Path:
    return Path(state_dir) / "server" / "migration.json"


def load_migration(state_dir: Path) -> dict[str, Any] | None:
    """Retained metadata about a failed legacy cutover, for safe recovery."""
    path = migration_path(state_dir)
    try:
        raw = _safe_record(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PortmapError(f"cannot read legacy migration metadata {path}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise PortmapError(f"invalid legacy migration metadata {path}: {exc}") from exc
    if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("record"), dict)
            or data.get("uid") != os.getuid() or data.get("state_dir") != str(Path(state_dir).expanduser().resolve())
            or not isinstance(data.get("phase"), str) or not isinstance(data.get("details"), dict)):
        raise PortmapError(f"invalid legacy migration metadata {path}")
    return data


def save_migration(state_dir: Path, *, record: Mapping[str, Any], phase: str, error: str, details: Mapping[str, Any] | None = None) -> None:
    """Persist the captured legacy identity and failure for a later retry."""
    directory = Path(state_dir) / "server"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "version": 1,
        "uid": os.getuid(),
        "state_dir": str(Path(state_dir).expanduser().resolve()),
        "phase": phase,
        "record": dict(record),
        "details": dict(details or {}),
        "error": error,
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }
    path = migration_path(state_dir)
    fd, name = tempfile.mkstemp(prefix=".migration-", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def clear_migration(state_dir: Path) -> None:
    migration_path(state_dir).unlink(missing_ok=True)


# --- Corefile parsing --------------------------------------------------------------------


def _environment(settings: PortmapSettings) -> dict[str, str]:
    """The legacy compose environment values the old Corefile substituted."""
    return {
        "PORTMAP_DNS_DOMAIN": settings.dns_domain,
        "PORTMAP_DNS_TARGET_IP": settings.host_ip,
        "PORTMAP_DNS_FORWARD": settings.dns_forward,
    }


def _parse_corefile(content: str, environment: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Split a CoreDNS Corefile into per-zone template/hosts/forward facts."""
    for name, value in environment.items():
        content = content.replace("{$" + name + "}", value)
    zones: dict[str, dict[str, Any]] = {}
    zone: str | None = None
    depth = 0
    in_hosts = False
    for line in content.splitlines():
        text = line.strip()
        if zone is None:
            match = re.fullmatch(r"([^\s{]+):\d+\s*\{", text)
            if match:
                zone = match.group(1)
                zones[zone] = {"records": {}, "templates": [], "forward": None}
                depth = 1
            continue
        if text == "}":
            depth -= 1
            if depth <= 1:
                in_hosts = False
            if depth == 0:
                zone = None
            continue
        if text.endswith("{"):
            depth += 1
            in_hosts = text == "hosts {"
            continue
        if depth == 1 and text.startswith("forward . "):
            value = text[len("forward . "):].strip()
            zones[zone]["forward"] = value
            continue
        if in_hosts and text and not text.startswith("#"):
            fields = text.split()
            if len(fields) >= 2:
                try:
                    address = str(ipaddress.ip_address(fields[0]))
                except ValueError:
                    continue
                for name in fields[1:]:
                    zones[zone]["records"][name.rstrip(".")] = address
            continue
        match = re.search(r'answer\s+"(.*)"', text)
        if match:
            target = re.search(r"IN A (\S+)", match.group(1))
            if target:
                value = target.group(1)
                zones[zone]["templates"].append(value)
    return zones


def _corefile_zone(root: Path, settings: PortmapSettings) -> dict[str, Any] | None:
    """Exact host bindings of the legacy zone, read from the old Corefile."""
    path = root / "gateway" / "Corefile"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise PortmapError(f"legacy DNS configuration is not a file owned by this user: {path}")
    if metadata.st_size > _MAX_COREFILE:
        raise PortmapError(f"legacy DNS configuration is implausibly large: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PortmapError(f"cannot read legacy DNS configuration {path}: {exc}") from exc
    zones = _parse_corefile(content, _environment(settings))
    zone = zones.get(settings.dns_domain)
    if zone is None:
        return None
    wildcard = zone["templates"][0] if zone["templates"] else None
    if wildcard is not None:
        try:
            wildcard = str(ipaddress.ip_address(wildcard))
        except ValueError as exc:
            raise PortmapError(f"legacy DNS wildcard answer is not an address: {wildcard}") from exc
    forward = zone["forward"] or zones.get(".", {}).get("forward")
    return {"records": zone["records"], "wildcard": wildcard, "forward": forward,
            "corefile": str(path), "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}


# --- capture -----------------------------------------------------------------------------


def _compose_files(root: Path, labels: Mapping[str, Any]) -> None:
    recorded = (labels.get(_CONFIG_FILES_LABEL) or "").split(",")
    files = [Path(item) for item in recorded if item]
    base = root / "docker-compose.yml"
    if not any(file == base for file in files):
        raise PortmapError(f"legacy Compose labels do not reference the recorded configuration root {root}")
    for file in files:
        if not file.is_absolute() or not file.resolve().is_relative_to(root):
            raise PortmapError(f"legacy Compose configuration file is outside the recorded root: {file}")


def _validate_service(item: Mapping[str, Any], root: Path) -> str:
    labels = item.get("Labels") or {}
    service = labels.get(_SERVICE_LABEL)
    if service not in LEGACY_SERVICES:
        raise PortmapError(f"the legacy Compose project contains an unexpected service {service!r}")
    names = [name.lstrip("/") for name in item.get("Names") or []]
    if f"{LEGACY_PROJECT}-{service}" not in names:
        raise PortmapError(f"container labels claim legacy service {service!r} but its name is {names}")
    if labels.get(_PROJECT_LABEL) != LEGACY_PROJECT:
        raise PortmapError("legacy container does not carry the recorded Compose project label")
    if labels.get(_WORKING_DIR_LABEL) != str(root):
        raise PortmapError(f"legacy Compose working directory is not the recorded configuration root {root}")
    _compose_files(root, labels)
    if service == "catalog" and labels.get("portmap.gateway") != "true":
        raise PortmapError("the legacy catalog container lacks its portmap ownership label")
    return service


def _ownership_labels(labels: Mapping[str, Any]) -> dict[str, str]:
    keys = (_PROJECT_LABEL, _SERVICE_LABEL, _WORKING_DIR_LABEL, _CONFIG_FILES_LABEL)
    ownership = {key: str(labels.get(key, "")) for key in keys}
    if labels.get(_SERVICE_LABEL) == "catalog":
        ownership["portmap.gateway"] = str(labels.get("portmap.gateway", ""))
    return ownership


def _container_entry(container: Mapping[str, Any], service: str, settings: PortmapSettings) -> dict[str, Any]:
    labels = container.get("Config", {}).get("Labels") or {}
    network = (container.get("NetworkSettings", {}).get("Networks") or {}).get(settings.gateway_network) or {}
    address = network.get("IPAddress") or None
    if address is not None:
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError as exc:
            raise PortmapError(f"legacy {service} container has an invalid gateway network address") from exc
    publish: dict[str, list[dict[str, str]]] = {}
    for binding, entries in (container.get("NetworkSettings", {}).get("Ports") or {}).items():
        hosts = [{"address": entry.get("HostIp", ""), "port": entry.get("HostPort", "")} for entry in entries or []]
        if hosts:
            publish[str(binding)] = hosts
    return {
        "service": service,
        "id": container["Id"],
        "name": f"{LEGACY_PROJECT}-{service}",
        "running": bool(container.get("State", {}).get("Running")),
        "labels": _ownership_labels(labels),
        "network": {"name": settings.gateway_network, "address": address},
        "publish": publish,
    }


def capture_legacy_server(settings: PortmapSettings, record: Mapping[str, Any], runtime: ContainerRuntime) -> dict[str, Any]:
    """Capture the exact identity of the legacy server stack without stopping it.

    ``record`` is the validated version-1 ownership record. The recorded
    configuration root, Compose project/service/working-dir/config-files
    labels, the catalog's ``portmap.gateway`` label and every container id are
    captured so later steps can revalidate ownership before each mutation.
    Absent services are captured as ``None``; the network, foreign Compose
    services and business containers are only recorded, never changed.
    """
    root = Path(record["settings"]["root"])
    for key, current in (("dns_domain", settings.dns_domain), ("gateway_network", settings.gateway_network),
                         ("runtime_socket", settings.runtime_socket)):
        saved = record["settings"].get(key)
        if saved != current:
            raise PortmapError(
                f"the legacy server record was written for {key}={saved!r} but the current configuration is {current!r}; "
                "keep the publishing configuration unchanged across the migration or remove the legacy state manually after switching"
            )
    listed = runtime.owned_containers({_PROJECT_LABEL: LEGACY_PROJECT})
    by_service: dict[str, dict[str, Any]] = {}
    foreign: list[str] = []
    for item in listed:
        labels = item.get("Labels") or {}
        if labels.get(_WORKING_DIR_LABEL) != str(root):
            foreign.append((item.get("Names") or ["?"])[0].lstrip("/"))
            continue
        service = _validate_service(item, root)
        container = runtime.inspect_container(item["Id"])
        if container is not None:
            by_service[service] = _container_entry(container, service, settings)
    zone = _corefile_zone(root, settings)
    ingress = None
    report = ingress_status(Path(record["client_state_dir"]), name=_INGRESS_NAME)
    if report.get("owned") and report.get("bind_address") and report.get("port"):
        ingress = {"bind_address": report["bind_address"], "port": int(report["port"]),
                   "target_port": int(report["target_port"] or record["backend_port"])}
    return {
        "version": 1,
        "uid": os.getuid(),
        "state_dir": str(settings.state_dir.expanduser().resolve()),
        "root": str(root),
        "domain": settings.dns_domain,
        "network": settings.gateway_network,
        "backend_port": record["backend_port"],
        "client_state_dir": record["client_state_dir"],
        "containers": {service: by_service.get(service) for service in LEGACY_SERVICES},
        "foreign_services": sorted(foreign),
        "zone": zone,
        "ingress": ingress,
    }


def public_dns_bindings(saved: PortmapSettings, capture: Mapping[str, Any]) -> dict[str, Any] | None:
    """Bindings payload for the controller's ``set_public_dns_bindings``.

    Returns None when the legacy DNS container had no published host binding
    and no zone answers to preserve. ``address`` is the legacy DNS container's
    address on the gateway network and is the required destination address for
    the recreated DNS container: business containers may have their engine DNS
    setting pinned to exactly that address. ``publish`` keeps the exact legacy
    host-side binding and ``forward`` keeps the legacy upstream forwarder so
    non-portmap business name resolution survives the cutover. The public-only
    view on the internal port 1054 resolves only the legacy zone answers and
    never remote SSH or private names.
    """
    dns = capture["containers"].get("dns")
    zone = capture.get("zone")
    publish = None
    if dns is not None:
        bindings: set[tuple[str, int]] = set()
        for container_port, hosts in (dns.get("publish") or {}).items():
            if not str(container_port).startswith("53/"):
                continue
            for host in hosts:
                try:
                    port = int(host["port"])
                except (TypeError, ValueError):
                    continue
                if 1 <= port <= 65535:
                    bindings.add((host["address"] or "0.0.0.0", port))
        if len(bindings) > 1:
            raise PortmapError(f"the legacy DNS container published several host bindings: {sorted(bindings)}")
        if bindings:
            address, port = bindings.pop()
            try:
                address = str(ipaddress.ip_address(address))
            except ValueError:
                pass
            publish = {"address": address, "port": port}
    records = dict(zone["records"]) if zone else {}
    wildcard = zone.get("wildcard") if zone else None
    if dns is not None and not dns.get("network", {}).get("address"):
        raise PortmapError("cannot preserve the legacy DNS container's resolver address; restore its gateway-network attachment before migrating")
    if publish is None and not records and wildcard is None:
        return None
    return {
        "version": 1,
        "zone": capture["domain"],
        "wildcard": wildcard,
        "records": records,
        "forward": zone.get("forward") if zone else None,
        "address": (dns or {}).get("network", {}).get("address"),
        "listen_port": _PUBLIC_DNS_LISTEN_PORT,
        "publish": publish,
    }


# --- stop / restore / retire -------------------------------------------------------------


def _require_captured(snapshot: Mapping[str, Any]) -> None:
    if (snapshot.get("version") != 1 or snapshot.get("uid") != os.getuid()
            or snapshot.get("state_dir") != str(Path(snapshot["state_dir"]).expanduser().resolve())
            or not isinstance(snapshot.get("containers"), dict)
            or snapshot.get("domain") is None):
        raise PortmapError("legacy capture snapshot does not match this state directory; capture the legacy server first")


def _validated_container(runtime: ContainerRuntime, entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Reinspect a captured container and require its exact captured identity."""
    container = runtime.inspect_container(entry["id"])
    if container is None:
        return None
    if container.get("Id") != entry["id"]:
        raise PortmapError("the container runtime returned a different container for the captured id")
    labels = container.get("Config", {}).get("Labels") or {}
    if _ownership_labels(labels) != entry["labels"]:
        raise PortmapError(f"refusing to change legacy container {entry['name']}: its Compose ownership labels changed since capture")
    return container


def _entries(snapshot: Mapping[str, Any], order: tuple[str, ...] = LEGACY_SERVICES) -> list[dict[str, Any]]:
    return [entry for service in order if (entry := snapshot["containers"].get(service)) is not None]


def stop_legacy_stack(snapshot: Mapping[str, Any], runtime: ContainerRuntime) -> list[str]:
    """Validate the whole captured set before stopping any legacy container.

    Returns the services that were actually stopped. Containers that already
    stopped or disappeared are skipped; the shared gateway network, foreign
    services and business containers are never touched.
    """
    _require_captured(snapshot)
    entries = _entries(snapshot)
    for entry in entries:
        _validated_container(runtime, entry)
    stopped: list[str] = []
    for entry in entries:
        container = _validated_container(runtime, entry)
        if container is None or not container.get("State", {}).get("Running"):
            continue
        runtime.request("POST", f"/containers/{entry['id']}/stop", None, statuses=(204, 304))
        stopped.append(entry["service"])
    return stopped


def restore_legacy_stack(snapshot: Mapping[str, Any], runtime: ContainerRuntime) -> list[str]:
    """Start again the captured containers that were running when captured."""
    _require_captured(snapshot)
    started: list[str] = []
    for entry in _entries(snapshot, tuple(reversed(LEGACY_SERVICES))):
        if not entry.get("running"):
            continue
        container = _validated_container(runtime, entry)
        if container is None:
            raise PortmapError(f"the captured legacy container {entry['name']} disappeared; its state is retained for manual recovery")
        if container.get("State", {}).get("Running"):
            continue
        runtime.request("POST", f"/containers/{entry['id']}/start", None, statuses=(204, 304))
        started.append(entry["service"])
    return started


def retire_legacy_server(settings: PortmapSettings, record: Mapping[str, Any], snapshot: Mapping[str, Any], runtime: ContainerRuntime) -> None:
    """Remove the captured legacy containers and the version-1 record.

    Only the three captured legacy services are removed, each revalidated
    against its captured Compose identity. The gateway network (shared with
    business containers), foreign Compose services and all user containers are
    left in place, as are the legacy configuration files under the recorded
    root. Missing containers and an absent record make this a no-op, so an
    interrupted retirement can be completed by calling this again.
    """
    _require_captured(snapshot)
    for entry in _entries(snapshot):
        container = _validated_container(runtime, entry)
        if container is not None:
            runtime.request("DELETE", f"/containers/{entry['id']}?force=1&v=0", None, statuses=(204, 404), timeout=30)
    current = load_legacy_record(settings)
    if current is not None:
        if current != record:
            raise PortmapError("legacy server ownership changed before retirement")
        _record_path(settings).unlink(missing_ok=True)


# --- verification ------------------------------------------------------------------------


def _skip_name(packet: bytes, position: int) -> int:
    while True:
        length = packet[position]
        if length == 0:
            return position + 1
        if length & 0xC0 == 0xC0:
            return position + 2
        position += 1 + length


def resolve_a(address: str, port: int, name: str, *, timeout: float = 1.0, attempts: int = 3) -> list[str] | None:
    """Ask a DNS listener for the A records of ``name`` over UDP.

    Returns None when no bounded response arrives; transport errors are never
    retried beyond ``attempts`` and never fall back to another resolver.
    """
    ident = secrets.randbelow(65536)
    question = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split(".")) + b"\x00"
    request = struct.pack("!6H", ident, 0x0100, 1, 0, 0, 0) + question + struct.pack("!HH", 1, 1)
    for _ in range(attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.settimeout(timeout)
                probe.connect((str(address), int(port)))
                probe.send(request)
                packet = probe.recv(4096)
        except OSError:
            continue
        try:
            header = struct.unpack_from("!6H", packet)
            _, flags, _, answers = header[0], header[1], header[2], header[3]
            if header[0] != ident or not flags & 0x8000:
                continue
            position = _skip_name(packet, 12) + 4
            found: list[str] = []
            for _index in range(answers):
                position = _skip_name(packet, position)
                kind, _, _, length = struct.unpack_from("!HHIH", packet, position)
                position += 10
                if kind == 1 and length == 4:
                    found.append(str(ipaddress.IPv4Address(packet[position:position + 4])))
                position += length
            return found
        except (struct.error, ValueError, IndexError):
            continue
    return None


def verify_public_dns_bindings(bindings: Mapping[str, Any]) -> None:
    """Verify the preserved public-only DNS binding after it is applied."""
    publish = bindings.get("publish")
    if not publish:
        return
    zone = bindings["zone"]
    expected: str | None = None
    if bindings.get("wildcard"):
        name, expected = f"portmap-migration-check.{zone}", bindings["wildcard"]
    elif bindings.get("records"):
        name, expected = next(iter(bindings["records"].items()))
    else:
        name = f"portmap-migration-check.{zone}"
    answers = resolve_a(publish["address"], publish["port"], name)
    if answers is None:
        raise PortmapError(f"the preserved DNS binding {publish['address']}:{publish['port']} stopped answering for {zone}")
    if expected is not None and expected not in answers:
        raise PortmapError(f"the preserved DNS binding {publish['address']}:{publish['port']} answers {name} with {answers}, not the legacy {expected}")
