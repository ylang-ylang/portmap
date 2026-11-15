from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import replace

import pytest

from portmap.browser_proxy import BrowserProxy, Endpoint, ProxyConfig, RequestError, load_config, parse_request
from portmap.errors import PortmapError

CONFIG = ProxyConfig("a.portmap", Endpoint("127.0.0.1", 0), Endpoint("127.0.0.1", 1), b"browser", b"secret")
SUCCESS = b"\x05\x00\x00\x01" + b"\0" * 6


def request(path="/path?x=1", host="app.a.portmap", *, method="GET", extra=b"", body=b""):
    return f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n".encode() + extra + b"\r\n" + body


def parse(data, destination="app.a.portmap"):
    return parse_request(data, CONFIG, destination)


def config_json():
    return {"domain": "a.portmap", "listen": {"host": "127.0.0.1", "port": 18080},
            "credentials": {"username": "browser", "password": "secret"},
            "ingress": {"host": "fixed-ingress", "port": 8080}}


def save_config(tmp_path, value):
    path = tmp_path / "browser-proxy.json"
    path.write_text(json.dumps(value))
    return path


def test_config_defaults_public_and_requires_explicit_isolated_tls(tmp_path):
    raw = config_json()
    config = load_config(save_config(tmp_path, raw))
    assert config.username == b"browser" and config.password == b"secret"
    assert not config.private_ingress and config.tls_ingress is None
    raw["ingress"]["scope"] = "private"
    raw["tls_ingress"] = {"host": "fixed-tls", "port": 8443, "scope_domain": "a.portmap"}
    config = load_config(save_config(tmp_path, raw))
    assert config.private_ingress
    assert config.tls_ingress == Endpoint("fixed-tls", 8443)


@pytest.mark.parametrize("patch", [
    {"domain": "portmap"}, {"domain": "a.portmap.evil"}, {"domain": "A.portmap"},
    {"domain": "bad_label.portmap"}, {"domain": "a..portmap"},
    {"credentials": {"username": "", "password": "secret"}},
    {"credentials": {"username": "browser", "password": "a\r\nb"}},
    {"credentials": {"username": "browser", "password": "\ud800"}},
    {"credentials": {"username": "u" * 256, "password": "secret"}},
    {"credentials": {"username": "browser", "password": "密" * 86}},
    {"listen": {"host": "localhost", "port": 18080}},
    {"listen": {"host": "127.0.0.1", "port": True}},
    {"ingress": {"host": "http://localhost", "port": 80}},
    {"ingress": {"host": "localhost", "port": 80, "scope": "automatic"}},
    {"ingress": {"host": "localhost", "port": 80, "scope": []}},
    {"tls_ingress": {"host": "tls", "port": 443}},
    {"tls_ingress": {"host": "tls", "port": 443, "scope_domain": "b.portmap"}},
    {"tls": {"certfile": "obsolete.pem", "keyfile": "obsolete.key"}},
    {"proxyType": "http"}, {"unknown": "do not silently accept misspellings"},
])
def test_config_rejects_invalid_boundaries_and_obsolete_modes(tmp_path, patch):
    raw = config_json()
    raw.update(patch)
    with pytest.raises(PortmapError, match="browser-proxy"):
        load_config(save_config(tmp_path, raw))


def test_config_utf8_byte_limits_and_colon_username(tmp_path):
    raw = config_json()
    raw["credentials"] = {"username": "user:β", "password": "密" * 85}
    config = load_config(save_config(tmp_path, raw))
    assert config.username == "user:β".encode()
    assert len(config.password) == 255


def test_config_duplicate_fields_and_missing_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"domain":"a.portmap","domain":"b.portmap"}')
    with pytest.raises(PortmapError, match="duplicate field"):
        load_config(path)
    with pytest.raises(PortmapError, match="cannot read"):
        load_config(tmp_path / "missing.json")


@pytest.mark.parametrize("host,status", [
    ("app.a.portmap", None), ("APP.A.PORTMAP.", None), ("app.a.portmap:80", None),
    ("b.portmap", 403), ("nota.portmap", 403), ("other.a.portmap", 400),
    ("app.a.portmap.evil", 400), ("127.0.0.1", 400), ("169.254.169.254", 400),
    ("app.a.portmap:8083", 400), ("user:pass@app.a.portmap", 400),
    ("app.a.portmap:", 400), ("app.a.portmap:0", 400), ("app.a.portmap..", 400),
])
def test_inner_host_is_bound_to_authenticated_socks_destination(host, status):
    if status is None:
        assert parse(request(host=host)).method == "GET"
    else:
        with pytest.raises(RequestError) as error:
            parse(request(host=host))
        assert error.value.status == status


@pytest.mark.parametrize("path,method", [
    ("http://app.a.portmap/path", "GET"), ("https://app.a.portmap/", "GET"),
    ("app.a.portmap:443", "CONNECT"), ("/path#fragment", "GET"), ("*", "OPTIONS"),
])
def test_inner_http_is_not_a_second_forward_proxy(path, method):
    with pytest.raises(RequestError) as error:
        parse(request(path, method=method))
    assert error.value.status == 400


@pytest.mark.parametrize("extra", [
    b"Content-Length: 1\r\nContent-Length: 1\r\n",
    b"Content-Length: 1\r\nContent-Length: 2\r\n",
    b"Content-Length: 1\r\nTransfer-Encoding: chunked\r\n",
    b"Transfer-Encoding: gzip, chunked\r\n",
    b"Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n",
    b"Content-Length: +2\r\n", b"Content-Length: -1\r\n",
    b"Connection: Host\r\n", b"Connection: Content-Length\r\n",
    b"Connection: Authorization\r\n", b"Host: b.portmap\r\n",
    b"Bad Header: value\r\n", b" folded: value\r\n",
    b"X-Test: a\x00b\r\n", b"Upgrade: h2c\r\nConnection: Upgrade\r\n",
])
def test_rejects_ambiguous_request_framing_and_hop_headers(extra):
    with pytest.raises(RequestError) as error:
        parse(request(extra=extra))
    assert error.value.status == 400


def test_request_keeps_origin_auth_not_proxy_headers_and_empty_query():
    parsed = parse(request("/original?", extra=b"Authorization: Bearer origin-secret\r\nProxy-Authorization: untrusted\r\nProxy-Connection: keep-alive\r\n"))
    assert parsed.path == "/original?"
    assert ("Authorization", "Bearer origin-secret") in parsed.headers
    assert not any(k.lower().startswith("proxy-") for k, v in parsed.headers)


@contextlib.asynccontextmanager
async def running_proxy(ingress_handler, *, tls=False, username=b"browser", password=b"secret"):
    ingress = await asyncio.start_server(ingress_handler, "127.0.0.1", 0)
    endpoint = Endpoint("127.0.0.1", ingress.sockets[0].getsockname()[1])
    config = replace(CONFIG, ingress=endpoint, tls_ingress=endpoint if tls else None, username=username, password=password)
    proxy = await BrowserProxy(config).start()
    try:
        yield proxy.sockets[0].getsockname()[1]
    finally:
        proxy.close()
        ingress.close()
        await proxy.wait_closed()
        await ingress.wait_closed()


async def close(writer):
    writer.close()
    await writer.wait_closed()


async def authenticate(port, *, username=b"browser", password=b"secret"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"\x05\x02\x00\x02")
    await writer.drain()
    assert await asyncio.wait_for(reader.readexactly(2), 3) == b"\x05\x02"
    writer.write(b"\x01" + bytes((len(username),)) + username + bytes((len(password),)) + password)
    await writer.drain()
    result = await asyncio.wait_for(reader.readexactly(2), 3)
    return reader, writer, result


def connect_request(domain="app.a.portmap", port=80, *, command=1, address_type=3):
    domain = domain.encode("ascii")
    address = bytes((len(domain),)) + domain if address_type == 3 else b"\0" * (4 if address_type == 1 else 16)
    return bytes((5, command, 0, address_type)) + address + port.to_bytes(2, "big")


async def open_socks(port, domain="app.a.portmap", destination_port=80, *, username=b"browser", password=b"secret"):
    reader, writer, authentication = await authenticate(port, username=username, password=password)
    assert authentication == b"\x01\x00"
    writer.write(connect_request(domain, destination_port))
    await writer.drain()
    reply = await asyncio.wait_for(reader.readexactly(10), 3)
    return reader, writer, reply


async def exchange(port, data, *, domain="app.a.portmap"):
    reader, writer, reply = await open_socks(port, domain)
    assert reply == SUCCESS
    writer.write(data)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 3)
    await close(writer)
    return response


@pytest.mark.parametrize("greeting", [b"\x05\x01\x00", b"\x05\x01\x01", b"\x05\x00", b"\x04\x01\x02", b"GE"])
def test_no_auth_fallback_and_old_proxy_protocols_are_rejected(greeting):
    async def scenario():
        calls = []
        async def ingress(reader, writer):
            calls.append(True)
            writer.close()
        async with running_proxy(ingress) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(greeting)
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 3) == b"\x05\xff"
            await close(writer)
        assert not calls
    asyncio.run(scenario())


@pytest.mark.parametrize("username,password", [(b"wrong", b"secret"), (b"browser", b"wrong"), (b"", b"secret"), (b"browser", b"")])
def test_socks_authentication_failure_never_contacts_ingress(username, password):
    async def scenario():
        calls = []
        async def ingress(reader, writer):
            calls.append(True)
            writer.close()
        async with running_proxy(ingress) as port:
            reader, writer, result = await authenticate(port, username=username, password=password)
            assert result == b"\x01\x01"
            assert await asyncio.wait_for(reader.read(), 3) == b""
            await close(writer)
        assert not calls
    asyncio.run(scenario())


@pytest.mark.parametrize("domain,port,command,address_type,reply", [
    ("b.portmap", 80, 1, 3, 2), ("nota.portmap", 80, 1, 3, 2),
    ("app.a.portmap.evil", 80, 1, 3, 2), ("app.a.portmap..", 80, 1, 3, 2),
    ("127.0.0.1", 80, 1, 3, 2), ("169.254.169.254", 80, 1, 3, 2),
    ("app.a.portmap:80", 80, 1, 3, 2), ("", 80, 1, 3, 2),
    ("app.a.portmap", 22, 1, 3, 2), ("app.a.portmap", 8083, 1, 3, 2),
    ("app.a.portmap", 443, 1, 3, 2),
    ("app.a.portmap", 80, 2, 3, 7), ("app.a.portmap", 80, 3, 3, 7),
    ("app.a.portmap", 80, 1, 1, 8), ("app.a.portmap", 80, 1, 4, 8),
])
def test_socks_scope_ports_commands_and_domain_only_address(domain, port, command, address_type, reply):
    async def scenario():
        calls = []
        async def ingress(reader, writer):
            calls.append(True)
            writer.close()
        async with running_proxy(ingress) as listener:
            reader, writer, auth = await authenticate(listener)
            assert auth == b"\x01\x00"
            writer.write(connect_request(domain, port, command=command, address_type=address_type))
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(10), 3) == bytes((5, reply, 0, 1)) + b"\0" * 6
            assert await asyncio.wait_for(reader.read(), 3) == b""
            await close(writer)
        assert not calls
    asyncio.run(scenario())


def test_utf8_colon_credentials_and_rooted_host_forward_without_destination_dns(monkeypatch):
    import socket
    original = socket.getaddrinfo
    def checked(host, *args, **kwargs):
        assert not str(host).lower().rstrip(".").endswith(".portmap"), "SOCKS destination must never be resolved"
        return original(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", checked)
    async def scenario():
        captured = []
        async def ingress(reader, writer):
            captured.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            await writer.drain()
            writer.close()
        username, password = "user:β".encode(), "pass-密".encode()
        async with running_proxy(ingress, username=username, password=password) as port:
            reader, writer, reply = await open_socks(port, "APP.A.PORTMAP.", username=username, password=password)
            assert reply == SUCCESS
            writer.write(request("/original%2Fpath?keep=one%20two", "APP.A.PORTMAP.:80", extra=b"Authorization: Bearer origin-secret\r\nProxy-Authorization: never-forward\r\n"))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
            await close(writer)
        assert response.endswith(b"OK")
        assert captured[0].startswith(b"GET /original%2Fpath?keep=one%20two HTTP/1.1\r\nHost: APP.A.PORTMAP.:80\r\n")
        assert b"Authorization: Bearer origin-secret\r\n" in captured[0]
        assert b"Proxy-Authorization" not in captured[0]
        assert password not in captured[0]
    asyncio.run(scenario())


@pytest.mark.parametrize("host", ["b.portmap", "other.a.portmap", "app.a.portmap:443"])
def test_socks_http_host_escape_rejected_before_ingress(host):
    async def scenario():
        calls = []
        async def ingress(reader, writer):
            calls.append(True)
            writer.close()
        async with running_proxy(ingress) as port:
            response = await exchange(port, request(host=host))
        assert response.startswith((b"HTTP/1.1 400", b"HTTP/1.1 403"))
        assert not calls
    asyncio.run(scenario())


def test_forwarding_post_body_host_path_and_origin_authorization():
    async def scenario():
        captured = []
        async def ingress(reader, writer):
            captured.append(await reader.readuntil(b"\r\n\r\n") + await reader.readexactly(11))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            await writer.drain()
            writer.close()
        async with running_proxy(ingress) as port:
            response = await exchange(port, request(method="POST", extra=b"Content-Length: 11\r\nAuthorization: Bearer origin-secret\r\n", body=b"hello world"))
        assert response.endswith(b"OK")
        assert captured[0].startswith(b"POST /path?x=1 HTTP/1.1\r\nHost: app.a.portmap\r\n")
        assert b"Authorization: Bearer origin-secret\r\n" in captured[0]
        assert b"secret" not in captured[0].replace(b"origin-secret", b"origin")
        assert captured[0].endswith(b"hello world")
    asyncio.run(scenario())


@pytest.mark.parametrize("body_header,body", [(b"", b""), (b"Content-Length: 3\r\n", b"abc"), (b"Connection: Upgrade\r\nUpgrade: websocket\r\n", b"")])
def test_pipelined_second_request_never_reaches_ingress(body_header, body):
    async def scenario():
        complete = asyncio.get_running_loop().create_future()
        async def ingress(reader, writer):
            first = await reader.readuntil(b"\r\n\r\n")
            if body:
                first += await reader.readexactly(len(body))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            complete.set_result((first, await asyncio.wait_for(reader.read(), 3)))
            writer.close()
        async with running_proxy(ingress) as port:
            await exchange(port, request(extra=body_header, body=body + b"GET /private HTTP/1.1\r\nHost: b.portmap\r\n\r\n"))
            first, remainder = await complete
        assert b"b.portmap" not in first
        assert remainder == b""
    asyncio.run(scenario())


def test_chunked_upload_streams_and_discards_routing_or_credential_trailers():
    async def scenario():
        captured = []
        async def ingress(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            captured.append(head + await reader.readuntil(b"0\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
        async with running_proxy(ingress) as port:
            response = await exchange(port, request(method="POST", extra=b"Transfer-Encoding: chunked\r\nTrailer: Proxy-Authorization\r\n", body=b"3\r\nabc\r\n0\r\nProxy-Authorization: should-not-reach-origin\r\nHost: b.portmap\r\n\r\n"))
        assert response.startswith(b"HTTP/1.1 200")
        assert captured[0].endswith(b"3\r\nabc\r\n0\r\n\r\n")
        assert b"should-not-reach-origin" not in captured[0]
        assert b"b.portmap" not in captured[0]
        assert b"Trailer:" not in captured[0]
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [b"garbage\r\n", b"3\r\nabcXX", b"-1\r\n", b"1;bad\x00extension\r\n"])
def test_malformed_chunks_close_without_forwarding_following_requests(invalid):
    async def scenario():
        complete = asyncio.get_running_loop().create_future()
        async def ingress(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            complete.set_result(head + await asyncio.wait_for(reader.read(), 3))
            writer.close()
        async with running_proxy(ingress) as port:
            await exchange(port, request(method="POST", extra=b"Transfer-Encoding: chunked\r\n", body=invalid + b"GET /private HTTP/1.1\r\nHost: b.portmap\r\n\r\n"))
            captured = await complete
        assert b"b.portmap" not in captured
    asyncio.run(scenario())


def test_upload_response_streaming_and_continue():
    async def scenario():
        async def ingress(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            await writer.drain()
            assert await reader.readexactly(3) == b"abc"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\none")
            await writer.drain()
            assert await reader.readexactly(3) == b"def"
            writer.write(b"two")
            await writer.drain()
            writer.close()
        async with running_proxy(ingress) as port:
            reader, writer, reply = await open_socks(port)
            assert reply == SUCCESS
            writer.write(request(method="POST", extra=b"Content-Length: 6\r\nExpect: 100-continue\r\n"))
            await writer.drain()
            assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)).startswith(b"HTTP/1.1 100")
            writer.write(b"abc")
            await writer.drain()
            assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)).startswith(b"HTTP/1.1 200")
            assert await asyncio.wait_for(reader.readexactly(3), 3) == b"one"
            writer.write(b"def")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 3) == b"two"
            await close(writer)
    asyncio.run(scenario())


def test_websocket_upgrade_preserves_bidirectional_transport():
    async def scenario():
        async def ingress(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            assert b"Upgrade: websocket\r\n" in head
            assert b"Proxy-Authorization" not in head
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
            await writer.drain()
            assert await reader.readexactly(4) == b"\x82\x02hi"
            writer.write(b"\x82\x02ok")
            await writer.drain()
            writer.close()
        async with running_proxy(ingress) as port:
            reader, writer, reply = await open_socks(port)
            assert reply == SUCCESS
            writer.write(request(extra=b"Connection: Upgrade\r\nUpgrade: websocket\r\n"))
            await writer.drain()
            assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)).startswith(b"HTTP/1.1 101")
            writer.write(b"\x82\x02hi")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(4), 3) == b"\x82\x02ok"
            await close(writer)
    asyncio.run(scenario())


def test_socks_443_uses_fixed_tls_ingress_without_intercepting_tls_bytes():
    # Byte-transport unit regression only. Actual TLS certificates, browser
    # verification and isolated SNI/Host ingress are real-browser fixture gates.
    async def scenario():
        seen = []
        async def ingress(reader, writer):
            data = await reader.readexactly(5)
            seen.append(data)
            writer.write(data)
            await writer.drain()
            writer.close()
        async with running_proxy(ingress, tls=True) as port:
            reader, writer, reply = await open_socks(port, "app.a.portmap.", 443)
            assert reply == SUCCESS
            writer.write(b"\x16\x03\x01\x00\x00")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(5), 3) == b"\x16\x03\x01\x00\x00"
            await close(writer)
        assert seen == [b"\x16\x03\x01\x00\x00"]
    asyncio.run(scenario())


@pytest.mark.parametrize("method,status,headers,body", [
    ("GET", b"200 OK", b"Content-Length: 3\r\n", b"abc"),
    ("HEAD", b"200 OK", b"Content-Length: 100\r\n", b""),
    ("GET", b"204 No Content", b"", b""),
    ("GET", b"304 Not Modified", b"Content-Length: 100\r\n", b""),
    ("GET", b"200 OK", b"Transfer-Encoding: chunked\r\nTrailer: X-Digest\r\n", b"3\r\nabc\r\n0\r\nX-Digest: value\r\n\r\n"),
])
def test_framed_response_completes_without_ingress_eof(method, status, headers, body):
    async def scenario():
        complete = asyncio.get_running_loop().create_future()
        async def ingress(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 " + status + b"\r\n" + headers + b"Connection: X-Hop\r\nX-Hop: removed\r\n\r\n" + body)
            await writer.drain()
            complete.set_result(await asyncio.wait_for(reader.read(), 3))
            writer.close()
        async with running_proxy(ingress) as port:
            response = await exchange(port, request(method=method))
            assert await complete == b""
        head, received_body = response.split(b"\r\n\r\n", 1)
        assert head.startswith(b"HTTP/1.1 " + status)
        assert b"X-Hop: removed" not in head
        assert received_body == body
    asyncio.run(scenario())


@pytest.mark.parametrize("headers", [
    b"Content-Length: 0\r\nContent-Length: 1\r\n",
    b"Content-Length: 0\r\nTransfer-Encoding: chunked\r\n",
    b"Transfer-Encoding: gzip\r\n", b"Content-Length: -1\r\n",
    b"Connection: Content-Length\r\nContent-Length: 0\r\n",
])
def test_invalid_ingress_response_framing_is_not_forwarded(headers):
    async def scenario():
        async def ingress(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\nunsafe")
            await writer.drain()
            writer.close()
        async with running_proxy(ingress) as port:
            response = await exchange(port, request())
        assert b"200 OK" not in response and b"unsafe" not in response
    asyncio.run(scenario())


@pytest.mark.parametrize("destination_port", [80, 443])
def test_unavailable_fixed_ingress_does_not_try_another_destination(destination_port):
    async def scenario():
        closed = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        endpoint = Endpoint("127.0.0.1", closed.sockets[0].getsockname()[1])
        closed.close()
        await closed.wait_closed()
        proxy = await BrowserProxy(replace(CONFIG, ingress=endpoint, tls_ingress=endpoint)).start()
        try:
            reader, writer, reply = await open_socks(proxy.sockets[0].getsockname()[1], destination_port=destination_port)
            if destination_port == 80:
                assert reply == SUCCESS
                writer.write(request())
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 3)
                assert response.startswith(b"HTTP/1.1 502")
            else:
                assert reply == b"\x05\x05\x00\x01" + b"\0" * 6
            await close(writer)
        finally:
            proxy.close()
            await proxy.wait_closed()
    asyncio.run(scenario())


def test_cli_browser_proxy_is_explicit_foreground_without_loading_runtime(monkeypatch, tmp_path):
    import portmap.browser_proxy as module
    from portmap.cli import main
    config_path = tmp_path / "explicit.json"
    calls = []
    monkeypatch.setattr(module, "run_browser_proxy", lambda path: calls.append(path) or 0)
    monkeypatch.setattr("portmap.cli.load_portmap_settings", lambda *a, **kw: pytest.fail("must not load or mutate runtime"))
    assert main(["browser-proxy", "--config", str(config_path)]) == 0
    assert calls == [config_path]
    assert main(["browser-proxy"]) == 2


@pytest.mark.parametrize("payload", [b"\x02\x01u\x01p", b"\x01\x00", b"\x01\x01u\x00"])
def test_malformed_rfc1929_authentication_is_rejected(payload):
    async def scenario():
        calls = []
        async def ingress(reader, writer):
            calls.append(True)
            writer.close()
        async with running_proxy(ingress) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"\x05\x01\x02")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(2), 3) == b"\x05\x02"
            writer.write(payload)
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(2), 3) == b"\x01\x01"
            await close(writer)
        assert not calls
    asyncio.run(scenario())


def test_rfc1929_maximum_length_credentials_authenticate_without_ingress():
    async def scenario():
        calls = []
        async def ingress(reader, writer):
            calls.append(True)
            writer.close()
        async with running_proxy(ingress, username=b"u" * 255, password=b"p" * 255) as port:
            reader, writer, authentication = await authenticate(port, username=b"u" * 255, password=b"p" * 255)
            assert authentication == b"\x01\x00"
            await close(writer)
        assert not calls
    asyncio.run(scenario())
