"""Authenticated, fixed-ingress SOCKS5 endpoint for Firefox remote DNS routing.

Run ``portmap browser-proxy --config PATH`` with a private JSON credentials file::

    {"domain": "a.portmap", "listen": {"host": "127.0.0.1", "port": 18080},
     "credentials": {"username": "browser", "password": "replace-me"},
     "ingress": {"host": "127.0.0.1", "port": 8080, "scope": "public"}}

SOCKS5 requires RFC1929 username/password authentication (each 1..255 UTF-8
bytes), CONNECT, and a domain-name address. No client-supplied IP, arbitrary
destination resolution, no-auth fallback, HTTP proxy mode or listener TLS is
supported. Use an already encrypted private transport such as Tailscale: SOCKS
credentials themselves are not encrypted. Firefox uses socks with proxyDNS true.

Port 80 validates exactly one origin-form HTTP request whose Host equals the
authenticated SOCKS destination before opening the fixed HTTP ingress. Host,
origin Authorization, escaped path/query and streaming are preserved. A single
terminal DNS root dot is normalized only for comparisons. HTTP pipelines are
discarded; opaque WebSocket transport starts only after verified ingress 101.

The HTTP ingress must be this node's existing PUBLIC/web ingress. Explicit
``ingress.scope: private`` exposes this namespace's private catalog/control
endpoints to the authenticated browser. No service/runtime is started or changed.

Port 443 requires ``tls_ingress: {host, port, scope_domain: "a.portmap"}``.
scope_domain asserts that this dedicated TLS ingress independently enforces
BOTH SNI and encrypted HTTP Host namespace boundaries. An opaque TLS tunnel
cannot inspect encrypted Host or origin credentials. Browser certificate checks
remain end-to-end; no origin TLS termination or verification bypass is performed.
Existing Portmap HTTP ingress does not provide this optional TLS capability.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import PortmapError

_HEADER_LIMIT = 65536
_IDLE_TIMEOUT = 300
_CONNECT_TIMEOUT = 10
_MAX_CONNECTIONS = 128
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+portmap$")


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


@dataclass(frozen=True)
class ProxyConfig:
    domain: str
    listen: Endpoint
    ingress: Endpoint
    username: bytes
    password: bytes
    private_ingress: bool = False
    tls_ingress: Endpoint | None = None


def _object(value: object, name: str, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict):
        raise PortmapError(f"browser-proxy {name} must be an object")
    if value.keys() - allowed or required - value.keys():
        raise PortmapError(f"browser-proxy {name}: expected fields {', '.join(sorted(allowed))}; required {', '.join(sorted(required))}")
    return value


def _endpoint(value: dict, name: str, *, listen: bool = False) -> Endpoint:
    host, port = value.get("host"), value.get("port")
    if not isinstance(host, str) or not host or any(c.isspace() for c in host) or any(c in host for c in "/@?#"):
        raise PortmapError(f"browser-proxy {name}.host must be a bare fixed host or IP address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise PortmapError(f"browser-proxy {name}.port must be an integer from 1 to 65535")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if listen or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise PortmapError(f"browser-proxy {name}.host must be {'a literal IP address' if listen else 'a fixed DNS name or IP address'}") from None
    else:
        if address.is_multicast or (address.is_unspecified and not listen):
            raise PortmapError(f"browser-proxy {name}.host is not a usable ingress address")
    return Endpoint(host, port)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PortmapError(f"browser-proxy config has duplicate field: {key}")
        result[key] = value
    return result


def load_config(path: Path) -> ProxyConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        raise PortmapError(f"cannot read browser-proxy JSON config {path}: {exc}") from exc
    raw = _object(raw, "config", {"domain", "listen", "credentials", "ingress", "tls_ingress"}, {"domain", "listen", "credentials", "ingress"})
    domain = raw["domain"]
    if not isinstance(domain, str) or len(domain) > 253 or not _DOMAIN.fullmatch(domain):
        raise PortmapError("browser-proxy domain must be a lowercase proper .portmap subtree, for example a.portmap")
    listen = _endpoint(_object(raw["listen"], "listen", {"host", "port"}, {"host", "port"}), "listen", listen=True)
    ingress_raw = _object(raw["ingress"], "ingress", {"host", "port", "scope"}, {"host", "port"})
    if ingress_raw.get("scope", "public") not in ("public", "private"):
        raise PortmapError("browser-proxy ingress.scope must be public (existing web ingress) or explicitly private")
    ingress = _endpoint(ingress_raw, "ingress")
    credentials = _object(raw["credentials"], "credentials", {"username", "password"}, {"username", "password"})
    encoded = {}
    for key, value in credentials.items():
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise PortmapError(f"browser-proxy credentials.{key} must be text without control characters")
        try:
            encoded[key] = value.encode("utf-8")
        except UnicodeError as exc:
            raise PortmapError(f"browser-proxy credentials.{key} must be valid UTF-8 text") from exc
        if not 1 <= len(encoded[key]) <= 255:
            raise PortmapError(f"browser-proxy credentials.{key} must contain 1..255 UTF-8 bytes for SOCKS5 authentication")
    tls_ingress = None
    if "tls_ingress" in raw:
        value = _object(raw["tls_ingress"], "tls_ingress", {"host", "port", "scope_domain"}, {"host", "port", "scope_domain"})
        if value["scope_domain"] != domain:
            raise PortmapError("browser-proxy tls_ingress.scope_domain must equal domain; the dedicated TLS ingress MUST enforce SNI and encrypted Host isolation (SOCKS cannot inspect them)")
        tls_ingress = _endpoint(value, "tls_ingress")
    return ProxyConfig(domain, listen, ingress, encoded["username"], encoded["password"], ingress_raw.get("scope") == "private", tls_ingress)


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


async def _head(reader: asyncio.StreamReader) -> bytes:
    try:
        data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _IDLE_TIMEOUT)
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as exc:
        raise RequestError(400, "Missing or oversized HTTP headers") from exc
    if len(data) > _HEADER_LIMIT:
        raise RequestError(431, "HTTP headers too large")
    return data


def _headers(data: bytes) -> tuple[str, list[tuple[str, str]]]:
    lines = data[:-4].decode("latin-1").split("\r\n")
    if any(ord(c) < 32 or ord(c) == 127 for c in lines[0]):
        raise RequestError(400, "Invalid HTTP start line")
    headers = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not _TOKEN.fullmatch(name) or any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in value):
            raise RequestError(400, "Invalid HTTP header")
        headers.append((name, value.strip(" \t")))
    return lines[0], headers


def _values(headers: list[tuple[str, str]], name: str) -> list[str]:
    return [v for k, v in headers if k.lower() == name]


def _authority(value: str, default_port: int) -> tuple[str, int]:
    try:
        parsed = urlsplit("//" + value)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment or value.endswith(":"):
            raise ValueError
        # A terminal DNS root dot is an equivalent authority for scope checks,
        # not a reason to rewrite the original Host header sent to ingress.
        host = parsed.hostname.removesuffix(".")
        port = parsed.port if parsed.port is not None else default_port
        if not _DOMAIN.fullmatch(host) or len(host) > 253:
            raise ValueError
        return host, port
    except ValueError as exc:
        raise RequestError(400, "Invalid .portmap target authority") from exc


def _scoped(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: list[tuple[str, str]]
    length: int
    chunked: bool
    upgrade: bool


def parse_request(data: bytes, config: ProxyConfig, destination: str) -> Request:
    """Validate one HTTP request inside an authenticated port-80 SOCKS stream."""
    first, headers = _headers(data)
    parts = first.split(" ")
    if len(parts) != 3 or not _TOKEN.fullmatch(parts[0]) or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}:
        raise RequestError(400, "Expected an origin-form HTTP/1.x request")
    method, path, _ = parts
    if not path.startswith("/") or "#" in path or method == "CONNECT":
        raise RequestError(400, "Port 80 SOCKS streams require an origin-form HTTP request, not a proxy request")
    hosts = _values(headers, "host")
    if len(hosts) != 1:
        raise RequestError(400, "Exactly one Host header is required")
    connections = {token.strip().lower() for value in _values(headers, "connection") for token in value.split(",")}
    if any(not _TOKEN.fullmatch(token) for token in connections) or connections & {"host", "content-length", "transfer-encoding", "authorization", "proxy-authorization"}:
        raise RequestError(400, "Unsafe Connection header")
    upgrade_values = _values(headers, "upgrade")
    upgrade = "upgrade" in connections and len(upgrade_values) == 1 and upgrade_values[0].lower() == "websocket"
    if (upgrade_values or "upgrade" in connections) and not upgrade:
        raise RequestError(400, "Only WebSocket upgrades are supported")
    host, port = _authority(hosts[0], 80)
    expected, _ = _authority(destination, 80)
    if not _scoped(expected, config.domain) or not _scoped(host, config.domain):
        raise RequestError(403, "Host is outside this node's configured domain")
    if (host, port) != (expected, 80):
        raise RequestError(400, "Host must match the authenticated SOCKS destination on port 80")
    lengths, encodings = _values(headers, "content-length"), _values(headers, "transfer-encoding")
    if len(lengths) > 1 or (lengths and encodings) or (encodings and encodings != ["chunked"]):
        raise RequestError(400, "Ambiguous or unsupported request body framing")
    if lengths and (not re.fullmatch(r"[0-9]{1,19}", lengths[0])):
        raise RequestError(400, "Invalid Content-Length")
    length = int(lengths[0]) if lengths else 0
    if upgrade and (length or encodings):
        raise RequestError(400, "WebSocket handshakes cannot carry a body")
    if upgrade and method != "GET":
        raise RequestError(400, "WebSocket handshake must use GET")
    # Never pass client-nominated hop headers or proxy credentials to the origin.
    removed = connections | {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "te", "trailer", "upgrade"}
    forwarded = [(k, v) for k, v in headers if k.lower() not in removed]
    forwarded.append(("Connection", "Upgrade" if upgrade else "close"))
    if upgrade:
        forwarded.append(("Upgrade", "websocket"))
    return Request(method, path, forwarded, length, bool(encodings), upgrade)


def _serialize(first: str, headers: list[tuple[str, str]]) -> bytes:
    return (first + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode("latin-1")




async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, count: int | None = None) -> None:
    while count is None or count:
        data = await asyncio.wait_for(reader.read(65536 if count is None else min(count, 65536)), _IDLE_TIMEOUT)
        if not data:
            if count:
                raise RequestError(400, "Incomplete request body")
            break
        writer.write(data)
        await writer.drain()
        if count is not None:
            count -= len(data)


async def _body(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: Request) -> None:
    if not request.chunked:
        await _copy(reader, writer, request.length)
        return
    await _chunked(reader, writer, discard_trailers=True)


async def _chunked(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, discard_trailers: bool) -> None:
    while True:
        line = await asyncio.wait_for(reader.readuntil(b"\r\n"), _IDLE_TIMEOUT)
        if len(line) > 4096 or not re.fullmatch(rb"[0-9A-Fa-f]{1,16}(?:;[\x20-\x7e]*)?\r\n", line):
            raise RequestError(400, "Invalid chunk size")
        size = int(line.split(b";", 1)[0].strip(), 16)
        if not size:
            # Request trailers cannot inject credentials/routing headers;
            # origin response trailers retain their ordinary HTTP semantics.
            if not discard_trailers:
                writer.write(b"0\r\n")
            total = 0
            while True:
                trailer = await asyncio.wait_for(reader.readuntil(b"\r\n"), _IDLE_TIMEOUT)
                total += len(trailer)
                if total > _HEADER_LIMIT:
                    raise RequestError(400, "Trailers too large")
                if trailer == b"\r\n":
                    break
                _headers(b"TRAILERS\r\n" + trailer + b"\r\n")
                if not discard_trailers:
                    writer.write(trailer)
            if discard_trailers:
                writer.write(b"0\r\n\r\n")
            else:
                writer.write(b"\r\n")
            await writer.drain()
            return
        writer.write(line)
        await _copy(reader, writer, size)
        if await reader.readexactly(2) != b"\r\n":
            raise RequestError(400, "Invalid chunk terminator")
        writer.write(b"\r\n")
        await writer.drain()


async def _tunnel(left: asyncio.StreamReader, left_writer: asyncio.StreamWriter, right: asyncio.StreamReader, right_writer: asyncio.StreamWriter) -> None:
    async def direction(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _copy(reader, writer)
        if writer.can_write_eof():
            writer.write_eof()
            await writer.drain()
    tasks = [asyncio.create_task(direction(left, right_writer)), asyncio.create_task(direction(right, left_writer))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _response(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: Request, client_reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter) -> None:
    while True:
        first, headers = _headers(await _head(reader))
        parts = first.split(" ", 2)
        if len(parts) < 2 or parts[0] not in {"HTTP/1.0", "HTTP/1.1"} or not re.fullmatch(r"[0-9]{3}", parts[1]):
            raise RequestError(502, "Invalid ingress response")
        status = int(parts[1])
        if status == 101:
            connections = {token.strip().lower() for value in _values(headers, "connection") for token in value.split(",")}
            if not request.upgrade or "upgrade" not in connections or [v.lower() for v in _values(headers, "upgrade")] != ["websocket"]:
                raise RequestError(502, "Unexpected ingress protocol upgrade")
            writer.write(_serialize(first, headers))
            await writer.drain()
            await _tunnel(client_reader, writer, reader, upstream_writer)
            return
        if 100 <= status < 200:
            writer.write(_serialize(first, headers))
            await writer.drain()
            continue
        lengths, encodings = _values(headers, "content-length"), _values(headers, "transfer-encoding")
        if len(lengths) > 1 or (lengths and encodings) or (encodings and encodings != ["chunked"]) or (lengths and not re.fullmatch(r"[0-9]{1,19}", lengths[0])):
            raise RequestError(502, "Ambiguous or unsupported ingress response framing")
        connections = {token.strip().lower() for value in _values(headers, "connection") for token in value.split(",")}
        if any(not _TOKEN.fullmatch(token) for token in connections) or connections & {"content-length", "transfer-encoding"}:
            raise RequestError(502, "Unsafe ingress Connection header")
        removed = connections | {"connection", "proxy-connection", "keep-alive", "upgrade"}
        headers = [(k, v) for k, v in headers if k.lower() not in removed]
        headers.append(("Connection", "close"))
        writer.write(_serialize(first, headers))
        await writer.drain()
        if request.method == "HEAD" or status in {204, 304}:
            return
        if encodings:
            await _chunked(reader, writer, discard_trailers=False)
        else:
            # A framed response ends without requiring upstream EOF. This
            # avoids deadlock with an ingress waiting for our connection close.
            await _copy(reader, writer, int(lengths[0]) if lengths else None)
        return


async def _error(writer: asyncio.StreamWriter, error: RequestError) -> None:
    body = (error.message + "\n").encode("utf-8")
    headers = [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body))), ("Connection", "close")]
    writer.write(_serialize(f"HTTP/1.1 {error.status} Proxy Error", headers) + body)
    await writer.drain()


class SocksError(Exception):
    def __init__(self, reply: int):
        self.reply = reply
        super().__init__(f"SOCKS request rejected ({reply})")


async def _socks_bytes(reader: asyncio.StreamReader, count: int) -> bytes:
    return await asyncio.wait_for(reader.readexactly(count), _CONNECT_TIMEOUT)


async def _socks_reply(writer: asyncio.StreamWriter, reply: int) -> None:
    # A fixed unspecified bound address discloses no internal ingress details.
    writer.write(bytes((5, reply, 0, 1)) + b"\0" * 6)
    await writer.drain()


async def _negotiate(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, config: ProxyConfig) -> tuple[str, int] | None:
    version, count = await _socks_bytes(reader, 2)
    if version != 5 or count == 0:
        writer.write(b"\x05\xff")
        await writer.drain()
        return None
    methods = await _socks_bytes(reader, count)
    if 2 not in methods:
        writer.write(b"\x05\xff")
        await writer.drain()
        return None
    writer.write(b"\x05\x02")
    await writer.drain()
    version, length = await _socks_bytes(reader, 2)
    if version != 1 or not length:
        writer.write(b"\x01\x01")
        await writer.drain()
        return None
    username = await _socks_bytes(reader, length)
    length = (await _socks_bytes(reader, 1))[0]
    if not length:
        writer.write(b"\x01\x01")
        await writer.drain()
        return None
    password = await _socks_bytes(reader, length)
    user_matches = hmac.compare_digest(username, config.username)
    password_matches = hmac.compare_digest(password, config.password)
    if not (user_matches and password_matches):
        writer.write(b"\x01\x01")
        await writer.drain()
        return None
    writer.write(b"\x01\x00")
    await writer.drain()
    version, command, reserved, address_type = await _socks_bytes(reader, 4)
    if version != 5 or reserved != 0:
        raise SocksError(1)
    if command != 1:
        raise SocksError(7)
    if address_type != 3:
        raise SocksError(8)
    length = (await _socks_bytes(reader, 1))[0]
    domain_bytes = await _socks_bytes(reader, length)
    port = int.from_bytes(await _socks_bytes(reader, 2), "big")
    try:
        domain = domain_bytes.decode("ascii").lower().removesuffix(".")
    except UnicodeError as exc:
        raise SocksError(2) from exc
    if not _DOMAIN.fullmatch(domain) or len(domain) > 253 or not _scoped(domain, config.domain) or port not in (80, 443):
        raise SocksError(2)
    if port == 443 and config.tls_ingress is None:
        raise SocksError(2)
    return domain, port


class BrowserProxy:
    def __init__(self, config: ProxyConfig):
        self.config = config
        self.active = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer = None
        tasks: list[asyncio.Task] = []
        if self.active >= _MAX_CONNECTIONS:
            try:
                writer.write(b"\x05\xff")
                await writer.drain()
            finally:
                writer.close()
            return
        self.active += 1
        http_stream = False
        started_response = False
        try:
            destination = await _negotiate(reader, writer, self.config)
            if destination is None:
                return
            domain, port = destination
            if port == 80:
                await _socks_reply(writer, 0)
                http_stream = True
                # Validate before opening ingress; no second HTTP request can
                # be pipelined through an already-authorized SOCKS stream.
                request = parse_request(await _head(reader), self.config, domain)
                endpoint = self.config.ingress
            else:
                endpoint = self.config.tls_ingress
                assert endpoint is not None
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(endpoint.host, endpoint.port, limit=_HEADER_LIMIT), _CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as exc:
                if http_stream:
                    raise RequestError(502, "Configured node ingress is unavailable") from exc
                raise SocksError(5) from exc
            if port == 443:
                await _socks_reply(writer, 0)
                await _tunnel(reader, writer, upstream_reader, upstream_writer)
            else:
                upstream_writer.write(_serialize(f"{request.method} {request.path} HTTP/1.1", request.headers))
                await upstream_writer.drain()
                # Response copying runs during body upload (100-continue and
                # streaming uploads). Only the framed body reaches the ingress;
                # buffered/pipelined second requests are discarded on close.
                started_response = True
                tasks = [asyncio.create_task(_body(reader, upstream_writer, request)), asyncio.create_task(_response(upstream_reader, writer, request, reader, upstream_writer))]
                pending = set(tasks)
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    if tasks[1] in done:
                        break
        except SocksError as exc:
            await _socks_reply(writer, exc.reply)
        except RequestError as exc:
            if http_stream and not started_response:
                await _error(writer, exc)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # Disconnected/invalid streams close, never retry elsewhere.
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if upstream_writer is not None:
                upstream_writer.close()
            writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 2)
            self.active -= 1

    async def start(self) -> asyncio.Server:
        return await asyncio.start_server(self.handle, self.config.listen.host, self.config.listen.port, limit=_HEADER_LIMIT)


def run_browser_proxy(path: Path) -> int:
    config = load_config(path)
    if config.private_ingress:
        print("portmap: WARNING: explicit private ingress exposes this node namespace's private catalog/control endpoints to authenticated browsers", file=sys.stderr, flush=True)
    if config.tls_ingress:
        print("portmap: SOCKS TLS forwarding enabled: configured TLS ingress must independently enforce SNI and encrypted Host namespace isolation; the adapter cannot inspect encrypted requests", file=sys.stderr, flush=True)

    async def serve() -> None:
        try:
            server = await BrowserProxy(config).start()
        except OSError as exc:
            raise PortmapError(f"cannot bind browser-proxy {config.listen.host}:{config.listen.port}: {exc}") from exc
        async with server:
            print(f"Browser proxy listening on {config.listen.host}:{config.listen.port} for {config.domain}", flush=True)
            await server.serve_forever()
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0
