"""Read-only, loopback catalog views for the client's registered gateways."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .catalog import CatalogHandler, vite_public_root_asset
from .client_discovery import probe_gateway
from .errors import PortmapError
from .client_dns import DNS_ADDRESS, DNS_PORT


PROBE_TIMEOUT = 3.0
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.IGNORECASE)
_AUTHORITY = re.compile(r"([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?\Z")


def _port(value: object) -> bool:
    return type(value) is int and 1 <= value <= 65535


def _dns_name(value: str) -> bool:
    return len(value) <= 253 and all(_DNS_LABEL.fullmatch(label) for label in value.split("."))


def _request_host(value: str) -> str | None:
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
            if host.lower() == domain:
                result["transport_supported"] = False
                result["reason"] = "the apex hostname is reserved for the local catalog; use an application subdomain"
                return result
            # Retain the URL's host spelling and path, but use the local HTTP
            # listener rather than a remote port or an unavailable TLS listener.
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
        via = profile["via"]
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


def project_catalog(catalog: dict[str, Any], profile: dict[str, Any], http_port: int) -> dict[str, Any]:
    """Project catalog endpoints without copying any private controller state."""
    if not _port(http_port) or profile.get("via") not in {"direct", "ssh"}:
        raise PortmapError("invalid local gateway state")
    domain = profile.get("domain")
    if not isinstance(domain, str) or not _dns_name(domain):
        raise PortmapError("invalid registered domain")
    result = dict(catalog)
    result["http_port"] = http_port
    result["dns_server"] = DNS_ADDRESS
    result["dns_port"] = DNS_PORT
    result["client_access"] = {
        "transport": profile["via"],
        "domain": domain,
        "http_port": http_port,
        "dns_setup_command": "portmap client setup",
        "dns_unset_command": "portmap client teardown",
    }
    result["services"] = [
        {
            **service,
            "endpoints": [_project_endpoint(endpoint, profile, http_port) for endpoint in service["endpoints"]],
        }
        for service in catalog["services"]
    ]
    return result


def _read_state(path: Path) -> dict[str, Any]:
    # The controller publishes with os.replace; one open observes one complete
    # snapshot, including while connections are added or removed.
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PortmapError("local client state is unavailable") from exc
    if not isinstance(value, dict):
        raise PortmapError("local client state is unavailable")
    return value


class ClientViewServer(ThreadingHTTPServer):
    def __init__(self, state_dir: Path, port: int, owner: str) -> None:
        self.state_dir = state_dir
        self.owner = owner
        super().__init__(("127.0.0.1", port), ClientViewHandler)


class ClientViewHandler(CatalogHandler):
    server: ClientViewServer
    server_version = "portmap-client-catalog/0.1"

    def _host(self) -> str | None:
        values = self.headers.get_all("Host", [])
        return _request_host(values[0]) if len(values) == 1 else None

    def _profile(self, host: str) -> dict[str, Any] | None:
        path = self.server.state_dir / "connections.json"
        if not path.exists():
            return None
        state = _read_state(path)
        profiles = state.get("connections")
        if state.get("version") != 1 or not isinstance(profiles, dict):
            raise PortmapError("local client state is unavailable")
        profile = profiles.get(host)
        if profile is None:
            return None
        if not isinstance(profile, dict) or profile.get("domain") != host:
            raise PortmapError("local client state is unavailable")
        return profile

    def handle_request(self, *, send_body: bool) -> None:
        host = self._host()
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self.write_not_found(send_body=send_body)
            return
        if host is None or parsed.scheme or parsed.netloc:
            self.write_not_found(send_body=send_body)
            return
        # The gateway must be able to verify ownership before any domain has
        # been registered. Other hosts never gain an unscoped health endpoint.
        if parsed.path == "/healthz" and host in {"127.0.0.1", "portmap-gateway.invalid"}:
            self.write_json({"ok": True, "owner": self.server.owner}, send_body=send_body)
            return
        try:
            profile = self._profile(host)
        except PortmapError:
            self.write_json({"error": "local client state is unavailable"}, status=503, send_body=send_body)
            return
        if profile is None:
            self.write_not_found(send_body=send_body)
            return
        if parsed.path == "/healthz":
            self.write_json({"ok": True, "owner": self.server.owner}, send_body=send_body)
            return
        if parsed.path == "/registry.json":
            try:
                gateway = _read_state(self.server.state_dir / "gateway.json")
                http_port = gateway.get("http_port")
                backend = profile.get("backend_url")
                if not _port(http_port) or not isinstance(backend, str):
                    raise PortmapError("invalid local gateway state")
            except PortmapError:
                self.write_json({"error": "local client state is unavailable"}, status=503, send_body=send_body)
                return
            try:
                info = probe_gateway(backend, expected_domain=host, timeout=PROBE_TIMEOUT)
                projected = project_catalog(info.catalog, profile, http_port)
            except PortmapError:
                # Probe errors may include remote details. Expose neither the
                # controller profile nor backend/tunnel diagnostics over HTTP.
                self.write_json({"error": "remote catalog unavailable; check portmap connections"}, status=502, send_body=send_body)
                return
            self.write_json(projected, send_body=send_body)
            return
        if parsed.path in {"/", "/index.html"}:
            asset = "index.html"
        elif parsed.path.startswith("/assets/"):
            asset = parsed.path.removeprefix("/")
        else:
            asset = vite_public_root_asset(parsed.path)
        if asset is not None and self.write_static(asset, send_body=send_body):
            return
        self.write_not_found(send_body=send_body)

    def do_POST(self) -> None:
        host = self._host()
        try:
            profile = self._profile(host) if host is not None else None
        except PortmapError:
            self.write_json({"error": "local client state is unavailable"}, status=503, send_body=True)
            return
        if profile is None:
            self.write_not_found(send_body=True)
            return
        self.send_response(405)
        self.send_header("allow", "GET, HEAD")
        self.send_header("content-length", "0")
        self.end_headers()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args(argv)
    if not args.state_dir.is_absolute():
        parser.error("--state-dir must be an absolute path")
    if not _port(args.port):
        parser.error("--port must be from 1 to 65535")
    if not args.owner:
        parser.error("--owner must not be empty")
    with ClientViewServer(args.state_dir, args.port, args.owner) as server:
        print(f"portmap client catalog listening on 127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
