"""Strict public-only connection codes and read-only node QR export.

Wire v1 is canonical compact sorted Unicode JSON in unpadded base64url.
This transfers infrastructure metadata, never authentication or authorization.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
import stat
import struct
import zlib
from pathlib import Path
from typing import Any

from .errors import PortmapError

PREFIX = "portmap-connect:v1:"
MAX_WIRE_BYTES = 2048
INVALID = "Invalid or unsupported Portmap connection QR code."
_COMMON = {"name", "domain", "mode", "host", "httpPort", "httpsPort"}
_SSH = {"port", "username", "targetHost", "hostKeySha256Hex"}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# ECMAScript String.trim(), shared with PortmapCore (not Python's broader strip).
_JS_SPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def _text(value: Any, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or _CONTROL.search(value):
        raise ValueError(INVALID)
    value.encode("utf-8", errors="strict")
    if len(value.encode("utf-16-le")) // 2 > maximum:
        raise ValueError(INVALID)
    return value


def _dns(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(INVALID)
    result = value.strip(_JS_SPACE).lower().removesuffix(".")
    if not result or len(result) > 253 or not all(_LABEL.fullmatch(label) for label in result.split(".")):
        raise ValueError(INVALID)
    return result


def _host(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(INVALID)
    result = value.strip(_JS_SPACE).lower().removesuffix(".")
    if ":" in result:
        if not re.fullmatch(r"(?:\[[0-9a-f:.]+\]|[0-9a-f:.]+)", result):
            raise ValueError(INVALID)
        address = ipaddress.IPv6Address(result[1:-1] if result.startswith("[") else result)
        # WHATWG uses hexadecimal hextets even for IPv4-mapped addresses;
        # IPv6Address.compressed differs across supported Python versions.
        groups = [format(part, "x") for part in struct.unpack("!8H", address.packed)]
        best_start, best_length, start = 0, 0, 0
        while start < len(groups):
            if groups[start] != "0":
                start += 1
                continue
            end = start + 1
            while end < len(groups) and groups[end] == "0":
                end += 1
            if end - start > best_length:
                best_start, best_length = start, end - start
            start = end
        if best_length >= 2:
            return ":".join(groups[:best_start]) + "::" + ":".join(groups[best_start + best_length:])
        return ":".join(groups)
    result = _dns(result)
    if result == "portmap" or result.endswith(".portmap"):
        raise ValueError(INVALID)
    if re.fullmatch(r"[0-9.]+", result):
        ipaddress.IPv4Address(result)  # Reject shorthand, leading zeroes and invalid octets.
    return result


def _port(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(INVALID)
    return value


def normalize_fingerprint(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(INVALID)
    pin = value.strip(_JS_SPACE)
    if re.fullmatch(r"[a-fA-F0-9]{64}", pin):
        return pin.lower()
    if re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}=?", pin):
        raw = base64.b64decode(pin[7:].rstrip("=") + "=", validate=True)
        if len(raw) == 32 and base64.b64encode(raw).decode("ascii").rstrip("=") == pin[7:].rstrip("="):
            return raw.hex()
    raise ValueError(INVALID)


def _keys(value: Any, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(INVALID)


def normalize(profile: Any) -> dict[str, Any]:
    """Normalize an exact public draft; reject every unknown field, including secrets."""
    try:
        _keys(profile, _COMMON | ({"ssh"} if isinstance(profile, dict) and profile.get("mode") == "ssh" else set()))
        if profile["mode"] not in ("direct", "ssh"):
            raise ValueError(INVALID)
        name = _text(profile["name"], 120).strip(_JS_SPACE)
        if not name:
            raise ValueError(INVALID)
        domain = _dns(profile["domain"])
        if not domain.endswith(".portmap"):
            raise ValueError(INVALID)
        result = {"name": name, "domain": domain, "mode": profile["mode"], "host": _host(profile["host"]),
                  "httpPort": _port(profile["httpPort"]),
                  "httpsPort": None if profile["httpsPort"] is None else _port(profile["httpsPort"])}
        if profile["mode"] == "ssh":
            ssh = profile["ssh"]
            _keys(ssh, _SSH)
            result["ssh"] = {"port": _port(ssh["port"]), "username": _text(ssh["username"], 255, empty=True),
                             "targetHost": _host(ssh["targetHost"]),
                             "hostKeySha256Hex": normalize_fingerprint(ssh["hostKeySha256Hex"])}
        return result
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as exc:
        raise PortmapError(INVALID) from exc


def _encode_normalized(profile: dict[str, Any]) -> str:
    payload = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    wire = PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    if len(wire) > MAX_WIRE_BYTES:
        raise PortmapError(INVALID)
    return wire


def encode(profile: Any) -> str:
    return _encode_normalized(normalize(profile))


def decode(wire: Any) -> dict[str, Any]:
    try:
        if not isinstance(wire, str) or len(wire) > MAX_WIRE_BYTES or not wire.startswith(PREFIX):
            raise ValueError(INVALID)
        payload = wire[len(PREFIX):]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", payload) or len(payload) % 4 == 1:
            raise ValueError(INVALID)
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        profile = normalize(json.loads(raw.decode("utf-8", errors="strict")))
        # Re-encode normalized data: duplicates, Unicode escapes, key order,
        # spacing, floats, BOM, alternative pad bits and aliases all fail closed.
        if _encode_normalized(profile) != wire:
            raise ValueError(INVALID)
        return profile
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, RecursionError) as exc:
        raise PortmapError(INVALID) from exc


def _read_regular(path: Path, *, private: bool = False, maximum: int = 65536) -> bytes:
    """No chmod, creation, lifecycle, sockets or following a final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (private and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
            raise PortmapError("QR metadata must be an owned private regular state file.")
        data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise PortmapError("QR metadata file is too large.")
        return data


def _saved_role(state_dir: Path | None = None) -> dict[str, Any] | None:
    """Read authoritative v2 server namespace/LAN ingress, never active status.

    load_portmap_settings probes container sockets and host routes; server._load
    may repair permissions. Neither is safe for this read-only export command.
    Only pure settings path/TOML helpers are reused to locate the saved role.
    """
    if state_dir is None:
        from .settings import CONFIG_FILE_NAME, USER_CONFIG_PATH, expand_path, load_config_file, resolve_portmap_root, table

        root = resolve_portmap_root(environ=os.environ)
        config = load_config_file(root / CONFIG_FILE_NAME)
        if not config and root != USER_CONFIG_PATH.parent:
            config = load_config_file(USER_CONFIG_PATH)
        configured = table(config, "state").get("dir")
        location = os.environ.get("PORTMAP_STATE_DIR") or configured or str(Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")) / "portmap")
        state_dir = expand_path(str(location), root=root)
    state_dir = state_dir.expanduser().resolve()
    try:
        data = json.loads(_read_regular(state_dir / "server" / "instance.json", private=True))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        raise PortmapError("Cannot read saved node metadata; supply --domain --host --http-port explicitly.") from exc
    try:
        if (not isinstance(data, dict) or data.get("version") != 2 or data.get("uid") != os.getuid()
                or data.get("state_dir") != str(state_dir) or not isinstance(data.get("settings"), dict)):
            raise ValueError()
        _port(data["http_port"])
        domain = _dns(data["settings"]["dns_domain"])
        if not domain.endswith(".portmap"):
            raise ValueError()
        lan = data.get("lan")
        if lan is not None:
            if not isinstance(lan, dict):
                raise ValueError()
            address = ipaddress.ip_address(lan["bind_address"])
            if address.is_unspecified or address.is_loopback or address.is_multicast:
                raise ValueError()
            _port(lan["port"])
        return {"domain": domain, "httpPort": data["http_port"], "lan": lan}
    except (ValueError, KeyError, TypeError) as exc:
        raise PortmapError("Invalid saved node metadata; supply --domain --host --http-port explicitly.") from exc


def public_key_fingerprint(path: Path) -> str:
    """Hash an explicitly selected OpenSSH PUBLIC .pub file, never a private key."""
    if path.suffix != ".pub":
        raise PortmapError("--ssh-host-key-file requires an explicit OpenSSH public .pub file, never a private key.")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        # Unbuffered readline must not prefetch the remainder of a mislabeled
        # private key before its first-line header has been rejected.
        with os.fdopen(descriptor, "rb", buffering=0) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError()
            first = handle.readline(16385)
            if len(first) > 16384 or first.lstrip().startswith(b"-----"):
                raise ValueError()
            fields = first.decode("ascii").strip().split()
            if len(fields) < 2 or fields[0] not in {
                "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
            }:
                raise ValueError()
            blob = base64.b64decode(fields[1], validate=True)
            parts, offset = [], 0
            while offset < len(blob):
                length = struct.unpack(">I", blob[offset:offset + 4])[0]
                offset += 4
                if length > len(blob) - offset:
                    raise ValueError()
                parts.append(blob[offset:offset + length])
                offset += length
            if not parts or parts[0] != fields[0].encode("ascii"):
                raise ValueError()
            kind = fields[0]
            if kind == "ssh-ed25519":
                valid = len(parts) == 2 and len(parts[1]) == 32
            elif kind == "ssh-rsa":
                valid = len(parts) == 3 and all(part and int.from_bytes(part, "big", signed=True) > 0 for part in parts[1:])
            elif kind == "sk-ssh-ed25519@openssh.com":
                valid = len(parts) == 3 and len(parts[1]) == 32 and bool(parts[2])
            else:
                curve = kind.removeprefix("sk-ecdsa-sha2-").removeprefix("ecdsa-sha2-").removesuffix("@openssh.com")
                expected = {"nistp256": 65, "nistp384": 97, "nistp521": 133}[curve]
                count = 4 if kind.startswith("sk-") else 3
                valid = (len(parts) == count and parts[1] == curve.encode("ascii")
                         and len(parts[2]) == expected and parts[2].startswith(b"\x04")
                         and (count == 3 or bool(parts[3])))
            if not valid:
                raise ValueError()
            consumed = len(first)
            while True:
                line = handle.readline(16385 - consumed)
                consumed += len(line)
                if consumed > 16384 or line.strip():
                    raise ValueError()
                if not line:
                    break
            return hashlib.sha256(blob).hexdigest()
    except (OSError, ValueError, UnicodeError, struct.error) as exc:
        raise PortmapError("Cannot read a valid OpenSSH public host key; use --ssh-host-key-sha256 or a public .pub file.") from exc


def profile_from_args(args: Any) -> dict[str, Any]:
    # Fully explicit public inputs do not even discover settings or state paths.
    role = _saved_role(args.state_dir) if any(value is None for value in (args.domain, args.host, args.http_port)) else None
    domain = args.domain if args.domain is not None else (role or {}).get("domain")
    lan = (role or {}).get("lan") or {}
    host = args.host if args.host is not None else lan.get("bind_address")
    http_port = args.http_port if args.http_port is not None else (lan.get("port") if args.mode == "direct" else (role or {}).get("httpPort"))
    missing = [flag for flag, value in (("--domain", domain), ("--host", host), ("--http-port", http_port)) if value is None]
    if missing:
        raise PortmapError("Saved state does not determine a reachable node endpoint; supply " + " ".join(missing) + ". No services were started.")
    if args.mode == "direct" and any(value is not None for value in
            (args.ssh_port, args.ssh_username, args.ssh_target_host, args.ssh_host_key_sha256, args.ssh_host_key_file)):
        raise PortmapError("SSH public metadata requires --mode ssh.")
    profile = {"name": args.name if args.name is not None else _safe_domain(domain), "domain": domain, "mode": args.mode,
               "host": host, "httpPort": http_port, "httpsPort": args.https_port}
    if args.mode == "ssh":
        missing = [flag for flag, value in (("--ssh-port", args.ssh_port), ("--ssh-target-host", args.ssh_target_host),
                   ("--ssh-host-key-sha256 or --ssh-host-key-file", args.ssh_host_key_sha256 or args.ssh_host_key_file)) if value is None]
        if missing:
            raise PortmapError("SSH QR requires verified public metadata: " + ", ".join(missing) + ".")
        fingerprint = public_key_fingerprint(args.ssh_host_key_file) if args.ssh_host_key_file is not None else args.ssh_host_key_sha256
        profile["ssh"] = {"port": args.ssh_port, "username": args.ssh_username or "", "targetHost": args.ssh_target_host,
                          "hostKeySha256Hex": fingerprint}
    return normalize(profile)


def _safe_domain(value: Any) -> str:
    try:
        return _dns(value)
    except (ValueError, TypeError) as exc:
        raise PortmapError(INVALID) from exc


def qr_matrix(text: str):
    """Encode bounded ASCII text with pinned Nayuki v1.8.0 QR Model 2, low ECC.

    Render helpers accept arbitrary bounded ASCII so diagnostic/negative fixtures
    can be generated, but the CLI always supplies encode(normalize(profile)).
    """
    from ._qr_encoder import QrCode

    if not isinstance(text, str) or not text or not text.isascii() or len(text) > MAX_WIRE_BYTES:
        raise PortmapError("QR text must contain 1 to 2048 ASCII bytes.")
    return QrCode.encode_binary(text.encode("ascii"), QrCode.Ecc.LOW)


def _dimensions(matrix, scale: int, border: int) -> int:
    if type(scale) is not int or not 1 <= scale <= 32 or type(border) is not int or not 4 <= border <= 32:
        raise PortmapError("QR scale must be 1..32 and quiet border 4..32 modules.")
    return (matrix.get_size() + border * 2) * scale


def _png(matrix, scale: int, border: int) -> bytes:
    width = _dimensions(matrix, scale, border)
    rows = bytearray()
    for y in range(-border, matrix.get_size() + border):
        row = b"\x00" + bytes(0 if matrix.get_module(x, y) else 255 for x in range(-border, matrix.get_size() + border) for _ in range(scale))
        rows.extend(row * scale)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xffffffff)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, width, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def qr_png(text: str, *, scale: int = 6, border: int = 4) -> bytes:
    return _png(qr_matrix(text), scale, border)


def _svg(matrix, scale: int, border: int) -> str:
    width = _dimensions(matrix, scale, border)
    size = matrix.get_size() + border * 2
    path = " ".join(f"M{x + border},{y + border}h1v1h-1z" for y in range(matrix.get_size()) for x in range(matrix.get_size()) if matrix.get_module(x, y))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{width}" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
            f'<path fill="#fff" d="M0,0h{size}v{size}H0z"/><path fill="#000" d="{path}"/></svg>\n')


def qr_svg(text: str, *, scale: int = 6, border: int = 4) -> str:
    return _svg(qr_matrix(text), scale, border)


def _terminal(matrix) -> str:
    # Two module rows per character keep normal-sized codes within common
    # terminal widths. Static colors keep black modules on a white quiet zone
    # independent of the user's terminal theme; no node text enters escapes.
    cells = " ▄▀█"
    return "\n".join("\x1b[30;107m" + "".join(
        cells[2 * matrix.get_module(x, y) + matrix.get_module(x, y + 1)]
        for x in range(-4, matrix.get_size() + 4)) + "\x1b[0m"
        for y in range(-4, matrix.get_size() + 4, 2))


def _write_outputs(outputs: list[tuple[Path, bytes]]) -> None:
    """Reserve every output exclusively before writing; never truncate a user file."""
    opened = []
    try:
        for path, content in outputs:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            opened.append((path, descriptor, content))
        for _, descriptor, content in opened:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(content)
    except OSError as exc:
        for path, _, _ in opened:
            path.unlink(missing_ok=True)
        raise PortmapError("Cannot create QR output exclusively; choose distinct new --png/--svg paths in an existing directory.") from exc
    finally:
        for _, descriptor, _ in opened:
            os.close(descriptor)


def run(args: Any) -> int:
    wire = encode(profile_from_args(args))
    if args.no_terminal and not (args.wire or args.png or args.svg):
        raise PortmapError("--no-terminal requires --wire, --png or --svg.")
    matrix = qr_matrix(wire) if not args.no_terminal or args.png or args.svg else None
    outputs = []
    if args.png:
        outputs.append((args.png, _png(matrix, 6, 4)))
    if args.svg:
        outputs.append((args.svg, _svg(matrix, 6, 4).encode("utf-8")))
    _write_outputs(outputs)
    if not args.no_terminal:
        print("Scan only on a trusted device. Verify the destination and SSH fingerprint before adding.")
        print("This QR contains infrastructure metadata, not credentials, a signature or authorization.")
        print(_terminal(matrix))
    if args.wire:
        print(wire)
    return 0
