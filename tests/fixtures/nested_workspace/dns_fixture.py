#!/usr/bin/env python3
"""Fixture DNS authority: one controlled zone plus real forwarding.

Used in two places by the nested-workspace integration fixture:

* as the outer fixture container that owns the workspace's *original*
  upstream resolver: authoritative for the fixture zone (so search-list
  semantics, A/AAAA, NODATA and NXDOMAIN are all real answers under the
  fixture's control) and forwarding every other name to the verification
  VM's real upstream servers read at prepare time;
* as an importable query helper for the in-workspace exercise program and
  the driver, so no ``dig`` binary is required anywhere.

It implements the minimum DNS wire format those checks need. It is a test
fixture, never a product component.
"""
from __future__ import annotations

import argparse
import json
import random
import select
import socket
import struct
import sys
import threading
import time

QTYPE_A = 1
QTYPE_AAAA = 28
QCLASS_IN = 1
_TYPE_NAMES = {QTYPE_A: "A", QTYPE_AAAA: "AAAA"}


# --- wire format helpers (question parsing, response building) -------------


def encode_name(name: str) -> bytes:
    encoded = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii")
        if not 1 <= len(raw) <= 63:
            raise ValueError(f"invalid DNS label in {name!r}")
        encoded += bytes([len(raw)]) + raw
    return encoded + b"\x00"


def read_name(packet: bytes, offset: int) -> tuple[str, int]:
    """Decode one (possibly compressed) name; returns (name, next offset)."""
    labels: list[str] = []
    end = -1
    jumps = 0
    while True:
        if offset >= len(packet):
            raise ValueError("truncated name")
        size = packet[offset]
        if size & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated pointer")
            pointer = ((size & 0x3F) << 8) | packet[offset + 1]
            if end < 0:
                end = offset + 2
            offset = pointer
            jumps += 1
            if jumps > 16:
                raise ValueError("compression loop")
            continue
        if size == 0:
            if end < 0:
                end = offset + 1
            break
        if size & 0xC0:
            raise ValueError("unsupported label type")
        labels.append(packet[offset + 1:offset + 1 + size].decode("ascii", errors="replace"))
        offset += 1 + size
    return ".".join(labels).lower(), end


def parse_question(packet: bytes) -> tuple[str, int, int]:
    if len(packet) < 12:
        raise ValueError("short packet")
    qdcount = struct.unpack("!H", packet[4:6])[0]
    if qdcount != 1:
        raise ValueError("only single-question datagrams are supported")
    name, offset = read_name(packet, 12)
    if offset + 4 > len(packet):
        raise ValueError("truncated question")
    qtype, qclass = struct.unpack("!HH", packet[offset:offset + 4])
    return name, qtype, qclass


def response(
    packet: bytes,
    *,
    rcode: int = 0,
    aa: bool = False,
    answers: list[tuple[int, bytes]] | None = None,
) -> bytes:
    """Echo the question back with the given rcode and answer rdata list."""
    answers = answers or []
    name, qtype, qclass = parse_question(packet)
    flags = 0x8000 | 0x0080 | (0x0400 if aa else 0) | 0x0100 | (rcode & 0xF)
    header = packet[:2] + struct.pack("!HHHHH", flags, 1, len(answers), 0, 0)
    body = encode_name(name) + struct.pack("!HH", qtype, qclass)
    for answer_type, rdata in answers:
        if answer_type != qtype:
            raise ValueError("answer type must match the question type")
        body += b"\xc0\x0c" + struct.pack("!HHIH", answer_type, QCLASS_IN, 30, len(rdata)) + rdata
    return header + body


def rdata_for(qtype: int, value: str) -> bytes:
    if qtype == QTYPE_A:
        return socket.inet_aton(value)
    if qtype == QTYPE_AAAA:
        return socket.inet_pton(socket.AF_INET6, value)
    raise ValueError(f"unsupported record type {_TYPE_NAMES.get(qtype, qtype)}")


# --- one-shot query client (used by the driver and the exercise program) ---


def query(server: str, name: str, qtype: int = QTYPE_A, *, port: int = 53, tcp: bool = False, timeout: float = 4.0) -> dict:
    """Ask one raw DNS question and return ``{'rcode', 'aa', 'answers'}``.

    ``answers`` is a list of ``(qtype, text)`` pairs, IPv4/IPv6 rendered as
    normal address strings. Raises ``OSError`` on transport failure.
    """
    identifier = random.randrange(0, 65536)
    packet = struct.pack("!HHHHHH", identifier, 0x0100, 1, 0, 0, 0) + encode_name(name) + struct.pack("!HH", qtype, QCLASS_IN)
    if tcp:
        with socket.create_connection((server, port), timeout=timeout) as stream:
            stream.sendall(struct.pack("!H", len(packet)) + packet)
            prefix = stream.recv(2)
            if len(prefix) < 2:
                raise OSError("short TCP DNS reply")
            reply = _read_exactly(stream, struct.unpack("!H", prefix)[0])
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (server, port))
            reply, _ = sock.recvfrom(4096)
    if len(reply) < 12 or reply[:2] != packet[:2]:
        raise OSError("DNS reply id mismatch")
    flags = struct.unpack("!H", reply[2:4])[0]
    qdcount, ancount = struct.unpack("!HH", reply[4:8])
    offset = 12
    for _ in range(qdcount):
        _, offset = read_name(reply, offset)
        offset += 4
    answers: list[tuple[int, str]] = []
    for _ in range(ancount):
        _, offset = read_name(reply, offset)
        atype, aclass = struct.unpack("!HH", reply[offset:offset + 4])
        _ttl = struct.unpack("!I", reply[offset + 4:offset + 8])[0]
        rdlength = struct.unpack("!H", reply[offset + 8:offset + 10])[0]
        rdata = reply[offset + 10:offset + 10 + rdlength]
        offset += 10 + rdlength
        if aclass != QCLASS_IN:
            continue
        if atype == QTYPE_A and rdlength == 4:
            answers.append((atype, socket.inet_ntoa(rdata)))
        elif atype == QTYPE_AAAA and rdlength == 16:
            answers.append((atype, socket.inet_ntop(socket.AF_INET6, rdata)))
        else:
            answers.append((atype, rdata.hex()))
    return {"rcode": flags & 0xF, "aa": bool(flags & 0x0400), "answers": answers}


def _read_exactly(stream: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = stream.recv(count - len(data))
        if not chunk:
            raise OSError("connection closed mid-reply")
        data += chunk
    return data


# --- server -----------------------------------------------------------------


class FixtureDnsServer:
    """Authoritative for one zone; forwards everything else, for real."""

    def __init__(self, *, zone: str, records: dict[str, dict[int, str]], forwarders: list[str], listen: str, port: int) -> None:
        self.zone = zone.rstrip(".").lower()
        self.records = records
        self.forwarders = forwarders
        self.listen = listen
        self.port = port
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind((listen, port))
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind((listen, port))
        self._tcp.listen(16)
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def serve_forever(self) -> None:
        threading.Thread(target=self._accept_tcp, daemon=True).start()
        while not self._stop.is_set():
            ready, _, _ = select.select([self._udp], [], [], 0.5)
            if not ready:
                continue
            try:
                packet, peer = self._udp.recvfrom(4096)
            except OSError:
                break
            threading.Thread(target=self._serve_one, args=(packet, peer), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._udp.close()
        self._tcp.close()

    def _accept_tcp(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._tcp], [], [], 0.5)
                if not ready:
                    continue
                connection, _ = self._tcp.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_tcp, args=(connection,), daemon=True).start()

    def _serve_tcp(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(8)
            prefix = _read_exactly(connection, 2)
            packet = _read_exactly(connection, struct.unpack("!H", prefix)[0])
            reply = self.answer(packet)
            connection.sendall(struct.pack("!H", len(reply)) + reply)
        except (OSError, ValueError):
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _serve_one(self, packet: bytes, peer) -> None:
        try:
            reply = self.answer(packet)
        except (ValueError, OSError):
            return
        try:
            self._udp.sendto(reply, peer)
        except OSError:
            pass

    def answer(self, packet: bytes) -> bytes:
        name, qtype, _qclass = parse_question(packet)
        if name == self.zone or name.endswith("." + self.zone):
            types = self.records.get(name)
            if types is None:
                return response(packet, rcode=3, aa=True)  # NXDOMAIN
            value = types.get(qtype)
            if value is None:
                return response(packet, rcode=0, aa=True)  # NODATA
            return response(packet, rcode=0, aa=True, answers=[(qtype, rdata_for(qtype, value))])
        return self._forward(packet)

    def _forward(self, packet: bytes) -> bytes:
        if not self.forwarders:
            return response(packet, rcode=2, aa=True)  # SERVFAIL: nothing real to ask
        with self._lock:
            for server in self.forwarders:
                try:
                    return _forward_udp(packet, server)
                except OSError:
                    continue
        return response(packet, rcode=2, aa=True)


def _forward_udp(packet: bytes, server: str) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(4)
        sock.sendto(packet, (server, 53))
        reply, _ = sock.recvfrom(4096)
        return reply


def parse_record(spec: str) -> tuple[str, int, str]:
    """Parse ``NAME=A:1.2.3.4`` / ``NAME=AAAA:fd42::1`` arguments."""
    name, separator, tail = spec.partition("=")
    if not separator:
        raise ValueError(f"--record must be NAME=TYPE:VALUE, got {spec!r}")
    rtype, separator, value = tail.partition(":")
    if not separator:
        raise ValueError(f"--record must be NAME=TYPE:VALUE, got {spec!r}")
    qtype = {"A": QTYPE_A, "AAAA": QTYPE_AAAA}.get(rtype.upper())
    if qtype is None:
        raise ValueError(f"unsupported record type {rtype!r}")
    rdata_for(qtype, value)  # validate the address eagerly
    return name.strip().lower(), qtype, value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fixture DNS authority")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=53)
    parser.add_argument("--zone", required=True)
    parser.add_argument("--record", action="append", default=[], help="NAME=TYPE:VALUE, repeatable")
    parser.add_argument("--forward", action="append", default=[], help="real upstream IP, repeatable")
    args = parser.parse_args(argv)
    records: dict[str, dict[int, str]] = {}
    for spec in args.record:
        name, qtype, value = parse_record(spec)
        records.setdefault(name, {})[qtype] = value
    server = FixtureDnsServer(
        zone=args.zone,
        records=records,
        forwarders=[item.strip() for item in args.forward if item.strip()],
        listen=args.listen,
        port=args.port,
    )
    print(json.dumps({"ok": True, "zone": args.zone, "records": len(records), "forwarders": server.forwarders, "port": args.port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
