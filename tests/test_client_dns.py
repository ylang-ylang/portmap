import re

import pytest

from portmap import client_dns, client_runtime
from portmap.errors import PortmapError


def test_direct_records_publish_verified_remote_addresses():
    corefile = client_dns.render_corefile(
        {"direct.portmap": "192.0.2.10", "tunnel.portmap": "127.0.0.1", "local.portmap": "127.0.0.1"},
        "owner:version",
    )
    assert "template IN A direct.portmap" in corefile
    assert 'answer "{{ .Name }} 5 IN A 192.0.2.10"' in corefile
    assert corefile.count('IN A 127.0.0.1') == 2
    # SSH and local roles answer on loopback; only direct uses the remote IP.
    assert "192.0.2.10" in corefile


def test_more_specific_suffix_precedes_parent_zone():
    corefile = client_dns.render_corefile({"a.portmap": "127.0.0.1", "b.a.portmap": "127.0.0.1"}, "owner:version")
    assert corefile.index("template IN A b.a.portmap") < corefile.index("template IN A a.portmap")


def test_ipv6_records_answer_aaaa_and_nxdomain_the_other_family():
    corefile = client_dns.render_corefile({"direct.portmap": "2001:db8::10"}, "owner:version")
    assert "template IN AAAA direct.portmap" in corefile
    assert 'answer "{{ .Name }} 5 IN AAAA 2001:db8::10"' in corefile
    assert re.search(r"template IN A direct\.portmap \{\n\s*rcode NOERROR", corefile)


def test_unmanaged_names_are_refused_and_portmap_is_nxdomain():
    corefile = client_dns.render_corefile({"remote.portmap": "192.0.2.10"}, "owner:version")
    assert "template ANY ANY portmap {" in corefile
    assert "rcode NXDOMAIN" in corefile
    assert "template ANY ANY {" in corefile
    assert "rcode REFUSED" in corefile


@pytest.mark.parametrize("records", [{"bad.portmap": "not-an-address"}, {"192.0.2.10": "127.0.0.1"}, {"host.portmap.example": "127.0.0.1"}])
def test_invalid_records_are_rejected(records):
    with pytest.raises(PortmapError):
        client_dns.render_corefile(records, "owner:version")


def test_marker_rejects_whitespace():
    with pytest.raises(PortmapError):
        client_dns.render_corefile({}, "owner token")


def test_sync_dns_refuses_session_escape_without_sudo_before_spawning(tmp_path, monkeypatch):
    monkeypatch.setattr(client_dns, "_marker", lambda timeout=0.3: None)
    monkeypatch.setattr(client_dns, "find_native_binary", lambda name: "/nonexistent-coredns")

    monkeypatch.setattr(client_runtime.sys, "platform", "linux")
    monkeypatch.setattr(client_runtime, "_persistent_scope", None)
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: ["/user.slice/user-1000.slice/session-1.scope"])
    monkeypatch.setattr(client_runtime.os, "geteuid", lambda: 1000)

    def must_not_spawn(*args, **kwargs):
        raise AssertionError("no CoreDNS worker may be spawned")

    monkeypatch.setattr(client_dns.subprocess, "Popen", must_not_spawn)
    with pytest.raises(PortmapError):
        client_dns.sync_dns(tmp_path, {"host.portmap": "127.0.0.1"}, use_sudo=False)
    assert not (tmp_path / "Corefile").exists()
    assert not (tmp_path / "dns.json").exists()
