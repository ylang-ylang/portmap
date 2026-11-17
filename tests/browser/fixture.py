"""Deterministic HTTP/TLS/WS origins, not a proxy or extension replacement."""
import base64
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import ssl
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

NODE = os.environ.get("NODE_ID", "trap")
DOMAIN = os.environ.get("NODE_DOMAIN", "")
REQUESTS = []
LOCK = threading.Lock()


def in_domain(host):
    return host == DOMAIN or host.endswith("." + DOMAIN)


def certificates():
    root = Path("/certs")
    root.mkdir(exist_ok=True)
    def openssl(*args):
        subprocess.run(["openssl", *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-keyout", str(root / "ca.key"), "-out", str(root / "ca.pem"), "-subj", "/CN=Portmap isolated browser fixture CA", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    for node in ("a", "b"):
        name = str(root / node)
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", name + ".key", "-out", name + ".csr", "-subj", "/CN=" + node + ".portmap")
        ext = root / (node + ".ext")
        ext.write_text("subjectAltName=DNS:" + node + ".portmap,DNS:*." + node + ".portmap\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
        openssl("x509", "-req", "-in", name + ".csr", "-CA", str(root / "ca.pem"), "-CAkey", str(root / "ca.key"), "-set_serial", "1" if node == "a" else "2", "-days", "2", "-extfile", str(ext), "-out", name + ".pem")
    # Only the public CA and test-only leaf credentials are needed at runtime.
    (root / "ca.key").unlink()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # Request evidence excludes credential values by construction.

    def reply(self, body, kind="application/json", status=200, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/requests":
            with LOCK:
                self.reply(json.dumps(REQUESTS))
            return
        if path == "/health":
            self.reply("{}")
            return
        host = self.headers.get("Host", "")
        # TLS ingress independently enforces Host, including CONNECT tunnels.
        if isinstance(self.connection, ssl.SSLSocket) and not in_domain(host.split(":")[0]):
            self.reply("{}", status=421)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= 262144 or self.headers.get("Transfer-Encoding"):
            self.reply("{}", status=413)
            self.close_connection = True
            return
        body = self.rfile.read(length)
        peer = self.client_address[0]
        evidence = {"node": NODE, "host": host, "path": self.path, "method": self.command,
                    "body": None if path == "/binary-echo" else body.decode("utf-8", errors="replace"),
                    "bodyLength": len(body), "bodySha256": hashlib.sha256(body).hexdigest(),
                    "peer": peer, "peerLoopback": ipaddress.ip_address(peer).is_loopback,
                    "authorization": "Authorization" in self.headers,
                    "originAuthorizationCorrect": self.headers.get("Authorization") == "Bearer fixture-origin-token",
                    "proxyAuthorization": "Proxy-Authorization" in self.headers}
        with LOCK:
            REQUESTS.append(evidence)
        if path.startswith("/ssh-only") and not evidence["peerLoopback"]:
            self.reply(json.dumps(evidence), status=403)
        elif path == "/binary":
            self.reply(bytes(range(256)), "application/octet-stream")
        elif path == "/binary-echo":
            self.reply(body, "application/octet-stream")
        elif path == "/redirect":
            self.reply("", status=302, extra={"Location": "/landing?via=redirect"})
        elif path == "/asset.js":
            self.reply("window.fixtureAsset=" + json.dumps(NODE) + ";", "application/javascript")
        elif path == "/asset.css":
            self.reply("#asset-proof { color: rgb(1, 2, 3); }", "text/css")
        elif path == "/echo":
            self.reply(json.dumps(evidence))
        elif path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            self.websocket(evidence)
        else:
            self.reply('<!doctype html><link rel="stylesheet" href="/asset.css"><script src="/asset.js"></script><p id="asset-proof">Asset proof</p><pre id="evidence">' + html.escape(json.dumps(evidence)) + '</pre>', "text/html")

    def websocket(self, evidence):
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.connection.settimeout(10)
        first = self.rfile.read(2)
        if len(first) != 2:
            return
        size = first[1] & 127
        if size == 126:
            size = struct.unpack("!H", self.rfile.read(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self.rfile.read(8))[0]
        opcode = first[0] & 15
        if not first[1] & 128 or size > 65536 or opcode not in (1, 2):
            return
        mask = self.rfile.read(4)
        encoded = self.rfile.read(size)
        decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(encoded))
        evidence.update(websocket=True, opcode=opcode, payloadLength=len(decoded),
                        payloadSha256=hashlib.sha256(decoded).hexdigest())
        if opcode == 1:
            evidence["message"] = decoded.decode()
            payload = json.dumps(evidence).encode()
        else:
            payload = decoded
        frame = bytes([128 | opcode, len(payload)]) if len(payload) < 126 else bytes([128 | opcode, 126]) + struct.pack("!H", len(payload))
        self.wfile.write(frame + payload)
        self.wfile.flush()
        self.close_connection = True


def serve():
    if DOMAIN:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        leaf = DOMAIN[0]
        context.load_cert_chain("/certs/" + leaf + ".pem", "/certs/" + leaf + ".key")
        def sni(_sock, name, _context):
            if not name or not in_domain(name):
                return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        context.set_servername_callback(sni)
        https = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
        https.socket = context.wrap_socket(https.socket, server_side=True)
        threading.Thread(target=https.serve_forever, daemon=True).start()
    if NODE == "dns-leak-trap":
        threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    certificates() if sys.argv[1:] == ["certificates"] else serve()
