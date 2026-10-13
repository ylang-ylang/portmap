"""OS resolver integration and DNS wire/config helpers for the containerized portmap DNS.

The portmap DNS server runs in a container and renders its Corefile with
:func:`render_corefile`; this module owns only the host-side resolver route
for ``.portmap`` and the DNS wire-format probe used to prove a listener.
No CoreDNS process is spawned, inspected, or stopped here.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from .client_runtime import native_environment
from .errors import PortmapError

# Older systemd-resolved (including Ubuntu 22.04's 249) cannot contact a
# loopback DNS address through a non-loopback routing link. The container's
# DNS port is published onto our own link-local dummy address instead; no
# physical-interface DNS is changed. On macOS the published loopback port is
# reachable directly.
DNS_ADDRESS = "169.254.254.53" if sys.platform.startswith("linux") else "127.0.0.1"
DNS_PORT = 1053
MARKER_NAME = "_portmap-client.portmap"

# Container-internal listener that serves container-scope names; the
# host-scope listener reuses DNS_PORT inside the container as well.
_CONTAINER_SCOPE_PORT = 53

_MAC_RESOLVER_PATH = Path("/etc/resolver/portmap")


def verify_resolution(domain: str, address: str, *, timeout: float = 5) -> None:
    """Check the OS resolver, not just CoreDNS: a pre-existing longer suffix
    route or a custom NSS setup must not silently bypass our local mapping.
    """
    name = f"probe-{secrets.token_hex(5)}.{_domain(domain)}"
    results: queue.Queue = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            values = {item[4][0] for item in socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)}
            results.put(values)
        except OSError:
            results.put(set())

    threading.Thread(target=resolve, daemon=True).start()
    try:
        values = results.get(timeout=timeout)
    except queue.Empty:
        values = set()
    if values != {address}:
        raise PortmapError(
            f"system DNS does not resolve *.{domain} to {address}; check OS resolver integration "
            "(for example systemd-resolved through libnss-resolve on Linux), more-specific "
            "resolver routes, and /etc/resolv.conf before connecting"
        )


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".portmap-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def _domain(value: str) -> str:
    if not isinstance(value, str):
        raise PortmapError(f"client DNS only accepts host subdomains of .portmap: {value!r}")
    domain = value.lower().strip(".")
    labels = domain.split(".")
    if (
        not domain.endswith(".portmap")
        or len(domain) > 253
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    ):
        raise PortmapError(f"client DNS only accepts host subdomains of .portmap: {value!r}")
    return domain


def _port(value: object, label: str = "DNS port") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise PortmapError(f"{label} must be an integer between 1 and 65535, got {value!r}")
    return value


def _marker_rows(marker: str) -> list[str]:
    return [
        f"    template IN TXT {MARKER_NAME} {{",
        f'        answer "{{{{ .Name }}}} 0 IN TXT \\"{marker}\\""',
        "    }",
    ]


def _record_rows(records: Mapping[str, str]) -> list[str]:
    """Shared record rendering; a more-specific suffix always precedes its parent."""
    rows: list[str] = []
    for raw_domain, raw_address in sorted(records.items(), key=lambda pair: (-pair[0].count("."), pair[0])):
        domain = _domain(raw_domain)
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise PortmapError(f"invalid client DNS address: {raw_address!r}") from exc
        family = "A" if address.version == 4 else "AAAA"
        other = "AAAA" if family == "A" else "A"
        rows += [f"    template IN {family} {domain} {{", f'        answer "{{{{ .Name }}}} 5 IN {family} {address}"', "    }"]
        rows += [f"    template IN {other} {domain} {{", "        rcode NOERROR", "    }"]
    return rows


def _server_block(
    port: int,
    marker: str,
    records: Mapping[str, str],
    *,
    refusal: bool = False,
    forward: str | None = None,
) -> list[str]:
    rows = [f".:{port} {{", "    reload 2s 1s", "    errors"]
    rows += _marker_rows(marker)
    rows += _record_rows(records)
    rows += ["    template ANY ANY portmap {", "        rcode NXDOMAIN", "    }"]
    if refusal:
        rows += ["    template ANY ANY {", "        rcode REFUSED", "    }"]
    if forward is not None:
        rows += [f"    forward . {forward}"]
    rows += ["}", ""]
    return rows


def render_corefile(
    host_records: Mapping[str, str],
    container_records: Mapping[str, str],
    marker: str,
    *,
    forward: str = "/etc/resolv.conf",
    public: Mapping[str, Any] | None = None,
) -> str:
    """Corefile for the single containerized portmap DNS server.

    Host requests use 1053; managed containers use 53 with external forwarding.
    A migrated, explicitly published legacy DNS listener uses a separate 1054
    view containing only its old public records, never the private remote map.
    """
    if not re.fullmatch(r"[a-z0-9:-]+", marker):
        raise PortmapError("invalid client DNS ownership marker")
    forward = str(forward)
    if not re.fullmatch(r"\S+", forward):
        raise PortmapError("invalid DNS forwarder specification")
    rows = (
        _server_block(DNS_PORT, marker, host_records, refusal=True)
        + _server_block(_CONTAINER_SCOPE_PORT, marker, container_records, forward=forward)
    )
    if public is not None:
        public_forward = public["forward"]
        if not isinstance(public_forward, str) or not re.fullmatch(r"\S+", public_forward):
            raise PortmapError("invalid public DNS forwarder specification")
        wildcard = public["wildcard"]
        suffixes = {public["zone"]: wildcard} if wildcard is not None else {}
        public_rows = _server_block(1054, marker, suffixes, forward=public_forward)
        records = public.get("records", {})
        if records:
            hosts = ["    hosts {"]
            for name, value in sorted(records.items()):
                if not isinstance(name, str) or len(name) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split(".")):
                    raise PortmapError("invalid public DNS host record")
                try:
                    address = ipaddress.ip_address(value)
                except ValueError as exc:
                    raise PortmapError("invalid public DNS host address") from exc
                hosts.append(f"        {address} {name}")
            hosts += ["        ttl 5", "        fallthrough", "    }"]
            public_rows[3:3] = hosts
        rows += public_rows
    return "\n".join(rows)


def _skip_name(packet: bytes, pos: int) -> int:
    while pos < len(packet):
        size = packet[pos]
        if size & 0xC0 == 0xC0:
            if pos + 2 > len(packet):
                break
            return pos + 2
        pos += 1
        if size == 0:
            return pos
        if size > 63 or pos + size > len(packet):
            break
        pos += size
    raise ValueError("truncated DNS name")


def probe_marker(address: str, port: int, *, timeout: float = 0.3) -> str | None:
    """Query a portmap DNS listener's TXT ownership record over the wire.

    Returns the marker string, or None when no portmap DNS listener answers.
    No DNS server is implemented here.
    """
    port = _port(port, "probe port")
    ident = secrets.randbelow(65536)
    name = b"".join(bytes([len(label)]) + label.encode("ascii") for label in MARKER_NAME.split(".")) + b"\x00"
    question = name + struct.pack("!HH", 16, 1)
    request = struct.pack("!6H", ident, 0x0100, 1, 0, 0, 0) + question
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(timeout)
            probe.connect((str(address), port))
            probe.send(request)
            packet = probe.recv(4096)
        received, flags, qd, an, _, _ = struct.unpack_from("!6H", packet)
        if received != ident or flags & 15 or not flags & 0x8000 or qd != 1:
            return None
        pos = _skip_name(packet, 12) + 4
        for _ in range(an):
            pos = _skip_name(packet, pos)
            kind, cls, _, length = struct.unpack_from("!HHIH", packet, pos)
            pos += 10
            data = packet[pos:pos + length]
            pos += length
            if kind == 16 and cls == 1 and data and len(data) == data[0] + 1:
                return data[1:].decode("ascii")
    except (OSError, ValueError, struct.error, UnicodeError):
        pass
    return None


# --- owned resolver state ---------------------------------------------------------


def _resolver_file(state_dir: Path) -> Path:
    return state_dir / "resolver.json"


def _saved_port(state_dir: Path) -> int | None:
    """Validated port persisted by setup_resolver; None when absent."""
    path = _resolver_file(state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PortmapError(f"invalid portmap resolver state {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("address") != DNS_ADDRESS:
        raise PortmapError(f"invalid portmap resolver state {path}")
    return _port(data.get("port"), f"port in {path}")


def _write_resolver_state(state_dir: Path, port: int) -> None:
    payload = {"version": 1, "address": DNS_ADDRESS, "port": port}
    _atomic_text(_resolver_file(state_dir), json.dumps(payload, sort_keys=True) + "\n")


def _link_name(state_dir: Path) -> str:
    suffix = hashlib.sha256(str(state_dir.resolve()).encode()).hexdigest()[:6]
    return f"pm-dns-{suffix}"


def _link_owner(state_dir: Path) -> str:
    return f"portmap-client:{os.getuid()}:{state_dir.resolve()}"


def _command(args: list[str], *, privileged: bool = False, use_sudo: bool = True) -> str:
    prefix = ["sudo", "--"] if privileged and os.geteuid() != 0 and use_sudo else []
    try:
        result = subprocess.run([*prefix, *args], capture_output=True, text=True, timeout=20, check=False, env=native_environment())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortmapError(f"cannot run {args[0]}: {exc}") from exc
    if result.returncode:
        raise PortmapError(f"{args[0]} failed: {(result.stderr or result.stdout).strip()[:1000]}")
    return result.stdout


def _linux_link(state_dir: Path) -> dict[str, Any] | None:
    try:
        result = subprocess.run(["ip", "-j", "link", "show", "dev", _link_name(state_dir)], capture_output=True, text=True, timeout=3, check=False, env=native_environment())
        if result.returncode:
            return None
        return json.loads(result.stdout)[0]
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def _ensure_link(state_dir: Path, *, use_sudo: bool = True) -> bool:
    """Validate/create only the owned dummy link and address, and bring it up.

    Resolver DNS routing is not changed. Returns True when the link was
    created by this call. A link that exists with our name but a foreign
    alias is refused, never adopted.
    """
    name = _link_name(state_dir)
    link = _linux_link(state_dir)
    if link and link.get("ifalias") != _link_owner(state_dir):
        raise PortmapError(f"refusing to change unrelated interface {name}")
    created = link is None
    created_index = None
    try:
        if created:
            # A link-local /32 enables a DNS scope without adding a default route.
            # Reject an address already assigned elsewhere rather than overriding it.
            addresses = json.loads(_command(["ip", "-j", "address", "show"]))
            if any(a.get("local") == DNS_ADDRESS for item in addresses for a in item.get("addr_info", [])):
                raise PortmapError(f"client DNS link address {DNS_ADDRESS} is already assigned")
            _command(["ip", "link", "add", name, "type", "dummy"], privileged=True, use_sudo=use_sudo)
            created_index = (_linux_link(state_dir) or {}).get("ifindex")
            _command(["ip", "link", "set", name, "alias", _link_owner(state_dir)], privileged=True, use_sudo=use_sudo)
            _command(["ip", "address", "add", f"{DNS_ADDRESS}/32", "dev", name], privileged=True, use_sudo=use_sudo)
        if "UP" not in (link or {}).get("flags", []):
            _command(["ip", "link", "set", name, "up"], privileged=True, use_sudo=use_sudo)
    except BaseException as exc:
        current = _linux_link(state_dir) or {}
        if created_index is not None and current.get("ifindex") == created_index:
            try:
                _command(["ip", "link", "delete", name], privileged=True, use_sudo=use_sudo)
            except Exception as cleanup:  # noqa: BLE001 - surfaced together with the cause
                raise PortmapError(f"{exc}; removing the freshly created DNS interface {name} also failed: {cleanup}") from exc
        raise
    return created


def prepare_resolver(state_dir: Path, *, use_sudo: bool = True) -> dict[str, Any]:
    """Make the owned dummy link/address exist and be up, changing no DNS routing.

    The caller proves the container stack is actually serving on this address
    (for example with :func:`probe_marker`) before :func:`setup_resolver`
    switches the existing owned suffix route.
    """
    state_dir = Path(state_dir).expanduser().resolve()
    if sys.platform.startswith("linux"):
        if not shutil.which("ip"):
            raise PortmapError("automatic Linux split DNS needs iproute2")
        created = _ensure_link(state_dir, use_sudo=use_sudo)
        return {"platform": "linux", "interface": _link_name(state_dir), "address": DNS_ADDRESS, "created": created}
    if sys.platform == "darwin":
        # The published loopback port needs no preparation on macOS.
        return {"platform": "darwin", "address": DNS_ADDRESS, "created": False}
    raise PortmapError(f"automatic split DNS is unsupported on {sys.platform}")


def _installed_port(state_dir: Path) -> int | None:
    """Port of the DNS route currently installed on the owned link, if any."""
    try:
        dns = _command(["resolvectl", "dns", _link_name(state_dir)])
    except PortmapError:
        return None
    match = re.search(rf"{re.escape(DNS_ADDRESS)}:(\d+)", dns)
    return int(match.group(1)) if match else None


def _restore_route(state_dir: Path, previous: int | None, *, use_sudo: bool) -> None:
    name = _link_name(state_dir)
    if previous is None:
        _command(["resolvectl", "revert", name], privileged=True, use_sudo=use_sudo)
    else:
        _command(["resolvectl", "dns", name, f"{DNS_ADDRESS}:{previous}"], privileged=True, use_sudo=use_sudo)


def resolver_status(state_dir: Path, *, dns_port: int | None = None) -> dict[str, Any]:
    """Read-only report of the owned ``.portmap`` resolver route.

    ``dns_port`` overrides the port to report on; by default the port saved
    in the owned resolver.json is used, falling back to the historical 1053.
    """
    state_dir = Path(state_dir)
    saved = _saved_port(state_dir)
    port = _port(dns_port if dns_port is not None else (saved if saved is not None else DNS_PORT))
    if sys.platform.startswith("linux"):
        link = _linux_link(state_dir)
        if not link or link.get("ifalias") != _link_owner(state_dir):
            return {"installed": False, "platform": "linux", "interface": _link_name(state_dir), "address": DNS_ADDRESS, "port": port, "saved_port": saved}
        try:
            dns = _command(["resolvectl", "dns", _link_name(state_dir)])
            domains = _command(["resolvectl", "domain", _link_name(state_dir)])
            default_route = _command(["resolvectl", "default-route", _link_name(state_dir)])
            installed = (
                dns.partition(":")[2].split() == [f"{DNS_ADDRESS}:{port}"]
                and domains.partition(":")[2].split() == ["~portmap"]
                and default_route.partition(":")[2].strip() == "no"
            )
        except PortmapError:
            installed = False
        return {"installed": installed, "platform": "linux", "interface": _link_name(state_dir), "address": DNS_ADDRESS, "port": port, "saved_port": saved}
    if sys.platform == "darwin":
        parsed = _parse_mac_resolver(state_dir, _MAC_RESOLVER_PATH)
        return {"installed": parsed == port, "platform": "darwin", "path": str(_MAC_RESOLVER_PATH), "address": DNS_ADDRESS, "port": port, "saved_port": saved}
    return {"installed": False, "platform": sys.platform, "address": DNS_ADDRESS, "port": port, "saved_port": saved}


def _mac_content(state_dir: Path, port: int) -> str:
    return f"# {_link_owner(state_dir)}\nnameserver {DNS_ADDRESS}\nport {port}\n"


def _parse_mac_resolver(state_dir: Path, path: Path) -> int | None:
    """Port recorded in an owned macOS resolver file; None when absent/foreign."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) != 3 or lines[0] != f"# {_link_owner(state_dir)}" or lines[1] != f"nameserver {DNS_ADDRESS}":
        return None
    if not lines[2].startswith("port "):
        return None
    try:
        return int(lines[2][len("port "):])
    except ValueError:
        return None


def setup_resolver(state_dir: Path, *, dns_port: int = DNS_PORT, use_sudo: bool = True) -> dict[str, Any]:
    """Install only the owned ``.portmap`` suffix route at the actual port.

    The containerized portmap DNS must already be published and answering at
    ``DNS_ADDRESS:dns_port`` (verified with :func:`probe_marker`) so the
    route is never pointed at a dead listener; this allows a dynamic port
    alongside a still-live native 1053 listener during migration. Global and
    physical-interface DNS are never touched. If the switch fails, the
    previously installed owned route (its port) is restored; the validated
    port is persisted in the owned resolver.json only after acceptance.
    """
    port = _port(dns_port, "dns_port")
    state_dir = Path(state_dir).expanduser().resolve()
    if probe_marker(DNS_ADDRESS, port) is None:
        raise PortmapError(
            f"no portmap DNS listener is answering at {DNS_ADDRESS}:{port}; "
            "start the container DNS stack before switching the system resolver"
        )
    current = resolver_status(state_dir, dns_port=port)
    if current["installed"]:
        if current["saved_port"] != port:
            _write_resolver_state(state_dir, port)
        return current
    if sys.platform.startswith("linux"):
        for binary in ("ip", "resolvectl"):
            if not shutil.which(binary):
                raise PortmapError("automatic Linux split DNS needs iproute2 and systemd-resolved")
        _command(["resolvectl", "status"])
        _ensure_link(state_dir, use_sudo=use_sudo)
        name = _link_name(state_dir)
        previous = _installed_port(state_dir)
        if previous is None:
            previous = _saved_port(state_dir)
        try:
            _command(["resolvectl", "dns", name, f"{DNS_ADDRESS}:{port}"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "domain", name, "~portmap"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "default-route", name, "no"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "dnssec", name, "no"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "dnsovertls", name, "no"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "llmnr", name, "no"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "mdns", name, "no"], privileged=True, use_sudo=use_sudo)
            status = resolver_status(state_dir, dns_port=port)
            if not status["installed"]:
                raise PortmapError("system resolver did not accept the .portmap route")
        except BaseException as exc:
            try:
                _restore_route(state_dir, previous, use_sudo=use_sudo)
            except Exception as rollback:  # noqa: BLE001 - surfaced together with the cause
                raise PortmapError(f"{exc}; restoring the previous owned DNS route also failed: {rollback}") from exc
            raise
        _write_resolver_state(state_dir, port)
        return status
    if sys.platform == "darwin":
        path = _MAC_RESOLVER_PATH
        content = _mac_content(state_dir, port)
        previous = None
        if path.exists():
            if _parse_mac_resolver(state_dir, path) is None:
                raise PortmapError(f"refusing to overwrite existing resolver {path}")
            previous = path.read_text(encoding="utf-8")
        try:
            _command(["mkdir", "-p", "/etc/resolver"], privileged=True, use_sudo=use_sudo)
            temp = state_dir / "resolver.conf"
            _atomic_text(temp, content)
            _command(["install", "-m", "644", str(temp), str(path)], privileged=True, use_sudo=use_sudo)
            status = resolver_status(state_dir, dns_port=port)
            if not status["installed"]:
                raise PortmapError("system resolver did not accept the .portmap route")
        except BaseException as exc:
            try:
                if previous is not None:
                    temp = state_dir / "resolver.conf.rollback"
                    _atomic_text(temp, previous)
                    _command(["install", "-m", "644", str(temp), str(path)], privileged=True, use_sudo=use_sudo)
                elif _parse_mac_resolver(state_dir, path) == port:
                    _command(["rm", str(path)], privileged=True, use_sudo=use_sudo)
            except Exception as rollback:  # noqa: BLE001 - surfaced together with the cause
                raise PortmapError(f"{exc}; restoring the previous owned resolver also failed: {rollback}") from exc
            raise
        _write_resolver_state(state_dir, port)
        return status
    raise PortmapError(f"automatic split DNS is unsupported on {sys.platform}")


def teardown_resolver(state_dir: Path, *, use_sudo: bool = True) -> None:
    """Remove only the owned resolver integration and the saved port metadata."""
    if sys.platform.startswith("linux"):
        link = _linux_link(state_dir)
        if link:
            if link.get("ifalias") != _link_owner(state_dir):
                raise PortmapError("refusing to remove an unrelated DNS interface")
            _command(["ip", "link", "delete", _link_name(state_dir)], privileged=True, use_sudo=use_sudo)
    elif sys.platform == "darwin":
        if _MAC_RESOLVER_PATH.exists():
            if _parse_mac_resolver(state_dir, _MAC_RESOLVER_PATH) is None:
                raise PortmapError("resolver was changed externally; refusing to delete it")
            _command(["rm", str(_MAC_RESOLVER_PATH)], privileged=True, use_sudo=use_sudo)
    else:
        raise PortmapError(f"automatic split DNS is unsupported on {sys.platform}")
    _resolver_file(Path(state_dir)).unlink(missing_ok=True)
