"""Read-only machine access snapshot for the container-hosted catalog.

The controller publishes one atomic JSON document (``PORTMAP_ACCESS_CONFIG``,
mounted read-only at ``/config/catalog-access.json``) describing the local
machine and every saved remote connection. The catalog re-reads it for each
request, never mutates it, and fails closed when it is missing or invalid:
there is no local-only mode without controller-owned access data. Remote
backend origins come exclusively from this snapshot, never from request
input, and no private descriptor (backend URLs, tunnel details, or sockets)
is exposed through HTTP responses.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PortmapError


ACCESS_CONFIG_ENV = "PORTMAP_ACCESS_CONFIG"
DEFAULT_ACCESS_CONFIG_PATH = "/config/catalog-access.json"
LOCAL_MACHINE_ID = "local"
MAX_ACCESS_CONFIG_BYTES = 1024 * 1024

_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.IGNORECASE)


@dataclass(frozen=True)
class LocalAccess:
    domain: str | None
    http_port: int | None
    enabled: bool


@dataclass(frozen=True)
class MachineAccess:
    """One saved remote machine; the backend origin is controller data."""

    id: str
    domain: str
    via: str
    target: str | None
    address: str | None
    http_port: int
    remote_http_port: int | None
    backend_url: str | None
    tcp_address: str | None
    tcp_ports: tuple[int, ...]
    include_tcp: bool
    available: bool

    def profile(self) -> dict[str, Any]:
        """The projection profile consumed by client_view.project_catalog."""
        return {
            "domain": self.domain,
            "via": self.via,
            "target": self.target,
            "address": self.address,
            "tcp_address": self.tcp_address,
            "tcp_ports": list(self.tcp_ports),
        }


@dataclass(frozen=True)
class AccessSnapshot:
    owner: str | None
    dns_address: str | None
    dns_port: int | None
    local: LocalAccess
    machines: dict[str, MachineAccess]
    # Appended (defaulted) DNS contract fields: a legacy snapshot without
    # host DNS state reads as host-enabled with no container probe metadata,
    # and positional callers keep working.
    dns_host_enabled: bool = True
    dns_container_address: str | None = None
    dns_marker: str | None = None


def load_access_snapshot(environ: dict[str, str] | None = None) -> AccessSnapshot:
    """Read the atomic controller snapshot, failing closed.

    A missing, unreadable, or invalid snapshot raises PortmapError: machine
    selection and local actions must fail before mutation rather than serve
    or mutate local projects without controller-owned access data.
    """
    env = os.environ if environ is None else environ
    try:
        payload = Path(env.get(ACCESS_CONFIG_ENV, DEFAULT_ACCESS_CONFIG_PATH)).read_bytes()
    except OSError as exc:
        raise PortmapError("catalog access snapshot is unavailable") from exc
    if len(payload) > MAX_ACCESS_CONFIG_BYTES:
        raise PortmapError("catalog access snapshot is invalid")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as exc:
        raise PortmapError("catalog access snapshot is invalid") from exc
    return parse_access_snapshot(document)


def parse_access_snapshot(document: Any) -> AccessSnapshot:
    """Validate one controller snapshot document, rejecting aliasing keys."""
    if not isinstance(document, dict):
        raise PortmapError("access snapshot must be an object")
    version = document.get("version")
    if type(version) is not int or version != 1:
        raise PortmapError("unsupported access snapshot version")
    owner = document.get("owner")
    if owner is not None and (not isinstance(owner, str) or not owner.strip()):
        raise PortmapError("access snapshot owner must be a non-empty string")

    dns = document.get("dns")
    dns_address: str | None = None
    dns_port: int | None = None
    dns_host_enabled = True
    dns_container_address: str | None = None
    dns_marker: str | None = None
    if dns is not None:
        if not isinstance(dns, dict):
            raise PortmapError("access snapshot dns must be an object")
        host_enabled = dns.get("host_enabled", True)
        if type(host_enabled) is not bool:
            raise PortmapError("access snapshot dns host_enabled must be a boolean")
        dns_host_enabled = host_enabled
        # Host-enabled snapshots must carry the published host endpoint;
        # a disabled one must omit it so a stale endpoint is never read.
        dns_address = _optional_ip_address(dns.get("address"))
        dns_port = _optional_snapshot_port(dns.get("port"))
        if dns_host_enabled:
            if dns_address is None or dns_port is None:
                raise PortmapError("access snapshot dns requires address and port while host DNS is enabled")
        elif dns_address is not None or dns_port is not None:
            raise PortmapError("access snapshot dns must omit the host endpoint while host DNS is disabled")
        dns_container_address = _optional_ip_address(dns.get("container_address"))
        dns_marker = _optional_text(dns.get("marker"))

    local_document = document.get("local")
    if local_document is None:
        local = LocalAccess(domain=None, http_port=None, enabled=False)
    else:
        if not isinstance(local_document, dict):
            raise PortmapError("access snapshot local must be an object")
        domain = local_document.get("domain")
        if domain is not None:
            domain = _domain_name(domain)
        http_port = local_document.get("http_port")
        if http_port is not None:
            http_port = _snapshot_port(http_port)
        enabled = local_document.get("enabled", False)
        if type(enabled) is not bool:
            raise PortmapError("access snapshot local enabled must be a boolean")
        local = LocalAccess(domain=domain, http_port=http_port, enabled=enabled)

    machines_document = document.get("machines")
    if machines_document is None:
        machines_document = {}
    if not isinstance(machines_document, dict):
        raise PortmapError("access snapshot machines must be an object")
    machines: dict[str, MachineAccess] = {}
    for machine_id, entry in machines_document.items():
        machine = _machine(machine_id, entry)
        if machine.domain == local.domain:
            raise PortmapError("access snapshot machine collides with the local domain")
        machines[machine.id] = machine
    return AccessSnapshot(
        owner=owner if isinstance(owner, str) else None,
        dns_address=dns_address,
        dns_port=dns_port,
        local=local,
        machines=machines,
        dns_host_enabled=dns_host_enabled,
        dns_container_address=dns_container_address,
        dns_marker=dns_marker,
    )


def host_selected_machine_id(snapshot: AccessSnapshot, host: str | None) -> str:
    """The machine id a request Host selects; local when it matches nothing."""
    if host:
        if snapshot.local.domain == host:
            return LOCAL_MACHINE_ID
        machine = snapshot.machines.get(host)
        if machine is not None:
            return machine.id
    return LOCAL_MACHINE_ID


def local_descriptor(snapshot: AccessSnapshot) -> dict[str, Any]:
    """Public view of the local machine; no controller-only fields."""
    domain = snapshot.local.domain
    return {
        "id": LOCAL_MACHINE_ID,
        "label": domain.split(".")[0] if domain else LOCAL_MACHINE_ID,
        "domain": domain,
        "target": LOCAL_MACHINE_ID,
        "via": "local",
        "catalog_url": _catalog_url(domain, snapshot.local.http_port),
        "available": True,
        "publishing": snapshot.local.enabled,
    }


def machine_descriptor(machine: MachineAccess) -> dict[str, Any]:
    """Public view of one saved remote machine; never the backend origin."""
    return {
        "id": machine.id,
        "label": machine.domain.split(".")[0],
        "domain": machine.domain,
        "target": machine.target,
        "via": machine.via,
        "catalog_url": _catalog_url(machine.domain, machine.http_port),
        "available": machine.available,
    }


def machines_payload(snapshot: AccessSnapshot, *, selected: str, private: bool) -> dict[str, Any]:
    machines = [local_descriptor(snapshot)]
    if private:
        machines.extend(machine_descriptor(machine) for machine in snapshot.machines.values())
    return {"machines": machines, "selected": selected}


def _catalog_url(domain: str | None, http_port: int | None) -> str:
    if not domain:
        return "http://localhost/"
    authority = domain if http_port in (None, 80) else f"{domain}:{http_port}"
    return f"http://{authority}/"


def _machine(machine_id: Any, entry: Any) -> MachineAccess:
    if not isinstance(machine_id, str) or not _dns_name(machine_id):
        raise PortmapError("access snapshot machine ids must be DNS domains")
    if not isinstance(entry, dict):
        raise PortmapError("access snapshot machine entries must be objects")
    domain = _domain_name(entry.get("domain"))
    if domain != machine_id:
        raise PortmapError("access snapshot machine id must match its domain")
    via = entry.get("via")
    if via not in {"direct", "ssh"}:
        raise PortmapError("access snapshot machine via must be direct or ssh")
    http_port = _snapshot_port(entry.get("http_port"))
    remote_http_port = entry.get("remote_http_port")
    if remote_http_port is not None:
        remote_http_port = _snapshot_port(remote_http_port)
    available = entry.get("available")
    if type(available) is not bool:
        raise PortmapError("access snapshot machine availability must be a boolean")
    tcp_ports_document = entry.get("tcp_ports")
    if tcp_ports_document is None:
        tcp_ports_document = []
    if not isinstance(tcp_ports_document, list):
        raise PortmapError("access snapshot machine tcp_ports must be an array")
    include_tcp = entry.get("include_tcp", False)
    if type(include_tcp) is not bool:
        raise PortmapError("access snapshot machine include_tcp must be a boolean")
    backend_url = entry.get("backend_url")
    # Saved-but-inactive machines may omit the backend origin, but an
    # available machine must carry one for probing and action forwarding.
    if backend_url is not None or available:
        backend_url = _backend_url(backend_url)
    return MachineAccess(
        id=machine_id,
        domain=domain,
        via=via,
        target=_optional_text(entry.get("target")),
        address=_optional_text(entry.get("address")),
        http_port=http_port,
        remote_http_port=remote_http_port,
        backend_url=backend_url,
        tcp_address=_optional_text(entry.get("tcp_address")),
        tcp_ports=tuple(_snapshot_port(port) for port in tcp_ports_document),
        include_tcp=include_tcp,
        available=available,
    )


def _backend_url(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise PortmapError("access snapshot machine backend_url must be a plain HTTP origin")
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PortmapError("access snapshot machine backend_url has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.netloc.endswith(":")
        or not parsed.hostname
    ):
        raise PortmapError("access snapshot machine backend_url must be a plain HTTP origin")
    if port is not None:
        _snapshot_port(port)
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_unspecified or address.is_multicast):
        raise PortmapError("access snapshot machine backend_url must be a usable address")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise PortmapError("access snapshot text fields must be non-empty strings")
    return value


def _domain_name(value: Any) -> str:
    if not isinstance(value, str) or not _dns_name(value):
        raise PortmapError("access snapshot domains must be DNS names")
    return value


def _dns_name(value: str) -> bool:
    return len(value) <= 253 and all(_DNS_LABEL.fullmatch(label) for label in value.split("."))


def _ip_address(value: Any) -> str:
    if not isinstance(value, str):
        raise PortmapError("access snapshot dns address must be an IP address")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise PortmapError("access snapshot dns address must be an IP address") from exc


def _snapshot_port(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise PortmapError("access snapshot ports must be integers from 1 to 65535")
    return value


def _optional_ip_address(value: Any) -> str | None:
    if value is None:
        return None
    return _ip_address(value)


def _optional_snapshot_port(value: Any) -> int | None:
    if value is None:
        return None
    return _snapshot_port(value)
