from __future__ import annotations

import glob
import http.client
import ipaddress
import json
import math
import os
import queue
import re
import selectors
import shlex
import socket
import ssl
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .errors import PortmapError


MAX_CATALOG_BYTES = 1024 * 1024
MAX_SSH_CONFIG_BYTES = 1024 * 1024
MAX_SSH_CONFIG_FILES = 64
MAX_SSH_INCLUDE_DEPTH = 16
MAX_SSH_OUTPUT_BYTES = 1024 * 1024
MAX_RESOLVED_ADDRESSES = 8
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SSH_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")


@dataclass(frozen=True)
class GatewayInfo:
    domain: str
    http_port: int
    address: str
    scheme: str
    catalog: dict
    catalog_url: str


def _timeout(value: float) -> float:
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise PortmapError("timeout must be a finite positive number of seconds")
    return float(value)


def _port(value: object, *, field: str) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise PortmapError(f"{field} must be an integer from 1 to 65535")
    return value


def _domain(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PortmapError(f"{field} must be a DNS domain, not a URL or address")
    try:
        domain = value.removesuffix(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise PortmapError(f"{field} must be a valid DNS domain") from exc
    if len(domain) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in domain.split(".")):
        raise PortmapError(f"{field} must contain only valid DNS labels")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise PortmapError(f"{field} must be a DNS domain, not an IP address")


def _clean_target(target: str) -> str:
    if not isinstance(target, str) or not target or len(target) > 2048:
        raise PortmapError("provide a host, IP address, or HTTP(S) URL")
    if target.startswith("-") or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in target):
        raise PortmapError("target must not contain options, whitespace, or control characters")
    return target


def _host(value: str) -> str:
    if "%" in value:
        raise PortmapError("scoped or percent-escaped addresses are not supported; use a routable IP address")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return _domain(value, field="target host")
    if address.is_unspecified or address.is_multicast:
        raise PortmapError("target must be a unicast destination, not an unspecified or multicast address")
    return str(address)


def _gateway_target(target: str) -> tuple[str, tuple[int, ...], str]:
    target = _clean_target(target)
    is_url = "://" in target
    if not is_url:
        try:
            return _host(str(ipaddress.ip_address(target))), (8080, 80), "http"
        except ValueError:
            pass
    try:
        parsed = urlsplit(target if is_url else f"//{target}")
        scheme = parsed.scheme.lower() if is_url else "http"
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise PortmapError("invalid gateway target; use host:port or [IPv6]:port") from exc
    if scheme not in {"http", "https"}:
        raise PortmapError("gateway URL scheme must be http or https")
    if not host or parsed.username is not None or parsed.password is not None:
        raise PortmapError("gateway target must contain a host and must not include credentials")
    if parsed.netloc.startswith("[") and not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parsed.netloc):
        raise PortmapError("invalid bracketed IPv6 authority; use [IPv6]:port")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/", "/registry.json"}:
        raise PortmapError("use the gateway origin URL or its /registry.json URL, without a query or fragment")
    if not is_url and parsed.path:
        raise PortmapError("bare gateway targets must be a host or host:port")
    if parsed.netloc.endswith(":"):
        raise PortmapError("gateway target has an empty port")
    host = _host(host)
    if port is not None:
        return host, (_port(port, field="target port"),), scheme
    if is_url:
        return host, (443 if scheme == "https" else 80,), scheme
    return host, (8080, 80), scheme


def _catalog(payload: bytes, expected_domain: str | None) -> tuple[dict, str, int]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        catalog = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PortmapError("/registry.json did not return valid UTF-8 JSON; check that this is a portmap gateway") from exc
    if not isinstance(catalog, dict):
        raise PortmapError("/registry.json must return a portmap catalog object")
    domain = _domain(catalog.get("dns_domain"), field="catalog dns_domain")
    http_port = _port(catalog.get("http_port"), field="catalog http_port")
    services = catalog.get("services")
    if not isinstance(services, list) or any(not isinstance(service, dict) for service in services):
        raise PortmapError("/registry.json must contain a services array of objects")
    for service in services:
        endpoints = service.get("endpoints")
        if not isinstance(endpoints, list) or any(not isinstance(endpoint, dict) for endpoint in endpoints):
            raise PortmapError("each catalog service must contain an endpoints array of objects")
        for endpoint in endpoints:
            for key in ("container_port", "host_port", "range_start", "range_end"):
                if endpoint.get(key) is not None:
                    _port(endpoint[key], field=f"catalog endpoint {key}")
    if "worktrees" in catalog and (
        not isinstance(catalog["worktrees"], list)
        or any(not isinstance(worktree, dict) for worktree in catalog["worktrees"])
    ):
        raise PortmapError("catalog worktrees must be an array of objects")
    if "agent" in catalog:
        agent = catalog["agent"]
        if not isinstance(agent, dict) or type(agent.get("available")) is not bool:
            raise PortmapError("catalog agent must be an object with an available boolean")
    if expected_domain is not None and domain != expected_domain:
        raise PortmapError(f"gateway domain {domain!r} does not match expected domain {expected_domain!r}")
    return catalog, domain, http_port


class _ProbeDeadline:
    def __init__(self, timeout: float) -> None:
        self.ends = time.monotonic() + timeout
        self.lock = threading.Lock()
        self.socket: socket.socket | None = None
        self.cancelled = False

    def remaining(self) -> float:
        remaining = self.ends - time.monotonic()
        if self.cancelled or remaining <= 0:
            raise TimeoutError("gateway probe deadline expired")
        return remaining

    def track(self, sock: socket.socket) -> None:
        with self.lock:
            if self.cancelled:
                sock.close()
                raise TimeoutError("gateway probe deadline expired")
            self.socket = sock

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            if self.socket is not None:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.socket.close()


def _bounded_probe(operation: Callable[[], GatewayInfo], deadline: _ProbeDeadline) -> GatewayInfo:
    # getaddrinfo has no portable timeout. A daemon bounds the caller even if the
    # OS resolver stalls; cancellation closes any active HTTP/TLS socket.
    result: queue.Queue = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result.put((True, operation()))
        except Exception as exc:
            result.put((False, exc))

    threading.Thread(target=run, name="portmap-gateway-probe", daemon=True).start()
    try:
        success, value = result.get(timeout=deadline.remaining())
    except (queue.Empty, TimeoutError) as exc:
        deadline.cancel()
        raise PortmapError("gateway probe timed out; check the address, port, firewall, or use SSH forwarding") from exc
    if success:
        return value
    if isinstance(value, PortmapError):
        raise value
    raise PortmapError("gateway probe failed; check the target address and local network configuration") from value


def _request_catalog(host: str, port: int, scheme: str, address: tuple, deadline: _ProbeDeadline, *, connect_timeout: float | None = None) -> bytes:
    family, sockaddr = address
    sock = socket.socket(family, socket.SOCK_STREAM)
    deadline.track(sock)
    connection = http.client.HTTPConnection(host, port, timeout=deadline.remaining())
    connection.sock = sock
    response = None
    try:
        sock.settimeout(min(deadline.remaining(), connect_timeout) if connect_timeout is not None else deadline.remaining())
        sock.connect((sockaddr[0], port, *sockaddr[2:]))
        if scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
            connection.sock = sock
            deadline.track(sock)
            sock.settimeout(deadline.remaining())
            sock.do_handshake()
        sock.settimeout(deadline.remaining())
        authority = f"[{host}]" if ":" in host else host
        connection.request("GET", "/registry.json", headers={"Host": f"{authority}:{port}", "Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close"})
        sock.settimeout(deadline.remaining())
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise PortmapError("gateway redirected /registry.json; use the explicit final gateway origin (redirects are not followed)")
        if response.status != 200:
            raise PortmapError(f"gateway returned HTTP {response.status} for /registry.json; check the portmap gateway URL")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise PortmapError("gateway returned compressed catalog data despite requesting identity encoding")
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError as exc:
                raise PortmapError("gateway returned an invalid Content-Length") from exc
            if not 0 <= length <= MAX_CATALOG_BYTES:
                raise PortmapError(f"gateway catalog exceeds the {MAX_CATALOG_BYTES}-byte limit")
        body = bytearray()
        while True:
            # HTTPResponse owns the socket after getresponse(). It can close
            # the descriptor as soon as the last Content-Length byte is read.
            # The watchdog owns the overall deadline; do not re-arm that fd.
            deadline.remaining()
            chunk = response.read1(min(65536, MAX_CATALOG_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_CATALOG_BYTES:
                raise PortmapError(f"gateway catalog exceeds the {MAX_CATALOG_BYTES}-byte limit")
        if content_length is not None and len(body) != length:
            raise PortmapError("gateway returned a truncated catalog response")
        return bytes(body)
    finally:
        if response is not None:
            response.close()
        connection.close()
        sock.close()


def probe_gateway(target: str, *, timeout: float = 3, expected_domain: str | None = None) -> GatewayInfo:
    """Probe one requested origin without proxies, redirects, or advertised-IP routing.

    Only bare hosts try 8080 then 80. URLs use their explicit or scheme-default
    port. ``address`` is the pinned numeric destination; ``http_port`` comes
    from the validated catalog. ``catalog_url`` retains the requested hostname
    for TLS/Host-header identity and is descriptive, not a new routing choice.
    """
    timeout = _timeout(timeout)
    host, ports, scheme = _gateway_target(target)
    expected = _domain(expected_domain, field="expected_domain") if expected_domain is not None else None
    deadline = _ProbeDeadline(timeout)

    def probe() -> GatewayInfo:
        try:
            resolved = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise PortmapError("cannot resolve gateway host; provide a reachable IP or use a configured SSH alias") from exc
        deadline.remaining()
        addresses = []
        for family, _, _, _, sockaddr in resolved:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            address = (family, sockaddr)
            if address not in addresses:
                addresses.append(address)
            if len(addresses) == MAX_RESOLVED_ADDRESSES:
                break
        if not addresses:
            raise PortmapError("gateway host has no usable IPv4 or IPv6 addresses")
        errors = []
        attempts_left = len(ports) * len(addresses)
        for port in ports:
            for address in addresses:
                try:
                    connect_budget = deadline.remaining() / attempts_left
                    attempts_left -= 1
                    payload = _request_catalog(host, port, scheme, address, deadline, connect_timeout=connect_budget)
                    catalog, domain, http_port = _catalog(payload, expected)
                except ssl.SSLCertVerificationError as exc:
                    errors.append(f"port {port}: TLS certificate verification failed; use a hostname matching a trusted certificate")
                except (OSError, http.client.HTTPException) as exc:
                    errors.append(f"port {port}: {type(exc).__name__}; check reachability and whether HTTP or HTTPS is required")
                except PortmapError as exc:
                    errors.append(f"port {port}: {exc}")
                else:
                    authority = f"[{host}]" if ":" in host else host
                    return GatewayInfo(domain, http_port, address[1][0], scheme, catalog, f"{scheme}://{authority}:{port}/registry.json")
        raise PortmapError("no portmap gateway found: " + "; ".join(dict.fromkeys(errors)))

    return _bounded_probe(probe, deadline)


def _ssh_target(target: str) -> str:
    target = _clean_target(target)
    if len(target) > 255 or target.count("@") > 1:
        raise PortmapError("SSH target must be a literal alias, hostname, or user@host")
    user, separator, host = target.rpartition("@")
    if not separator:
        host = target
    elif not user or user.startswith("-") or not _SSH_NAME.fullmatch(user):
        raise PortmapError("SSH target contains an invalid username")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not host or host.startswith("-") or not _SSH_NAME.fullmatch(host):
            raise PortmapError("SSH target must be a literal alias or hostname, not options, a URL, or a wildcard")
    if "%" in host:
        raise PortmapError("scoped SSH addresses are not supported; use a configured literal alias")
    return f"{user}@{host}" if separator else host


def _ssh_config(config_file: Path | None, *, safe_resolution: bool = False, system: bool = False) -> tuple[Path, list[str]]:
    try:
        root = Path(config_file).expanduser() if config_file is not None else Path.home() / ".ssh" / "config"
    except RuntimeError as exc:
        raise PortmapError("cannot expand the SSH config home directory") from exc
    include_base = Path("/etc/ssh") if system else Path.home() / ".ssh"
    aliases: list[str] = []
    alias_set: set[str] = set()
    visited: set[tuple[int, int]] = set()
    total_bytes = 0

    def visit(path: Path, depth: int, *, required: bool) -> None:
        nonlocal total_bytes
        if depth > MAX_SSH_INCLUDE_DEPTH:
            raise PortmapError("SSH Include nesting exceeds the discovery limit; simplify the local configuration")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            if required:
                raise PortmapError(f"SSH config not found: {path}") from exc
            return
        except OSError as exc:
            raise PortmapError(f"cannot read SSH config: {path}") from exc
        with os.fdopen(fd, "rb") as file:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise PortmapError(f"SSH config must be a regular file: {path}")
            identity = (metadata.st_dev, metadata.st_ino)
            if identity in visited:
                return
            visited.add(identity)
            if len(visited) > MAX_SSH_CONFIG_FILES:
                raise PortmapError("SSH Include count exceeds the discovery limit")
            raw = file.read(MAX_SSH_CONFIG_BYTES - total_bytes + 1)
        total_bytes += len(raw)
        if total_bytes > MAX_SSH_CONFIG_BYTES:
            raise PortmapError("SSH config exceeds the discovery input-size limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise PortmapError(f"SSH config is not UTF-8: {path}") from exc
        for number, line in enumerate(text.splitlines(), 1):
            statement = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9]*)(?:\s*=\s*|\s+|$)(.*)", line)
            if statement is None:
                continue
            key, raw_arguments = statement.groups()
            try:
                arguments = shlex.split(raw_arguments, comments=True, posix=True)
            except ValueError as exc:
                raise PortmapError(f"invalid quoting in SSH config {path}:{number}") from exc
            key = key.lower()
            if key == "host":
                for alias in arguments:
                    if alias.startswith(("!", "-")) or any(character in alias for character in "*?["):
                        continue
                    try:
                        _ssh_target(alias)
                    except PortmapError:
                        continue
                    if "@" not in alias and alias not in alias_set:
                        alias_set.add(alias)
                        aliases.append(alias)
            elif key == "include":
                for value in arguments:
                    if any(character in value for character in "%$"):
                        if safe_resolution:
                            raise PortmapError("cannot safely inspect token-expanded SSH Includes; use literal paths or filesystem globs")
                        continue
                    try:
                        include = Path(value).expanduser()
                    except RuntimeError as exc:
                        raise PortmapError(f"cannot expand an SSH Include home directory in {path}:{number}") from exc
                    if not include.is_absolute():
                        include = include_base / include
                    matches = []
                    for match in glob.iglob(str(include)):
                        matches.append(match)
                        if len(matches) > MAX_SSH_CONFIG_FILES:
                            raise PortmapError("SSH Include glob exceeds the discovery file-count limit")
                    for match in sorted(matches):
                        visit(Path(match), depth + 1, required=False)
            elif safe_resolution and key == "match" and any(argument.lstrip("!").partition("=")[0].lower() == "exec" for argument in arguments):
                raise PortmapError("cannot inspect SSH Match exec safely: ssh -G can execute it; use a config without Match exec")

    try:
        visit(root, 0, required=config_file is not None and not system)
    except OSError as exc:
        raise PortmapError(f"cannot read SSH configuration beneath {root}") from exc
    return root, aliases


def discover_ssh_hosts(config_file: Path | None = None) -> list[str]:
    """List positive literal aliases only, without executing SSH.

    Filesystem Include globs are bounded, sorted, and interpreted relative to
    ~/.ssh as OpenSSH user configuration requires, not the including file.
    Host wildcards and Match conditions never synthesize candidates.
    """
    return _ssh_config(config_file)[1]


def _ssh_output(command: list[str], timeout: float) -> str:
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except FileNotFoundError as exc:
        raise PortmapError("OpenSSH ssh is not installed; install it to inspect an SSH target") from exc
    except OSError as exc:
        raise PortmapError("cannot start OpenSSH to inspect the selected target") from exc
    deadline = time.monotonic() + timeout
    output = bytearray()
    size = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PortmapError("ssh -G timed out; check the selected alias and local SSH configuration")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(chunk)
                    if size > MAX_SSH_OUTPUT_BYTES:
                        raise PortmapError("ssh -G output exceeds the discovery size limit")
                    if key.data:
                        output.extend(chunk)
        try:
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise PortmapError("ssh -G timed out; check the local SSH configuration") from exc
        if returncode != 0:
            # stderr and the full -G output may contain commands, tokens, and
            # key paths. Never include either in user-facing errors.
            raise PortmapError(f"ssh -G failed (exit {returncode}); check the selected alias and SSH configuration locally")
        try:
            return output.decode("utf-8")
        except UnicodeError as exc:
            raise PortmapError("ssh -G returned invalid text") from exc
    except OSError as exc:
        raise PortmapError("cannot read ssh -G output; check the local OpenSSH installation") from exc
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def resolve_ssh_host(target: str, *, config_file: Path | None = None, timeout: float = 3) -> dict:
    """Inspect one target using OpenSSH, without authentication or proxy execution.

    User and system configuration are inspected before OpenSSH reads them;
    an explicit config follows normal -F semantics (no system config).
    Match exec and token-expanded Includes are refused. ProxyCommand is only
    a boolean to avoid leaking secrets; actual SSH reuses the target/config.
    """
    target = _ssh_target(target)
    timeout = _timeout(timeout)
    root, _ = _ssh_config(config_file, safe_resolution=True)
    command = ["ssh", "-G"]
    if config_file is None:
        _ssh_config(Path("/etc/ssh/ssh_config"), safe_resolution=True, system=True)
    else:
        command.extend(["-F", str(root)])
    text = _ssh_output([*command, "--", target], timeout)
    values = {}
    for line in text.splitlines():
        key, separator, value = line.partition(" ")
        if separator and key.lower() in {"hostname", "user", "port", "proxycommand", "proxyjump"}:
            values.setdefault(key.lower(), value.strip())
    try:
        hostname = _ssh_target(values["hostname"])
        if "@" in hostname:
            raise ValueError("invalid hostname")
        user = values["user"]
        if not user or user.startswith("-") or not _SSH_NAME.fullmatch(user):
            raise ValueError("invalid user")
        port = _port(int(values["port"]), field="SSH port")
    except (KeyError, ValueError, PortmapError) as exc:
        raise PortmapError("ssh -G did not return a valid hostname, user, and port") from exc
    proxy_jump = values.get("proxyjump")
    if proxy_jump is not None and proxy_jump.lower() == "none":
        proxy_jump = None
    if proxy_jump is not None:
        # Preserve ordinary jump identities, but never echo URL credentials,
        # shell metacharacters, control characters, or arbitrary command text.
        if any(not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?(?:[A-Za-z0-9_.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?", jump) for jump in proxy_jump.split(",")):
            proxy_jump = "[configured]"
    return {
        "target": target,
        "hostname": hostname,
        "user": user,
        "port": port,
        "proxy_command": values.get("proxycommand", "none").lower() not in {"none", ""},
        "proxy_jump": proxy_jump,
        "auth_verified": False,
    }


def discover_targets(target: str | None = None, *, config_file: Path | None = None, timeout: float = 3) -> dict:
    """Inventory local aliases and optionally probe only the supplied direct target."""
    timeout = _timeout(timeout)
    if target is not None:
        try:
            _ssh_target(target)
        except PortmapError:
            _gateway_target(target)
    hosts = discover_ssh_hosts(config_file)
    result = {
        "target": target,
        "direct": None,
        "direct_error": None,
        "ssh_hosts": hosts,
        "ssh_candidates": [],
        "guidance": [
            "Choose a reachable gateway IP/HTTP(S) URL or one explicit local SSH alias.",
            "SSH candidates come from local configuration only; authentication has not been tested.",
            "Host patterns are not expanded; manual aliases such as a .coder target are accepted.",
        ],
    }
    if target is None:
        return result
    try:
        info = probe_gateway(target, timeout=timeout)
        result["direct"] = {
            "domain": info.domain,
            "http_port": info.http_port,
            "address": info.address,
            "scheme": info.scheme,
            "catalog": info.catalog,
            "catalog_url": info.catalog_url,
        }
    except PortmapError as exc:
        result["direct_error"] = str(exc)
    try:
        candidate = _ssh_target(target)
    except PortmapError:
        try:
            host, _, _ = _gateway_target(target)
            candidate = _ssh_target(host)
        except PortmapError:
            return result
    result["ssh_candidates"] = [candidate] if candidate in hosts or "@" in target else []
    if not result["ssh_candidates"]:
        result["manual_ssh_target"] = candidate
        result["guidance"].append("No concrete SSH alias was found; provide the actual SSH target with --via ssh.")
    return result
