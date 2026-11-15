"""Foreground browser pairing; never starts or repairs a publisher or a VPN.

Automatic settings live in <state_dir>/browser-proxy/pairing.json. They are
node-owner-bound, contain reusable SOCKS credentials, and are not an encrypted
or one-time invitation. Explicit --config pairing is an advanced transport
escape hatch: the operator, not discovery, chooses and protects its ingress.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator

from .browser_proxy import BrowserProxy, Endpoint, ProxyConfig, _unique_object, parse_config
from .client_discovery import probe_gateway
from .client_runtime import native_environment
from .errors import PortmapError
from .settings import PortmapSettings, load_portmap_settings

_PREFIX = "portmap-connect-v1:"
_RECORD = "pairing.json"
_MAX_FILE = 16384
_MISSING = object()
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(frozen=True)
class Publisher:
    domain: str
    owner: str
    ingress: Endpoint


@dataclass(frozen=True)
class Pairing:
    name: str
    host: str
    config: ProxyConfig
    persist: Callable[[ProxyConfig], None] | None = None
    explicit: bool = False


def discover_publisher(settings: PortmapSettings) -> Publisher:
    """Use the owned running local role, not the private portal or a relay."""
    from .infrastructure import _configuration_paths, load_infrastructure
    from .server import _load, _saved_settings, server_status

    record = _load(settings)
    if record is None:
        raise PortmapError("browser-proxy --pair needs an already running local publisher; run portmap up separately first")
    effective = _saved_settings(settings, record)
    if "://" in effective.runtime_socket:
        raise PortmapError("browser-proxy --pair requires a local publisher, not a remote runtime")
    status = server_status(settings)
    if not status.get("running") or status.get("migration_required"):
        raise PortmapError("local publisher is not running; inspect portmap status and start it separately before pairing")
    state = load_infrastructure(Path(record["client_state_dir"]))
    infra = status.get("infrastructure") or {}
    gateway = infra.get("gateway") or {}
    if (state is None or state.get("phase") != "ready" or not infra.get("running")
            or infra.get("owner") != state["owner"] or infra.get("domain") != effective.dns_domain
            or state["domain"] != effective.dns_domain):
        raise PortmapError("local publisher infrastructure identity changed or is not running; inspect portmap status")
    try:
        snapshot = json.loads(_configuration_paths(state)["access"].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PortmapError("cannot verify the active local publisher role; inspect portmap status") from exc
    local = snapshot.get("local") if isinstance(snapshot, dict) else None
    if (not isinstance(local, dict) or local.get("enabled") is not True
            or local.get("domain") != effective.dns_domain or snapshot.get("owner") != state["owner"]):
        raise PortmapError("the running infrastructure has no matching local publisher role; private/control ingress cannot be paired")
    port = gateway.get("public_backend_port")
    if (type(port) is not int or not 1 <= port <= 65535 or port != state["public_backend_port"]
            or port in {state["http_port"], state["listen_port"], state["management_port"]}):
        raise PortmapError("no verified public web ingress is available; private/control ingress cannot be paired")
    return Publisher(effective.dns_domain, state["owner"], Endpoint("127.0.0.1", port))


def tailscale_address() -> str:
    """Read only local Self data; do not inventory peers or alter VPN settings."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json", "--peers=false"],
            env=native_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False,
        )
    except FileNotFoundError as exc:
        raise PortmapError("Tailscale is not installed; install/connect it separately before browser-proxy --pair") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortmapError("cannot read local Tailscale status; connect Tailscale and retry browser-proxy --pair") from exc
    try:
        status = json.loads(result.stdout)
        if result.returncode or status.get("BackendState") != "Running":
            raise ValueError("not running")
        own = status["Self"]
        addresses = own["TailscaleIPs"]
        if not isinstance(addresses, list) or own.get("Online") is False:
            raise ValueError("not online")
        usable = []
        for value in addresses:
            address = ipaddress.ip_address(value)
            tailnet = ipaddress.ip_network("100.64.0.0/10") if address.version == 4 else ipaddress.ip_network("fd7a:115c:a1e0::/48")
            if address in tailnet:
                usable.append(address)
        if not usable:
            raise ValueError("no tailnet address")
        return str(min(usable, key=lambda address: (address.version, int(address))))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PortmapError("Tailscale is down or has no usable local address; connect it separately and retry browser-proxy --pair") from exc


def advertised_host(value: str, *, allow_loopback: bool = False) -> str:
    """Return the extension's bare DNS/IPv4 or bracketed IPv6 host syntax."""
    if not isinstance(value, str) or not value or value != value.strip() or "%" in value:
        raise PortmapError("advertised proxy host must be a bare independently reachable hostname or IP address")
    candidate = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        host = value.lower()
        if (len(host) > 253 or not all(_LABEL.fullmatch(label) for label in host.split("."))
                or host == "portmap" or host.endswith(".portmap") or host == "localhost" or host.endswith(".localhost")):
            raise PortmapError("advertised proxy host must be independently reachable, not a URL, wildcard, localhost or .portmap name")
        return host
    if (address.is_unspecified or address.is_multicast or address.is_link_local
            or (address.is_loopback and not allow_loopback)):
        raise PortmapError("advertised proxy host must not be unspecified, multicast, link-local or loopback")
    return f"[{address}]" if address.version == 6 else str(address)


def connection_code(pairing: Pairing, config: ProxyConfig) -> str:
    """Encode the exact six-field import schema, never diagnostics or metadata."""
    # Reuse SOCKS/domain validation, including strict UTF-8 credential limits.
    parse_config(_config_record(config))
    name = pairing.name
    if not name.strip() or len(name) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise PortmapError("browser connection name must be nonblank text of at most 120 characters")
    payload = {
        "name": name, "domain": config.domain,
        "proxyHost": advertised_host(pairing.host, allow_loopback=True),
        "proxyPort": config.listen.port,
        "username": config.username.decode("utf-8"), "password": config.password.decode("utf-8"),
    }
    code = _PREFIX + base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    if len(code) > 8192:
        raise PortmapError("browser connection settings exceed the connection-code size limit")
    return code


def _config_record(config: ProxyConfig) -> dict:
    result = {
        "domain": config.domain,
        "listen": {"host": config.listen.host, "port": config.listen.port},
        "ingress": {"host": config.ingress.host, "port": config.ingress.port, "scope": "private" if config.private_ingress else "public"},
        "credentials": {"username": config.username.decode("utf-8"), "password": config.password.decode("utf-8")},
    }
    if config.tls_ingress:
        result["tls_ingress"] = {"host": config.tls_ingress.host, "port": config.tls_ingress.port, "scope_domain": config.domain}
    return result


def _private_descriptor(fd: int) -> None:
    metadata = os.fstat(fd)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
        raise PortmapError("browser pairing files must be owned regular files with mode 0600 and no hard links")


def _read_private(name: str | Path, *, directory: int | None = None) -> object:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            _private_descriptor(handle.fileno())
            data = handle.read(_MAX_FILE + 1)
        if len(data) > _MAX_FILE:
            raise ValueError("oversized record")
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except FileNotFoundError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError, PortmapError) as exc:
        # Parsing exceptions and duplicate keys may include credential text.
        raise PortmapError("cannot read private browser pairing settings; require an owned, unlinked regular 0600 UTF-8 JSON file") from exc


@contextmanager
def _private_directory(path: Path) -> Iterator[int]:
    # Walk instead of resolve(): a symlink in any component must fail closed.
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        components = path.absolute().parts[1:]
        for index, component in enumerate(components):
            if component == "..":
                raise PortmapError("browser pairing state path must not contain parent traversal")
            if index == len(components) - 1:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=fd)
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        metadata = os.fstat(fd)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PortmapError("browser pairing directory must be owned and private (0700)")
        yield fd
    finally:
        os.close(fd)


def _save_private(directory: int, value: dict) -> None:
    temporary = ".pairing-" + secrets.token_hex(12)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _RECORD, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


@contextmanager
def automatic_pairing(settings: PortmapSettings) -> Iterator[Pairing]:
    publisher = discover_publisher(settings)
    host = tailscale_address()
    advertised = advertised_host(host)
    name = publisher.domain.removesuffix(".portmap")[:120]
    path = settings.state_dir.expanduser().absolute() / "browser-proxy"
    try:
        with _private_directory(path) as directory:
            fd = os.open("pairing.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
            with os.fdopen(fd, "a+") as lock:
                _private_descriptor(lock.fileno())
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise PortmapError("this node's browser pairing proxy is already running; stop that foreground command first") from exc
                try:
                    record = _read_private(_RECORD, directory=directory)
                except FileNotFoundError:
                    record = _MISSING
                if record is _MISSING:
                    config = ProxyConfig(publisher.domain, Endpoint(host, 0), publisher.ingress,
                                         ("browser-" + secrets.token_hex(8)).encode("ascii"), secrets.token_urlsafe(32).encode("ascii"))
                else:
                    if (not isinstance(record, dict) or set(record) != {"version", "owner", "name", "config"}
                            or type(record["version"]) is not int or record["version"] != 1):
                        raise PortmapError("invalid saved browser pairing settings; inspect the private pairing file")
                    config = parse_config(record["config"])
                    if (record["owner"] != publisher.owner or record["name"] != name
                            or config.domain != publisher.domain or config.listen.host != host
                            or config.ingress != publisher.ingress or config.listen.port < 1024
                            or config.private_ingress or config.tls_ingress is not None):
                        raise PortmapError("saved browser pairing settings no longer match this node/ingress/Tailscale address; inspect the private pairing file, do not silently rotate credentials")

                def persist(bound: ProxyConfig) -> None:
                    if discover_publisher(settings) != publisher or tailscale_address() != host:
                        raise PortmapError("publisher or Tailscale identity changed during pairing; retry after inspecting local status")
                    if record is _MISSING:
                        _save_private(directory, {"version": 1, "owner": publisher.owner, "name": name, "config": _config_record(bound)})

                yield Pairing(name, advertised, config, persist)
    except OSError as exc:
        raise PortmapError("cannot open or save private browser pairing state; check ownership, permissions and symlinks") from exc


def check_live_ingress(config: ProxyConfig) -> None:
    host = f"[{config.ingress.host}]" if ":" in config.ingress.host else config.ingress.host
    try:
        probe_gateway(f"http://{host}:{config.ingress.port}", timeout=5,
                      expected_domain=config.domain, http_host=config.domain)
    except PortmapError as exc:
        raise PortmapError("browser pairing ingress is unavailable or serves a different node; no connection code was issued") from exc


async def _check_explicit_ingress(config: ProxyConfig) -> None:
    """Advanced configs can point at an app fixture, not a catalog server."""
    async def probe() -> None:
        reader, writer = await asyncio.open_connection(config.ingress.host, config.ingress.port, limit=65536)
        try:
            writer.write(f"GET / HTTP/1.1\r\nHost: {config.domain}\r\nConnection: close\r\n\r\n".encode("ascii"))
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            if not re.fullmatch(rb"HTTP/1\.[01] [23][0-9][0-9](?: [^\r\n]*)?", head.split(b"\r\n", 1)[0]):
                raise PortmapError("explicit browser pairing ingress did not return a successful HTTP response; no connection code was issued")
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        if config.tls_ingress:
            # Liveness does not prove SNI/Host isolation; the explicit operator
            # warning remains mandatory, and no certificate bypass is installed.
            _reader, tls_writer = await asyncio.open_connection(config.tls_ingress.host, config.tls_ingress.port)
            tls_writer.close()
            with contextlib.suppress(OSError):
                await tls_writer.wait_closed()

    try:
        await asyncio.wait_for(probe(), 5)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise PortmapError("explicit browser pairing HTTP/TLS ingress is unavailable; no connection code was issued") from exc


async def serve_pairing(pairing: Pairing) -> None:
    config = pairing.config
    try:
        server = await BrowserProxy(config).start()
    except OSError as exc:
        raise PortmapError("cannot bind browser pairing listener; verify Tailscale and the saved port (another listener is never replaced)") from exc
    async with server:
        sockets = server.sockets or ()
        if len(sockets) != 1:
            raise PortmapError("browser pairing requires one exact bound IP listener")
        bound = replace(config, listen=Endpoint(config.listen.host, sockets[0].getsockname()[1]))
        if bound.listen.port < 1024:
            raise PortmapError("browser pairing requires an unprivileged listener port")
        if pairing.explicit:
            await _check_explicit_ingress(bound)
        else:
            await asyncio.to_thread(check_live_ingress, bound)
        # Validate and persist only the port of the actual live socket.
        code = connection_code(pairing, bound)
        if pairing.persist is not None:
            pairing.persist(bound)
        print(f"Browser proxy ready on {pairing.host}:{bound.listen.port} for {bound.domain}; keep this foreground command running. Ctrl-C stops only this proxy.", file=sys.stderr, flush=True)
        print("Connection code contains reusable credentials (not encrypted or one-time); share privately. SOCKS transport must remain on Tailscale or another trusted encrypted private network.", file=sys.stderr, flush=True)
        print(code, flush=True)
        await server.serve_forever()


def run_browser_pairing(path: Path | None = None, *, advertise: str | None = None) -> int:
    if path is None and advertise is not None:
        raise PortmapError("--advertise-host requires advanced --pair --config; automatic pairing always uses this node's Tailscale address")
    try:
        if path is None:
            with automatic_pairing(load_portmap_settings()) as pairing:
                asyncio.run(serve_pairing(pairing))
        else:
            try:
                config = parse_config(_read_private(path))
            except FileNotFoundError as exc:
                raise PortmapError("explicit browser pairing config does not exist") from exc
            host = advertised_host(advertise if advertise is not None else config.listen.host, allow_loopback=True)
            print("portmap: advanced explicit pairing: operator is responsible for ingress scope and encrypted private transport", file=sys.stderr, flush=True)
            if config.private_ingress:
                print("portmap: WARNING: explicit private ingress exposes this node namespace's private catalog/control endpoints to authenticated browsers", file=sys.stderr, flush=True)
            if config.tls_ingress:
                print("portmap: TLS ingress must independently enforce SNI and encrypted Host namespace isolation", file=sys.stderr, flush=True)
            asyncio.run(serve_pairing(Pairing(config.domain.removesuffix(".portmap")[:120], host, config, explicit=True)))
    except KeyboardInterrupt:
        print("Browser proxy stopped; saved settings retained for restart.", file=sys.stderr, flush=True)
    except OSError as exc:
        raise PortmapError("browser pairing listener or private state is unavailable") from exc
    return 0
