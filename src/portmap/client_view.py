"""Shared catalog projection helpers for client-side endpoint views.

The loopback HTTP worker that used to live here is gone: the controller owns
connection state and the container-hosted catalog serves projected remote
registries itself. These pure functions remain the single definition of how
a catalog is presented for direct, SSH, and local access.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .errors import PortmapError


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.IGNORECASE)
_AUTHORITY = re.compile(r"([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?\Z")


def _port(value: object) -> bool:
    return type(value) is int and 1 <= value <= 65535


def _dns_name(value: str) -> bool:
    return len(value) <= 253 and all(_DNS_LABEL.fullmatch(label) for label in value.split("."))


def request_host(value: str) -> str | None:
    """The bare lowercase hostname of one Host header value, if usable."""
    match = _AUTHORITY.fullmatch(value)
    if match is None or not _dns_name(match[1]):
        return None
    if match[2] is not None and not _port(int(match[2])):
        return None
    return match[1].lower()


def _managed_host(host: str, domain: str) -> bool:
    host = host.lower()
    return _dns_name(host) and (host == domain or host.endswith("." + domain))


def _project_endpoint(endpoint: dict[str, Any], profile: dict[str, Any], http_port: int) -> dict[str, Any]:
    result = dict(endpoint)
    kind = endpoint.get("kind")
    via = profile["via"]
    reason = "endpoint kind is not supported by this client"
    if kind == "http":
        try:
            url = endpoint.get("url")
            if not isinstance(url, str) or any(character.isspace() or ord(character) < 32 for character in url):
                raise ValueError("invalid URL")
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            original_port = parsed.port
            domain = profile["domain"]
            declared_host = endpoint.get("host")
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.netloc.endswith(":")
                or original_port is not None and not _port(original_port)
                or not _managed_host(host, domain)
                or declared_host is not None and (
                    not isinstance(declared_host, str) or declared_host.lower() != host.lower()
                )
            ):
                raise ValueError("unmanaged URL")
            if host.lower() == domain and via == "ssh":
                # Only the SSH aggregate reserves the apex for its projected
                # catalog view; direct traffic reaches the remote gateway and
                # the local role proxies its whole domain to its backend.
                result["transport_supported"] = False
                result["reason"] = "the apex hostname is reserved for the local catalog; use an application subdomain"
                return result
            if via == "direct":
                # DNS maps the domain straight to the verified remote IP, so
                # the remote catalog URL (host and port) is already reachable.
                # Nothing is rewritten for direct connections.
                pass
            else:
                # SSH/local: retain the URL's host spelling and path, but use
                # the shared local HTTP listener rather than a remote port or
                # an unavailable TLS listener.
                original_host = parsed.netloc.rsplit(":", 1)[0] if original_port is not None else parsed.netloc
                authority = original_host if http_port == 80 else f"{original_host}:{http_port}"
                result["url"] = urlunsplit(("http", authority, parsed.path, parsed.query, parsed.fragment))
                result["host_port"] = http_port
        except ValueError:
            reason = "HTTP endpoint does not use a valid host in this managed DNS zone"
        else:
            result["transport_supported"] = True
            result.pop("reason", None)
            return result
    elif kind in ("tcp", "udp", "range"):
        port = endpoint.get("host_port")
        has_range = kind == "range" or endpoint.get("range_start") is not None or endpoint.get("range_end") is not None
        if via == "ssh" and has_range:
            reason = "port ranges are not forwarded over this SSH connection"
        elif via == "ssh" and kind == "udp":
            reason = "UDP is not forwarded over this SSH connection"
        elif not _port(port):
            reason = "endpoint has no valid published port"
        elif via == "ssh" and port not in profile.get("tcp_ports", []):
            reason = "TCP port is not forwarded over this SSH connection"
        elif has_range and not (
            _port(endpoint.get("range_start"))
            and _port(endpoint.get("range_end"))
            and endpoint["range_start"] <= endpoint["range_end"]
        ):
            reason = "endpoint has no valid published port range"
        else:
            try:
                address = str(ipaddress.ip_address(profile.get("tcp_address", "")))
            except ValueError:
                reason = "connection has no valid raw TCP/UDP address"
            else:
                result["host"] = address
                # Raw endpoints are presented as address + published port. A
                # remote URL must not override the forwarded address in the UI.
                result.pop("url", None)
                result["transport_supported"] = True
                result.pop("reason", None)
                return result
    result["transport_supported"] = False
    result["reason"] = reason
    return result


def project_catalog(
    catalog: dict[str, Any],
    profile: dict[str, Any],
    http_port: int,
    *,
    dns_address: str | None = None,
    dns_port: int | None = None,
) -> dict[str, Any]:
    """Project catalog endpoints without copying any private controller state."""
    if not _port(http_port) or profile.get("via") not in {"direct", "ssh", "local"}:
        raise PortmapError("invalid local gateway state")
    domain = profile.get("domain")
    if not isinstance(domain, str) or not _dns_name(domain):
        raise PortmapError("invalid registered domain")
    result = dict(catalog)
    result["http_port"] = http_port
    # A projected remote catalog cannot choose this machine's DNS listener.
    # Only the controller's recorded binding may advertise that metadata.
    for key, value in (("dns_server", dns_address), ("dns_port", dns_port)):
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    # The remote's own http_address is never carried over unverified: direct
    # views use the controller-verified address, while ssh/local views keep
    # the browser's virtual Host origin instead of an unreachable address.
    result.pop("http_address", None)
    if profile["via"] == "direct":
        verified_address = profile.get("address")
        if isinstance(verified_address, str) and verified_address:
            result["http_address"] = verified_address
    result["client_access"] = {
        "transport": profile["via"],
        "domain": domain,
        "http_port": http_port,
        "dns_setup_command": "portmap setup",
        "dns_unset_command": "portmap teardown",
    }
    result["services"] = [
        {
            **service,
            "endpoints": [_project_endpoint(endpoint, profile, http_port) for endpoint in service["endpoints"]],
        }
        for service in catalog["services"]
    ]
    return result
