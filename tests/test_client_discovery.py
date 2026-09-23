from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from portmap import client_discovery
from portmap.client_discovery import discover_ssh_hosts, discover_targets, probe_gateway, resolve_ssh_host
from portmap.errors import PortmapError


@pytest.fixture
def catalog() -> dict:
    return {
        "dns_domain": "work.portmap",
        "http_port": 8080,
        "dns_server": "203.0.113.99",
        "services": [{"endpoints": [{"host_port": 20100, "container_port": 3000}]}],
        "agent": {"available": False, "message": "agent unavailable"},
        "worktrees": [],
    }


@pytest.fixture
def serve_catalog():
    servers = []

    def serve(payload, *, status=200, headers=None, content_length=True, release=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if content_length:
                    self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if release is not None:
                    release.wait(2)
                try:
                    self.wfile.write(body)
                except ConnectionError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}", requests

    yield serve
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_probe_pins_requested_address_and_ignores_proxy_environment(catalog, serve_catalog, monkeypatch):
    catalog["dns_domain"] = "Work.Portmap."
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    url, requests = serve_catalog(catalog)

    info = probe_gateway(url, expected_domain="work.portmap")

    assert info.address == "127.0.0.1"
    assert info.domain == "work.portmap"
    assert info.http_port == 8080
    assert info.catalog_url == f"{url}/registry.json"
    assert requests == ["/registry.json"]


@pytest.mark.parametrize("target", [
    "-oProxyCommand=touch-marker",
    "http://user:private-token@127.0.0.1",
    "ftp://127.0.0.1",
    "127.0.0.1:65536",
    "127.0.0.1:0",
    "[::1]suffix",
    "http://127.0.0.1/other-path",
    "127.0.0.1\nexample.com",
])
def test_probe_rejects_unsafe_or_ambiguous_targets_before_network(target, monkeypatch):
    def unexpected_resolution(*args, **kwargs):
        pytest.fail("invalid target reached the network resolver")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_resolution)
    with pytest.raises(PortmapError) as error:
        probe_gateway(target)
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize("replacement", [
    [],
    {"dns_domain": "work.portmap", "http_port": 8080},
    {"dns_domain": "work.portmap", "http_port": True, "services": []},
    {"dns_domain": "http://work.portmap", "http_port": 8080, "services": []},
    {"dns_domain": "127.0.0.1", "http_port": 8080, "services": []},
    {"dns_domain": "work.portmap", "http_port": 8080, "services": [{"endpoints": [{"host_port": 65536}]}]},
    {"dns_domain": "work.portmap", "http_port": 8080, "services": [], "agent": {"available": "yes"}},
])
def test_probe_rejects_non_catalog_or_unsafe_metadata(replacement, serve_catalog):
    url, _ = serve_catalog(replacement)
    with pytest.raises(PortmapError):
        probe_gateway(url)


@pytest.mark.parametrize("body", [
    b'{"dns_domain":"work.portmap","http_port":8080,"http_port":80,"services":[]}',
    b'{"dns_domain":"work.portmap","http_port":8080,"services":[],"other":NaN}',
    b'\xffnot-utf8',
])
def test_probe_rejects_ambiguous_or_non_json_payloads(body, serve_catalog):
    url, _ = serve_catalog(body)
    with pytest.raises(PortmapError, match="JSON"):
        probe_gateway(url)


def test_probe_rejects_expected_domain_mismatch(catalog, serve_catalog):
    url, _ = serve_catalog(catalog)
    with pytest.raises(PortmapError, match="does not match"):
        probe_gateway(url, expected_domain="different.portmap")


def test_probe_does_not_follow_redirects(catalog, serve_catalog):
    destination, destination_requests = serve_catalog(catalog)
    origin, origin_requests = serve_catalog(b"", status=302, headers={"Location": f"{destination}/registry.json"})

    with pytest.raises(PortmapError, match="redirect"):
        probe_gateway(origin)

    assert origin_requests == ["/registry.json"]
    assert destination_requests == []


def test_probe_reports_http_failure_without_echoing_response_secrets(serve_catalog):
    url, _ = serve_catalog(b"authentication token: private-token", status=403)
    with pytest.raises(PortmapError, match="HTTP 403") as error:
        probe_gateway(url)
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize("content_length", [True, False])
def test_probe_bounds_catalog_size_with_or_without_length(serve_catalog, monkeypatch, content_length):
    monkeypatch.setattr(client_discovery, "MAX_CATALOG_BYTES", 128)
    url, _ = serve_catalog(b"x" * 129, content_length=content_length)
    with pytest.raises(PortmapError, match="limit"):
        probe_gateway(url)


def test_probe_reports_refused_connection():
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = unavailable.getsockname()[1]
        with pytest.raises(PortmapError, match="ConnectionRefusedError"):
            probe_gateway(f"127.0.0.1:{port}", timeout=1)


def test_probe_deadline_covers_a_stalled_response(catalog, serve_catalog):
    release = threading.Event()
    url, _ = serve_catalog(catalog, release=release)
    started = time.monotonic()
    try:
        with pytest.raises(PortmapError):
            probe_gateway(url, timeout=0.05)
        assert time.monotonic() - started < 1
    finally:
        release.set()


def test_probe_deadline_covers_a_stalled_resolver(monkeypatch):
    release = threading.Event()

    def stalled_resolution(*args, **kwargs):
        release.wait(2)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", stalled_resolution)
    started = time.monotonic()
    try:
        with pytest.raises(PortmapError, match="timed out"):
            probe_gateway("stalled.example", timeout=0.05)
        assert time.monotonic() - started < 1
    finally:
        release.set()


def test_eof_terminated_catalog_does_not_touch_closed_socket(catalog, serve_catalog):
    url, _ = serve_catalog(catalog, headers={"Connection": "close"}, content_length=False)
    assert probe_gateway(url).domain == "work.portmap"


def test_default_ports_leave_time_for_fallback_after_blackhole(catalog, monkeypatch):
    attempted = []

    def request(host, port, scheme, address, deadline, *, connect_timeout=None):
        attempted.append(port)
        if port == 8080:
            time.sleep(connect_timeout if connect_timeout is not None else deadline.remaining())
            raise TimeoutError("simulated filtered port")
        return json.dumps(catalog).encode()

    monkeypatch.setattr(client_discovery, "_request_catalog", request)
    info = probe_gateway("127.0.0.1", timeout=0.2)
    assert info.domain == "work.portmap"
    assert info.catalog_url == "http://127.0.0.1:80/registry.json"
    assert attempted == [8080, 80]


def test_inventory_expands_include_files_not_host_patterns_and_never_executes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    parts = ssh_dir / "conf.d"
    parts.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    marker = tmp_path / "must-not-exist"
    config = tmp_path / "custom-config"
    config.write_text(
        'Host local "literal" !excluded *.wild ?.wild bracket[12]\n'
        'Include = "conf.d/*.conf"\n'
        f'Match exec "touch {marker}"\n'
        'Host manual.coder\n',
        encoding="utf-8",
    )
    (parts / "02-second.conf").write_text('Host=second !ignored second\n', encoding="utf-8")
    (parts / "01-first.conf").write_text('Host first\nInclude "shared config"\n', encoding="utf-8")
    (ssh_dir / "shared config").write_text('Host shared\nInclude "conf.d/01-first.conf"\n', encoding="utf-8")
    # A wrong including-file-relative implementation would discover this decoy.
    (parts / "shared config").write_text('Host wrong-relative-base\n', encoding="utf-8")

    assert discover_ssh_hosts(config) == ["local", "literal", "first", "shared", "second", "manual.coder"]
    assert not marker.exists()


def test_inventory_limits_total_input_bytes(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text("#" + "x" * 128, encoding="utf-8")
    monkeypatch.setattr(client_discovery, "MAX_SSH_CONFIG_BYTES", 128)
    with pytest.raises(PortmapError, match="input-size"):
        discover_ssh_hosts(config)


def test_inventory_limits_include_glob_files(tmp_path, monkeypatch):
    config = tmp_path / "config"
    parts = tmp_path / "parts"
    parts.mkdir()
    config.write_text(f'Include "{parts}/*.conf"\n', encoding="utf-8")
    for index in range(3):
        (parts / f"{index}.conf").write_text(f"Host host-{index}\n", encoding="utf-8")
    monkeypatch.setattr(client_discovery, "MAX_SSH_CONFIG_FILES", 2)
    with pytest.raises(PortmapError, match="limit"):
        discover_ssh_hosts(config)


def test_inventory_reports_invalid_quoting_without_echoing_contents(tmp_path):
    config = tmp_path / "config"
    config.write_text('Host "private-token\n', encoding="utf-8")
    with pytest.raises(PortmapError, match="quoting") as error:
        discover_ssh_hosts(config)
    assert "private-token" not in str(error.value)


@pytest.mark.skipif(shutil.which("ssh") is None, reason="OpenSSH is not installed")
def test_resolve_uses_effective_openssh_config_without_proxy_auth_or_secret_output(tmp_path):
    config = tmp_path / "config"
    marker = tmp_path / "proxy-was-run"
    config.write_text(
        'Host manual.coder\n'
        '  HostName remote.example\n'
        '  User developer\n'
        '  Port 2222\n'
        f"  ProxyCommand sh -c 'touch {marker}; echo private-token'\n"
        '  IdentityFile /private/key-path\n',
        encoding="utf-8",
    )

    result = resolve_ssh_host("manual.coder", config_file=config)

    assert (result["hostname"], result["user"], result["port"]) == ("remote.example", "developer", 2222)
    assert result["proxy_command"] is True
    assert result["auth_verified"] is False
    assert "private-token" not in json.dumps(result)
    assert "/private/key-path" not in json.dumps(result)
    assert not marker.exists()


@pytest.mark.skipif(shutil.which("ssh") is None, reason="OpenSSH is not installed")
def test_resolve_preserves_proxyjump_metadata(tmp_path):
    config = tmp_path / "config"
    config.write_text(
        'Host destination\n'
        '  HostName remote.example\n'
        '  User developer\n'
        '  ProxyJump relay@bastion.example:2222\n',
        encoding="utf-8",
    )
    result = resolve_ssh_host("destination", config_file=config)
    assert result["proxy_jump"] == "relay@bastion.example:2222"
    assert result["auth_verified"] is False


def test_resolve_refuses_match_exec_before_openssh_can_execute_it(tmp_path):
    marker = tmp_path / "match-was-run"
    config = tmp_path / "config"
    config.write_text(f'Match exec "touch {marker}"\nHost target\n', encoding="utf-8")

    assert discover_ssh_hosts(config) == ["target"]
    with pytest.raises(PortmapError, match="Match exec"):
        resolve_ssh_host("target", config_file=config)
    assert not marker.exists()


@pytest.mark.parametrize("target", ["-oProxyCommand=bad", "alias\nHost evil", "$(touch-marker)", "user:private-token@alias"])
def test_resolve_rejects_options_controls_and_shell_tokens(target, tmp_path):
    with pytest.raises(PortmapError) as error:
        resolve_ssh_host(target, config_file=tmp_path / "does-not-exist")
    assert "private-token" not in str(error.value)


def test_no_target_inventories_aliases_without_network_or_ssh(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text('Host workstation manual.coder\n', encoding="utf-8")

    def unexpected_attempt(*args, **kwargs):
        pytest.fail("inventory attempted DNS, SSH, or authentication")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_attempt)
    monkeypatch.setattr(subprocess, "Popen", unexpected_attempt)

    result = discover_targets(config_file=config)

    assert result["ssh_hosts"] == ["workstation", "manual.coder"]
    assert result["direct"] is None
    assert result["ssh_candidates"] == []


def test_manual_coder_target_remains_a_suggestion_after_direct_dns_failure(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text('Host unrelated other-host\n', encoding="utf-8")
    attempted_hosts = []

    def unresolved(host, *args, **kwargs):
        attempted_hosts.append(host)
        raise socket.gaierror(socket.EAI_NONAME, "not found")

    def unexpected_ssh(*args, **kwargs):
        pytest.fail("discovery must not authenticate candidate aliases")

    monkeypatch.setattr(socket, "getaddrinfo", unresolved)
    monkeypatch.setattr(subprocess, "Popen", unexpected_ssh)

    result = discover_targets("manual.coder", config_file=config)

    assert attempted_hosts == ["manual.coder"]
    assert result["direct"] is None
    assert result["direct_error"]
    assert result["ssh_candidates"] == []
    assert result["manual_ssh_target"] == "manual.coder"


def test_discovery_rejects_credentials_instead_of_echoing_them(tmp_path):
    config = tmp_path / "config"
    config.write_text("Host safe-alias\n", encoding="utf-8")
    with pytest.raises(PortmapError) as error:
        discover_targets("http://user:private-token@host.example", config_file=config)
    assert "private-token" not in str(error.value)
