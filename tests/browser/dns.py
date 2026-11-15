"""Authoritative test DNS with observable queries; never forwards public DNS."""
import json
import socket
import socketserver
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUERIES = []
LOCK = threading.Lock()
INTERNAL = {"fixture-a", "fixture-b", "unrelated", "trap", "node-a", "node-b", "a", "b", "dns"}


def response(packet, peer, transport):
    if len(packet) < 12 or struct.unpack("!H", packet[4:6])[0] != 1:
        return b""
    cursor = 12
    labels = []
    while cursor < len(packet):
        size = packet[cursor]
        cursor += 1
        if size == 0:
            break
        if size > 63 or cursor + size > len(packet):
            return b""
        labels.append(packet[cursor:cursor + size].decode("ascii"))
        cursor += size
    if cursor + 4 > len(packet):
        return b""
    kind, cls = struct.unpack("!HH", packet[cursor:cursor + 4])
    question = packet[12:cursor + 4]
    name = ".".join(labels).lower()
    with LOCK:
        QUERIES.append({"name": name, "type": kind, "source_ip": peer[0], "source_port": peer[1], "transport": transport, "transaction_id": int.from_bytes(packet[:2], "big"), "at_ns": time.time_ns()})
    target = "trap" if name == "portmap" or name.endswith(".portmap") else name if name in INTERNAL else None
    if target is None:
        return packet[:2] + struct.pack("!HHHHH", 0x8183, 1, 0, 0, 0) + question
    if kind != 1 or cls != 1:
        return packet[:2] + struct.pack("!HHHHH", 0x8180, 1, 0, 0, 0) + question
    address = socket.inet_aton(socket.gethostbyname(target))
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 0, 4) + address
    return packet[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0) + question + answer


class UDP(socketserver.BaseRequestHandler):
    def handle(self):
        packet, transport = self.request
        result = response(packet, self.client_address, "udp")
        if result:
            transport.sendto(result, self.client_address)


class TCP(socketserver.StreamRequestHandler):
    def handle(self):
        length = self.rfile.read(2)
        if len(length) == 2:
            result = response(self.rfile.read(struct.unpack("!H", length)[0]), self.client_address, "tcp")
            if result:
                self.wfile.write(struct.pack("!H", len(result)) + result)


class Evidence(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        with LOCK:
            payload = json.dumps(QUERIES if self.path == "/queries" else {}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    for server in (socketserver.ThreadingUDPServer(("0.0.0.0", 53), UDP), socketserver.ThreadingTCPServer(("0.0.0.0", 53), TCP)):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Evidence).serve_forever()
