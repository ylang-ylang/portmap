import json
import socket as socket_module
import struct
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
        {"laptop.portmap": "172.20.0.5", "db.portmap": "172.20.0.9"},
        "owner:version",
    )
    blocks = split_blocks(corefile)
    assert set(blocks) == {client_dns.DNS_PORT, 53}
    host = blocks[client_dns.DNS_PORT]
    assert 'answer "{{ .Name }} 5 IN A 127.0.0.1"' in host
    assert "172.20.0.5" not in host and "db.portmap" not in host
    assert "rcode REFUSED" in host  # the host resolver keeps using its own DNS
    assert "template ANY ANY portmap {" in host and "rcode NXDOMAIN" in host
    assert "forward" not in host


def test_container_view_serves_container_scope_and_forwards_upstream():
    corefile = client_dns.render_corefile(
        {"laptop.portmap": "127.0.0.1"},
        {"laptop.portmap": "172.20.0.5", "db.portmap": "172.20.0.9"},
        "owner:version",
    )
    container = split_blocks(corefile)[53]
    assert "template IN A db.portmap" in container
    assert 'answer "{{ .Name }} 5 IN A 172.20.0.5"' in container
    assert "127.0.0.1" not in container
    assert "forward . /etc/resolv.conf" in container
    assert "rcode REFUSED" not in container  # containers resolve normal names too
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
        {"a.portmap": "127.0.0.1", "b.a.portmap": "127.0.0.2"},
        {"a.portmap": "172.20.0.1", "b.a.portmap": "172.20.0.2"},
        "owner:version",
    )
    for block in split_blocks(corefile).values():
        assert block.index("template IN A b.a.portmap") < block.index("template IN A a.portmap")


def test_ipv6_records_answer_aaaa_and_empty_the_other_family():
    corefile = client_dns.render_corefile({}, {"direct.portmap": "2001:db8::10"}, "owner:version")
    container = split_blocks(corefile)[53]
    assert "template IN AAAA direct.portmap" in container
    assert 'answer "{{ .Name }} 5 IN AAAA 2001:db8::10"' in container
    other = container[container.index("template IN A direct.portmap {"):]
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


# --- probe_marker wire format ---------------------------------------------------------


class TxtServer:
    """Answers portmap marker TXT queries over real UDP, like the container DNS."""

    def __init__(self, marker: str):
        self.marker = marker
        self.socket = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.05)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.socket.getsockname()[1]

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                request, peer = self.socket.recvfrom(4096)
            except (socket_module.timeout, OSError):
                continue
            self.socket.sendto(self._answer(request), peer)

    def _answer(self, request: bytes) -> bytes:
        question = request[12:]
        ident = request[:2]
        text = self.marker.encode("ascii")
        rdata = bytes([len(text)]) + text
        answer = question[:-4] + struct.pack("!HHIH", 16, 1, 0, len(rdata)) + rdata
        return ident + struct.pack("!5H", 0x8180, 1, 1, 0, 0) + question + answer

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=1)
        self.socket.close()


def test_probe_marker_reads_the_ownership_record_over_the_wire():
    server = TxtServer("owner:abc123")
    try:
        assert client_dns.probe_marker("127.0.0.1", server.port, timeout=1.0) == "owner:abc123"
    finally:
        server.close()


def test_probe_marker_returns_none_without_a_portmap_listener():
    with socket_module.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    assert client_dns.probe_marker("127.0.0.1", port, timeout=0.05) is None


def test_probe_marker_rejects_answers_that_do_not_match_the_query():
    class WrongIdent(TxtServer):
        # Answers with an identifier that does not match the request.
        def _answer(self, request: bytes) -> bytes:
            question = request[12:]
            return b"\x00\x00" + struct.pack("!5H", 0x8180, 1, 1, 0, 0) + question

    wrong = WrongIdent("owner:abc123")
    try:
        assert client_dns.probe_marker("127.0.0.1", wrong.port, timeout=1.0) is None
    finally:
        wrong.close()


def test_probe_marker_validates_the_port():
    with pytest.raises(PortmapError):
        client_dns.probe_marker("127.0.0.1", True)
    with pytest.raises(PortmapError):
        client_dns.probe_marker("127.0.0.1", 70000)


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

    # -- probe ---------------------------------------------------------------

    def probe(self, address, port, *, timeout=0.3):
        self.probes.append((address, port))
        return "owner:probe" if (address, port) in self.live_probes else None

    # -- subprocess.run replacement -------------------------------------------

    def run(self, command, **kwargs):
        decoded = list(command[2:]) if command[:2] == ["sudo", "--"] else list(command)
        if command[:2] == ["sudo", "--"]:
            self.privileged.append(decoded)
        self.commands.append(decoded)
        joined = " ".join(decoded)
        if self.fail_marker and self.fail_marker in joined:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"simulated failure: {joined}")
        if decoded[0] == "ip":
            return self.ip(decoded)
        if decoded[0] == "resolvectl":
            return self.resolvectl(decoded)
        if decoded[0] in ("mkdir", "install", "rm"):
            return self.filesystem(decoded)
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
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def verbs(self, tool):
        return [command for command in self.commands if command[0] == tool]


@pytest.fixture
def host(tmp_path, monkeypatch):
    fake = FakeHost()
    monkeypatch.setattr(client_dns.subprocess, "run", fake.run)
    monkeypatch.setattr(client_dns.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(client_dns, "probe_marker", fake.probe)
    monkeypatch.setattr(client_dns, "_MAC_RESOLVER_PATH", tmp_path / "etc-resolver-portmap")
    return fake


# --- prepare_resolver --------------------------------------------------------------------


def test_prepare_creates_only_the_owned_link_without_resolver_routing(tmp_path, host):
    result = client_dns.prepare_resolver(tmp_path)

    name = client_dns._link_name(tmp_path)
    assert result["created"] is True
    assert result["interface"] == name
    assert result["address"] == client_dns.DNS_ADDRESS
    link = host.links[name]
    assert link["ifalias"] == client_dns._link_owner(tmp_path)
    assert client_dns.DNS_ADDRESS in link["addresses"]
    assert link["up"] is True
    assert host.verbs("resolvectl") == []  # no DNS routing change while proving the container
    assert host.verbs("ip")


def test_prepare_is_idempotent_and_still_brings_the_link_up(tmp_path, host):
    assert client_dns.prepare_resolver(tmp_path)["created"] is True
    host.links[client_dns._link_name(tmp_path)]["up"] = False

    result = client_dns.prepare_resolver(tmp_path)

    assert result["created"] is False
    assert host.links[client_dns._link_name(tmp_path)]["up"] is True
    assert host.verbs("resolvectl") == []


def test_prepare_refuses_an_unrelated_interface_with_the_same_name(tmp_path, host):
    name = client_dns._link_name(tmp_path)
    host.links[name] = {"ifalias": "someone else", "ifindex": 7, "up": True, "addresses": []}

    with pytest.raises(PortmapError, match="refusing to change unrelated interface"):
        client_dns.prepare_resolver(tmp_path)
    assert host.links[name]["ifalias"] == "someone else"


def test_prepare_refuses_an_address_already_assigned_elsewhere(tmp_path, host):
    host.global_addresses.append(client_dns.DNS_ADDRESS)
    with pytest.raises(PortmapError, match="already assigned"):
        client_dns.prepare_resolver(tmp_path)
    assert host.links == {}


def test_prepare_failure_deletes_the_freshly_created_link(tmp_path, host):
    host.fail_marker = "ip address add"
    with pytest.raises(PortmapError, match="simulated failure"):
        client_dns.prepare_resolver(tmp_path)
    assert host.links == {}


def test_prepare_without_state_is_a_noop_on_macos(tmp_path, host, monkeypatch):
    monkeypatch.setattr(client_dns.sys, "platform", "darwin")
    result = client_dns.prepare_resolver(tmp_path)
    assert result == {"platform": "darwin", "address": client_dns.DNS_ADDRESS, "created": False}
    assert host.commands == []


# --- setup_resolver -------------------------------------------------------------------------


def test_setup_refuses_to_route_at_a_dead_listener_before_any_mutation(tmp_path, host):
    with pytest.raises(PortmapError, match="no portmap DNS listener is answering"):
        client_dns.setup_resolver(tmp_path, dns_port=1054)
    assert host.commands == []
    assert host.probes == [(client_dns.DNS_ADDRESS, 1054)]


def test_setup_installs_the_owned_suffix_route_at_the_actual_port(tmp_path, host):
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
    assert saved == {"version": 1, "address": client_dns.DNS_ADDRESS, "port": 1054}


def test_setup_port_switch_restores_the_previous_owned_port_on_failure(tmp_path, host):
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


def test_setup_failure_without_previous_route_reverts_the_link(tmp_path, host):
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


# --- resolver_status --------------------------------------------------------------------------


def test_status_defaults_to_saved_port_then_the_historical_one(tmp_path, host):
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

    explicit = client_dns.resolver_status(tmp_path, dns_port=53)
    assert explicit["port"] == 53
    assert explicit["installed"] is False


def test_status_defaults_to_the_historical_port_for_a_native_route(tmp_path, host):
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


# --- teardown_resolver --------------------------------------------------------------------------


def test_teardown_removes_only_owned_integration_and_port_metadata(tmp_path, host):
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
