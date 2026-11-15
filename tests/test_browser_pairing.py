from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import browser_pairing as pairing
from portmap import infrastructure, server
from portmap.browser_proxy import BrowserProxy, Endpoint, ProxyConfig
from portmap.cli import main
from portmap.errors import PortmapError
from portmap.settings import PortmapSettings


@pytest.fixture
def settings(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return PortmapSettings(
        root=tmp_path, state_dir=state, agent_runtime_dir=tmp_path / "agent",
        http_bind="0.0.0.0", http_port=80, dns_bind="0.0.0.0", dns_port=53,
        dns_domain="node.portmap", dns_forward="1.1.1.1", gateway_network="portmap_gateway",
        tcp_port_start=18000, udp_port_start=19000, range_port_start=49160,
        host_ip="192.0.2.10", runtime="docker", runtime_socket="/unused/docker.sock",
    )


@pytest.fixture
def publisher(settings, monkeypatch):
    directory = settings.state_dir / "server"
    directory.mkdir(mode=0o700)
    record = {
        "version": 2, "uid": os.getuid(), "state_dir": str(settings.state_dir.resolve()),
        "client_state_dir": str(settings.client_state_dir), "http_port": 80,
        "settings": server._settings_record(settings), "lan": None,
    }
    path = directory / "instance.json"
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    instance = settings.state_dir / "instance"
    config = instance / "config"
    config.mkdir(parents=True)
    snapshot = config / "catalog-access.json"
    snapshot.write_text(json.dumps({"owner": "a" * 32, "local": {"enabled": True, "domain": "node.portmap"}}))
    state = {
        "owner": "a" * 32, "domain": "node.portmap", "phase": "ready", "instance_dir": str(instance),
        "public_backend_port": 18081, "http_port": 80, "listen_port": 18080, "management_port": 18083,
    }
    status = {
        "running": True, "owner": state["owner"], "domain": state["domain"],
        "gateway": {"running": True, "public_backend_port": 18081, "http_port": 80, "listen_port": 18080},
    }
    monkeypatch.setattr(infrastructure, "load_infrastructure", lambda _: state)
    monkeypatch.setattr(server, "infrastructure_status", lambda *_args, **_kwargs: status)
    monkeypatch.setattr(server, "agent_status", lambda _: SimpleNamespace(as_dict=lambda: {"running": True}))
    return SimpleNamespace(state=state, status=status, snapshot=snapshot, record=path)


def test_discovery_requires_local_publisher_not_just_access_infrastructure(settings):
    with pytest.raises(PortmapError, match="already running local publisher"):
        pairing.discover_publisher(settings)
    assert not (settings.state_dir / "browser-proxy").exists()


def test_discovery_selects_owned_public_backend_without_requiring_lan_publication(settings, publisher):
    found = pairing.discover_publisher(settings)
    assert found == pairing.Publisher("node.portmap", "a" * 32, Endpoint("127.0.0.1", 18081))
    assert found.ingress.port not in (80, 18080, 18083)


@pytest.mark.parametrize("failure", ["stopped", "missing_public", "private_port", "management_port", "wrong_owner", "wrong_domain", "local_disabled", "local_other_domain", "local_wrong_owner"])
def test_discovery_never_falls_back_to_private_control_or_foreign_ingress(settings, publisher, failure):
    if failure == "stopped":
        publisher.status["running"] = False
    elif failure == "missing_public":
        publisher.status["gateway"].pop("public_backend_port")
    elif failure in ("private_port", "management_port"):
        port = publisher.state["listen_port" if failure == "private_port" else "management_port"]
        publisher.status["gateway"]["public_backend_port"] = port
        publisher.state["public_backend_port"] = port
    elif failure == "wrong_owner":
        publisher.status["owner"] = "b" * 32
    elif failure == "wrong_domain":
        publisher.status["domain"] = "remote.portmap"
    else:
        snapshot = json.loads(publisher.snapshot.read_text())
        if failure == "local_disabled":
            snapshot["local"]["enabled"] = False
        elif failure == "local_other_domain":
            snapshot["local"]["domain"] = "remote.portmap"
        else:
            snapshot["owner"] = "b" * 32
        publisher.snapshot.write_text(json.dumps(snapshot))
    with pytest.raises(PortmapError):
        pairing.discover_publisher(settings)
    assert not (settings.state_dir / "browser-proxy").exists()


def tailscale_result(value, returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=json.dumps(value).encode(), stderr=b"sensitive diagnostic must not be printed")


def test_tailscale_uses_only_local_self_and_prefers_ipv4(monkeypatch, capsys):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return tailscale_result({"BackendState": "Running", "Self": {"TailscaleIPs": ["fd7a:115c:a1e0::7", "100.101.102.103"]}, "Peer": {"ignored": {"TailscaleIPs": ["100.64.0.1"]}}})

    monkeypatch.setattr(pairing.subprocess, "run", run)
    assert pairing.tailscale_address() == "100.101.102.103"
    assert calls[0][0] == ["tailscale", "status", "--json", "--peers=false"]
    assert calls[0][1]["timeout"] == 5
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("status", [
    {"BackendState": "Stopped", "Self": {"TailscaleIPs": ["100.101.102.103"]}},
    {"BackendState": "NeedsLogin"},
    {"BackendState": "Running", "Self": {"TailscaleIPs": []}},
    {"BackendState": "Running", "Self": {"TailscaleIPs": ["127.0.0.1", "0.0.0.0", "192.0.2.1"]}},
    {"BackendState": "Running", "Self": {"Online": False, "TailscaleIPs": ["100.101.102.103"]}},
    {"BackendState": "Running", "Peer": {"other": {"TailscaleIPs": ["100.101.102.103"]}}},
])
def test_tailscale_down_or_missing_self_never_uses_peer_or_public_address(monkeypatch, capsys, status):
    monkeypatch.setattr(pairing.subprocess, "run", lambda *_args, **_kwargs: tailscale_result(status))
    with pytest.raises(PortmapError, match="Tailscale is down") as error:
        pairing.tailscale_address()
    assert "sensitive" not in str(error.value)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("tailscale", 5), OSError("private detail")])
def test_tailscale_absent_or_unreachable_is_actionable(monkeypatch, error):
    def unavailable(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(pairing.subprocess, "run", unavailable)
    with pytest.raises(PortmapError, match="Tailscale") as failure:
        pairing.tailscale_address()
    assert "private detail" not in str(failure.value)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "127.0.0.1", "[::1]", "localhost", "a.localhost", "node.portmap", "portmap", "https://node", "node:1234", "[fe80::1%eth0]", "224.0.0.1", " foo "])
def test_automatic_advertisement_rejects_unreachable_or_routing_dependent_hosts(host):
    with pytest.raises(PortmapError, match="host"):
        pairing.advertised_host(host)


def prepare_automatic(settings, monkeypatch):
    publisher = pairing.Publisher("node.portmap", "a" * 32, Endpoint("127.0.0.1", 18081))
    monkeypatch.setattr(pairing, "discover_publisher", lambda _: publisher)
    monkeypatch.setattr(pairing, "tailscale_address", lambda: "100.101.102.103")
    return publisher


def test_settings_persist_real_bound_port_and_dedicated_credentials_across_restart(settings, monkeypatch):
    prepare_automatic(settings, monkeypatch)
    async def bind_port(config):
        listener = await BrowserProxy(replace(config, listen=Endpoint("127.0.0.1", 0))).start()
        async with listener:
            return listener.sockets[0].getsockname()[1]

    with pairing.automatic_pairing(settings) as first:
        assert first.config.listen == Endpoint("100.101.102.103", 0)
        assert first.config.username.startswith(b"browser-") and len(first.config.password) >= 40
        assert not (settings.state_dir / "browser-proxy" / "pairing.json").exists()
        bound = replace(first.config, listen=Endpoint(first.config.listen.host, asyncio.run(bind_port(first.config))))
        first.persist(bound)
        original = bound
    path = settings.state_dir / "browser-proxy" / "pairing.json"
    saved = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pairing.automatic_pairing(settings) as second:
        assert second.config == original
        second.persist(second.config)
    assert path.read_bytes() == saved
    record = json.loads(saved)
    assert record["config"]["listen"]["port"] == original.listen.port >= 1024
    assert record["config"]["credentials"]["password"].encode() == original.password


def test_lifetime_lock_prevents_concurrent_proxy_and_secret_rotation(settings, monkeypatch):
    prepare_automatic(settings, monkeypatch)
    with pairing.automatic_pairing(settings):
        with pytest.raises(PortmapError, match="already running"):
            with pairing.automatic_pairing(settings):
                pytest.fail("concurrent setup was admitted")


@pytest.mark.parametrize("change", ["owner", "domain", "host", "ingress"])
def test_restart_refuses_stale_identity_without_rotating_or_overwriting(settings, monkeypatch, change):
    publisher = prepare_automatic(settings, monkeypatch)
    with pairing.automatic_pairing(settings) as initial:
        initial.persist(replace(initial.config, listen=Endpoint(initial.config.listen.host, 22080)))
    path = settings.state_dir / "browser-proxy" / "pairing.json"
    before = path.read_bytes()
    if change == "host":
        monkeypatch.setattr(pairing, "tailscale_address", lambda: "100.101.102.104")
    else:
        publisher = replace(publisher, **{
            "owner": {"owner": "b" * 32}, "domain": {"domain": "other.portmap"},
            "ingress": {"ingress": Endpoint("127.0.0.1", 18082)},
        }[change])
        monkeypatch.setattr(pairing, "discover_publisher", lambda _: publisher)
    with pytest.raises(PortmapError, match="no longer match"):
        with pairing.automatic_pairing(settings):
            pytest.fail("stale settings accepted")
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "world_readable", "malformed", "null"])
def test_unsafe_private_settings_are_not_used_or_replaced(settings, monkeypatch, tmp_path, kind):
    prepare_automatic(settings, monkeypatch)
    directory = settings.state_dir / "browser-proxy"
    directory.mkdir(mode=0o700)
    path = directory / "pairing.json"
    target = tmp_path / "unchanged"
    target.write_text("secret fixture must stay unchanged")
    target.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("null" if kind == "null" else '{"password":"do-not-leak", broken')
        path.chmod(0o644 if kind == "world_readable" else 0o600)
    with pytest.raises(PortmapError) as failure:
        with pairing.automatic_pairing(settings):
            pytest.fail("unsafe config accepted")
    assert "do-not-leak" not in str(failure.value)
    assert target.read_text() == "secret fixture must stay unchanged"
    if kind == "world_readable":
        assert path.stat().st_mode & 0o777 == 0o644


def test_symlinked_state_ancestor_is_rejected_without_creating_files(settings, monkeypatch, tmp_path):
    prepare_automatic(settings, monkeypatch)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "redirected-state"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PortmapError, match="symlinks"):
        with pairing.automatic_pairing(replace(settings, state_dir=link)):
            pytest.fail("symlinked state accepted")
    assert list(target.iterdir()) == []


class CodeOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.ready = asyncio.Event()

    def write(self, value):
        result = super().write(value)
        if value.startswith("portmap-connect-v1:"):
            self.ready.set()
        return result


def decode_code(code):
    prefix, payload = code.strip().split(":", 1)
    assert prefix == "portmap-connect-v1"
    assert "=" not in payload and len(code.strip()) <= 8192
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8"))


@contextlib.asynccontextmanager
async def live_ingress(*, domain="node.portmap", entered=None, release=None):
    async def handle(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            assert b"Host: node.portmap:" in request
            if entered is not None:
                entered.set()
            if release is not None:
                await release.wait()
            body = json.dumps({"dns_domain": domain, "http_port": 80, "services": []}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with listener:
        yield Endpoint("127.0.0.1", listener.sockets[0].getsockname()[1])


def config(ingress, port=0):
    return ProxyConfig("node.portmap", Endpoint("127.0.0.1", port), ingress, "browser:β".encode(), "密-secret".encode())


async def stop(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_live_readiness_precedes_actual_code_and_real_socks_authentication(monkeypatch):
    async def scenario():
        output = CodeOutput()
        monkeypatch.setattr(sys, "stdout", output)
        entered, release = asyncio.Event(), asyncio.Event()
        async with live_ingress(entered=entered, release=release) as ingress:
            persisted = []
            setup = pairing.Pairing("Node β", "node-a", config(ingress), persisted.append)
            task = asyncio.create_task(pairing.serve_pairing(setup))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert output.getvalue() == "" and persisted == []
                release.set()
                await asyncio.wait_for(output.ready.wait(), 3)
                payload = decode_code(output.getvalue())
                assert set(payload) == {"name", "domain", "proxyHost", "proxyPort", "username", "password"}
                assert payload == {"name": "Node β", "domain": "node.portmap", "proxyHost": "node-a",
                                   "proxyPort": persisted[0].listen.port, "username": "browser:β", "password": "密-secret"}
                assert payload["proxyPort"] >= 1024
                reader, writer = await asyncio.open_connection("127.0.0.1", payload["proxyPort"])
                writer.write(b"\x05\x01\x02")
                await writer.drain()
                assert await reader.readexactly(2) == b"\x05\x02"
                username, password = payload["username"].encode(), payload["password"].encode()
                writer.write(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
                await writer.drain()
                assert await reader.readexactly(2) == b"\x01\x00"
                writer.close()
                await writer.wait_closed()
            finally:
                await stop(task)
            replacement = await BrowserProxy(replace(setup.config, listen=persisted[0].listen)).start()
            replacement.close()
            await replacement.wait_closed()
    asyncio.run(scenario())


def test_live_ingress_failure_issues_no_code_persists_nothing_and_releases_listener(monkeypatch):
    async def scenario():
        output = CodeOutput()
        monkeypatch.setattr(sys, "stdout", output)
        async with live_ingress(domain="other.portmap") as ingress:
            with socket.socket() as chosen:
                chosen.bind(("127.0.0.1", 0))
                port = chosen.getsockname()[1]
            fixed = config(ingress, port)
            persisted = []
            with pytest.raises(PortmapError, match="no connection code"):
                await pairing.serve_pairing(pairing.Pairing("Node", "node-a", fixed, persisted.append))
            assert persisted == [] and output.getvalue() == ""
            replacement = await BrowserProxy(fixed).start()
            replacement.close()
            await replacement.wait_closed()
    asyncio.run(scenario())


def test_occupied_saved_port_is_not_reselected_or_hijacked(monkeypatch):
    async def scenario():
        output = CodeOutput()
        monkeypatch.setattr(sys, "stdout", output)
        occupied = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
        async with occupied:
            port = occupied.sockets[0].getsockname()[1]
            persisted = []
            with pytest.raises(PortmapError, match="another listener is never replaced"):
                await pairing.serve_pairing(pairing.Pairing("Node", "node-a", config(Endpoint("127.0.0.1", 1), port), persisted.append))
            assert persisted == [] and output.getvalue() == ""
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_codec_brackets_ipv6_and_preserves_exact_utf8_credentials():
    value = config(Endpoint("127.0.0.1", 8080), 22080)
    payload = decode_code(pairing.connection_code(pairing.Pairing("Node", "fd7a:115c:a1e0::7", value), value))
    assert payload["proxyHost"] == "[fd7a:115c:a1e0::7]"
    assert payload["username"] == "browser:β" and payload["password"] == "密-secret"


def test_cli_rejects_incomplete_or_unsafe_flag_combinations_without_code(capsys):
    assert main(["browser-proxy"]) == 2
    assert main(["browser-proxy", "--advertise-host", "node-a"]) == 2
    assert main(["browser-proxy", "--pair", "--advertise-host", "node-a"]) == 2
    assert capsys.readouterr().out == ""


def test_explicit_fixture_cli_produces_real_import_code_and_stops_only_proxy(tmp_path):
    class AppIngress(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/" and self.headers["Host"] == "node.portmap"
            body = b"<!doctype html><title>Real advanced fixture ingress</title>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    ingress = ThreadingHTTPServer(("127.0.0.1", 0), AppIngress)
    thread = threading.Thread(target=ingress.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as chosen:
        chosen.bind(("127.0.0.1", 0))
        port = chosen.getsockname()[1]
    raw = {"domain": "node.portmap", "listen": {"host": "0.0.0.0", "port": port},
           "credentials": {"username": "fixture-user", "password": "fixture-secret"},
           "ingress": {"host": "127.0.0.1", "port": ingress.server_port}}
    path = tmp_path / "private.json"
    path.write_text(json.dumps(raw))
    path.chmod(0o600)
    before = path.read_bytes()
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    process = subprocess.Popen([sys.executable, "-m", "portmap.cli", "browser-proxy", "--pair", "--config", str(path), "--advertise-host", "node-a"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "foreground command did not produce readiness output"
        code = process.stdout.readline()
        payload = decode_code(code)
        assert payload["proxyHost"] == "node-a" and payload["proxyPort"] == port
        assert payload["username"] == "fixture-user" and payload["password"] == "fixture-secret"
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.sendall(b"\x05\x01\x02")
            assert client.recv(2) == b"\x05\x02"
            client.sendall(b"\x01\x0cfixture-user\x0efixture-secret")
            assert client.recv(2) == b"\x01\x00"
        process.send_signal(signal.SIGINT)
        remaining, diagnostics = process.communicate(timeout=10)
        assert process.returncode == 0 and remaining == ""
        assert "fixture-secret" not in diagnostics and code.strip() not in diagnostics
        assert path.read_bytes() == before
        with socket.create_connection(("127.0.0.1", ingress.server_port), timeout=3):
            pass
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)
        ingress.shutdown()
        ingress.server_close()
        thread.join(timeout=5)


def test_automatic_pairing_live_listener_and_stable_restart(settings, publisher, monkeypatch):
    # Only the external runtime/agent and Tailscale discovery boundaries are
    # simulated. The bind, HTTP readiness probe, credentials, private persistence,
    # producer output and restarted SOCKS authentication are real.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        try:
            route.connect(("192.0.2.1", 9))  # Selects an interface; sends no packet.
        except OSError:
            pytest.skip("no non-loopback interface is available for an exact-IP bind")
        host = route.getsockname()[0]
    if pairing.ipaddress.ip_address(host).is_loopback:
        pytest.skip("no non-loopback interface is available for an exact-IP bind")
    monkeypatch.setattr(pairing, "tailscale_address", lambda: host)

    async def scenario():
        async with live_ingress() as ingress:
            publisher.state["public_backend_port"] = ingress.port
            publisher.status["gateway"]["public_backend_port"] = ingress.port
            first_payload = None
            saved = None
            for attempt in range(2):
                output = CodeOutput()
                monkeypatch.setattr(sys, "stdout", output)
                with pairing.automatic_pairing(settings) as setup:
                    task = asyncio.create_task(pairing.serve_pairing(setup))
                    try:
                        await asyncio.wait_for(output.ready.wait(), 5)
                        payload = decode_code(output.getvalue())
                        assert payload["proxyHost"] == host
                        assert payload["proxyPort"] >= 1024
                        reader, writer = await asyncio.open_connection(host, payload["proxyPort"])
                        writer.write(b"\x05\x01\x02")
                        await writer.drain()
                        assert await reader.readexactly(2) == b"\x05\x02"
                        username, password = payload["username"].encode(), payload["password"].encode()
                        writer.write(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
                        await writer.drain()
                        assert await reader.readexactly(2) == b"\x01\x00"
                        writer.close()
                        await writer.wait_closed()
                    finally:
                        await stop(task)
                path = settings.state_dir / "browser-proxy" / "pairing.json"
                if attempt == 0:
                    first_payload, saved = payload, path.read_bytes()
                else:
                    assert payload == first_payload
                    assert path.read_bytes() == saved
    asyncio.run(scenario())


def test_identity_change_after_live_probe_cannot_persist_or_emit_code(settings, publisher, monkeypatch):
    async def scenario():
        async with live_ingress() as ingress:
            publisher.state["public_backend_port"] = ingress.port
            publisher.status["gateway"]["public_backend_port"] = ingress.port
            monkeypatch.setattr(pairing, "tailscale_address", lambda: "100.101.102.103")
            with pairing.automatic_pairing(settings) as setup:
                # An actual loopback transport keeps this fixture independent of
                # host tailnet state; production never rewrites the bind this way.
                setup = replace(setup, config=replace(setup.config, listen=Endpoint("127.0.0.1", 0)))
                publisher.status["owner"] = "b" * 32
                output = CodeOutput()
                monkeypatch.setattr(sys, "stdout", output)
                with pytest.raises(PortmapError, match="identity"):
                    await pairing.serve_pairing(setup)
                assert output.getvalue() == ""
                assert not (settings.state_dir / "browser-proxy" / "pairing.json").exists()
    asyncio.run(scenario())


def test_private_save_failure_never_reveals_a_code(monkeypatch):
    async def scenario():
        output = CodeOutput()
        monkeypatch.setattr(sys, "stdout", output)
        async with live_ingress() as ingress:
            def fail_save(_config):
                raise PortmapError("private state is not writable")

            with pytest.raises(PortmapError, match="not writable"):
                await pairing.serve_pairing(pairing.Pairing("Node", "node-a", config(ingress), fail_save))
            assert output.getvalue() == ""
    asyncio.run(scenario())
