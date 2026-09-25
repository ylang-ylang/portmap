"""Local CoreDNS plus an isolated OS resolver route for .portmap only."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .client_runtime import find_native_binary, native_environment
from .errors import PortmapError

# Older systemd-resolved (including Ubuntu 22.04's 249) cannot contact a
# loopback DNS address through a non-loopback routing link. Bind to our own
# link-local dummy address instead; no physical-interface DNS is changed.
DNS_ADDRESS = "169.254.254.53" if sys.platform.startswith("linux") else "127.0.0.1"
DNS_PORT = 1053
MARKER_NAME = "_portmap-client.portmap"


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
            f"system DNS does not resolve *.{domain} to {address}; check existing "
            "more-specific resolver routes and /etc/resolv.conf before connecting"
        )


def _state_file(state_dir: Path) -> Path:
    return state_dir / "dns.json"


def _load(state_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(_state_file(state_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise PortmapError(f"invalid client DNS state: {exc}") from exc
    if not isinstance(data, dict):
        raise PortmapError("invalid client DNS state")
    return data


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
    domain = value.lower().strip(".")
    labels = domain.split(".")
    if (
        not domain.endswith(".portmap")
        or len(domain) > 253
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    ):
        raise PortmapError(f"client DNS only accepts host subdomains of .portmap: {value!r}")
    return domain


def render_corefile(records: Mapping[str, str], marker: str) -> str:
    if not re.fullmatch(r"[a-z0-9:-]+", marker):
        raise PortmapError("invalid client DNS ownership marker")
    rows = [f".:{DNS_PORT} {{", f"    bind {DNS_ADDRESS}", "    reload 2s 1s", "    errors"]
    rows += [f"    template IN TXT {MARKER_NAME} {{", f'        answer "{{{{ .Name }}}} 0 IN TXT \\"{marker}\\""', "    }"]
    # A more-specific registered suffix always precedes its parent.
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
    rows += ["    template ANY ANY portmap {", "        rcode NXDOMAIN", "    }", "    template ANY ANY {", "        rcode REFUSED", "    }", "}", ""]
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


def _marker(timeout: float = 0.3) -> str | None:
    """Query our TXT ownership record; no DNS server is implemented here."""
    ident = secrets.randbelow(65536)
    name = b"".join(bytes([len(label)]) + label.encode("ascii") for label in MARKER_NAME.split(".")) + b"\x00"
    question = name + struct.pack("!HH", 16, 1)
    request = struct.pack("!6H", ident, 0x0100, 1, 0, 0, 0) + question
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(timeout)
            probe.connect((DNS_ADDRESS, DNS_PORT))
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


def _owned_process(state_dir: Path, state: dict[str, Any]) -> bool:
    pid = state.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    config = str((state_dir / "Corefile").resolve())
    if sys.platform.startswith("linux"):
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
            return config.encode() in argv and b"-conf" in argv and Path(f"/proc/{pid}").stat().st_uid == os.getuid()
        except OSError:
            return False
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "uid=,args="], capture_output=True, text=True, timeout=2, check=False, env=native_environment())
        uid, args = result.stdout.strip().split(None, 1)
        return int(uid) == os.getuid() and config in args and "-conf" in args
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def dns_status(state_dir: Path) -> dict[str, Any]:
    state = _load(state_dir)
    marker = _marker() if state else None
    running = bool(_owned_process(state_dir, state) and marker and marker.startswith(str(state.get("owner", "")) + ":"))
    return {"running": running, "address": DNS_ADDRESS, "port": DNS_PORT, "pid": state.get("pid"), "marker": marker}


def sync_dns(state_dir: Path, records: Mapping[str, str]) -> dict[str, Any]:
    """Caller serializes client mutations. Publish only after reload is observable."""
    state_dir = state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = _load(state_dir)
    owner = state.get("owner") or secrets.token_hex(16)
    version = hashlib.sha256(json.dumps(dict(records), sort_keys=True).encode()).hexdigest()[:24]
    marker = f"{owner}:{version}"
    config = state_dir / "Corefile"
    content = render_corefile(records, marker)
    owned = _owned_process(state_dir, state)
    observed = _marker()
    if observed and not observed.startswith(f"{owner}:"):
        raise PortmapError(f"DNS port {DNS_ADDRESS}:{DNS_PORT} is owned by another client")
    if observed and not owned:
        raise PortmapError("client DNS listener is live but process ownership cannot be verified; refusing to replace it")
    previous = config.read_text(encoding="utf-8") if config.exists() else None
    _atomic_text(config, content)
    process = None
    try:
        if not owned:
            binary = find_native_binary("coredns")
            if binary is None:
                raise PortmapError("client DNS needs CoreDNS: use a portmap-client release bundle or install coredns on PATH (brew install coredns)")
            with (state_dir / "dns.log").open("ab") as log:
                process = subprocess.Popen([str(binary), "-conf", str(config)], env=native_environment(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            state = {"pid": process.pid, "owner": owner}
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise PortmapError(f"CoreDNS exited; see {state_dir / 'dns.log'}")
            if _marker() == marker:
                _atomic_text(_state_file(state_dir), json.dumps(state) + "\n")
                return {"running": True, "address": DNS_ADDRESS, "port": DNS_PORT}
            time.sleep(0.1)
        raise PortmapError(f"CoreDNS did not publish the requested zones; see {state_dir / 'dns.log'}")
    except Exception:
        if previous is None:
            config.unlink(missing_ok=True)
        else:
            _atomic_text(config, previous)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        raise


def stop_dns(state_dir: Path) -> None:
    state = _load(state_dir)
    if _owned_process(state_dir, state):
        os.kill(state["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _owned_process(state_dir, state):
            time.sleep(0.1)
        if _owned_process(state_dir, state):
            raise PortmapError("client CoreDNS did not stop; ownership state retained")
    elif state:
        marker = _marker()
        if marker and marker.startswith(str(state.get("owner", "")) + ":"):
            raise PortmapError("client DNS is live but its process ownership is uncertain; refusing to discard state")
    _state_file(state_dir).unlink(missing_ok=True)


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


def resolver_status(state_dir: Path) -> dict[str, Any]:
    if sys.platform.startswith("linux"):
        link = _linux_link(state_dir)
        if not link or link.get("ifalias") != _link_owner(state_dir):
            return {"installed": False, "platform": "linux"}
        try:
            dns = _command(["resolvectl", "dns", _link_name(state_dir)])
            domains = _command(["resolvectl", "domain", _link_name(state_dir)])
            installed = f"{DNS_ADDRESS}:{DNS_PORT}" in dns and "~portmap" in domains.split()
        except PortmapError:
            installed = False
        return {"installed": installed, "platform": "linux", "interface": _link_name(state_dir)}
    if sys.platform == "darwin":
        path = Path("/etc/resolver/portmap")
        try:
            installed = path.read_text(encoding="utf-8") == _mac_content(state_dir)
        except OSError:
            installed = False
        return {"installed": installed, "platform": "darwin", "path": str(path)}
    return {"installed": False, "platform": sys.platform}


def _mac_content(state_dir: Path) -> str:
    return f"# {_link_owner(state_dir)}\nnameserver {DNS_ADDRESS}\nport {DNS_PORT}\n"


def setup_resolver(state_dir: Path, *, use_sudo: bool = True) -> dict[str, Any]:
    """Only our suffix/virtual link is changed; existing global DNS stays intact."""
    if sys.platform.startswith("linux"):
        for binary in ("ip", "resolvectl"):
            if not shutil.which(binary):
                raise PortmapError("automatic Linux split DNS needs iproute2 and systemd-resolved")
        _command(["resolvectl", "status"])
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
            _command(["ip", "link", "set", name, "up"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "dns", name, f"{DNS_ADDRESS}:{DNS_PORT}"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "domain", name, "~portmap"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "default-route", name, "no"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "dnssec", name, "no"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "dnsovertls", name, "no"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "llmnr", name, "no"], privileged=True, use_sudo=use_sudo)
            _command(["resolvectl", "mdns", name, "no"], privileged=True, use_sudo=use_sudo)
        except Exception:
            current = _linux_link(state_dir) or {}
            if created_index is not None and current.get("ifindex") == created_index:
                _command(["ip", "link", "delete", name], privileged=True, use_sudo=use_sudo)
            raise
    elif sys.platform == "darwin":
        path = Path("/etc/resolver/portmap")
        content = _mac_content(state_dir)
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise PortmapError(f"refusing to overwrite existing resolver {path}")
        _command(["mkdir", "-p", "/etc/resolver"], privileged=True, use_sudo=use_sudo)
        temp = state_dir / "resolver.conf"
        _atomic_text(temp, content)
        _command(["install", "-m", "644", str(temp), str(path)], privileged=True, use_sudo=use_sudo)
    else:
        raise PortmapError(f"automatic split DNS is unsupported on {sys.platform}")
    status = resolver_status(state_dir)
    if not status["installed"]:
        raise PortmapError("system resolver did not accept the .portmap route")
    return status


def teardown_resolver(state_dir: Path, *, use_sudo: bool = True) -> None:
    if sys.platform.startswith("linux"):
        link = _linux_link(state_dir)
        if link:
            if link.get("ifalias") != _link_owner(state_dir):
                raise PortmapError("refusing to remove an unrelated DNS interface")
            _command(["ip", "link", "delete", _link_name(state_dir)], privileged=True, use_sudo=use_sudo)
    elif sys.platform == "darwin":
        path = Path("/etc/resolver/portmap")
        if path.exists():
            if path.read_text(encoding="utf-8") != _mac_content(state_dir):
                raise PortmapError("resolver was changed externally; refusing to delete it")
            _command(["rm", str(path)], privileged=True, use_sudo=use_sudo)
    else:
        raise PortmapError(f"automatic split DNS is unsupported on {sys.platform}")
