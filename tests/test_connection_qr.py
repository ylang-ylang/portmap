"""Deterministic public-wire, read-only CLI and actual QR raster contracts."""
import base64
import binascii
import copy
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from portmap import cli, connection_qr as qr, settings
from portmap.errors import PortmapError

DIRECT = {"name": "Node A", "domain": "a.portmap", "mode": "direct", "host": "192.0.2.10", "httpPort": 8080, "httpsPort": None}
DIRECT_JSON = '{"domain":"a.portmap","host":"192.0.2.10","httpPort":8080,"httpsPort":null,"mode":"direct","name":"Node A"}'
DIRECT_WIRE = "portmap-connect:v1:eyJkb21haW4iOiJhLnBvcnRtYXAiLCJob3N0IjoiMTkyLjAuMi4xMCIsImh0dHBQb3J0Ijo4MDgwLCJodHRwc1BvcnQiOm51bGwsIm1vZGUiOiJkaXJlY3QiLCJuYW1lIjoiTm9kZSBBIn0"
SSH = {"name": "节点 🛰", "domain": "xn--fsqu00a.portmap", "mode": "ssh", "host": "2001:db8::1", "httpPort": 1, "httpsPort": 65535,
       "ssh": {"port": 2222, "username": "", "targetHost": "::ffff:c000:201", "hostKeySha256Hex": "ab" * 32}}
SSH_WIRE = "portmap-connect:v1:eyJkb21haW4iOiJ4bi0tZnNxdTAwYS5wb3J0bWFwIiwiaG9zdCI6IjIwMDE6ZGI4OjoxIiwiaHR0cFBvcnQiOjEsImh0dHBzUG9ydCI6NjU1MzUsIm1vZGUiOiJzc2giLCJuYW1lIjoi6IqC54K5IPCfm7AiLCJzc2giOnsiaG9zdEtleVNoYTI1NkhleCI6ImFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWIiLCJwb3J0IjoyMjIyLCJ0YXJnZXRIb3N0IjoiOjpmZmZmOmMwMDA6MjAxIiwidXNlcm5hbWUiOiIifX0"
IPV6_CASES = [
    ("::", "::"),
    ("::ffff:192.0.2.1", "::ffff:c000:201"),
    ("[0:0:0:0:0:ffff:c000:0201]", "::ffff:c000:201"),
    ("2001:0:0:1:0:0:1:1", "2001::1:0:0:1:1"),
    ("2001:db8:0:1:2:3:4:5", "2001:db8:0:1:2:3:4:5"),
    ("2001:db8:1:0:0:0:0:0", "2001:db8:1::"),
    ("0:0:1:0:0:0:0:1", "0:0:1::1"),
]


def wire_json(text):
    return qr.PREFIX + base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def args(*flags):
    return cli.build_parser().parse_args(["connection-qr", *flags])


def explicit(*flags):
    return args("--name", "Node A", "--domain", "a.portmap", "--host", "192.0.2.10", "--http-port", "8080", *flags)


def fail(*_args, **_kwargs):
    raise AssertionError("unexpected settings, lifecycle, file or network access")


@pytest.mark.parametrize("profile,wire", [(DIRECT, DIRECT_WIRE), (SSH, SSH_WIRE)])
def test_exact_cross_language_vectors_and_no_input_mutation(profile, wire):
    before = copy.deepcopy(profile)
    assert qr.encode(profile) == wire
    assert qr.decode(wire) == profile
    assert profile == before
    assert wire.isascii() and len(wire) <= 2048


def test_producer_normalization_matches_core_semantics():
    profile = {**SSH, "name": "\ufeff节点 🛰\u00a0", "domain": " XN--FSQU00A.PORTMAP. ", "host": "[2001:0DB8:0:0::1]",
               "ssh": {**SSH["ssh"], "targetHost": "::ffff:192.0.2.1", "hostKeySha256Hex": "SHA256:" + base64.b64encode(bytes.fromhex("ab" * 32)).decode().rstrip("=")}}
    assert qr.encode(profile) == SSH_WIRE
    assert qr.decode(qr.encode({**DIRECT, "name": "\u0085visible"}))["name"] == "\u0085visible"


@pytest.mark.parametrize("host,expected", IPV6_CASES)
def test_ipv6_canonicalization_matches_whatwg_across_python_versions(host, expected):
    draft = {**SSH, "host": host, "ssh": {**SSH["ssh"], "targetHost": host}}
    normalized = qr.decode(qr.encode(draft))
    assert normalized["host"] == expected
    assert normalized["ssh"]["targetHost"] == expected


@pytest.mark.parametrize("change", [
    {"name": ""}, {"name": " "}, {"name": "x" * 121}, {"name": "🛰" * 61}, {"name": "x\x1by"}, {"name": "\ud800"},
    {"domain": "portmap"}, {"domain": "a..portmap"}, {"domain": "-a.portmap"}, {"domain": "例子.portmap"},
    {"host": "app.a.portmap"}, {"host": "http://example.test"}, {"host": "user@example.test"}, {"host": "host:80"},
    {"host": "256.1.2.3"}, {"host": "127.1"}, {"host": "010.0.0.1"}, {"host": "[::1]:80"}, {"host": "fe80::1%eth0"},
    {"httpPort": 0}, {"httpPort": 65536}, {"httpPort": True}, {"httpPort": "80"}, {"httpsPort": 0},
    {"mode": "socks"}, {"ssh": {}}, {"password": "secret"}, {"id": "owned"}, {"enabled": True},
])
def test_invalid_public_fields_and_forbidden_extras_reject(change):
    with pytest.raises(PortmapError, match="^" + qr.INVALID.replace(".", r"\.") + "$"):
        qr.encode({**DIRECT, **change})


@pytest.mark.parametrize("field", ["name", "domain", "mode", "host", "httpPort", "httpsPort"])
def test_every_common_key_is_required(field):
    value = {key: item for key, item in DIRECT.items() if key != field}
    with pytest.raises(PortmapError):
        qr.encode(value)


@pytest.mark.parametrize("change", [
    {"password": "secret"}, {"privateKey": "secret"}, {"auth": {"type": "password", "password": "secret"}},
    {"username": None}, {"username": "x\ny"}, {"username": "x" * 256}, {"hostKeySha256Hex": "*"},
    {"hostKeySha256Hex": "SHA256:" + "A" * 42 + "B"}, {"targetHost": "a.portmap"}, {"port": False},
])
def test_ssh_secrets_and_invalid_public_fields_reject(change):
    with pytest.raises(PortmapError):
        qr.encode({**SSH, "ssh": {**SSH["ssh"], **change}})


def test_ssh_unknown_username_is_empty_not_guessed_and_not_save_ready():
    draft = qr.profile_from_args(explicit("--mode", "ssh", "--ssh-port", "22", "--ssh-target-host", "127.0.0.1", "--ssh-host-key-sha256", "AB" * 32))
    assert draft["ssh"]["username"] == ""
    assert set(draft["ssh"]) == {"port", "username", "targetHost", "hostKeySha256Hex"}
    assert "auth" not in draft["ssh"]
    for field in draft["ssh"]:
        value = copy.deepcopy(draft)
        del value["ssh"][field]
        with pytest.raises(PortmapError):
            qr.encode(value)


def test_strict_canonical_decoder_rejects_duplicates_order_escaping_and_aliases():
    duplicate = DIRECT_JSON.replace('"name":"Node A"', '"name":"stolen","name":"Node A"')
    ssh_json = canonical(SSH)
    texts = [duplicate, " " + DIRECT_JSON, DIRECT_JSON + "\n", json.dumps(DIRECT),
             DIRECT_JSON.replace("Node A", r"Node \u0041"), DIRECT_JSON.replace("8080", "8080.0"),
             DIRECT_JSON.replace('"192.0.2.10"', '"192.0.2.10."'), DIRECT_JSON.replace("a.portmap", "A.PORTMAP"),
             ssh_json.replace('"username":""', '"username":"stolen","username":""'),
             canonical({**DIRECT, "password": "do-not-echo-this"}), '\ufeff' + DIRECT_JSON]
    for text in texts:
        with pytest.raises(PortmapError) as error:
            qr.decode(wire_json(text))
        assert str(error.value) == qr.INVALID


@pytest.mark.parametrize("wire", ["", "https://example.test/pair", "portmap-connect:v2:AA", DIRECT_WIRE + "=", DIRECT_WIRE + "\n",
    qr.PREFIX + "A", qr.PREFIX + "+/", qr.PREFIX + "_w", qr.PREFIX + "eyJ4Ijoi7aCAIn0", qr.PREFIX + "A" * 2048,
    DIRECT_WIRE[:-1] + "1", qr.PREFIX + "null", None, 42])
def test_malformed_wire_never_leaks_contents(wire):
    with pytest.raises(PortmapError) as error:
        qr.decode(wire)
    assert str(error.value) == qr.INVALID


def test_wire_size_limit_and_utf16_name_boundaries():
    assert qr.decode(qr.encode({**DIRECT, "name": "🛰" * 60}))["name"] == "🛰" * 60
    maximal = {**SSH, "name": "🛰" * 60, "host": ".".join(["h" * 63] * 3 + ["h" * 61]),
               "ssh": {**SSH["ssh"], "targetHost": ".".join(["t" * 63] * 3 + ["t" * 61]), "username": "汉" * 181}}
    # Prefix/base64 geometry makes 2047 the largest reachable <=2048 length.
    assert len(qr.encode(maximal)) == 2047
    assert qr.decode(qr.encode(maximal)) == maximal
    maximal["ssh"]["username"] += "x"
    assert len(wire_json(canonical(maximal))) == 2049
    with pytest.raises(PortmapError):
        qr.encode(maximal)
    with pytest.raises(PortmapError):
        qr.decode(qr.PREFIX + "A" * (2048 - len(qr.PREFIX) + 1))


def test_fully_explicit_cli_has_no_settings_lifecycle_or_network_access(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(qr, "_saved_role", fail)
    monkeypatch.setattr(settings, "load_portmap_settings", fail)
    monkeypatch.setattr(settings, "resolve_portmap_root", fail)
    monkeypatch.setattr(cli, "load_portmap_settings", fail)
    monkeypatch.setattr(cli, "ensure_agent_started", fail)
    monkeypatch.setattr(socket, "socket", fail)
    monkeypatch.setattr(subprocess, "run", fail)
    assert qr.run(explicit("--wire", "--no-terminal", "--state-dir", str(tmp_path / "does-not-exist"))) == 0
    assert capsys.readouterr().out == DIRECT_WIRE + "\n"


def saved_role(tmp_path, *, lan=True):
    state = tmp_path / "state"
    path = state / "server" / "instance.json"
    path.parent.mkdir(parents=True)
    value = {"version": 2, "uid": os.getuid(), "state_dir": str(state.resolve()), "client_state_dir": str(tmp_path / "client"),
             "settings": {"dns_domain": "saved.portmap"}, "http_port": 8081,
             "lan": {"bind_address": "192.0.2.9", "port": 8082} if lan else None}
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return state, path, value


def test_saved_role_sources_only_authoritative_namespace_and_ingress(tmp_path, monkeypatch):
    state, path, _ = saved_role(tmp_path)
    before = path.stat()
    monkeypatch.setattr(settings, "load_portmap_settings", fail)
    monkeypatch.setattr(socket, "socket", fail)
    result = qr.profile_from_args(args("--state-dir", str(state)))
    assert result == {**DIRECT, "name": "saved.portmap", "domain": "saved.portmap", "host": "192.0.2.9", "httpPort": 8082}
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_mode == before.st_mode
    overridden = qr.profile_from_args(args("--state-dir", str(state), "--name", "Override", "--host", "198.51.100.3", "--http-port", "8084", "--https-port", "8443"))
    assert overridden == {**result, "name": "Override", "host": "198.51.100.3", "httpPort": 8084, "httpsPort": 8443}


def test_settings_location_reads_only_toml_state_path_without_runtime_discovery(tmp_path, monkeypatch):
    state, _, _ = saved_role(tmp_path)
    (tmp_path / "portmap.toml").write_text('[state]\ndir = "state"\n')
    monkeypatch.setattr(settings, "resolve_portmap_root", lambda **kwargs: tmp_path)
    monkeypatch.setattr(settings, "load_portmap_settings", fail)
    monkeypatch.setattr(settings, "resolve_runtime", fail)
    monkeypatch.setattr(settings, "detect_host_ip", fail)
    monkeypatch.delenv("PORTMAP_STATE_DIR", raising=False)
    assert qr.profile_from_args(args())["domain"] == "saved.portmap"
    assert state.is_dir()


def test_no_lan_never_advertises_loopback_wildcard_or_backend_port(tmp_path):
    state, path, value = saved_role(tmp_path, lan=False)
    with pytest.raises(PortmapError, match="--host --http-port"):
        qr.profile_from_args(args("--state-dir", str(state)))
    for bind in ["0.0.0.0", "::", "127.0.0.1", "::1"]:
        value["lan"] = {"bind_address": bind, "port": 80}
        path.write_text(json.dumps(value))
        with pytest.raises(PortmapError, match="Invalid saved node metadata"):
            qr.profile_from_args(args("--state-dir", str(state)))


def test_saved_state_never_repairs_permissions_or_follows_final_symlink(tmp_path):
    state, path, _ = saved_role(tmp_path)
    path.chmod(0o644)
    with pytest.raises(PortmapError, match="private"):
        qr.profile_from_args(args("--state-dir", str(state)))
    assert path.stat().st_mode & 0o777 == 0o644
    moved = path.with_suffix(".saved")
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(PortmapError, match="Cannot read saved"):
        qr.profile_from_args(args("--state-dir", str(state)))


def test_ssh_explicit_public_file_hash_and_no_private_file_read(tmp_path, monkeypatch):
    algorithm = b"ssh-ed25519"
    blob = struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", 32) + bytes(range(32))
    path = tmp_path / "owned-host.pub"
    path.write_text("ssh-ed25519 " + base64.b64encode(blob).decode() + " owned-fixture\n")
    assert qr.public_key_fingerprint(path) == hashlib.sha256(blob).hexdigest()
    draft = qr.profile_from_args(explicit("--mode", "ssh", "--ssh-port", "2222", "--ssh-target-host", "127.0.0.1", "--ssh-host-key-file", str(path)))
    assert draft["ssh"]["hostKeySha256Hex"] == hashlib.sha256(blob).hexdigest()
    path.write_text(path.read_text() * 2)
    with pytest.raises(PortmapError):
        qr.public_key_fingerprint(path)
    monkeypatch.setattr(qr, "_read_regular", fail)
    with pytest.raises(PortmapError, match="PUBLIC|public"):
        qr.public_key_fingerprint(tmp_path / "id_rsa")

@pytest.mark.parametrize("header", [b"-----BEGIN OPENSSH PRIVATE KEY-----\n", b"-----BEGIN RSA PRIVATE KEY-----\n"])
def test_mislabeled_private_pub_rejects_first_header_without_reading_body(tmp_path, monkeypatch, header):
    path = tmp_path / "mislabeled.pub"
    path.write_bytes(header + b"sensitive-private-key-body\n" * 4096)
    original = os.fdopen
    positions = []

    class HeaderOnly:
        def __init__(self, handle):
            self.handle = handle
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def readline(self, maximum):
            self.calls += 1
            assert self.calls == 1, "private body must not be read"
            value = self.handle.readline(maximum)
            positions.append(self.handle.tell())
            return value

    def guarded(descriptor, mode, *, buffering):
        assert mode == "rb" and buffering == 0, "buffering could prefetch private body"
        return HeaderOnly(original(descriptor, mode, buffering=buffering))

    monkeypatch.setattr(os, "fdopen", guarded)
    with pytest.raises(PortmapError) as error:
        qr.public_key_fingerprint(path)
    assert positions == [len(header)]
    assert "sensitive" not in str(error.value) and "PRIVATE KEY" not in str(error.value)


def test_public_file_rejects_invalid_binary_key_structure_and_allows_blank_tail(tmp_path):
    algorithm = b"ssh-ed25519"
    prefix = struct.pack(">I", len(algorithm)) + algorithm
    path = tmp_path / "host.pub"
    for blob in [prefix + b"x", prefix + struct.pack(">I", 32) + b"short",
                 prefix + struct.pack(">I", 31) + bytes(31),
                 prefix + struct.pack(">I", 32) + bytes(32) + struct.pack(">I", 1) + b"x"]:
        path.write_text("ssh-ed25519 " + base64.b64encode(blob).decode() + "\n")
        with pytest.raises(PortmapError):
            qr.public_key_fingerprint(path)
    blob = prefix + struct.pack(">I", 32) + bytes(range(32))
    path.write_text("ssh-ed25519 " + base64.b64encode(blob).decode() + " owned\n\n \t\n")
    assert qr.public_key_fingerprint(path) == hashlib.sha256(blob).hexdigest()



def test_missing_ssh_metadata_and_invalid_output_flags_are_actionable():
    with pytest.raises(PortmapError, match="--ssh-port.*--ssh-target-host.*--ssh-host-key"):
        qr.profile_from_args(explicit("--mode", "ssh"))
    with pytest.raises(PortmapError, match="requires --mode ssh"):
        qr.profile_from_args(explicit("--ssh-username", "somebody"))
    with pytest.raises(PortmapError, match="--no-terminal requires"):
        qr.run(explicit("--no-terminal"))


def png_pixels(png):
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    position, compressed, width = 8, bytearray(), None
    kinds = []
    while position < len(png):
        length = struct.unpack(">I", png[position:position + 4])[0]
        kind = png[position + 4:position + 8]
        data = png[position + 8:position + 8 + length]
        crc = struct.unpack(">I", png[position + 8 + length:position + 12 + length])[0]
        assert crc == binascii.crc32(kind + data) & 0xffffffff
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            assert width == height and (depth, color, compression, filtering, interlace) == (8, 0, 0, 0, 0)
        if kind == b"IDAT":
            compressed.extend(data)
        kinds.append(kind)
        position += 12 + length
    assert kinds == [b"IHDR", b"IDAT", b"IEND"]
    pixels = zlib.decompress(compressed)
    assert len(pixels) == width * (width + 1)
    return width, [pixels[y * (width + 1) + 1:(y + 1) * (width + 1)] for y in range(width)]


def test_png_has_exact_scannable_matrix_white_quiet_zone_and_correct_chunks():
    matrix = qr.qr_matrix(DIRECT_WIRE)
    width, pixels = png_pixels(qr.qr_png(DIRECT_WIRE, scale=2))
    assert width == (matrix.get_size() + 8) * 2
    for y in range(width):
        for x in range(width):
            assert pixels[y][x] == (0 if matrix.get_module(x // 2 - 4, y // 2 - 4) else 255)
    assert all(pixel == 255 for row in pixels[:8] for pixel in row)
    # Actual finder pattern: 7x7 black perimeter, white inner ring, black 3x3.
    assert [[matrix.get_module(x, y) for x in range(7)] for y in range(7)] == [
        [True] * 7, [True, False, False, False, False, False, True],
        [True, False, True, True, True, False, True], [True, False, True, True, True, False, True],
        [True, False, True, True, True, False, True], [True, False, False, False, False, False, True], [True] * 7]


def test_actual_cli_outputs_png_svg_wire_and_never_overwrites(tmp_path, capsys):
    png, svg = tmp_path / "node.png", tmp_path / "node.svg"
    assert qr.run(explicit("--png", str(png), "--svg", str(svg), "--wire", "--no-terminal")) == 0
    assert capsys.readouterr().out == DIRECT_WIRE + "\n"
    png_pixels(png.read_bytes())
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in svg.read_text()
    assert "<script" not in svg.read_text() and "href" not in svg.read_text()
    assert png.stat().st_mode & 0o777 == 0o600
    before = png.read_bytes(), svg.read_bytes()
    with pytest.raises(PortmapError, match="exclusively"):
        qr.run(explicit("--png", str(png), "--svg", str(svg)))
    assert before == (png.read_bytes(), svg.read_bytes())
    fresh = tmp_path / "fresh.png"
    with pytest.raises(PortmapError):
        qr.run(explicit("--png", str(fresh), "--svg", str(svg)))
    assert not fresh.exists() and svg.read_bytes() == before[1]
    with pytest.raises(PortmapError):
        qr.run(explicit("--png", str(fresh), "--svg", str(fresh)))
    assert not fresh.exists()
    alias = tmp_path / "symlink.png"
    alias.symlink_to(png)
    with pytest.raises(PortmapError):
        qr.run(explicit("--png", str(alias)))
    assert png.read_bytes() == before[0]


def test_terminal_default_is_real_qr_static_escapes_and_no_user_control(capsys):
    assert qr.run(explicit()) == 0
    output = capsys.readouterr().out
    assert "not credentials" in output and "\x1b[30;107m" in output and any(cell in output for cell in "▄▀█")
    assert DIRECT_WIRE not in output and "192.0.2.10" not in output


def test_python_node_roundtrip_vectors_and_generated_unicode_boundaries():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the cross-language roundtrip")
    codec = Path(__file__).parents[1] / "extension/firefox/connection-code.js"
    profiles = [DIRECT, SSH, {**DIRECT, "name": "🛰" * 60},
                {**SSH, "name": "Unicode\u2028separator\u2029", "ssh": {**SSH["ssh"], "username": "用户"}}]
    profiles += [{**DIRECT, "host": host} for host, _ in IPV6_CASES]
    script = "const c=require(process.argv[1]);let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(s).map(x=>({wire:c.encode(x.profile),profile:c.decode(x.wire)})))));"
    payload = [{"profile": value, "wire": qr.encode(value)} for value in profiles]
    result = subprocess.run([node, "-e", script, str(codec)], input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True, check=True)
    rows = json.loads(result.stdout)
    for value, row in zip(profiles, rows, strict=True):
        assert row["wire"] == qr.encode(value)
        assert row["profile"] == qr.decode(row["wire"]) == qr.normalize(value)
