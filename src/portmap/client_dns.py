"""OS resolver integration and DNS wire/config helpers for the containerized portmap DNS.

The portmap DNS server runs in a container and renders its Corefile with
:func:`render_corefile`; this module owns only the host-side resolver
integration (see :func:`resolver_plan` for the backend selection: the
default Linux /etc/resolv.conf forward takeover at 127.0.0.1:53, the
policy-preserving systemd-resolved split route for loopback stubs, the
macOS /etc/resolver file, or an externally preconfigured suffix route)
and the DNS wire-format probe used to prove a listener.
No CoreDNS process is spawned, inspected, or stopped here.
"""
from __future__ import annotations

import errno
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
from typing import Any, Mapping, Sequence

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

# Resolver backend selection (see resolver_plan). "forward" rewrites the
# visible /etc/resolv.conf to the container DNS published at loopback :53
# and forwards ordinary names to the original upstreams (the Linux
# default); "resolved" is the legacy policy-preserving systemd-resolved
# split route for loopback-stub policies; "darwin" is the macOS
# /etc/resolver file; "external" expects a preconfigured suffix route to
# the loopback entrypoint and writes nothing on the host.
FORWARD_ADDRESS = "127.0.0.1"
FORWARD_PORT = 53
EXTERNAL_ADDRESS = "127.0.0.1"
EXTERNAL_PORT = DNS_PORT
_BACKENDS = ("resolved", "forward", "external", "darwin")

_RESOLV_CONF = Path("/etc/resolv.conf")

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
    ordered_forward: bool = False,
) -> list[str]:
    rows = [f".:{port} {{", "    reload 2s 1s", "    errors"]
    rows += _marker_rows(marker)
    rows += _record_rows(records)
    rows += ["    template ANY ANY portmap {", "        rcode NXDOMAIN", "    }"]
    if refusal:
        rows += ["    template ANY ANY {", "        rcode REFUSED", "    }"]
    if forward is not None:
        if ordered_forward:
            rows += [f"    forward . {forward} {{", "        policy sequential", "    }"]
        else:
            rows += [f"    forward . {forward}"]
    rows += ["}", ""]
    return rows


def _validated_upstream(value: object, label: str) -> str:
    """Accept only upstreams the DNS container can actually reach.

    Host loopback, wildcard, multicast, reserved, and the portmap DNS address
    itself are rejected: the container could not reach them, and silently
    substituting public servers instead would change whose DNS is used.
    """
    if not isinstance(value, str):
        raise PortmapError(f"invalid {label}: {value!r}")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise PortmapError(f"invalid {label}: {value!r}") from exc
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_reserved or value == DNS_ADDRESS:
        raise PortmapError(
            f"unusable {label} {value!r}: the DNS container cannot forward to host loopback, "
            "wildcard, or the portmap DNS address itself, and portmap refuses to guess public servers"
        )
    return value


def render_corefile(
    host_records: Mapping[str, str],
    container_records: Mapping[str, str],
    marker: str,
    *,
    forward: str = "/etc/resolv.conf",
    public: Mapping[str, Any] | None = None,
    host_forward: Sequence[str] | None = None,
    container_forward: Sequence[str] | None = None,
) -> str:
    """Corefile for the single containerized portmap DNS server.

    Host requests use 1053; managed containers use 53 with external forwarding.
    A migrated, explicitly published legacy DNS listener uses a separate 1054
    view containing only its old public records, never the private remote map.
    ``host_forward`` lists the original upstream servers for the host view:
    when given, everything outside the ``.portmap`` templates is passed
    through to them instead of being refused (the forward resolver backend);
    when None the historical REFUSED-only host view is rendered unchanged.
    ``container_forward`` pins captured upstreams in the reloadable container
    view instead of relying on the container's possibly stale resolver file.
    """
    if not re.fullmatch(r"[a-z0-9:-]+", marker):
        raise PortmapError("invalid client DNS ownership marker")
    forward = str(forward)
    if not re.fullmatch(r"\S+", forward):
        raise PortmapError("invalid DNS forwarder specification")
    if host_forward is None:
        host_forward_spec: str | None = None
    else:
        upstreams = [_validated_upstream(value, "host forward upstream") for value in host_forward]
        if not upstreams:
            raise PortmapError("host forward needs at least one original upstream server")
        host_forward_spec = " ".join(upstreams)
    if container_forward is not None:
        upstreams = [_validated_upstream(value, "container forward upstream") for value in container_forward]
        if not upstreams:
            raise PortmapError("container forward needs at least one original upstream server")
        forward = " ".join(upstreams)
    rows = (
        _server_block(
            DNS_PORT, marker, host_records, refusal=host_forward_spec is None,
            forward=host_forward_spec, ordered_forward=True,
        )
        + _server_block(
            _CONTAINER_SCOPE_PORT, marker, container_records,
            forward=forward, ordered_forward=container_forward is not None,
        )
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


def verify_forwarding(address: str, port: int, *, name: str | None = None, timeout: float = 2.5) -> None:
    """Prove non-.portmap forwarding through the published DNS listener.

    Sends one bounded A query for a neutral name and accepts NOERROR or
    NXDOMAIN (a legitimate answer for a random label): either means the
    query really traversed the container to the original upstreams and
    back, so pointing the host resolver at this listener cannot strand
    ordinary DNS. SERVFAIL, REFUSED, and timeouts are failures. No public
    internet is required and no public resolver is hardcoded.
    """
    port = _port(port, "probe port")
    probe_name = name if name is not None else "localhost"
    if not isinstance(probe_name, str):
        raise PortmapError(f"invalid forwarding probe name: {probe_name!r}")
    labels = probe_name.strip(".").split(".")
    if not labels or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
        raise PortmapError(f"invalid forwarding probe name: {probe_name!r}")
    wire = b"".join(bytes([len(label)]) + label.lower().encode("ascii") for label in labels) + b"\x00"
    ident = secrets.randbelow(65536)
    request = struct.pack("!6H", ident, 0x0100, 1, 0, 0, 0) + wire + struct.pack("!HH", 1, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(timeout)
            probe.connect((str(address), port))
            probe.send(request)
            packet = probe.recv(4096)
        received, flags = struct.unpack_from("!2H", packet)
    except (OSError, struct.error) as exc:
        raise PortmapError(
            f"forwarded DNS through {address}:{port} did not answer for {probe_name} ({exc}); "
            "the original upstreams may be unreachable from the DNS container, so the resolver is left unchanged"
        ) from exc
    if received != ident or not flags & 0x8000:
        raise PortmapError(f"forwarded DNS through {address}:{port} returned an unmatched answer for {probe_name}")
    rcode = flags & 0xF
    if rcode not in (0, 3):
        reason = {1: "FORMERR", 2: "SERVFAIL", 4: "NOTIMP", 5: "REFUSED"}.get(rcode, f"rcode {rcode}")
        raise PortmapError(
            f"forwarded DNS through {address}:{port} answered {reason} for {probe_name}; "
            "the DNS container may not have loaded the original upstreams, so the resolver is left unchanged"
        )


# --- owned resolver state ---------------------------------------------------------


def _resolver_file(state_dir: Path) -> Path:
    return state_dir / "resolver.json"


def _state_original(value: object, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") not in ("symlink", "bind", "regular"):
        raise PortmapError(f"invalid portmap resolver state {path}")
    if not isinstance(value.get("content"), str):
        raise PortmapError(f"invalid portmap resolver state {path}")
    for key in ("dev", "ino", "mode", "uid", "gid"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int):
            raise PortmapError(f"invalid portmap resolver state {path}")
    target = value.get("target")
    if value["kind"] == "symlink" and not isinstance(target, str):
        raise PortmapError(f"invalid portmap resolver state {path}")
    return {
        "kind": value["kind"],
        "content": value["content"],
        "target": target,
        "dev": value["dev"],
        "ino": value["ino"],
        "mode": value["mode"],
        "uid": value["uid"],
        "gid": value["gid"],
    }


def _read_resolver_state(state_dir: Path) -> dict[str, Any] | None:
    """Validated owned resolver selection; None when never configured.

    Version 1 files (the historical resolved split route) migrate in memory
    to the backend they were installed with; their recorded address must
    still match the platform value to prove ownership.
    """
    path = _resolver_file(Path(state_dir))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PortmapError(f"invalid portmap resolver state {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PortmapError(f"invalid portmap resolver state {path}")
    if data.get("version") == 1:
        if data.get("address") != DNS_ADDRESS:
            raise PortmapError(f"invalid portmap resolver state {path}")
        backend = "darwin" if sys.platform == "darwin" else "resolved"
        return {
            "version": 2,
            "backend": backend,
            "address": DNS_ADDRESS,
            "port": _port(data.get("port"), f"port in {path}"),
            "forwarders": [],
        }
    if data.get("version") != 2 or data.get("backend") not in _BACKENDS:
        raise PortmapError(f"invalid portmap resolver state {path}")
    state: dict[str, Any] = {"version": 2, **validate_resolver_plan(data)}
    if state["backend"] == "forward":
        state["original"] = _state_original(data.get("original"), path)
        state["active"] = None if data.get("active") is None else _state_original(data.get("active"), path)
        environment = data.get("environment")
        if environment is not None and (
            not isinstance(environment, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items())
        ):
            raise PortmapError(f"invalid portmap resolver state {path}")
        state["environment"] = environment
    return state


def _saved_port(state_dir: Path) -> int | None:
    """Validated port persisted by setup_resolver; None when absent."""
    state = _read_resolver_state(state_dir)
    return None if state is None else state["port"]


def _write_resolver_state(
    state_dir: Path,
    plan: Mapping[str, Any],
    *,
    original: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
    active: Mapping[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "version": 2,
        "backend": plan["backend"],
        "address": plan["address"],
        "port": plan["port"],
        "forwarders": list(plan["forwarders"]),
    }
    if original is not None:
        payload["original"] = dict(original)
        payload["environment"] = dict(environment) if environment else None
        payload["active"] = dict(active) if active else None
    _atomic_text(_resolver_file(state_dir), json.dumps(payload, sort_keys=True) + "\n")


def _link_name(state_dir: Path) -> str:
    suffix = hashlib.sha256(str(state_dir.resolve()).encode()).hexdigest()[:6]
    return f"pm-dns-{suffix}"


def _link_owner(state_dir: Path) -> str:
    return f"portmap-client:{os.getuid()}:{state_dir.resolve()}"


def _command(args: list[str], *, privileged: bool = False, use_sudo: bool = True, input: str | None = None) -> str:
    prefix = ["sudo", "--"] if privileged and os.geteuid() != 0 and use_sudo else []
    try:
        result = subprocess.run([*prefix, *args], capture_output=True, text=True, timeout=20, check=False, env=native_environment(), input=input)
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


# --- resolver backend selection ---------------------------------------------------


def _nameservers(text: str) -> list[str]:
    servers: list[str] = []
    for line in text.splitlines():
        match = re.match(r"\s*nameserver\s+(\S+)", line, re.IGNORECASE)
        if match:
            servers.append(match.group(1))
    return servers


def _resolver_upstream_policy(text: str) -> tuple[list[str], bool]:
    """(usable non-loopback upstreams, whether a loopback stub is offered).

    A loopback nameserver is a local proxy whose split-DNS/VPN/DNSSEC
    policy lives in the proxy rather than in this file; dropping or mixing
    it away would silently change whose DNS answers, so its presence marks
    the whole resolver as stub policy for a forward takeover.
    """
    usable: list[str] = []
    stub = False
    for server in _nameservers(text):
        try:
            address = ipaddress.ip_address(server)
        except ValueError as exc:
            raise PortmapError(f"invalid nameserver in the system resolver: {server!r}") from exc
        if address.is_loopback:
            stub = True
            continue
        if address.is_unspecified or address.is_multicast or address.is_reserved or server == DNS_ADDRESS:
            continue
        if server not in usable:
            usable.append(server)
    return usable, stub


def _stub_policy_error(*, mixed: bool) -> PortmapError:
    situation = (
        "mixes loopback and non-loopback nameservers"
        if mixed
        else "only offers loopback nameservers (for example a systemd-resolved or VPN stub)"
    )
    return PortmapError(
        f"the current system resolver {situation}; portmap refuses to flatten that DNS policy or guess "
        "public servers. Keep the policy-preserving systemd-resolved adapter available, or point "
        f"{_RESOLV_CONF} at the original non-loopback servers, before forwarding directly"
    )


def _usable_upstreams(text: str) -> list[str]:
    """Non-loopback upstreams named by resolver content, in order, deduplicated."""
    return _resolver_upstream_policy(text)[0]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError as exc:
        raise PortmapError(f"cannot read {path}: {exc}") from exc


def _original_upstreams() -> list[str]:
    """Original non-loopback upstreams for the forward backend.

    Read from the current resolver configuration only before the takeover
    (afterwards the saved state is authoritative). A resolver offering a
    loopback stub — alone or mixed with real servers — carries
    split-DNS/VPN/DNSSEC policy that must not be silently flattened to a
    guessed uplink list, and public servers are never substituted.
    """
    usable, stub = _resolver_upstream_policy(_read_text(_RESOLV_CONF))
    if not usable or stub:
        raise _stub_policy_error(mixed=stub and bool(usable))
    return usable


def resolver_environment() -> dict[str, str] | None:
    """Generic Linux environment identity for the resolver journal.

    Combines the kernel boot id, mount/network/user namespaces and PID 1's
    start time. Namespace inode numbers can be recycled after a container
    is destroyed; its new init process distinguishes that generation.
    Plain CLI restarts keep the same identity. No platform or provider
    metadata is consulted. Returns None when no identifier can be read.
    """
    if not sys.platform.startswith("linux"):
        return None
    identity: dict[str, str] = {}
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        boot_id = ""
    if boot_id:
        identity["boot_id"] = boot_id
    for namespace in ("mnt", "net", "user"):
        try:
            value = os.readlink(f"/proc/self/ns/{namespace}")
        except OSError:
            continue
        if value:
            identity[f"{namespace}_ns"] = value
    try:
        stat = Path("/proc/1/stat").read_text(encoding="ascii")
        # The parenthesized comm can contain spaces and closing parentheses.
        # Field 22 is the start time; the first field after comm is field 3.
        start = stat.rsplit(")", 1)[1].split()[19]
        if start.isdecimal():
            identity["init_start_ticks"] = start
    except (OSError, ValueError, IndexError):
        pass
    return identity or None


def _environment_matches(saved: Mapping[str, Any] | None, current: Mapping[str, str] | None) -> bool | None:
    """Whether the saved journal was written in this same environment.

    True: same environment. False: provably a different one (reboot or
    Pod recreation). None: not provable either way (a legacy journal
    without a recorded environment, or the identity could not be read);
    callers must then keep the historical fail-closed behavior.
    """
    recorded = saved.get("environment") if saved is not None else None
    if not isinstance(recorded, dict) or not recorded or not isinstance(current, dict) or not current:
        return None
    if set(recorded) != set(current):
        return None
    return dict(recorded) == dict(current)


def _redirect_is_provably_owned(saved: Mapping[str, Any], live: Mapping[str, Any]) -> bool:
    """Whether a live file carrying the portmap redirect is provably the file portmap wrote.

    Checked at every redirect reuse/restoration boundary (setup refresh,
    teardown, status), in any environment. The journal's post-write
    identity proves kind/device/inode/owner. A journal without one (a
    pre-write or legacy forward journal; forward v2 was never published)
    can still prove a bind-mount rewrite, which kept the backed-up
    original's inode; an unprovable regular replacement fails closed.
    Identical bytes alone never prove ownership: an atomically replaced
    regular file has an unrecorded inode, and a fresh file anywhere can
    carry the same 127.0.0.1 content.
    """
    active = saved.get("active")
    if isinstance(active, dict):
        return all(live.get(key) == active.get(key) for key in ("kind", "dev", "ino", "uid", "gid"))
    original = saved.get("original") or {}
    return (
        original.get("kind") == "bind"
        and live.get("kind") == "bind"
        and (live.get("dev"), live.get("ino")) == (original.get("dev"), original.get("ino"))
    )


def _unprovable_redirect_error(state_dir: Path) -> str:
    """Manual-recovery error for a live redirect whose file ownership is unprovable."""
    return (
        f"{_RESOLV_CONF} carries the portmap DNS redirect but portmap cannot prove this exact file is the one it "
        "wrote (the file was replaced, or a previous takeover was interrupted before its recovery identity was "
        "saved); refusing to reuse or restore over it. The original resolver content remains saved in "
        f"{_resolver_file(Path(state_dir))}; restore it manually if needed, then remove that state file to clear "
        "the portmap resolver state"
    )


_CAP_NET_ADMIN = 12


def _capability(prefix: str, capability: int) -> bool:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith(prefix):
                return bool(int(line.split(None, 1)[1], 16) & (1 << capability))
    except (OSError, ValueError, IndexError):
        pass
    return False


def _net_admin_usable(*, use_sudo: bool) -> bool:
    """Whether this process (or its elevation) can create the dummy link.

    Restricted containers can ship iproute2 without CAP_NET_ADMIN, and a
    capability missing from the bounding set cannot be regained even by
    root: auto must not select that doomed path there. Elevation itself is
    not probed here: with ``use_sudo`` an interactive flow may legitimately
    prompt later, and an explicit denial propagates from that privileged
    command instead of silently degrading to the external backend. Only the
    absence of the sudo binary (or opting out of sudo) rules the path out.
    """
    if not _capability("CapBnd:", _CAP_NET_ADMIN):
        return False
    if os.geteuid() == 0 or _capability("CapEff:", _CAP_NET_ADMIN):
        return True
    return use_sudo and shutil.which("sudo") is not None


def _resolved_available(*, use_sudo: bool = True) -> bool:
    if not (shutil.which("ip") and shutil.which("resolvectl")):
        return False
    if not _net_admin_usable(use_sudo=use_sudo):
        return False
    try:
        _command(["resolvectl", "status"])
    except PortmapError:
        return False
    return True


def _auto_linux_plan(*, use_sudo: bool) -> dict[str, Any]:
    """New-install Linux default; read-only, no privileged probing.

    Plain non-loopback upstreams forward through the container DNS
    published at 127.0.0.1:53, so ordinary names keep using the original
    upstreams. A loopback stub — alone or mixed with real servers —
    carries split-DNS/VPN/DNSSEC policy this file cannot represent, so
    the policy-preserving systemd-resolved adapter is used where it
    actually works, and otherwise the real limitation is reported instead
    of flattening the policy, guessing servers, or silently degrading to
    an unproven external route.
    """
    usable, stub = _resolver_upstream_policy(_read_text(_RESOLV_CONF))
    if usable and not stub:
        return {"backend": "forward", "address": FORWARD_ADDRESS, "port": FORWARD_PORT, "forwarders": usable}
    if _resolved_available(use_sudo=use_sudo):
        return {"backend": "resolved", "address": DNS_ADDRESS, "port": DNS_PORT, "forwarders": []}
    raise PortmapError(
        "no automatic resolver integration can preserve this machine's DNS: the system resolver "
        + (
            "mixes loopback and non-loopback nameservers"
            if stub and usable
            else "offers no non-loopback upstream servers (for example a systemd-resolved or VPN stub)"
        )
        + ", and the policy-preserving systemd-resolved adapter is unavailable here (iproute2/resolvectl "
        "missing, or link administration is not permitted). Install systemd-resolved tooling, point "
        f"{_RESOLV_CONF} at the original non-loopback servers, or configure a suffix route to the "
        "portmap DNS and pass that resolver backend explicitly"
    )


def _rechecked_forward_plan(saved: Mapping[str, Any]) -> dict[str, Any]:
    """Forward plan after the environment recheck; reads, never mutates.

    Same (or unprovable) environment: the saved upstreams stay
    authoritative — after the takeover the live file points at this
    portmap listener and must never be reread as the original. A proven
    new environment re-inspects the live file: this portmap's own redirect
    or the untouched original keeps the saved restorable original, while a
    genuinely fresh file with usable non-loopback upstreams becomes the
    new plan (the dead inode/content of the old environment is never
    reused). Any other fresh file fails closed with a real limitation.
    """
    plan = validate_resolver_plan(saved)
    if _environment_matches(saved, resolver_environment()) is not False:
        return plan
    live = _resolv_identity()
    original = saved["original"]
    if live["content"] in (original["content"], _forward_resolv_content(original["content"])):
        return plan
    usable, stub = _resolver_upstream_policy(live["content"])
    if not usable or stub:
        raise PortmapError(
            f"the resolver file {_RESOLV_CONF} changed in a new environment without usable non-loopback "
            "upstream servers; portmap refuses to flatten a loopback stub, guess public servers, or reuse "
            "the previous environment's original. Point the resolver at the original non-loopback servers "
            "or run teardown to drop the stale portmap resolver state"
        )
    return {"backend": "forward", "address": FORWARD_ADDRESS, "port": FORWARD_PORT, "forwarders": usable}


def resolver_plan(state_dir: Path, *, backend: str = "auto", use_sudo: bool = True) -> dict[str, Any]:
    """Select the resolver backend and its exact entrypoint; mutates nothing.

    Returns ``{"backend", "address", "port", "forwarders"}``; ``forwarders``
    carries the original non-loopback upstream IPs and is non-empty only for
    the forward backend. ``backend="auto"`` keeps the saved owned selection
    (a saved forward journal is rechecked against the current environment so
    a recreated Pod's fresh resolver becomes the new plan), then picks the
    platform default: on Linux the /etc/resolv.conf forward takeover at
    127.0.0.1:53 when the original upstreams are plain non-loopback servers,
    the policy-preserving systemd-resolved adapter for a loopback-stub
    policy where that adapter is usable (otherwise a real, actionable error
    — never a silent external fallback), and the macOS /etc/resolver
    integration on darwin. ``use_sudo`` only gates whether auto's resolved
    availability may rely on sudo elevation; planning itself executes no
    privileged command and never prompts.
    """
    state_dir = Path(state_dir).expanduser().resolve()
    saved = _read_resolver_state(state_dir)
    if saved is not None and backend in {"auto", saved["backend"]}:
        return _rechecked_forward_plan(saved) if saved["backend"] == "forward" else validate_resolver_plan(saved)
    if backend == "auto":
        if sys.platform == "darwin":
            backend = "darwin"
        elif sys.platform.startswith("linux"):
            return _auto_linux_plan(use_sudo=use_sudo)
        else:
            raise PortmapError(f"no automatic resolver backend exists on {sys.platform}; pass backend='forward' or backend='external' explicitly")
    if backend not in _BACKENDS:
        raise PortmapError(f"unknown resolver backend {backend!r}; choose auto, resolved, forward, or external")
    if backend == "forward":
        if not sys.platform.startswith("linux"):
            raise PortmapError("the forward backend replaces /etc/resolv.conf, which macOS ignores; use the darwin integration or the external backend")
        return {"backend": "forward", "address": FORWARD_ADDRESS, "port": FORWARD_PORT, "forwarders": _original_upstreams()}
    if backend == "external":
        return {"backend": "external", "address": EXTERNAL_ADDRESS, "port": EXTERNAL_PORT, "forwarders": []}
    if backend == "darwin":
        if sys.platform != "darwin":
            raise PortmapError("the darwin /etc/resolver integration only exists on macOS")
        return {"backend": "darwin", "address": DNS_ADDRESS, "port": DNS_PORT, "forwarders": []}
    if not sys.platform.startswith("linux"):
        raise PortmapError(f"systemd-resolved is unavailable on {sys.platform}; use the darwin integration (auto on macOS), forward, or external")
    return {"backend": "resolved", "address": DNS_ADDRESS, "port": DNS_PORT, "forwarders": []}


def validate_resolver_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a resolver plan mapping.

    Single source of the backend/address/port/upstream rules, applied to
    :func:`resolver_plan` results, caller-supplied plans, and recorded
    ``state['resolver']`` entries before config/health paths use them.
    Returns the normalized ``{"backend", "address", "port", "forwarders"}``.
    """
    if not isinstance(plan, Mapping):
        raise PortmapError("plan must be the mapping returned by resolver_plan()")
    backend = plan.get("backend")
    if backend not in _BACKENDS:
        raise PortmapError(f"invalid resolver plan backend {backend!r}")
    address = plan.get("address")
    port = _port(plan.get("port"), "port in resolver plan")
    raw = plan.get("forwarders")
    if not isinstance(raw, list):
        raise PortmapError("invalid resolver plan forwarders")
    forwarders = [_validated_upstream(item, "forwarder in resolver plan") for item in raw]
    if backend == "forward":
        if not sys.platform.startswith("linux"):
            raise PortmapError("the forward backend is Linux-only")
        if address != FORWARD_ADDRESS or port != FORWARD_PORT:
            raise PortmapError(f"a forward resolver plan must publish at {FORWARD_ADDRESS}:{FORWARD_PORT}")
        if not forwarders:
            raise PortmapError("a forward resolver plan needs at least one original upstream")
    elif backend == "external":
        if address != EXTERNAL_ADDRESS or port != EXTERNAL_PORT or forwarders:
            raise PortmapError(f"an external resolver plan is {EXTERNAL_ADDRESS}:{EXTERNAL_PORT} without forwarders")
    else:
        if address != DNS_ADDRESS or forwarders:
            raise PortmapError(f"a {backend} resolver plan serves {DNS_ADDRESS} without forwarders")
    if backend == "darwin" and sys.platform != "darwin":
        raise PortmapError("the darwin resolver integration only exists on macOS")
    if backend == "resolved" and not sys.platform.startswith("linux"):
        raise PortmapError("the resolved backend needs systemd-resolved on Linux")
    return {"backend": backend, "address": address, "port": port, "forwarders": forwarders}


def _selected_plan(state_dir: Path, plan: Mapping[str, Any] | None, *, use_sudo: bool) -> dict[str, Any]:
    return validate_resolver_plan(plan) if plan is not None else resolver_plan(state_dir, use_sudo=use_sudo)


def _loopback_bindable() -> bool:
    """Check listener conflicts; the container runtime owns bind permissions."""
    if probe_marker(FORWARD_ADDRESS, FORWARD_PORT) is not None:
        return True
    for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
        with socket.socket(socket.AF_INET, kind) as probe:
            try:
                probe.bind((FORWARD_ADDRESS, FORWARD_PORT))
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    continue
                if exc.errno == errno.EADDRINUSE:
                    return False
                raise PortmapError(f"cannot inspect DNS listener {FORWARD_ADDRESS}:{FORWARD_PORT}: {exc}") from exc
    return True


def prepare_resolver(state_dir: Path, *, use_sudo: bool = True, plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Make the selected backend's prerequisites true, changing no resolver routing.

    The caller proves the container stack is actually serving (for example
    with :func:`probe_marker`) before :func:`setup_resolver` switches the
    existing owned route. resolved: only the owned dummy link/address is
    created and brought up. forward: the original upstreams are captured and
    loopback :53 must be provably publishable by the DNS container (or
    already served by it). external and darwin write nothing and need no
    preparation.
    """
    state_dir = Path(state_dir).expanduser().resolve()
    selected = _selected_plan(state_dir, plan, use_sudo=use_sudo)
    if selected["backend"] == "resolved":
        if not sys.platform.startswith("linux"):
            raise PortmapError(f"the resolved backend is unsupported on {sys.platform}")
        if not shutil.which("ip"):
            raise PortmapError("automatic Linux split DNS needs iproute2")
        created = _ensure_link(state_dir, use_sudo=use_sudo)
        return {"platform": "linux", "backend": "resolved", "interface": _link_name(state_dir), "address": DNS_ADDRESS, "port": DNS_PORT, "created": created}
    if selected["backend"] == "darwin":
        # The published loopback port needs no preparation on macOS.
        return {"platform": "darwin", "backend": "darwin", "address": DNS_ADDRESS, "port": DNS_PORT, "created": False}
    if selected["backend"] == "forward":
        if not _loopback_bindable():
            raise PortmapError(
                f"the forward backend needs loopback {FORWARD_ADDRESS}:{FORWARD_PORT} free for the DNS container; another service already holds it"
            )
        return {
            "platform": sys.platform,
            "backend": "forward",
            "address": FORWARD_ADDRESS,
            "port": FORWARD_PORT,
            "forwarders": list(selected["forwarders"]),
            "bindable": True,
            "created": False,
        }
    return {"platform": sys.platform, "backend": "external", "address": EXTERNAL_ADDRESS, "port": EXTERNAL_PORT, "forwarders": [], "created": False}


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


def resolver_status(state_dir: Path, *, dns_port: int | None = None, plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read-only report of the owned resolver integration.

    The backend is inferred from the saved owned selection (or an explicit
    plan). The forward backend is verified by comparing the live resolver
    file with the content this integration writes: it reports ``pending``
    for a journaled but unapplied original (never ``installed``), and
    ``stale`` when the journal provably predates the current environment
    over a fresh or unprovable file (never ``installed``, and the saved
    original must not be restored); external is verified by probing its
    loopback entrypoint; neither ever executes ip, resolvectl, or systemctl.
    ``dns_port`` overrides the port to report; by default the port saved
    in the owned resolver.json is used, falling back to the backend's
    historical default.
    """
    state_dir = Path(state_dir)
    saved = _read_resolver_state(state_dir)
    selected = (
        validate_resolver_plan(plan)
        if plan is not None
        else None
        if saved is None
        else {"backend": saved["backend"], "address": saved["address"], "port": saved["port"], "forwarders": list(saved["forwarders"])}
    )
    backend = selected["backend"] if selected is not None else None
    saved_port = saved["port"] if saved is not None else None
    if backend in ("forward", "external"):
        default_port = FORWARD_PORT if backend == "forward" else EXTERNAL_PORT
        address = FORWARD_ADDRESS if backend == "forward" else EXTERNAL_ADDRESS
        port = _port(dns_port if dns_port is not None else (saved_port if saved_port is not None else default_port))
        installed = False
        stale = False
        pending = False
        if backend == "forward":
            if saved is not None and saved["backend"] == "forward":
                try:
                    live = _resolv_identity()
                    original = saved["original"]
                    env_changed = _environment_matches(saved, resolver_environment()) is False
                    if live["content"] == _forward_resolv_content(original["content"]):
                        # A live redirect is only the installed integration when the
                        # live file is provably the one portmap wrote; identical
                        # bytes on any other file claim nothing.
                        installed = _redirect_is_provably_owned(saved, live)
                        stale = not installed
                    elif live["content"] == original["content"]:
                        # An interrupted startup: the journal is a pending
                        # recovery, not an actual integration.
                        pending = True
                    elif env_changed:
                        # The journal predates a proven environment change and the
                        # live file is fresh: the saved original is stale and rollback
                        # must never reapply it over the new environment's resolver.
                        stale = True
                except PortmapError:
                    # An unreadable resolver file proves nothing: claim neither
                    # installed, pending, nor stale.
                    installed = False
        else:
            # Only the entrypoint can be probed; the preconfigured OS route
            # itself must be proven by the caller with verify_resolution.
            installed = saved is not None and saved["backend"] == "external" and probe_marker(EXTERNAL_ADDRESS, EXTERNAL_PORT) is not None
        report = {"installed": installed, "stale": stale, "pending": pending, "platform": sys.platform, "backend": backend, "address": address, "port": port, "saved_port": saved_port}
    elif sys.platform.startswith("linux") and backend != "darwin":
        port = _port(dns_port if dns_port is not None else (saved_port if saved_port is not None else DNS_PORT))
        link = _linux_link(state_dir) if shutil.which("ip") else None
        if not link or link.get("ifalias") != _link_owner(state_dir):
            installed = False
        elif not shutil.which("resolvectl"):
            installed = False
        else:
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
        report = {"installed": installed, "platform": "linux", "backend": backend, "interface": _link_name(state_dir), "address": DNS_ADDRESS, "port": port, "saved_port": saved_port}
    elif sys.platform == "darwin" or backend == "darwin":
        port = _port(dns_port if dns_port is not None else (saved_port if saved_port is not None else DNS_PORT))
        parsed = _parse_mac_resolver(state_dir, _MAC_RESOLVER_PATH)
        report = {"installed": parsed == port, "platform": "darwin", "backend": backend, "path": str(_MAC_RESOLVER_PATH), "address": DNS_ADDRESS, "port": port, "saved_port": saved_port}
    else:
        port = _port(dns_port if dns_port is not None else (saved_port if saved_port is not None else DNS_PORT))
        report = {"installed": False, "platform": sys.platform, "backend": backend, "address": selected["address"] if selected else DNS_ADDRESS, "port": port, "saved_port": saved_port}
    if selected is not None:
        report["plan"] = selected
    return report


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


def setup_resolver(state_dir: Path, *, dns_port: int = DNS_PORT, use_sudo: bool = True, plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Install the selected owned resolver integration at the actual port.

    resolved/darwin: the containerized portmap DNS must already be published
    and answering at ``DNS_ADDRESS:dns_port`` (verified with
    :func:`probe_marker`) so the route is never pointed at a dead listener;
    this allows a dynamic port alongside a still-live native 1053 listener
    during migration. Global and physical-interface DNS are never touched;
    if the switch fails, the previously installed owned route is restored and
    the validated port is persisted only after acceptance. forward: the DNS
    container must already answer at 127.0.0.1:53 (a plain resolv.conf cannot
    select a port); the takeover preserves the original resolver content and
    identity and is rolled back exactly on failure. external: only the
    loopback entrypoint is verified and ownership recorded; nothing on the
    host is written.
    """
    port = _port(dns_port, "dns_port")
    state_dir = Path(state_dir).expanduser().resolve()
    selected = _selected_plan(state_dir, plan, use_sudo=use_sudo)
    saved = _read_resolver_state(state_dir)
    if saved is not None and saved["backend"] != selected["backend"]:
        raise PortmapError(
            f"the saved portmap resolver state selects the {saved['backend']} backend; "
            f"call teardown_resolver first to switch to {selected['backend']}"
        )
    if selected["backend"] == "forward":
        if port != FORWARD_PORT:
            raise PortmapError(f"the forward backend must publish at {FORWARD_ADDRESS}:{FORWARD_PORT}; a plain resolv.conf cannot select another port")
        return _setup_forward(state_dir, selected, use_sudo=use_sudo)
    if selected["backend"] == "external":
        if port != EXTERNAL_PORT:
            raise PortmapError(f"the external backend serves the suffix route at {EXTERNAL_ADDRESS}:{EXTERNAL_PORT}")
        return _setup_external(state_dir, selected)
    if probe_marker(DNS_ADDRESS, port) is None:
        raise PortmapError(
            f"no portmap DNS listener is answering at {DNS_ADDRESS}:{port}; "
            "start the container DNS stack before switching the system resolver"
        )
    current = resolver_status(state_dir, dns_port=port)
    if current["installed"]:
        if current["saved_port"] != port:
            _write_resolver_state(state_dir, {"backend": selected["backend"], "address": DNS_ADDRESS, "port": port, "forwarders": []})
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
        _write_resolver_state(state_dir, {"backend": "resolved", "address": DNS_ADDRESS, "port": port, "forwarders": []})
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
        _write_resolver_state(state_dir, {"backend": "darwin", "address": DNS_ADDRESS, "port": port, "forwarders": []})
        return status
    raise PortmapError(f"automatic split DNS is unsupported on {sys.platform}")


def _setup_external(state_dir: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Record ownership of a preconfigured suffix route; write nothing on the host."""
    if probe_marker(EXTERNAL_ADDRESS, EXTERNAL_PORT) is None:
        raise PortmapError(
            f"no portmap DNS listener is answering at {EXTERNAL_ADDRESS}:{EXTERNAL_PORT}; "
            "the external backend needs the loopback entrypoint for the preconfigured suffix route"
        )
    _write_resolver_state(state_dir, plan)
    return {"installed": True, "platform": sys.platform, "backend": "external", "address": EXTERNAL_ADDRESS, "port": EXTERNAL_PORT, "saved_port": EXTERNAL_PORT}


def _forward_probe_name(original_content: str) -> str:
    """Neutral name for the forwarding probe: a random label under the
    original search/domain zone when one exists (proving the real internal
    zone answers, without public internet), else ``localhost`` which every
    real resolver answers locally."""
    for line in original_content.splitlines():
        match = re.match(r"\s*(?:search|domain)\s+([A-Za-z0-9.-]+)", line, re.IGNORECASE)
        if not match:
            continue
        zone = match.group(1).strip(".").rstrip(".")
        labels = zone.split(".")
        # A zone inside .portmap would be answered by our own wildcard
        # template and falsely prove upstream reachability; never probe it.
        if labels[-1].lower() == "portmap":
            continue
        if len(labels) >= 2 and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", item) for item in labels):
            return f"portmap-forward-check-{secrets.token_hex(4)}.{zone}"
    return "localhost"


def _forward_resolv_content(original: str) -> str:
    """Resolver content this integration writes: the single loopback nameserver
    of the published DNS container, every other line (search, options,
    comments) preserved verbatim in its original position."""
    rows: list[str] = []
    replaced = False
    for line in original.splitlines():
        if re.match(r"\s*nameserver\s+\S+", line, re.IGNORECASE):
            if not replaced:
                rows.append(f"nameserver {FORWARD_ADDRESS}")
                replaced = True
            continue
        rows.append(line)
    if not replaced:
        rows.append(f"nameserver {FORWARD_ADDRESS}")
    return "\n".join(rows).rstrip("\n") + "\n"


def _is_bind_mount(stat: os.stat_result, parent_stat: os.stat_result) -> bool:
    """Detect file mounts, including bind mounts on the same filesystem."""
    if stat.st_dev != parent_stat.st_dev:
        return True
    if sys.platform.startswith("linux"):
        try:
            mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        target = str(_RESOLV_CONF.resolve())
        for line in mounts:
            fields = line.split()
            if len(fields) > 4:
                mounted = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])
                if mounted == target:
                    return True
    return False


def _resolv_identity() -> dict[str, Any]:
    """Kind, identity, and current content of the system resolver file."""
    path = _RESOLV_CONF
    try:
        if path.is_symlink():
            target = os.readlink(path)
            stat = path.stat()
            return {
                "kind": "symlink",
                "target": target,
                "content": _read_text(path),
                "mode": stat.st_mode & 0o777,
                "uid": stat.st_uid,
                "gid": stat.st_gid,
                "dev": stat.st_dev,
                "ino": stat.st_ino,
            }
        stat = path.stat()
        return {
            "kind": "bind" if _is_bind_mount(stat, path.parent.stat()) else "regular",
            "target": None,
            "content": _read_text(path),
            "mode": stat.st_mode & 0o777,
            "uid": stat.st_uid,
            "gid": stat.st_gid,
            "dev": stat.st_dev,
            "ino": stat.st_ino,
        }
    except OSError as exc:
        raise PortmapError(f"cannot inspect {path}: {exc}") from exc


def _write_resolver_file(
    state_dir: Path,
    content: str,
    *,
    kind: str,
    mode: int,
    uid: int | None = None,
    gid: int | None = None,
    use_sudo: bool = True,
) -> None:
    """Write resolver content honoring how /etc/resolv.conf is mounted.

    Bind-mounted files keep their inode (rewritten in place, optionally
    through a privileged tee); everything else is replaced atomically when
    the directory is writable, otherwise through a privileged install that
    also restores the recorded mode and owner.
    """
    path = _RESOLV_CONF
    if kind == "bind":
        try:
            with open(path, "r+", encoding="utf-8", errors="surrogateescape") as handle:
                handle.write(content)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            return
        except OSError:
            _command(["tee", str(path)], privileged=True, use_sudo=use_sudo, input=content)
            return
    staged = state_dir / "resolv.conf.staged"
    _atomic_text(staged, content)
    target: Path | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.portmap.", dir=path.parent)
        target = Path(temporary)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(target, mode & 0o777)
        if uid is not None:
            os.chown(target, uid, gid if gid is not None else -1)
        os.replace(target, path)
        return
    except OSError:
        pass
    finally:
        if target is not None:
            target.unlink(missing_ok=True)
    install = ["install", "-m", f"{mode & 0o777:o}"]
    if uid is not None:
        install += ["-o", str(uid), "-g", str(gid if gid is not None else -1)]
    _command([*install, str(staged), str(path)], privileged=True, use_sudo=use_sudo)


def _restore_resolv(state_dir: Path, original: Mapping[str, Any], *, use_sudo: bool) -> None:
    """Restore the original resolver file exactly; never overwrite foreign edits."""
    live = _resolv_identity()
    if live["kind"] == "symlink":
        if (original["kind"] == "symlink" and live["target"] == original["target"]
                and live["content"] == original["content"]):
            return
        raise PortmapError(f"{_RESOLV_CONF} was replaced by a symlink after the portmap takeover; refusing to touch it")
    if original["kind"] != "symlink" and live["content"] == original["content"]:
        return  # nothing was changed, or a previous restore already completed
    ours = _forward_resolv_content(original["content"])
    if live["content"] not in (original["content"], ours):
        raise PortmapError(f"{_RESOLV_CONF} was changed externally after the portmap takeover; refusing to overwrite it")
    if original["kind"] == "symlink":
        target = _RESOLV_CONF.parent / f".{_RESOLV_CONF.name}.portmap.link.{os.getpid()}"
        try:
            os.symlink(original["target"], target)
            os.replace(target, _RESOLV_CONF)
            return
        except OSError:
            target.unlink(missing_ok=True)
        _command(["rm", str(_RESOLV_CONF)], privileged=True, use_sudo=use_sudo)
        _command(["ln", "-s", str(original["target"]), str(_RESOLV_CONF)], privileged=True, use_sudo=use_sudo)
        return
    if original["kind"] == "bind" and live["ino"] != original["ino"]:
        raise PortmapError(f"{_RESOLV_CONF} was replaced since the portmap takeover; refusing to restore over it")
    _write_resolver_file(
        state_dir,
        original["content"],
        kind=original["kind"],
        mode=original["mode"],
        uid=original["uid"],
        gid=original["gid"],
        use_sudo=use_sudo,
    )


def _resolver_settings(original_content: str) -> tuple[list[str], list[str]]:
    """(search/domain values, options values) carried by the original file."""
    search: list[str] = []
    options: list[str] = []
    for line in original_content.splitlines():
        match = re.match(r"\s*(?:search|domain)\s+(\S.*)", line, re.IGNORECASE)
        if match:
            search.append(match.group(1).strip())
            continue
        match = re.match(r"\s*options\s+(\S.*)", line, re.IGNORECASE)
        if match:
            options.append(match.group(1).strip())
    return search, options


def _emit_scope_notice(original_content: str, forwarders: Sequence[str], *, use_sudo: bool) -> None:
    """Concise pre-takeover scope notice on stderr; no prompt, no backend choice."""
    search, options = _resolver_settings(original_content)
    rows = [
        f"portmap: redirecting the visible resolver file {_RESOLV_CONF} to the portmap DNS at {FORWARD_ADDRESS}:{FORWARD_PORT}",
        f"  - .portmap names resolve locally; ordinary names keep forwarding to the original upstreams {', '.join(forwarders)}",
        "  - the original resolver settings are preserved and restored exactly on teardown "
        f"(search: {' '.join(search) if search else 'none'}; options: {' '.join(options) if options else 'none'})",
        f"  - every other user of this same resolver file (for example containers sharing {_RESOLV_CONF}) switches with it",
    ]
    if use_sudo and os.geteuid() != 0 and shutil.which("sudo"):
        rows.append("  - sudo may be used to write the resolver file where its permissions require it")
    print("\n".join(rows), file=sys.stderr)


def _setup_forward(state_dir: Path, plan: Mapping[str, Any], *, use_sudo: bool) -> dict[str, Any]:
    """Point the system resolver at the DNS container published on loopback :53.

    The local marker only proves the listener itself; one real non-.portmap
    query must prove the original upstreams answer through the container
    before the host resolver is rewritten, otherwise ordinary DNS would
    break on an upstream the container cannot reach. The original file and
    the environment it was captured in are journaled before any write. A
    proven environment change (Pod recreation, reboot) re-inspects the live
    file first: a fresh resolver file becomes the new original and plan,
    the same owned redirect keeps its restorable original, and portmap's
    own 127.0.0.1 entry is never backed up as an original.
    """
    if probe_marker(FORWARD_ADDRESS, FORWARD_PORT) is None:
        raise PortmapError(
            f"no portmap DNS listener is answering at {FORWARD_ADDRESS}:{FORWARD_PORT}; "
            "publish the container DNS on loopback port 53 before switching the system resolver"
        )
    identity = _resolv_identity()
    saved = _read_resolver_state(state_dir)
    environment = resolver_environment()
    env_changed = _environment_matches(saved, environment) is False
    if saved is not None:  # the dispatcher already refused foreign saved backends
        original = saved["original"]
        if identity["content"] == _forward_resolv_content(original["content"]):
            if not _redirect_is_provably_owned(saved, identity):
                # Identical redirect bytes never prove ownership — not across a
                # recreated environment, not for a same-environment replacement,
                # and not for an interrupted takeover: never adopt the file.
                raise PortmapError(_unprovable_redirect_error(state_dir))
            verify_forwarding(FORWARD_ADDRESS, FORWARD_PORT, name=_forward_probe_name(original["content"]))
            if env_changed:
                # The same owned redirect survived a proven environment
                # change: keep the restorable original, re-journal the current
                # environment and active identity, and never treat the
                # 127.0.0.1 redirect itself as a new original.
                _write_resolver_state(state_dir, plan, original=original, environment=environment, active=identity)
            return {"installed": True, "platform": sys.platform, "backend": "forward", "address": FORWARD_ADDRESS, "port": FORWARD_PORT, "saved_port": saved["port"]}
        if identity != original:
            if not env_changed:
                # Anything but the untouched original or our own redirect is
                # an external edit in this environment; fail closed.
                raise PortmapError(f"{_RESOLV_CONF} was changed externally after the portmap takeover; refusing to overwrite it")
            if identity["content"] != original["content"]:
                usable, stub = _resolver_upstream_policy(identity["content"])
                if not usable or stub:
                    raise PortmapError(
                        f"the resolver file {_RESOLV_CONF} changed in a new environment without usable non-loopback "
                        "upstream servers; portmap refuses to flatten a loopback stub, guess public servers, or reuse "
                        "the previous environment's original. Point the resolver at the original non-loopback servers, "
                        "or tear down the stale portmap resolver state first"
                    )
                # A recreated environment's genuinely fresh resolver becomes
                # the NEW original and plan; the old backup/inode is never reused.
                plan = {"backend": "forward", "address": FORWARD_ADDRESS, "port": FORWARD_PORT, "forwarders": usable}
            # Same bytes under a fresh identity: acquire the current file as
            # the backup (a pending cutover still resumes against it).
            original = dict(identity)
    else:
        usable, stub = _resolver_upstream_policy(identity["content"])
        if not usable or stub:
            raise _stub_policy_error(mixed=stub and bool(usable))
        if usable != list(plan["forwarders"]):
            raise PortmapError(f"the upstream DNS servers in {_RESOLV_CONF} changed before the portmap takeover; select the resolver again")
        original = dict(identity)
    verify_forwarding(FORWARD_ADDRESS, FORWARD_PORT, name=_forward_probe_name(original["content"]))
    content = _forward_resolv_content(original["content"])
    _emit_scope_notice(original["content"], list(plan["forwarders"]), use_sudo=use_sudo)
    # Save recovery data before changing the resolver. A disk/state write
    # failure must never leave DNS redirected without a restorable original.
    # The pre-write journal records only the original; the active post-write
    # identity is added after the redirect is verified below.
    _write_resolver_state(state_dir, plan, original=original, environment=environment)
    try:
        _write_resolver_file(
            state_dir, content, kind=original["kind"], mode=original["mode"],
            uid=original["uid"], gid=original["gid"], use_sudo=use_sudo,
        )
        live = _resolv_identity()
        if live["content"] != content:
            raise PortmapError(f"the forwarded resolver configuration for {FORWARD_ADDRESS} did not take effect")
        _write_resolver_state(state_dir, plan, original=original, environment=environment, active=live)
    except BaseException as exc:
        try:
            _restore_resolv(state_dir, original, use_sudo=use_sudo)
        except Exception as rollback:
            raise PortmapError(
                f"{exc}; restoring the original resolver failed: {rollback}. "
                "The saved original remains in the portmap resolver state for recovery"
            ) from exc
        _resolver_file(state_dir).unlink(missing_ok=True)
        raise
    return {"installed": True, "platform": sys.platform, "backend": "forward", "address": FORWARD_ADDRESS, "port": FORWARD_PORT, "saved_port": FORWARD_PORT}


def _teardown_forward(state_dir: Path, saved: Mapping[str, Any], *, use_sudo: bool) -> None:
    """Restore the original resolver, or drop a journal proven stale.

    A live file carrying this portmap's redirect — in any environment,
    with or without a recorded active identity — is only restored over
    when portmap can prove it owns that exact file; when it cannot (a
    replaced file with identical bytes, or a takeover whose recovery-
    identity commit and rollback both failed), teardown fails closed
    with a manual-recovery error and keeps the journal. The live file
    must be inspectable: an unreadable one proves nothing. Under a proven
    environment change a genuinely fresh foreign file proves the journal
    stale and the record is dropped without touching it. The in-process
    rollback of a just-performed write bypasses this boundary
    (:func:`_restore_resolv` directly), so a failed active-identity
    journal write never blocks its own same-process rollback.
    """
    original = saved["original"]
    env_changed = _environment_matches(saved, resolver_environment()) is False
    live = _resolv_identity()
    ours = _forward_resolv_content(original["content"])
    if env_changed and live["content"] not in (original["content"], ours):
        return  # provably stale over the new environment's fresh foreign file
    if live["content"] == ours and not _redirect_is_provably_owned(saved, live):
        raise PortmapError(_unprovable_redirect_error(state_dir))
    _restore_resolv(state_dir, original, use_sudo=use_sudo)


def teardown_resolver(state_dir: Path, *, use_sudo: bool = True, plan: Mapping[str, Any] | None = None) -> None:
    """Remove only the owned resolver integration and the saved selection.

    The backend is inferred from the saved owned state (an explicit plan is
    only consulted when nothing was saved). forward restores the original
    resolver file exactly and refuses to overwrite externally changed ones;
    external only drops the ownership record; never-configured machines and
    machines without systemd are tolerated without executing ip/systemctl.
    """
    state_dir = Path(state_dir).expanduser().resolve()
    saved = _read_resolver_state(state_dir)
    if saved is not None:
        backend = saved["backend"]
    elif plan is not None:
        backend = validate_resolver_plan(plan)["backend"]
    else:
        backend = "darwin" if sys.platform == "darwin" else "resolved"
    if backend == "forward":
        if saved is not None and saved["backend"] == "forward":
            _teardown_forward(state_dir, saved, use_sudo=use_sudo)
    elif backend == "external":
        pass  # nothing was ever written on the host
    elif backend == "resolved" and sys.platform.startswith("linux"):
        if shutil.which("ip"):
            link = _linux_link(state_dir)
            if link:
                if link.get("ifalias") != _link_owner(state_dir):
                    raise PortmapError("refusing to remove an unrelated DNS interface")
                _command(["ip", "link", "delete", _link_name(state_dir)], privileged=True, use_sudo=use_sudo)
    elif backend == "darwin" and sys.platform == "darwin":
        if _MAC_RESOLVER_PATH.exists():
            if _parse_mac_resolver(state_dir, _MAC_RESOLVER_PATH) is None:
                raise PortmapError("resolver was changed externally; refusing to delete it")
            _command(["rm", str(_MAC_RESOLVER_PATH)], privileged=True, use_sudo=use_sudo)
    _resolver_file(state_dir).unlink(missing_ok=True)
