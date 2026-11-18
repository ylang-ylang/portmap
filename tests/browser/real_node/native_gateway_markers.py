#!/usr/bin/env python3
"""Reconstruct native ingress requests in bounded memory; persist only proof metadata.

The live PCAP pipe, HTTP headers and bodies never reach disk. A proof needs an
observed client SYN and one complete, unambiguous Content-Length HTTP request;
unknown framing, gaps, conflicting retransmissions and limits fail closed.
"""
from collections import OrderedDict
import datetime
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit

READY = Path('/var/tmp/portmap-real-node-observer-ready')
PORTS = {80, 8080, 5173}
MAX_PACKET_BYTES = 1024 * 1024
MAX_FLOWS = 128
MAX_STREAM_BYTES = 256 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 64 * 1024
MAX_REQUESTS = 256
NONCE = re.compile(r'portmap-realnode-[A-Za-z0-9_.:-]{1,240}')
HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class CaptureError(RuntimeError):
    pass


class RequestError(ValueError):
    pass


def _read_exact(stream, size, *, eof=False):
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if eof and remaining == size:
                return None
            raise CaptureError('truncated native packet capture')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def decode_packet(frame, link_type):
    """Decode unfragmented IPv4 TCP from Ethernet or Linux any cooked capture."""
    if link_type == 1:
        offset, protocol = 14, frame[12:14]
    elif link_type == 113:
        offset, protocol = 16, frame[14:16]
    elif link_type == 276:
        offset, protocol = 20, frame[:2]
    else:
        raise CaptureError('unsupported native packet link type')
    if protocol != b'\x08\x00' or len(frame) < offset + 20:
        return None
    header_size = (frame[offset] & 15) * 4
    total = int.from_bytes(frame[offset + 2:offset + 4], 'big')
    fragment = int.from_bytes(frame[offset + 6:offset + 8], 'big')
    if (frame[offset] >> 4 != 4 or header_size < 20 or total < header_size + 20 or
            len(frame) < offset + total or fragment & 0x3fff or frame[offset + 9] != 6):
        return None
    tcp = offset + header_size
    tcp_size = (frame[tcp + 12] >> 4) * 4
    if tcp_size < 20 or header_size + tcp_size > total:
        return None
    source_port, destination_port, sequence = struct.unpack_from('!HHI', frame, tcp)
    return {'source_address': socket.inet_ntoa(frame[offset + 12:offset + 16]),
            'destination_address': socket.inet_ntoa(frame[offset + 16:offset + 20]),
            'source_port': source_port, 'destination_port': destination_port,
            'sequence': sequence, 'flags': frame[tcp + 13],
            'payload': memoryview(frame)[tcp + tcp_size:offset + total]}


class PcapReader:
    def __init__(self, stream):
        self.stream = stream
        header = _read_exact(stream, 24)
        formats = {b'\xd4\xc3\xb2\xa1': ('<', 1000000), b'\xa1\xb2\xc3\xd4': ('>', 1000000),
                   b'\x4d\x3c\xb2\xa1': ('<', 1000000000), b'\xa1\xb2\x3c\x4d': ('>', 1000000000)}
        if header[:4] not in formats:
            raise CaptureError('unsupported native packet capture format')
        self.endian, self.scale = formats[header[:4]]
        major, minor, _, _, self.snaplen, network = struct.unpack(self.endian + 'HHIIII', header[4:])
        self.link_type = network & 0xffff
        if ((major, minor) != (2, 4) or self.link_type not in (1, 113, 276) or
                not 0 < self.snaplen <= MAX_PACKET_BYTES):
            raise CaptureError('invalid native packet capture header')

    def __iter__(self):
        while (header := _read_exact(self.stream, 16, eof=True)) is not None:
            seconds, fraction, captured, original = struct.unpack(self.endian + 'IIII', header)
            if not 0 < captured <= self.snaplen or captured != original or fraction >= self.scale:
                raise CaptureError('truncated or oversized native packet')
            packet = decode_packet(_read_exact(self.stream, captured), self.link_type)
            if packet is not None:
                packet['at'] = seconds + fraction / self.scale
                yield packet


def _headers(data):
    lines = data.split(b'\r\n')
    if len(lines) > 128:
        raise RequestError('request-framing')
    found = re.fullmatch(rb'([A-Z]{3,12}) ([^\x00-\x20\x7f]{1,4096}) HTTP/1\.[01]', lines[0])
    if found is None:
        raise RequestError('request-framing')
    fields = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b':')
        if not separator or HEADER_NAME.fullmatch(name) is None or any(byte < 32 and byte != 9 or byte == 127 for byte in value):
            raise RequestError('request-framing')
        name = name.lower()
        if name in fields and name in (b'host', b'content-length', b'transfer-encoding', b'content-type'):
            raise RequestError('request-framing')
        fields[name] = value.strip(b' \t')
    length = fields.get(b'content-length', b'0')
    if (b'transfer-encoding' in fields or re.fullmatch(rb'[0-9]{1,10}', length) is None or
            found[1] == b'POST' and b'content-length' not in fields):
        raise RequestError('request-framing')
    host = fields.get(b'host', b'').decode('ascii', errors='replace')
    if re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]{1,5})?', host) is None:
        raise RequestError('request-framing')
    try:
        parsed_host = urlsplit('http://' + host)
        if parsed_host.port is not None and not 0 < parsed_host.port <= 65535:
            raise ValueError()
        target = found[2].decode('ascii')
        parsed_target = urlsplit(target)
        if not target.startswith('/') or target.startswith('//') or parsed_target.scheme or parsed_target.netloc:
            raise ValueError()
    except (UnicodeError, ValueError):
        raise RequestError('request-framing') from None
    return {'method': found[1].decode('ascii'), 'host': host.lower(), 'path': parsed_target.path,
            'body_bytes': int(length), 'json': fields.get(b'content-type', b'').split(b';', 1)[0].lower() == b'application/json',
            'encoded': fields.get(b'content-encoding', b'identity').lower() != b'identity',
            'upgrade': b'upgrade' in fields}


def _nonce(body):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(body, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        return None
    value = value.get('nonce') if isinstance(value, dict) else None
    return value if isinstance(value, str) and NONCE.fullmatch(value) else None


class Flow:
    def __init__(self, packet, now):
        self.identity = {key: packet[key] for key in
                         ('source_address', 'source_port', 'destination_address', 'destination_port')}
        self.identity.update(initial_sequence=packet['sequence'], syn_observed=True)
        self.data, self.present = bytearray(), bytearray()
        self.contiguous = self.cursor = self.ordinal = 0
        self.last_seen = now
        self.upgraded = False

    def feed(self, packet, *, max_stream_bytes, max_header_bytes, max_body_bytes):
        offset = (packet['sequence'] + bool(packet['flags'] & 2) - self.identity['initial_sequence'] - 1) & 0xffffffff
        payload = packet['payload']
        if not payload:
            return []
        end = offset + len(payload)
        if end > max_stream_bytes:
            raise RequestError('stream-limit')
        if end > len(self.data):
            self.data.extend(b'\0' * (end - len(self.data)))
            self.present.extend(b'\0' * (end - len(self.present)))
        for index, byte in enumerate(payload, offset):
            if self.present[index] and self.data[index] != byte:
                raise RequestError('conflicting-tcp-overlap')
            self.data[index], self.present[index] = byte, 1
        while self.contiguous < len(self.present) and self.present[self.contiguous]:
            self.contiguous += 1
        result = []
        while self.cursor < self.contiguous:
            boundary = self.data.find(b'\r\n\r\n', self.cursor, min(self.contiguous, self.cursor + max_header_bytes + 1))
            if boundary < 0:
                if self.contiguous - self.cursor > max_header_bytes:
                    raise RequestError('header-limit')
                break
            header_bytes = boundary + 4 - self.cursor
            if header_bytes > max_header_bytes:
                raise RequestError('header-limit')
            fields = _headers(bytes(self.data[self.cursor:boundary]))
            body_bytes = fields['body_bytes']
            end = self.cursor + header_bytes + body_bytes
            if body_bytes > max_body_bytes or end > max_stream_bytes:
                raise RequestError('body-limit')
            if self.contiguous < end:
                break
            self.ordinal += 1
            if self.ordinal > MAX_REQUESTS:
                raise RequestError('request-limit')
            if fields['method'] == 'POST' and fields['path'] == '/api/echo' and fields['json'] and not fields['encoded']:
                nonce = _nonce(bytes(self.data[self.cursor + header_bytes:end]))
                if nonce is not None:
                    result.append({'type': 'native-gateway-http-request', 'schemaVersion': 1,
                                   'at': datetime.datetime.fromtimestamp(packet['at'], datetime.timezone.utc).isoformat(),
                                   'flow': dict(self.identity),
                                   'request': {'ordinal': self.ordinal, 'start_offset': self.cursor, 'header_bytes': header_bytes,
                                               'body_bytes': body_bytes, 'end_offset': end, 'complete': True, 'framing': 'content-length'},
                                   'method': 'POST', 'path': '/api/echo', 'host': fields['host'], 'nonce': nonce})
            self.cursor = end
            if fields['upgrade']:
                self.upgraded = True
                break
        return result


class RequestObserver:
    def __init__(self, *, max_flows=MAX_FLOWS, max_stream_bytes=MAX_STREAM_BYTES,
                 max_header_bytes=MAX_HEADER_BYTES, max_body_bytes=MAX_BODY_BYTES, idle_seconds=120, clock=time.monotonic):
        self.flows = OrderedDict()
        self.max_flows, self.max_stream_bytes = max_flows, max_stream_bytes
        self.max_header_bytes, self.max_body_bytes = max_header_bytes, max_body_bytes
        self.idle_seconds, self.clock = idle_seconds, clock

    def feed(self, packet):
        now = self.clock()
        while self.flows and next(iter(self.flows.values())).last_seen + self.idle_seconds < now:
            self.flows.popitem(last=False)
        key = tuple(packet[field] for field in ('source_address', 'source_port', 'destination_address', 'destination_port'))
        if packet['destination_port'] not in PORTS:
            if packet['flags'] & 5:
                self.flows.pop((key[2], key[3], key[0], key[1]), None)
            return []
        if packet['flags'] & 2 and not packet['flags'] & 16:
            previous = self.flows.get(key)
            if previous is None or previous.identity['initial_sequence'] != packet['sequence']:
                if len(self.flows) >= self.max_flows:
                    self.flows.popitem(last=False)
                self.flows[key] = Flow(packet, now)
        flow = self.flows.get(key)
        if flow is None:
            return []
        self.flows.move_to_end(key)
        flow.last_seen = now
        try:
            result = flow.feed(packet, max_stream_bytes=self.max_stream_bytes,
                               max_header_bytes=self.max_header_bytes, max_body_bytes=self.max_body_bytes)
        except RequestError as error:
            self.flows.pop(key)
            return [{'type': 'native-gateway-flow-invalid', 'schemaVersion': 1,
                     'at': datetime.datetime.fromtimestamp(packet['at'], datetime.timezone.utc).isoformat(),
                     'flow': dict(flow.identity), 'reason': str(error)}]
        if flow.upgraded or packet['flags'] & 5:
            self.flows.pop(key, None)
        return result


def observe(stream, emit, *, ready=None, observer=None):
    reader = PcapReader(stream)
    if ready is not None:
        ready()
    observer = observer if observer is not None else RequestObserver()
    for packet in reader:
        for marker in observer.feed(packet):
            emit(marker)


def main():
    READY.unlink(missing_ok=True)
    process = subprocess.Popen(
        ['/usr/bin/tcpdump', '-n', '-U', '-s', '0', '-w', '-', '-i', 'any',
         'tcp and (port 80 or port 8080 or port 5173)'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        observe(process.stdout, lambda row: print(json.dumps(row), flush=True),
                ready=lambda: READY.touch(mode=0o600, exist_ok=False))
        raise CaptureError('native packet capture ended')
    except CaptureError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    raise SystemExit(main())
