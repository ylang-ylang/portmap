import json
import os
import socket as socket_module
import struct
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import client_dns
from portmap.errors import PortmapError


def split_blocks(corefile: str) -> dict[int, str]:
    blocks: dict[int, list[str]] = {}
    current: int | None = None
    for line in corefile.splitlines():
        if line.startswith(".:") and line.endswith("{"):
            current = int(line[2:-2])
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {port: "\n".join(rows) for port, rows in blocks.items()}


# --- Corefile view distinction ------------------------------------------------------


def test_host_view_publishes_host_scope_records_and_refuses_the_rest():
    corefile = client_dns.render_corefile(
        {"laptop.portmap": "127.0.0.1"},
        {"box.portmap": "127.0.0.2"},
        "owner:version",
    )
    host, container = split_blocks(corefile)[1053], split_blocks(corefile)[53]
    assert "laptop.portmap" in host and "box.portmap" not in host
    assert "rcode REFUSED" in host
    assert "box.portmap" in container and "laptop.portmap" not in container
    assert "forward" not in host


def test_container_view_serves_container_scope_and_forwards_upstream():
    corefile = client_dns.render_corefile(
        {"laptop.portmap": "127.0.0.1"},
        {"box.portmap": "127.0.0.2"},
        "owner:version",
    )
    container = split_blocks(corefile)[53]
    assert "box.portmap" in container
    assert "forward . /etc/resolv.conf" in container
    assert "template ANY ANY portmap {" in container and "rcode NXDOMAIN" in container


def test_marker_is_probeable_on_both_listeners():
    corefile = client_dns.render_corefile({}, {}, "owner:version")
    for block in split_blocks(corefile).values():
        assert f"template IN TXT {client_dns.MARKER_NAME}" in block


def test_listeners_never_bind_the_host_dummy_address():
    corefile = client_dns.render_corefile({"laptop.portmap": "127.0.0.1"}, {}, "owner:version")
    assert client_dns.DNS_ADDRESS not in corefile
    assert not [line for line in corefile.splitlines() if line.strip().startswith("bind ")]


def test_more_specific_suffix_precedes_parent_zone_in_each_view():
    corefile = client_dns.render_corefile(
        {"b.a.portmap": "127.0.0.1", "a.portmap": "127.0.0.2"},
        {"b.a.portmap": "127.0.0.3", "a.portmap": "127.0.0.4"},
        "owner:version",
    )
    for block in split_blocks(corefile).values():
        assert block.index("template IN A b.a.portmap") < block.index("template IN A a.portmap")


def test_ipv6_records_answer_aaaa_and_empty_the_other_family():
    corefile = client_dns.render_corefile({}, {"direct.portmap": "2001:db8::10"}, "owner:version")
    block = split_blocks(corefile)[53]
    assert "template IN AAAA direct.portmap" in block
    other = block[block.index("template IN A direct.portmap") :]
    assert "rcode NOERROR" in other[: other.index("}")]


@pytest.mark.parametrize("records", [{"bad.portmap": "not-an-address"}, {"192.0.2.10": "127.0.0.1"}, {"host.portmap.example": "127.0.0.1"}])
def test_invalid_records_are_rejected_in_either_view(records):
    with pytest.raises(PortmapError):
        client_dns.render_corefile(records, {}, "owner:version")
    with pytest.raises(PortmapError):
        client_dns.render_corefile({}, records, "owner:version")


def test_marker_rejects_whitespace():
    with pytest.raises(PortmapError):
        client_dns.render_corefile({}, {}, "owner token")


def test_forward_target_is_validated_and_configurable():
    corefile = client_dns.render_corefile({}, {}, "owner:version", forward="/run/portmap/upstream.conf")
    assert "forward . /run/portmap/upstream.conf" in split_blocks(corefile)[53]
    with pytest.raises(PortmapError):
        client_dns.render_corefile({}, {}, "owner:version", forward="8.8.8.8 8.8.4.4")


# --- Corefile host_forward (forward resolver backend) --------------------------------


def test_host_forward_passes_original_upstreams_through_the_host_view():
    corefile = client_dns.render_corefile(
        {"laptop.portmap": "127.0.0.1"},
        {"box.portmap": "127.0.0.2"},
        "owner:version",
        host_forward=["192.168.1.1", "2001:db8::53"],
    )
    blocks = split_blocks(corefile)
    host = blocks[1053]
    assert "forward . 192.168.1.1 2001:db8::53" in host
    assert "rcode REFUSED" not in host  # pass-through replaces the refusal
    assert f"template IN TXT {client_dns.MARKER_NAME}" in host
    assert "template ANY ANY portmap {" in host and "rcode NXDOMAIN" in host
    assert "laptop.portmap" in host and "box.portmap" not in host
    assert "forward . /etc/resolv.conf" in blocks[53]  # container view unchanged
    assert "forward . 192.168.1.1 2001:db8::53" not in blocks[53]


def test_host_forward_renders_the_staging_configuration_too():
    corefile = client_dns.render_corefile({}, {}, "owner:version", host_forward=["192.0.2.53"])
    assert "forward . 192.0.2.53" in split_blocks(corefile)[1053]


@pytest.mark.parametrize("upstreams", [["127.0.0.1"], ["::1"], ["0.0.0.0"], ["169.254.254.53"], []])
def test_host_forward_rejects_upstreams_the_container_cannot_reach(upstreams):
    with pytest.raises(PortmapError, match="refuses to guess public servers|at least one original upstream"):
        client_dns.render_corefile({}, {}, "owner:version", host_forward=upstreams)


def test_host_forward_none_keeps_the_refused_host_view():
    corefile = client_dns.render_corefile({}, {}, "owner:version", host_forward=None)
    assert "rcode REFUSED" in split_blocks(corefile)[1053]


# --- probe_marker wire format ---------------------------------------------------------


class TxtServer:
    """Answers portmap marker TXT queries over real UDP, like the container DNS."""

    def __init__(self, marker: str):
        self.socket = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.marker = marker
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while True:
            try:
                packet, peer = self.socket.recvfrom(4096)
            except OSError:
                return
            ident, _, _, _, _, _ = struct.unpack_from("!6H", packet)
            answer = packet[12:] + b"\xc0\x0c" + struct.pack("!HHIH", 16, 1, 0, len(self.marker) + 1) + bytes([len(self.marker)]) + self.marker.encode()
            self.socket.sendto(struct.pack("!6H", ident, 0x8000, 1, 1, 0, 0) + answer, peer)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.socket.close()

    @property
    def port(self):
        return self.socket.getsockname()[1]


def test_probe_marker_reads_the_ownership_record_over_the_wire():
    server = TxtServer("owner:abc123")
    with server:
        assert client_dns.probe_marker("127.0.0.1", server.port, timeout=1) == "owner:abc123"


def test_probe_marker_returns_none_without_a_portmap_listener():
    with socket_module.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    assert client_dns.probe_marker("127.0.0.1", port, timeout=0.05) is None


def test_probe_marker_rejects_answers_that_do_not_match_the_query():
    class WrongIdent(TxtServer):
        def _serve(self):
            packet, peer = self.socket.recvfrom(4096)
            self.socket.sendto(b"\x00\x00" + packet[2:], peer)

    wrong = WrongIdent("owner:abc123")
    with wrong:
        assert client_dns.probe_marker("127.0.0.1", wrong.port, timeout=0.2) is None


def test_probe_marker_validates_the_port():
    with pytest.raises(PortmapError):
        client_dns.probe_marker("127.0.0.1", 0)
    with pytest.raises(PortmapError):
        client_dns.probe_marker("127.0.0.1", 70000)


# --- verify_forwarding wire format ----------------------------------------------------


class ForwardResponder:
    """Answers every A query with a chosen rcode, like the forwarding CoreDNS."""

    def __init__(self, rcode: int):
        self.socket = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.rcode = rcode
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while True:
            try:
                packet, peer = self.socket.recvfrom(4096)
            except OSError:
                return
            flags = 0x8000 | (self.rcode & 0xF)
            self.socket.sendto(packet[:2] + struct.pack("!5H", flags, 1, 0, 0, 0) + packet[12:], peer)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.socket.close()

    @property
    def port(self):
        return self.socket.getsockname()[1]


@pytest.mark.parametrize("rcode", [0, 3])
def test_verify_forwarding_accepts_real_answers_including_nxdomain(rcode):
    with ForwardResponder(rcode) as responder:
        client_dns.verify_forwarding("127.0.0.1", responder.port, name="check.office.example", timeout=1)


@pytest.mark.parametrize("rcode", [2, 5])
def test_verify_forwarding_rejects_servfail_and_refused(rcode):
    with ForwardResponder(rcode) as responder:
        with pytest.raises(PortmapError, match="resolver is left unchanged"):
            client_dns.verify_forwarding("127.0.0.1", responder.port, timeout=1)


def test_verify_forwarding_rejects_silence():
    with socket_module.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    with pytest.raises(PortmapError, match="resolver is left unchanged"):
        client_dns.verify_forwarding("127.0.0.1", port, timeout=0.1)


def test_verify_forwarding_rejects_malformed_probe_names():
    for name in ("bad name", "", 7):
        with pytest.raises(PortmapError):
            client_dns.verify_forwarding("127.0.0.1", 53, name=name, timeout=0.1)


def test_forward_probe_name_uses_the_original_search_zone():
    name = client_dns._forward_probe_name("search office.example corp.example\nnameserver 10.0.0.1\n")
    assert name.startswith("portmap-forward-check-") and name.endswith(".office.example")
    assert client_dns._forward_probe_name("nameserver 10.0.0.1\n") == "localhost"
    assert client_dns._forward_probe_name("domain legacy.example\n") .endswith(".legacy.example")
    # Never probe inside .portmap: our own wildcard would answer it.
    assert client_dns._forward_probe_name("search corp.portmap\nnameserver 10.0.0.1\n") == "localhost"
    assert client_dns._forward_probe_name("search portmap.office.example\n") .endswith(".portmap.office.example")


# --- resolver backend selection -------------------------------------------------------


def install_resolv_conf(tmp_path, monkeypatch, content, *, mode=0o644, symlink_to=None):
    path = tmp_path / "etc" / "resolv.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    if symlink_to is not None:
        target = tmp_path / "etc" / symlink_to
        target.write_text(content, encoding="utf-8")
        path.symlink_to(target)
    else:
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
    monkeypatch.setattr(client_dns, "_RESOLV_CONF", path)
    return path


def write_saved_state(tmp_path, payload):
    (tmp_path / "resolver.json").write_text(json.dumps(payload), encoding="utf-8")


def saved_forward_state(tmp_path, forwarders, original, *, environment=None):
    payload = {
        "version": 2,
        "backend": "forward",
        "address": client_dns.FORWARD_ADDRESS,
        "port": client_dns.FORWARD_PORT,
        "forwarders": forwarders,
        "original": original,
    }
    if environment is not None:
        payload["environment"] = environment
    return payload


def fake_original_identity(path, content, *, kind="regular", target=None):
    stat = path.stat() if not path.is_symlink() else path.stat()
    return {
        "kind": kind,
        "content": content,
        "target": target,
        "dev": stat.st_dev,
        "ino": stat.st_ino,
        "mode": stat.st_mode & 0o777,
        "uid": stat.st_uid,
        "gid": stat.st_gid,
    }


def test_plan_auto_keeps_the_saved_owned_selection_without_rereading_the_live_resolver(tmp_path, monkeypatch):
    original = "search office.example\nnameserver 192.168.1.1\n"
    path = install_resolv_conf(tmp_path, monkeypatch, original)
    write_saved_state(tmp_path, saved_forward_state(tmp_path, ["192.168.1.1"], fake_original_identity(path, original)))

    plan = client_dns.resolver_plan(tmp_path, backend="auto")

    assert plan == {"backend": "forward", "address": "127.0.0.1", "port": 53, "forwarders": ["192.168.1.1"]}


def test_plan_forward_reuses_saved_upstreams_once_the_live_resolver_points_at_portmap(tmp_path, monkeypatch):
    # Loop prevention: after the takeover the live resolver names only our
    # loopback listener; the original upstreams must come from the saved state.
    original = "search office.example\nnameserver 192.168.1.1\nnameserver 10.0.0.7\n"
    path = install_resolv_conf(tmp_path, monkeypatch, original)
    write_saved_state(tmp_path, saved_forward_state(tmp_path, ["192.168.1.1", "10.0.0.7"], fake_original_identity(path, original)))
    path.write_text("search office.example\nnameserver 127.0.0.1\n", encoding="utf-8")

    plan = client_dns.resolver_plan(tmp_path, backend="forward")

    assert plan["forwarders"] == ["192.168.1.1", "10.0.0.7"]


def test_plan_forward_collects_original_nonloopback_upstreams(tmp_path, monkeypatch):
    install_resolv_conf(
        tmp_path,
        monkeypatch,
        "search office.example\nnameserver 192.168.1.1\nnameserver 10.0.0.7\nnameserver 192.168.1.1\n",
    )
    plan = client_dns.resolver_plan(tmp_path, backend="forward")
    assert plan == {"backend": "forward", "address": "127.0.0.1", "port": 53, "forwarders": ["192.168.1.1", "10.0.0.7"]}


def test_plan_forward_rejects_a_loopback_stub_without_guessing_public_servers(tmp_path, monkeypatch):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 127.0.0.53\noptions edns0\n")
    with pytest.raises(PortmapError, match="refuses to flatten") as excinfo:
        client_dns.resolver_plan(tmp_path, backend="forward")
    assert "8.8.8.8" not in str(excinfo.value) and "1.1.1.1" not in str(excinfo.value)


def test_plan_forward_refuses_to_flatten_a_mixed_stub_policy(tmp_path, monkeypatch):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 127.0.0.53\nnameserver 192.168.1.1\n")
    with pytest.raises(PortmapError, match="mixes loopback and non-loopback"):
        client_dns.resolver_plan(tmp_path, backend="forward")


def test_plan_auto_forwards_through_the_container_for_plain_upstreams(tmp_path, monkeypatch, host):
    install_resolv_conf(tmp_path, monkeypatch, "search office.example\nnameserver 192.168.1.1\nnameserver 10.0.0.7\n")

    plan = client_dns.resolver_plan(tmp_path, backend="auto")

    assert plan == {"backend": "forward", "address": "127.0.0.1", "port": 53, "forwarders": ["192.168.1.1", "10.0.0.7"]}
    assert host.commands == []  # the default needs no resolved probing, ip, or elevation


def test_plan_auto_forwards_without_consulting_resolved_at_all(tmp_path, monkeypatch, host):
    def forbidden(*, use_sudo=True):
        raise AssertionError("plain upstreams must forward without probing systemd-resolved")

    install_resolv_conf(tmp_path, monkeypatch, "nameserver 192.168.1.1\n")
    monkeypatch.setattr(client_dns, "_resolved_available", forbidden)
    assert client_dns.resolver_plan(tmp_path, backend="auto")["backend"] == "forward"


def test_plan_auto_selects_resolved_for_a_loopback_stub_policy(tmp_path, host, monkeypatch):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 127.0.0.53\noptions edns0\n")
    monkeypatch.setattr(client_dns, "_net_admin_usable", lambda *, use_sudo: True)
    plan = client_dns.resolver_plan(tmp_path, backend="auto")
    assert plan == {"backend": "resolved", "address": client_dns.DNS_ADDRESS, "port": client_dns.DNS_PORT, "forwarders": []}
    assert ["resolvectl", "status"] in host.commands


def test_plan_auto_keeps_the_stub_policy_for_mixed_loopback_and_real_servers(tmp_path, host, monkeypatch):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 127.0.0.53\nnameserver 192.168.1.1\n")
    monkeypatch.setattr(client_dns, "_net_admin_usable", lambda *, use_sudo: True)
    assert client_dns.resolver_plan(tmp_path, backend="auto")["backend"] == "resolved"


def test_plan_auto_reports_a_real_limitation_without_net_admin(tmp_path, host, monkeypatch):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 127.0.0.53\n")
    monkeypatch.setattr(client_dns, "_net_admin_usable", lambda *, use_sudo: False)
    with pytest.raises(PortmapError, match="systemd-resolved adapter is unavailable") as excinfo:
        client_dns.resolver_plan(tmp_path, backend="auto")
    assert "8.8.8.8" not in str(excinfo.value) and "1.1.1.1" not in str(excinfo.value)
    assert host.verbs("resolvectl") == []  # a doomed path is never even probed


def test_plan_auto_reports_the_limitation_for_a_mixed_stub_without_net_admin(tmp_path, host, monkeypatch):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 127.0.0.53\nnameserver 192.168.1.1\n")
    monkeypatch.setattr(client_dns, "_net_admin_usable", lambda *, use_sudo: False)
    with pytest.raises(PortmapError, match="mixes loopback and non-loopback"):
        client_dns.resolver_plan(tmp_path, backend="auto")


def test_plan_auto_keeps_resolved_for_interactive_sudo_hosts(tmp_path, host, monkeypatch):
    # A password prompt later is legitimate: planning never probes sudo -n
    # and never silently degrades merely for a missing cache.
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 127.0.0.53\n")
    monkeypatch.setattr(client_dns, "_capability", lambda prefix, capability: prefix == "CapBnd:")
    plan = client_dns.resolver_plan(tmp_path, backend="auto")
    assert plan["backend"] == "resolved"
    assert not any("sudo" in " ".join(command) for command in host.commands)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the elevation branch")
def test_plan_auto_reports_the_limitation_without_the_sudo_binary(tmp_path, host, monkeypatch):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 127.0.0.53\n")
    monkeypatch.setattr(client_dns, "_capability", lambda prefix, capability: prefix == "CapBnd:")
    monkeypatch.setattr(client_dns.shutil, "which", lambda name: None if name == "sudo" else f"/usr/sbin/{name}")
    with pytest.raises(PortmapError, match="systemd-resolved adapter is unavailable"):
        client_dns.resolver_plan(tmp_path, backend="auto")
    assert host.verbs("resolvectl") == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the elevation branch")
def test_plan_auto_honors_use_sudo_false_for_elevation(tmp_path, host, monkeypatch):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 127.0.0.53\n")
    monkeypatch.setattr(client_dns, "_capability", lambda prefix, capability: prefix == "CapBnd:")
    with pytest.raises(PortmapError, match="systemd-resolved adapter is unavailable"):
        client_dns.resolver_plan(tmp_path, backend="auto", use_sudo=False)
    assert client_dns.resolver_plan(tmp_path, backend="auto", use_sudo=True)["backend"] == "resolved"


def test_plan_auto_selects_darwin_on_macos(tmp_path, host, monkeypatch):
    monkeypatch.setattr(client_dns.sys, "platform", "darwin")
    plan = client_dns.resolver_plan(tmp_path, backend="auto")
    assert plan == {"backend": "darwin", "address": client_dns.DNS_ADDRESS, "port": client_dns.DNS_PORT, "forwarders": []}
    assert host.commands == []


def test_plan_auto_errors_helpfully_on_other_platforms(tmp_path, host, monkeypatch):
    monkeypatch.setattr(client_dns.sys, "platform", "win32")
    with pytest.raises(PortmapError, match="pass backend='forward' or backend='external'"):
        client_dns.resolver_plan(tmp_path, backend="auto")


def test_plan_rejects_unknown_backends(tmp_path, host):
    with pytest.raises(PortmapError, match="unknown resolver backend"):
        client_dns.resolver_plan(tmp_path, backend="magic")


def test_plan_rejects_platform_mismatched_backends(tmp_path, host, monkeypatch):
    monkeypatch.setattr(client_dns.sys, "platform", "darwin")
    with pytest.raises(PortmapError, match="systemd-resolved is unavailable"):
        client_dns.resolver_plan(tmp_path, backend="resolved")
    with pytest.raises(PortmapError, match="forward backend replaces /etc/resolv.conf"):
        client_dns.resolver_plan(tmp_path, backend="forward")
    monkeypatch.setattr(client_dns.sys, "platform", "linux")
    with pytest.raises(PortmapError, match="only exists on macOS"):
        client_dns.resolver_plan(tmp_path, backend="darwin")


# --- validate_resolver_plan ------------------------------------------------------------


def test_validate_resolver_plan_round_trips_every_backend(tmp_path, monkeypatch, host):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 192.168.1.1\n")
    for plan in (
        client_dns.resolver_plan(tmp_path, backend="auto"),
        client_dns.resolver_plan(tmp_path, backend="forward"),
        client_dns.resolver_plan(tmp_path, backend="external"),
    ):
        assert client_dns.validate_resolver_plan(plan) == plan


@pytest.mark.parametrize(
    "plan",
    [
        {"backend": "forward", "address": "127.0.0.1", "port": 1053, "forwarders": ["192.0.2.1"]},  # wrong port
        {"backend": "forward", "address": "127.0.0.1", "port": 53, "forwarders": []},  # no upstreams
        {"backend": "forward", "address": "127.0.0.1", "port": 53, "forwarders": ["127.0.0.1"]},  # loopback upstream
        {"backend": "external", "address": "127.0.0.1", "port": 1053, "forwarders": ["192.0.2.1"]},  # never forwards
        {"backend": "resolved", "address": "127.0.0.1", "port": 1053, "forwarders": []},  # wrong address
        {"backend": "resolved", "address": None, "port": 1053, "forwarders": []},
        {"backend": "internal", "address": "127.0.0.1", "port": 1053, "forwarders": []},
        ["not", "a", "mapping"],
    ],
)
def test_validate_resolver_plan_rejects_drift(plan):
    with pytest.raises(PortmapError):
        client_dns.validate_resolver_plan(plan)


# --- owned resolver integration --------------------------------------------------------


class FakeHost:
    """Emulates exactly the privileged surface the owned resolver route uses."""

    def __init__(self):
        self.links: dict[str, dict] = {}
        self.global_addresses: list[str] = []
        self.ifindex = 100
        self.link_dns: dict[str, str] = {}
        self.link_domains: dict[str, list[str]] = {}
        self.link_default_routes: dict[str, str] = {}
        self.reverted: list[str] = []
        self.commands: list[list[str]] = []
        self.privileged: list[list[str]] = []
        self.fail_marker: str | None = None
        self.probes: list[tuple[str, int]] = []
        self.live_probes: set[tuple[str, int]] = set()
        self.forward_checks: list[tuple[str, int, object]] = []
        self.forwarding_ok = True

    # -- probe ---------------------------------------------------------------

    def probe(self, address, port, *, timeout=0.3):
        self.probes.append((address, port))
        return "owner:probe" if (address, port) in self.live_probes else None

    def verify_forwarding(self, address, port, *, name=None, timeout=2.5):
        self.forward_checks.append((address, port, name))
        if not self.forwarding_ok:
            raise PortmapError(f"forwarding through the DNS container failed for {name}; the resolver is left unchanged")

    # -- subprocess.run replacement -------------------------------------------

    def run(self, command, **kwargs):
        decoded = list(command)
        if decoded[:1] == ["sudo"]:
            body = decoded[3:] if decoded[1:2] == ["--non-interactive"] else decoded[2:]
            self.privileged.append(body)
            decoded = body
        self.commands.append(decoded)
        joined = " ".join(decoded)
        if self.fail_marker and self.fail_marker in joined:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"simulated failure: {joined}")
        if decoded[0] == "ip":
            return self.ip(decoded)
        if decoded[0] == "resolvectl":
            return self.resolvectl(decoded)
        if decoded[0] in ("mkdir", "install", "rm", "ln"):
            return self.filesystem(decoded)
        if decoded[0] == "tee":
            Path(decoded[-1]).write_text(kwargs.get("input") or "", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected privileged command: {command}")

    def ip(self, argv):
        if argv[1] == "-j" and argv[2] == "link" and argv[3] == "show":
            link = self.links.get(argv[-1])
            if link is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            return SimpleNamespace(returncode=0, stdout=json.dumps([link]), stderr="")
        if argv[1] == "-j" and argv[2] == "address":
            info = [
                {"ifname": name, "addr_info": [{"local": address} for address in link["addresses"]]}
                for name, link in self.links.items()
            ]
            info.append({"ifname": "eth0", "addr_info": [{"local": address} for address in self.global_addresses]})
            return SimpleNamespace(returncode=0, stdout=json.dumps(info), stderr="")
        if argv[1:3] == ["link", "add"]:
            self.ifindex += 1
            self.links[argv[3]] = {"ifalias": None, "ifindex": self.ifindex, "up": False, "addresses": []}
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["link", "set"]:
            name = argv[3]
            if argv[4] == "alias":
                self.links[name]["ifalias"] = argv[5]
            elif argv[4] == "up":
                self.links[name]["up"] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["link", "delete"]:
            self.links.pop(argv[3], None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["address", "add"]:
            self.links[argv[5]]["addresses"].append(argv[3].split("/")[0])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected ip command: {argv}")

    def resolvectl(self, argv):
        verb = argv[1]
        if verb == "status":
            return SimpleNamespace(returncode=0, stdout="resolvectl ok\n", stderr="")
        name = argv[2]
        if verb == "dns" and len(argv) == 3:
            value = self.link_dns.get(name)
            detail = f"Link 5 ({name}): {value}" if value else f"Link 5 ({name}):"
            return SimpleNamespace(returncode=0, stdout=detail + "\n", stderr="")
        if verb == "domain" and len(argv) == 3:
            domains = " ".join(self.link_domains.get(name, []))
            return SimpleNamespace(returncode=0, stdout=f"Link 5 ({name}): {domains}\n", stderr="")
        if verb == "default-route" and len(argv) == 3:
            value = self.link_default_routes.get(name, "yes")
            return SimpleNamespace(returncode=0, stdout=f"Link 5 ({name}): {value}\n", stderr="")
        if verb == "dns":
            self.link_dns[name] = argv[3]
        elif verb == "domain":
            self.link_domains[name] = argv[3:]
        elif verb == "default-route":
            self.link_default_routes[name] = argv[3]
        elif verb == "revert":
            self.reverted.append(name)
            self.link_dns.pop(name, None)
            self.link_domains.pop(name, None)
            self.link_default_routes.pop(name, None)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def filesystem(self, argv):
        if argv[0] == "mkdir":
            pass  # recorded only: the fake never creates real system directories
        elif argv[0] == "install":
            source, destination = argv[-2:]
            Path(destination).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
        elif argv[0] == "rm":
            Path(argv[-1]).unlink(missing_ok=True)
        elif argv[0] == "ln":
            os.symlink(argv[-2], argv[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def verbs(self, tool):
        return [command for command in self.commands if command[0] == tool]


@pytest.fixture
def host(tmp_path, monkeypatch):
    fake = FakeHost()
    monkeypatch.setattr(client_dns.subprocess, "run", fake.run)
    monkeypatch.setattr(client_dns.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(client_dns, "probe_marker", fake.probe)
    monkeypatch.setattr(client_dns, "verify_forwarding", fake.verify_forwarding)
    monkeypatch.setattr(client_dns, "_MAC_RESOLVER_PATH", tmp_path / "etc-resolver-portmap")
    return fake


@pytest.fixture
def resolved_auto(tmp_path, monkeypatch):
    """Deterministic auto->resolved selection: a loopback-stub resolver policy
    (the only policy that keeps the adapter) plus a live systemd-resolved."""
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 127.0.0.53\noptions edns0\n")
    monkeypatch.setattr(client_dns, "_resolved_available", lambda *, use_sudo=True: True)


@pytest.fixture
def forward_stack(tmp_path, monkeypatch, host):
    """A forward plan with its listener alive and a real staged resolver file."""
    host.live_probes.add((client_dns.FORWARD_ADDRESS, client_dns.FORWARD_PORT))
    original = (
        "# managed by NetworkManager\n"
        "search office.example corp.example\n"
        "nameserver 192.168.1.1\n"
        "nameserver 10.0.0.7\n"
        "options edns0 trust-ad\n"
    )
    path = install_resolv_conf(tmp_path, monkeypatch, original, mode=0o600)
    plan = client_dns.resolver_plan(tmp_path, backend="forward")
    return path, plan, original


ENV_NAMESPACE_A = {"boot_id": "b1c2d3e4-f5a6-4789-9000-feedface0123", "mnt_ns": "mnt:[4026532401]", "net_ns": "net:[4026532402]", "user_ns": "user:[4026532403]"}
ENV_NAMESPACE_B = {"boot_id": ENV_NAMESPACE_A["boot_id"], "mnt_ns": "mnt:[4026532501]", "net_ns": "net:[4026532502]", "user_ns": "user:[4026532403]"}


def set_environment(monkeypatch, value):
    monkeypatch.setattr(client_dns, "resolver_environment", lambda: dict(value))


# --- prepare_resolver --------------------------------------------------------------------


def test_prepare_creates_only_the_owned_link_without_resolver_routing(tmp_path, host, resolved_auto):
    result = client_dns.prepare_resolver(tmp_path)

    name = client_dns._link_name(tmp_path)
    assert result["created"] is True
    assert result["interface"] == name
    assert result["address"] == client_dns.DNS_ADDRESS
    link = host.links[name]
    assert link["ifalias"] == client_dns._link_owner(tmp_path)
    assert client_dns.DNS_ADDRESS in link["addresses"]
    assert link["up"] is True
    assert all(command == ["resolvectl", "status"] for command in host.verbs("resolvectl"))
    assert host.verbs("ip")


def test_prepare_is_idempotent_and_still_brings_the_link_up(tmp_path, host, resolved_auto):
    assert client_dns.prepare_resolver(tmp_path)["created"] is True
    host.links[client_dns._link_name(tmp_path)]["up"] = False

    result = client_dns.prepare_resolver(tmp_path)

    assert result["created"] is False
    assert host.links[client_dns._link_name(tmp_path)]["up"] is True
    assert all(command == ["resolvectl", "status"] for command in host.verbs("resolvectl"))


def test_prepare_refuses_an_unrelated_interface_with_the_same_name(tmp_path, host, resolved_auto):
    name = client_dns._link_name(tmp_path)
    host.links[name] = {"ifalias": "someone else", "ifindex": 7, "up": True, "addresses": []}

    with pytest.raises(PortmapError, match="refusing to change unrelated interface"):
        client_dns.prepare_resolver(tmp_path)
    assert host.links[name]["ifalias"] == "someone else"


def test_prepare_refuses_an_address_already_assigned_elsewhere(tmp_path, host, resolved_auto):
    host.global_addresses.append(client_dns.DNS_ADDRESS)
    with pytest.raises(PortmapError, match="already assigned"):
        client_dns.prepare_resolver(tmp_path)
    assert host.links == {}


def test_prepare_failure_deletes_the_freshly_created_link(tmp_path, host, resolved_auto):
    host.fail_marker = "ip address add"
    with pytest.raises(PortmapError, match="simulated failure"):
        client_dns.prepare_resolver(tmp_path)
    assert host.links == {}


def test_prepare_without_state_is_a_noop_on_macos(tmp_path, host, monkeypatch):
    monkeypatch.setattr(client_dns.sys, "platform", "darwin")
    result = client_dns.prepare_resolver(tmp_path)
    assert result == {
        "platform": "darwin",
        "backend": "darwin",
        "address": client_dns.DNS_ADDRESS,
        "port": client_dns.DNS_PORT,
        "created": False,
    }
    assert host.commands == []


def test_prepare_forward_captures_upstreams_and_bindability(forward_stack, host):
    path, plan, original = forward_stack

    result = client_dns.prepare_resolver(path.parents[1], plan=plan)

    assert result["backend"] == "forward" and result["port"] == 53
    assert result["forwarders"] == ["192.168.1.1", "10.0.0.7"]
    assert result["bindable"] is True and result["created"] is False
    assert host.verbs("ip") == [] and host.verbs("resolvectl") == []  # forward never touches systemd tooling
    assert path.read_text(encoding="utf-8") == original


def test_prepare_forward_fails_when_a_foreign_service_holds_loopback_53(forward_stack, host, monkeypatch):
    path, plan, _ = forward_stack
    monkeypatch.setattr(client_dns, "_loopback_bindable", lambda: False)
    with pytest.raises(PortmapError, match="another service already holds it"):
        client_dns.prepare_resolver(path.parents[1], plan=plan)


def test_prepare_external_writes_nothing_and_runs_nothing(tmp_path, host):
    result = client_dns.prepare_resolver(tmp_path, plan={"backend": "external", "address": "127.0.0.1", "port": 1053, "forwarders": []})
    assert result == {"platform": client_dns.sys.platform, "backend": "external", "address": "127.0.0.1", "port": 1053, "forwarders": [], "created": False}
    assert host.commands == []


# --- setup_resolver -------------------------------------------------------------------------


def test_setup_refuses_to_route_at_a_dead_listener_before_any_mutation(tmp_path, host, resolved_auto):
    with pytest.raises(PortmapError, match="no portmap DNS listener is answering"):
        client_dns.setup_resolver(tmp_path, dns_port=1054)
    # Only read-only selection queries may precede the gate; nothing mutates.
    assert host.verbs("ip") == []
    assert all(command[0] == "resolvectl" for command in host.commands)
    assert host.probes == [(client_dns.DNS_ADDRESS, 1054)]


def test_setup_installs_the_owned_suffix_route_at_the_actual_port(tmp_path, host, resolved_auto):
    host.live_probes.add((client_dns.DNS_ADDRESS, 1054))
    client_dns.prepare_resolver(tmp_path)

    status = client_dns.setup_resolver(tmp_path, dns_port=1054)

    name = client_dns._link_name(tmp_path)
    assert status["installed"] is True
    assert status["port"] == 1054
    assert host.link_dns[name] == f"{client_dns.DNS_ADDRESS}:1054"
    assert "~portmap" in host.link_domains[name]
    assert any(command[:2] == ["resolvectl", "default-route"] for command in host.commands)
    saved = json.loads((tmp_path / "resolver.json").read_text(encoding="utf-8"))
    assert saved == {"version": 2, "backend": "resolved", "address": client_dns.DNS_ADDRESS, "port": 1054, "forwarders": []}


def test_setup_port_switch_restores_the_previous_owned_port_on_failure(tmp_path, host, resolved_auto):
    host.live_probes.add((client_dns.DNS_ADDRESS, 1054))
    client_dns.prepare_resolver(tmp_path)
    name = client_dns._link_name(tmp_path)
    # A live native 1053 route from before the container migration.
    host.link_dns[name] = f"{client_dns.DNS_ADDRESS}:1053"
    host.link_domains[name] = ["~portmap"]
    host.fail_marker = "resolvectl domain"

    with pytest.raises(PortmapError, match="simulated failure"):
        client_dns.setup_resolver(tmp_path, dns_port=1054)

    assert host.link_dns[name] == f"{client_dns.DNS_ADDRESS}:1053"
    assert not (tmp_path / "resolver.json").exists()


def test_setup_failure_without_previous_route_reverts_the_link(tmp_path, host, resolved_auto):
    host.live_probes.add((client_dns.DNS_ADDRESS, 1054))
    client_dns.prepare_resolver(tmp_path)
    host.fail_marker = "resolvectl domain"

    with pytest.raises(PortmapError, match="simulated failure"):
        client_dns.setup_resolver(tmp_path, dns_port=1054)

    assert host.reverted == [client_dns._link_name(tmp_path)]
    assert not (tmp_path / "resolver.json").exists()


def test_setup_rejects_invalid_ports_before_probing_or_mutating(tmp_path, host):
    with pytest.raises(PortmapError, match="between 1 and 65535"):
        client_dns.setup_resolver(tmp_path, dns_port="53")
    assert host.commands == []
    assert host.probes == []


# --- setup_resolver: forward backend ---------------------------------------------------------


def test_setup_forward_takes_over_resolv_conf_preserving_search_options(forward_stack, host):
    path, plan, original = forward_stack
    state_dir = path.parents[1]

    status = client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)

    assert status["installed"] is True and status["backend"] == "forward" and status["port"] == 53
    assert (client_dns.FORWARD_ADDRESS, client_dns.FORWARD_PORT) in host.probes  # marker gate
    assert host.forward_checks and host.forward_checks[0][:2] == ("127.0.0.1", 53)
    assert str(host.forward_checks[0][2]).endswith(".office.example")  # search-zone probe
    assert path.read_text(encoding="utf-8") == (
        "# managed by NetworkManager\n"
        "search office.example corp.example\n"
        "nameserver 127.0.0.1\n"
        "options edns0 trust-ad\n"
    )
    saved = json.loads((state_dir / "resolver.json").read_text(encoding="utf-8"))
    assert saved["backend"] == "forward" and saved["forwarders"] == ["192.168.1.1", "10.0.0.7"]
    assert saved["original"]["content"] == original
    assert saved["original"]["kind"] == "regular" and saved["original"]["mode"] == 0o600


def test_setup_forward_requires_the_marker_listener(tmp_path, monkeypatch, host):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 192.168.1.1\n")
    plan = {"backend": "forward", "address": "127.0.0.1", "port": 53, "forwarders": ["192.168.1.1"]}
    with pytest.raises(PortmapError, match="no portmap DNS listener is answering"):
        client_dns.setup_resolver(tmp_path, dns_port=53, plan=plan)
    assert (tmp_path / "etc" / "resolv.conf").read_text(encoding="utf-8") == "nameserver 192.168.1.1\n"
    assert not (tmp_path / "resolver.json").exists()


def test_setup_forward_proves_real_forwarding_before_the_cutover(forward_stack, host):
    # A dead upstream must leave the original resolver completely unchanged.
    path, plan, original = forward_stack
    host.forwarding_ok = False

    with pytest.raises(PortmapError, match="resolver is left unchanged"):
        client_dns.setup_resolver(path.parents[1], dns_port=53, plan=plan)

    assert path.read_text(encoding="utf-8") == original
    assert not (path.parents[1] / "resolver.json").exists()


def test_setup_forward_pins_loopback_port_53(forward_stack, host):
    path, plan, original = forward_stack
    with pytest.raises(PortmapError, match="cannot select another port"):
        client_dns.setup_resolver(path.parents[1], dns_port=1053, plan=plan)
    assert path.read_text(encoding="utf-8") == original


def test_setup_forward_restores_the_original_when_the_write_fails(forward_stack, host, monkeypatch):
    path, plan, original = forward_stack
    real_write = client_dns._write_resolver_file
    pending_failure = [True]

    def broken_write(state_dir, content, **kwargs):
        real_write(state_dir, content, **kwargs)
        if pending_failure:
            pending_failure.pop()
            raise PortmapError("simulated write failure")

    monkeypatch.setattr(client_dns, "_write_resolver_file", broken_write)
    with pytest.raises(PortmapError, match="simulated write failure"):
        client_dns.setup_resolver(path.parents[1], dns_port=53, plan=plan)
    assert path.read_text(encoding="utf-8") == original
    assert not (path.parents[1] / "resolver.json").exists()


def test_setup_forward_does_not_redirect_without_durable_recovery_state(forward_stack, monkeypatch):
    path, plan, original = forward_stack

    def fail_state_write(*args, **kwargs):
        raise OSError("state disk full")

    monkeypatch.setattr(client_dns, "_write_resolver_state", fail_state_write)
    with pytest.raises(OSError):
        client_dns.setup_resolver(path.parents[1], dns_port=53, plan=plan)
    assert path.read_text(encoding="utf-8") == original


def test_setup_forward_resumes_a_saved_but_unapplied_cutover(forward_stack):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    client_dns._write_resolver_state(state_dir, plan, original=client_dns._resolv_identity())

    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    assert path.read_text(encoding="utf-8") == client_dns._forward_resolv_content(original)
    client_dns.teardown_resolver(state_dir)
    assert path.read_text(encoding="utf-8") == original
    assert not (state_dir / "resolver.json").exists()


def test_setup_forward_refuses_an_adopted_loopback_without_recovery_state(forward_stack):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    redirected = client_dns._forward_resolv_content(original)
    path.write_text(redirected, encoding="utf-8")

    with pytest.raises(PortmapError):
        client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    assert path.read_text(encoding="utf-8") == redirected
    assert not (state_dir / "resolver.json").exists()


def test_setup_forward_keeps_recovery_state_when_the_restore_fails(forward_stack, host, monkeypatch):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    real_write = client_dns._write_resolver_file
    real_restore = client_dns._restore_resolv
    write_failures = [1]
    restore_failures = [1]

    def failing_write(state_dir_, content, **kwargs):
        real_write(state_dir_, content, **kwargs)
        if write_failures:
            write_failures.pop()
            raise PortmapError("simulated write failure")

    def failing_restore(*args, **kwargs):
        if restore_failures:
            restore_failures.pop()
            raise PortmapError("simulated restore failure")
        return real_restore(*args, **kwargs)

    monkeypatch.setattr(client_dns, "_write_resolver_file", failing_write)
    monkeypatch.setattr(client_dns, "_restore_resolv", failing_restore)
    with pytest.raises(PortmapError, match="restore"):
        client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)

    saved = json.loads((state_dir / "resolver.json").read_text(encoding="utf-8"))
    assert saved["backend"] == "forward" and saved["original"]["content"] == original
    assert saved["active"] is None  # only the pre-write journal exists; ownership is now uncertain

    # The write was applied but neither the active-identity commit nor the
    # in-process rollback succeeded: a later invocation must not invent
    # ownership over the unprovable redirect file. Teardown fails closed and
    # keeps the journal for manual recovery instead of restoring blindly.
    with pytest.raises(PortmapError, match="cannot prove"):
        client_dns.teardown_resolver(state_dir)
    assert path.read_text(encoding="utf-8") == client_dns._forward_resolv_content(original)
    assert (state_dir / "resolver.json").exists()
    report = client_dns.resolver_status(state_dir)
    assert report["installed"] is False and report["stale"] is True


def test_setup_forward_is_idempotent_while_installed(forward_stack, host):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    taken = path.read_text(encoding="utf-8")

    fresh = client_dns.resolver_plan(state_dir, backend="forward")
    status = client_dns.setup_resolver(state_dir, dns_port=53, plan=fresh)

    assert status["installed"] is True
    assert path.read_text(encoding="utf-8") == taken
    assert host.forward_checks  # the forwarding gate still runs on re-setup


def test_setup_forward_refuses_an_externally_changed_resolver(forward_stack, host):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    path.write_text("nameserver 10.9.9.9\n", encoding="utf-8")

    with pytest.raises(PortmapError, match="changed externally"):
        client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    with pytest.raises(PortmapError, match="changed externally"):
        client_dns.teardown_resolver(state_dir)
    assert path.read_text(encoding="utf-8") == "nameserver 10.9.9.9\n"
    assert (state_dir / "resolver.json").exists()  # recovery state survives the refusal


def test_setup_forward_refuses_to_switch_over_a_saved_foreign_backend(tmp_path, monkeypatch, host):
    path = install_resolv_conf(tmp_path, monkeypatch, "nameserver 192.168.1.1\n")
    write_saved_state(tmp_path, {"version": 1, "address": client_dns.DNS_ADDRESS, "port": 1054})
    plan = {"backend": "forward", "address": "127.0.0.1", "port": 53, "forwarders": ["192.168.1.1"]}

    with pytest.raises(PortmapError, match="call teardown_resolver first"):
        client_dns.setup_resolver(tmp_path, dns_port=53, plan=plan)
    assert path.read_text(encoding="utf-8") == "nameserver 192.168.1.1\n"


def test_forward_paths_never_execute_ip_or_resolvectl(forward_stack, host):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    client_dns.prepare_resolver(state_dir, plan=plan)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    client_dns.resolver_status(state_dir)
    client_dns.teardown_resolver(state_dir)
    assert host.verbs("ip") == []
    assert host.verbs("resolvectl") == []
    assert host.verbs("systemctl") == []
    assert host.verbs("sudo") == []


@pytest.mark.skipif(os.geteuid() == 0, reason="permission-based fallback invisible to root")
def test_forward_uses_privileged_install_when_the_directory_is_not_writable(forward_stack, host):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    etc = path.parent
    etc.chmod(0o500)
    try:
        client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
        assert any(command[0] == "install" for command in host.privileged)
        assert "nameserver 127.0.0.1" in path.read_text(encoding="utf-8")
    finally:
        etc.chmod(0o755)


# --- setup_resolver: forward takeover scope notice ---------------------------------------------


def test_takeover_announces_the_scope_on_stderr_before_the_redirect(forward_stack, host, capsys):
    path, plan, original = forward_stack

    client_dns.setup_resolver(path.parents[1], dns_port=53, plan=plan)

    notice = capsys.readouterr().err
    assert str(client_dns._RESOLV_CONF) in notice  # the visible resolver file
    assert "127.0.0.1:53" in notice
    assert "192.168.1.1" in notice and "10.0.0.7" in notice  # original forwarding retained
    assert "office.example" in notice and "options" in notice  # search/options preserved
    assert "same resolver file" in notice  # shared-file impact is announced
    assert "choose" not in notice.lower()  # no backend-chooser prompt


def test_no_scope_notice_on_an_unchanged_refresh(forward_stack, host, capsys):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    capsys.readouterr()

    fresh = client_dns.resolver_plan(state_dir, backend="forward")
    client_dns.setup_resolver(state_dir, dns_port=53, plan=fresh)

    assert capsys.readouterr().err == ""


def test_no_scope_notice_when_the_cutover_is_refused(forward_stack, host, capsys):
    path, plan, _ = forward_stack
    host.forwarding_ok = False

    with pytest.raises(PortmapError, match="resolver is left unchanged"):
        client_dns.setup_resolver(path.parents[1], dns_port=53, plan=plan)

    assert capsys.readouterr().err == ""


# --- forward journal: environment identity and Pod recreation -----------------------------------


@pytest.mark.parametrize("new_start", ["100", "200"])
def test_recycled_namespaces_use_init_lifetime_for_resolver_ownership(forward_stack, host, monkeypatch, new_start):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    generation = {"start": "100", "counter": "0"}
    read_text, readlink = Path.read_text, os.readlink
    namespaces = {f"/proc/self/ns/{name}": ENV_NAMESPACE_A[f"{name}_ns"] for name in ("mnt", "net", "user")}

    def proc_text(file, *args, **kwargs):
        if file == Path("/proc/1/stat"):
            return "1 (fixture (init)) S " + (generation["counter"] + " ") * 18 + generation["start"] + " 0\n"
        if file == Path("/proc/sys/kernel/random/boot_id"):
            return ENV_NAMESPACE_A["boot_id"]
        return read_text(file, *args, **kwargs)

    def namespace_link(file, *args, **kwargs):
        if str(file) in namespaces:
            return namespaces[str(file)]
        return readlink(file, *args, **kwargs)

    monkeypatch.setattr(client_dns.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", proc_text)
    monkeypatch.setattr(os, "readlink", namespace_link)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    journal = state_dir / "resolver.json"
    before = journal.read_bytes()
    generation.update(start=new_start, counter="7")
    fresh = "nameserver 10.77.0.1\nsearch fresh.example\noptions attempts:2\n"
    replacement = path.with_name("fresh-resolver")
    replacement.write_text(fresh, encoding="utf-8")
    os.replace(replacement, path)
    current_plan = client_dns.resolver_plan(state_dir, backend="auto")

    if new_start == "100":
        # CPU/stat changes are not a new generation: never overwrite an
        # external resolver edit in the same init lifetime.
        with pytest.raises(PortmapError, match="changed externally"):
            client_dns.setup_resolver(state_dir, dns_port=53, plan=current_plan)
        assert path.read_text(encoding="utf-8") == fresh
        assert journal.read_bytes() == before
    else:
        assert current_plan["forwarders"] == ["10.77.0.1"]
        client_dns.setup_resolver(state_dir, dns_port=53, plan=current_plan)
        assert json.loads(journal.read_text(encoding="utf-8"))["original"]["content"] == fresh
        client_dns.teardown_resolver(state_dir)
        assert path.read_text(encoding="utf-8") == fresh
        assert not journal.exists()


def test_pod_recreation_adopts_the_fresh_resolver_not_the_old_backup(forward_stack, host, monkeypatch):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    first = json.loads((state_dir / "resolver.json").read_text(encoding="utf-8"))
    assert first["environment"] == ENV_NAMESPACE_A

    # Pod recreated: a new environment identity and a brand-new resolver file
    # with different servers and a different inode.
    set_environment(monkeypatch, ENV_NAMESPACE_B)
    fresh = "nameserver 10.77.0.1\nsearch cluster.local\n"
    path.unlink()
    path.write_text(fresh, encoding="utf-8")

    plan = client_dns.resolver_plan(state_dir, backend="auto")
    assert plan["forwarders"] == ["10.77.0.1"]  # the current upstreams, not the dead ones

    status = client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    assert status["installed"] is True
    saved = json.loads((state_dir / "resolver.json").read_text(encoding="utf-8"))
    assert saved["environment"] == ENV_NAMESPACE_B
    assert saved["original"]["content"] == fresh and saved["original"]["ino"] != first["original"]["ino"]
    assert saved["forwarders"] == ["10.77.0.1"]
    assert path.read_text(encoding="utf-8") == client_dns._forward_resolv_content(fresh)

    client_dns.teardown_resolver(state_dir)
    assert path.read_text(encoding="utf-8") == fresh  # teardown restores the NEW original
    assert not (state_dir / "resolver.json").exists()


def test_environment_change_keeps_the_restorable_original_for_the_same_owned_file(forward_stack, host, monkeypatch):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    taken = path.read_text(encoding="utf-8")
    inode = path.stat().st_ino
    saved = json.loads((state_dir / "resolver.json").read_text(encoding="utf-8"))
    assert saved["active"]["ino"] == inode and saved["active"]["kind"] == "regular"  # post-write identity journaled

    # Recreated environment carrying the same owned resolver file: the active
    # identity proves ownership of the carried inode before the redirect is reused.
    set_environment(monkeypatch, ENV_NAMESPACE_B)
    status = client_dns.setup_resolver(state_dir, dns_port=53, plan=client_dns.resolver_plan(state_dir, backend="auto"))
    assert status["installed"] is True
    assert path.read_text(encoding="utf-8") == taken and path.stat().st_ino == inode
    saved = json.loads((state_dir / "resolver.json").read_text(encoding="utf-8"))
    assert saved["environment"] == ENV_NAMESPACE_B
    assert saved["active"]["ino"] == inode
    assert saved["original"]["content"] == original  # the 127.0.0.1 redirect never became its own original

    client_dns.teardown_resolver(state_dir)
    assert path.read_text(encoding="utf-8") == original
    assert not (state_dir / "resolver.json").exists()


def test_a_fresh_loopback_file_in_a_new_environment_never_becomes_its_own_original(forward_stack, host, monkeypatch):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)

    set_environment(monkeypatch, ENV_NAMESPACE_B)
    path.write_text("nameserver 127.0.0.1\n", encoding="utf-8")  # a copy of portmap's own redirect

    with pytest.raises(PortmapError, match="new environment"):
        client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    saved = json.loads((state_dir / "resolver.json").read_text(encoding="utf-8"))
    assert saved["original"]["content"] == original  # fail closed, never a self-loop backup
    with pytest.raises(PortmapError):
        client_dns.resolver_plan(state_dir, backend="auto")  # planning fails closed too

    client_dns.teardown_resolver(state_dir)  # the now-provably-stale record drops untouched
    assert path.read_text(encoding="utf-8") == "nameserver 127.0.0.1\n"
    assert not (state_dir / "resolver.json").exists()


def test_fresh_inode_with_the_redirect_bytes_in_a_new_environment_is_refused(forward_stack, host, monkeypatch):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    taken = path.read_text(encoding="utf-8")  # portmap's own redirect bytes

    set_environment(monkeypatch, ENV_NAMESPACE_B)
    path.unlink()
    path.write_text(taken, encoding="utf-8")  # a fresh inode seeded with identical bytes

    with pytest.raises(PortmapError, match="cannot prove"):
        client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    report = client_dns.resolver_status(state_dir)
    assert report["installed"] is False and report["stale"] is True  # never claimed as the integration
    with pytest.raises(PortmapError, match="cannot prove"):
        client_dns.teardown_resolver(state_dir)  # the old original is not restored over it either
    saved = json.loads((state_dir / "resolver.json").read_text(encoding="utf-8"))
    assert saved["original"]["content"] == original  # recovery state survives, nothing was adopted
    assert path.read_text(encoding="utf-8") == taken


def test_same_env_identical_bytes_replacement_of_the_redirect_is_refused(forward_stack, host, monkeypatch):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    taken = path.read_text(encoding="utf-8")
    path.unlink()
    path.write_text(taken, encoding="utf-8")  # identical bytes, fresh inode, SAME environment

    with pytest.raises(PortmapError, match="cannot prove"):
        client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)  # never adopted as the reuse target
    report = client_dns.resolver_status(state_dir)
    assert report["installed"] is False and report["stale"] is True
    with pytest.raises(PortmapError, match="cannot prove"):
        client_dns.teardown_resolver(state_dir)  # the original is not restored over it either
    assert path.read_text(encoding="utf-8") == taken
    assert (state_dir / "resolver.json").exists()  # recovery state survives the refusal


def test_unreadable_resolver_file_never_proves_staleness(forward_stack, host, monkeypatch):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)

    set_environment(monkeypatch, ENV_NAMESPACE_B)
    path.unlink()  # the live resolver cannot be inspected at all

    with pytest.raises(PortmapError, match="cannot inspect"):
        client_dns.teardown_resolver(state_dir)
    assert (state_dir / "resolver.json").exists()  # fail closed: recovery is retained
    report = client_dns.resolver_status(state_dir)
    assert report["installed"] is False and report["stale"] is False and report["pending"] is False


def test_status_reports_pending_for_an_unapplied_original(forward_stack, host, monkeypatch):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    client_dns._write_resolver_state(state_dir, plan, original=client_dns._resolv_identity(), environment=ENV_NAMESPACE_A)

    report = client_dns.resolver_status(state_dir)
    assert report["installed"] is False and report["pending"] is True and report["stale"] is False

    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    report = client_dns.resolver_status(state_dir)
    assert report["installed"] is True and report["pending"] is False


def test_bind_rewrite_surviving_recreation_stays_provable_without_an_active_identity(forward_stack, host, monkeypatch):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    monkeypatch.setattr(client_dns, "_is_bind_mount", lambda stat, parent_stat: True)
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    identity = client_dns._resolv_identity()
    assert identity["kind"] == "bind"
    client_dns._write_resolver_state(state_dir, plan, original=identity, environment=ENV_NAMESPACE_A)  # no active yet
    path.write_text(client_dns._forward_resolv_content(original), encoding="utf-8")  # in-place rewrite keeps the inode

    set_environment(monkeypatch, ENV_NAMESPACE_B)
    status = client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)  # the original inode still proves ownership
    assert status["installed"] is True
    saved = json.loads((state_dir / "resolver.json").read_text(encoding="utf-8"))
    assert saved["environment"] == ENV_NAMESPACE_B and saved["active"]["ino"] == identity["ino"]

    client_dns.teardown_resolver(state_dir)
    assert path.read_text(encoding="utf-8") == original
    assert not (state_dir / "resolver.json").exists()


def test_planning_is_never_rejected_for_a_busy_port(tmp_path, monkeypatch, host):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 192.168.1.1\n")
    monkeypatch.setattr(client_dns, "_loopback_bindable", lambda: False)  # even a held 53 must not gate planning

    plan = client_dns.resolver_plan(tmp_path, backend="auto")

    assert plan["backend"] == "forward" and plan["forwarders"] == ["192.168.1.1"]


def test_prepare_accepts_an_already_live_owned_dns_container_on_53(tmp_path, monkeypatch, host):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 192.168.1.1\n")
    plan = client_dns.resolver_plan(tmp_path, backend="auto")
    host.live_probes.add((client_dns.FORWARD_ADDRESS, client_dns.FORWARD_PORT))  # retained CoreDNS answers the marker

    result = client_dns.prepare_resolver(tmp_path, plan=plan)

    assert result["bindable"] is True  # probe-first: an owned live listener is not "port busy"
    assert host.commands == []


def test_same_environment_foreign_edits_fail_closed(forward_stack, host, monkeypatch):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    path.write_text("nameserver 10.9.9.9\n", encoding="utf-8")

    with pytest.raises(PortmapError, match="changed externally"):
        client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    with pytest.raises(PortmapError, match="changed externally"):
        client_dns.teardown_resolver(state_dir)
    assert path.read_text(encoding="utf-8") == "nameserver 10.9.9.9\n"
    assert (state_dir / "resolver.json").exists()  # recovery state survives the refusal


def test_teardown_drops_a_proven_stale_record_without_touching_the_fresh_file(forward_stack, host, monkeypatch):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)

    set_environment(monkeypatch, ENV_NAMESPACE_B)
    fresh = "nameserver 10.77.0.1\n"
    path.write_text(fresh, encoding="utf-8")  # the new environment's own resolver
    client_dns.teardown_resolver(state_dir)

    assert path.read_text(encoding="utf-8") == fresh
    assert not (state_dir / "resolver.json").exists()


def test_status_marks_a_proven_stale_journal_and_never_claims_installed(forward_stack, host, monkeypatch):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    assert client_dns.resolver_status(state_dir)["stale"] is False

    set_environment(monkeypatch, ENV_NAMESPACE_B)
    path.write_text("nameserver 10.77.0.1\n", encoding="utf-8")
    report = client_dns.resolver_status(state_dir)
    assert report["installed"] is False and report["stale"] is True


def test_pending_cutover_resumes_and_rebacks_up_the_current_file_after_recreation(forward_stack, host, monkeypatch):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    set_environment(monkeypatch, ENV_NAMESPACE_A)
    stale_inode = path.stat().st_ino
    client_dns._write_resolver_state(state_dir, plan, original=client_dns._resolv_identity(), environment=ENV_NAMESPACE_A)

    set_environment(monkeypatch, ENV_NAMESPACE_B)
    sibling = path.with_name(".resolv.conf.recreated")
    sibling.write_text(original, encoding="utf-8")
    os.replace(sibling, path)  # a genuinely new inode carrying identical bytes
    fresh_inode = path.stat().st_ino

    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    saved = json.loads((state_dir / "resolver.json").read_text(encoding="utf-8"))
    assert saved["environment"] == ENV_NAMESPACE_B
    assert saved["original"]["ino"] == fresh_inode and fresh_inode != stale_inode
    assert path.read_text(encoding="utf-8") == client_dns._forward_resolv_content(original)

    client_dns.teardown_resolver(state_dir)
    assert path.read_text(encoding="utf-8") == original
    assert not (state_dir / "resolver.json").exists()


def test_legacy_journal_without_environment_still_fails_closed_on_a_replaced_file(forward_stack, monkeypatch):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    client_dns._write_resolver_state(state_dir, plan, original=client_dns._resolv_identity())  # legacy: no environment
    path.write_text("nameserver 10.77.0.1\n", encoding="utf-8")

    with pytest.raises(PortmapError, match="changed externally"):
        client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)


# --- setup/status/teardown: external backend --------------------------------------------------


def test_setup_external_verifies_the_entrypoint_and_records_ownership_only(tmp_path, monkeypatch, host):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 192.168.1.1\n")
    host.live_probes.add((client_dns.EXTERNAL_ADDRESS, client_dns.EXTERNAL_PORT))

    status = client_dns.setup_resolver(tmp_path, dns_port=1053, plan={"backend": "external", "address": "127.0.0.1", "port": 1053, "forwarders": []})

    assert status["installed"] is True and status["backend"] == "external"
    assert host.commands == []  # no ip/resolvectl/systemctl ever
    assert (tmp_path / "etc" / "resolv.conf").read_text(encoding="utf-8") == "nameserver 192.168.1.1\n"
    saved = json.loads((tmp_path / "resolver.json").read_text(encoding="utf-8"))
    assert saved == {"version": 2, "backend": "external", "address": "127.0.0.1", "port": 1053, "forwarders": []}


def test_setup_external_requires_the_entrypoint_listener(tmp_path, monkeypatch, host):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 192.168.1.1\n")
    with pytest.raises(PortmapError, match="no portmap DNS listener is answering"):
        client_dns.setup_resolver(tmp_path, dns_port=1053, plan={"backend": "external", "address": "127.0.0.1", "port": 1053, "forwarders": []})
    assert not (tmp_path / "resolver.json").exists()


def test_status_external_tracks_only_the_entrypoint_and_teardown_writes_nothing(tmp_path, monkeypatch, host):
    install_resolv_conf(tmp_path, monkeypatch, "nameserver 192.168.1.1\n")
    host.live_probes.add((client_dns.EXTERNAL_ADDRESS, client_dns.EXTERNAL_PORT))
    client_dns.setup_resolver(tmp_path, dns_port=1053, plan={"backend": "external", "address": "127.0.0.1", "port": 1053, "forwarders": []})
    host.commands.clear()

    status = client_dns.resolver_status(tmp_path)
    assert status["installed"] is True
    assert status["backend"] == "external" and status["port"] == 1053
    assert status["plan"]["backend"] == "external"

    host.live_probes.clear()
    assert client_dns.resolver_status(tmp_path)["installed"] is False

    client_dns.teardown_resolver(tmp_path)
    assert not (tmp_path / "resolver.json").exists()
    assert (tmp_path / "etc" / "resolv.conf").read_text(encoding="utf-8") == "nameserver 192.168.1.1\n"
    assert host.commands == []


# --- resolver_status --------------------------------------------------------------------------


def test_status_defaults_to_saved_port_then_the_historical_one(tmp_path, host, resolved_auto):
    client_dns.prepare_resolver(tmp_path)
    name = client_dns._link_name(tmp_path)
    assert client_dns.resolver_status(tmp_path)["port"] == client_dns.DNS_PORT  # nothing saved yet

    host.link_dns[name] = f"{client_dns.DNS_ADDRESS}:1054"
    host.link_domains[name] = ["~portmap"]
    host.link_default_routes[name] = "no"
    (tmp_path / "resolver.json").write_text(
        json.dumps({"version": 1, "address": client_dns.DNS_ADDRESS, "port": 1054}), encoding="utf-8"
    )

    status = client_dns.resolver_status(tmp_path)
    assert status["installed"] is True
    assert status["port"] == 1054
    assert status["saved_port"] == 1054
    assert status["backend"] == "resolved"
    assert status["plan"]["backend"] == "resolved"

    explicit = client_dns.resolver_status(tmp_path, dns_port=53)
    assert explicit["port"] == 53
    assert explicit["installed"] is False


def test_status_defaults_to_the_historical_port_for_a_native_route(tmp_path, host, resolved_auto):
    client_dns.prepare_resolver(tmp_path)
    name = client_dns._link_name(tmp_path)
    host.link_dns[name] = f"{client_dns.DNS_ADDRESS}:1053"
    host.link_domains[name] = ["~portmap"]
    host.link_default_routes[name] = "no"
    status = client_dns.resolver_status(tmp_path)
    assert status["installed"] is True
    assert status["port"] == 1053
    assert status["saved_port"] is None


def test_status_reports_unowned_link_as_not_installed(tmp_path, host):
    name = client_dns._link_name(tmp_path)
    host.links[name] = {"ifalias": "someone else", "ifindex": 9, "up": True, "addresses": []}
    assert client_dns.resolver_status(tmp_path)["installed"] is False


def test_status_surfaces_invalid_saved_state(tmp_path, host):
    (tmp_path / "resolver.json").write_text(
        json.dumps({"version": 1, "address": "10.0.0.1", "port": 53}), encoding="utf-8"
    )
    with pytest.raises(PortmapError, match="invalid portmap resolver state"):
        client_dns.resolver_status(tmp_path)


def test_status_forward_compares_the_live_file_without_running_anything(forward_stack, host):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    host.commands.clear()

    status = client_dns.resolver_status(state_dir)
    assert status["installed"] is True and status["backend"] == "forward" and status["port"] == 53
    assert host.commands == []

    path.write_text("nameserver 10.9.9.9\n", encoding="utf-8")
    assert client_dns.resolver_status(state_dir)["installed"] is False


def test_status_tolerates_missing_binaries_without_executing_them(tmp_path, host, monkeypatch):
    write_saved_state(tmp_path, {"version": 2, "backend": "resolved", "address": client_dns.DNS_ADDRESS, "port": 1054, "forwarders": []})
    monkeypatch.setattr(client_dns.shutil, "which", lambda name: None)

    status = client_dns.resolver_status(tmp_path)

    assert status["installed"] is False
    assert status["saved_port"] == 1054
    assert host.commands == []


# --- teardown_resolver --------------------------------------------------------------------------


def test_teardown_removes_only_owned_integration_and_port_metadata(tmp_path, host, resolved_auto):
    host.live_probes.add((client_dns.DNS_ADDRESS, 1054))
    client_dns.prepare_resolver(tmp_path)
    client_dns.setup_resolver(tmp_path, dns_port=1054)
    name = client_dns._link_name(tmp_path)

    client_dns.teardown_resolver(tmp_path)

    assert name not in host.links
    assert not (tmp_path / "resolver.json").exists()


def test_teardown_refuses_foreign_link_and_keeps_metadata(tmp_path, host):
    name = client_dns._link_name(tmp_path)
    host.links[name] = {"ifalias": "someone else", "ifindex": 9, "up": True, "addresses": []}
    (tmp_path / "resolver.json").write_text(
        json.dumps({"version": 1, "address": client_dns.DNS_ADDRESS, "port": 1054}), encoding="utf-8"
    )

    with pytest.raises(PortmapError, match="refusing to remove an unrelated DNS interface"):
        client_dns.teardown_resolver(tmp_path)
    assert name in host.links
    assert (tmp_path / "resolver.json").exists()


def test_teardown_forward_restores_content_and_mode_exactly(forward_stack, host):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    assert path.stat().st_mode & 0o777 == 0o600  # the takeover preserves the original mode

    client_dns.teardown_resolver(state_dir)

    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (state_dir / "resolver.json").exists()


def test_teardown_forward_restores_a_symlinked_resolver(tmp_path, monkeypatch, host):
    target = tmp_path / "etc" / "upstream-resolv.conf"
    path = install_resolv_conf(tmp_path, monkeypatch, "nameserver 192.168.1.1\n", symlink_to="upstream-resolv.conf")
    host.live_probes.add((client_dns.FORWARD_ADDRESS, client_dns.FORWARD_PORT))
    plan = {"backend": "forward", "address": "127.0.0.1", "port": 53, "forwarders": ["192.168.1.1"]}

    client_dns.setup_resolver(tmp_path, dns_port=53, plan=plan)
    assert not path.is_symlink() and path.read_text(encoding="utf-8") == "nameserver 127.0.0.1\n"
    assert target.read_text(encoding="utf-8") == "nameserver 192.168.1.1\n"  # target remains untouched

    client_dns.teardown_resolver(tmp_path)
    assert path.is_symlink() and os.readlink(path) == str(target)
    assert path.read_text(encoding="utf-8") == "nameserver 192.168.1.1\n"
    assert not (tmp_path / "resolver.json").exists()


def test_teardown_forward_rewrites_bind_mounts_in_place(forward_stack, host, monkeypatch):
    path, plan, original = forward_stack
    state_dir = path.parents[1]
    monkeypatch.setattr(client_dns, "_is_bind_mount", lambda stat, parent_stat: True)
    ino = path.stat().st_ino

    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    assert path.stat().st_ino == ino  # a bind mount keeps its inode
    assert path.read_text(encoding="utf-8") != original

    client_dns.teardown_resolver(state_dir)
    assert path.stat().st_ino == ino
    assert path.read_text(encoding="utf-8") == original


def test_teardown_forward_uses_privileged_tee_for_an_unwritable_bind_mount(forward_stack, host, monkeypatch):
    path, plan, _ = forward_stack
    state_dir = path.parents[1]
    monkeypatch.setattr(client_dns, "_is_bind_mount", lambda stat, parent_stat: True)
    monkeypatch.setattr(client_dns, "open", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("read-only mount")), raising=False)
    client_dns.setup_resolver(state_dir, dns_port=53, plan=plan)
    host.privileged.clear()

    client_dns.teardown_resolver(state_dir)

    assert any(command[0] == "tee" for command in host.privileged)


def test_teardown_never_configured_is_a_noop_without_systemd_tooling(tmp_path, host, monkeypatch):
    monkeypatch.setattr(client_dns.shutil, "which", lambda name: None)
    client_dns.teardown_resolver(tmp_path)  # linux without iproute2: no error, no execution
    monkeypatch.setattr(client_dns.sys, "platform", "win32")
    client_dns.teardown_resolver(tmp_path)  # unsupported platform, never configured
    assert host.commands == []
    assert not (tmp_path / "resolver.json").exists()


# --- legacy v1 state regression -----------------------------------------------------------------


def test_v1_state_migrates_in_memory_to_the_legacy_backend(tmp_path, host):
    write_saved_state(tmp_path, {"version": 1, "address": client_dns.DNS_ADDRESS, "port": 1054})

    plan = client_dns.resolver_plan(tmp_path, backend="auto")
    assert plan == {"backend": "resolved", "address": client_dns.DNS_ADDRESS, "port": 1054, "forwarders": []}

    status = client_dns.resolver_status(tmp_path)
    assert status["backend"] == "resolved" and status["saved_port"] == 1054


def test_v2_state_rejects_foreign_addresses_and_shapes(tmp_path, host):
    write_saved_state(tmp_path, {"version": 2, "backend": "forward", "address": "10.0.0.9", "port": 53, "forwarders": ["192.0.2.1"], "original": {"kind": "regular", "content": "nameserver 192.0.2.1\n", "target": None, "dev": 1, "ino": 2, "mode": 420, "uid": 0, "gid": 0}})
    with pytest.raises(PortmapError):
        client_dns.resolver_plan(tmp_path, backend="auto")


@pytest.mark.parametrize("environment", [{"boot_id": 7}, {"boot_id": None}, {"ns": ["mnt:[1]"]}, "linux"])
def test_v2_state_rejects_invalid_environment_shapes(tmp_path, host, environment):
    original = {"kind": "regular", "content": "nameserver 192.0.2.1\n", "target": None, "dev": 1, "ino": 2, "mode": 420, "uid": 0, "gid": 0}
    write_saved_state(tmp_path, {**saved_forward_state(tmp_path, ["192.0.2.1"], original), "environment": environment})
    with pytest.raises(PortmapError, match="invalid portmap resolver state"):
        client_dns.resolver_plan(tmp_path, backend="auto")


@pytest.mark.parametrize("active", ["linux", {"kind": "regular"}, {"kind": "regular", "content": "nameserver 127.0.0.1\n", "target": None, "dev": 1, "ino": "x", "mode": 420, "uid": 0, "gid": 0}])
def test_v2_state_rejects_invalid_active_identity_shapes(tmp_path, host, active):
    original = {"kind": "regular", "content": "nameserver 192.0.2.1\n", "target": None, "dev": 1, "ino": 2, "mode": 420, "uid": 0, "gid": 0}
    write_saved_state(tmp_path, {**saved_forward_state(tmp_path, ["192.0.2.1"], original), "active": active})
    with pytest.raises(PortmapError, match="invalid portmap resolver state"):
        client_dns.resolver_plan(tmp_path, backend="auto")
    write_saved_state(tmp_path, {"version": 3, "backend": "resolved", "address": client_dns.DNS_ADDRESS, "port": 1053, "forwarders": []})
    with pytest.raises(PortmapError):
        client_dns.resolver_plan(tmp_path, backend="auto")


# --- macOS integration ---------------------------------------------------------------------------


def test_macos_setup_status_and_teardown_use_the_saved_port(tmp_path, host, monkeypatch):
    monkeypatch.setattr(client_dns.sys, "platform", "darwin")
    path = tmp_path / "etc-resolver-portmap"
    host.live_probes.update({(client_dns.DNS_ADDRESS, 1054), (client_dns.DNS_ADDRESS, 1055)})

    status = client_dns.setup_resolver(tmp_path, dns_port=1054)

    assert status["installed"] is True
    assert path.read_text(encoding="utf-8") == client_dns._mac_content(tmp_path, 1054)
    assert json.loads((tmp_path / "resolver.json").read_text(encoding="utf-8"))["port"] == 1054

    # A port switch replaces only the owned file.
    client_dns.setup_resolver(tmp_path, dns_port=1055)
    assert "port 1055" in path.read_text(encoding="utf-8")

    client_dns.teardown_resolver(tmp_path)
    assert not path.exists()
    assert not (tmp_path / "resolver.json").exists()


def test_macos_setup_refuses_to_overwrite_a_foreign_resolver(tmp_path, host, monkeypatch):
    monkeypatch.setattr(client_dns.sys, "platform", "darwin")
    path = tmp_path / "etc-resolver-portmap"
    path.write_text("# some other tool\nnameserver 10.0.0.1\n", encoding="utf-8")
    host.live_probes.add((client_dns.DNS_ADDRESS, 1054))

    with pytest.raises(PortmapError, match="refusing to overwrite existing resolver"):
        client_dns.setup_resolver(tmp_path, dns_port=1054)
    assert path.read_text(encoding="utf-8").startswith("# some other tool")


def test_macos_teardown_refuses_an_externally_changed_resolver(tmp_path, host, monkeypatch):
    monkeypatch.setattr(client_dns.sys, "platform", "darwin")
    path = tmp_path / "etc-resolver-portmap"
    path.write_text("# another owner\nnameserver 10.0.0.1\nport 53\n", encoding="utf-8")

    with pytest.raises(PortmapError, match="changed externally"):
        client_dns.teardown_resolver(tmp_path)
    assert path.exists()
