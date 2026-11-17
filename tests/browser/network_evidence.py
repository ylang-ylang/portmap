"""Actual syscall/packet evidence; emulator NIC capture does not see guest loopback."""
import json
from pathlib import Path
import struct

ADMISSION_HOST = '127.0.0.1'
ADMISSION_PORT = 9


def desktop(artifacts):
    traces = sorted(Path(artifacts).glob('desktop-network.*'))
    assert traces, 'No actual desktop connect/execve capture'
    events = attempts = 0
    observed_direct = observed_ssh = False
    for trace in traces:
        for line in trace.read_text(errors='replace').splitlines():
            events += 1
            if not line.startswith('connect('):
                continue
            is_local = 'sin_addr=inet_addr("127.0.0.1")' in line or 'inet_pton(AF_INET6, "::ffff:127.0.0.1",' in line
            is_admission_port = 'sin_port=htons(9)' in line or 'sin6_port=htons(9)' in line
            attempts += int(is_local and is_admission_port)
            observed_direct |= 'sin_port=htons(8080)' in line
            observed_ssh |= 'sin_port=htons(2222)' in line
    assert attempts == 0, 'Firefox connected to the actual blocked-admission endpoint'
    assert observed_direct and observed_ssh, 'Recorder did not observe direct HTTP and SSH'
    result = {'recorder':'strace-connect-execve','events':events,
              'admission_address':ADMISSION_HOST,'admission_port':ADMISSION_PORT,
              'admission_attempts':attempts,'observed_direct_http':observed_direct,'observed_ssh':observed_ssh}
    (Path(artifacts) / 'network-audit.json').write_text(json.dumps(result, indent=2))


def android(artifacts):
    packets = ip_packets = 0
    destinations = set()
    with (Path(artifacts) / 'android-network.pcap').open('rb') as capture:
        header = capture.read(24)
        assert len(header) == 24, 'Native emulator capture missing header'
        endian = {b'\xd4\xc3\xb2\xa1':'<',b'\xa1\xb2\xc3\xd4':'>',
                  b'\x4d\x3c\xb2\xa1':'<',b'\xa1\xb2\x3c\x4d':'>'}.get(header[:4])
        assert endian, 'Unknown native packet format'
        assert struct.unpack(endian+'I',header[20:24])[0] == 1, 'Expected Ethernet capture'
        while entry := capture.read(16):
            assert len(entry) == 16, 'Truncated packet record'
            size = struct.unpack(endian+'IIII',entry)[2]
            frame = capture.read(size)
            assert len(frame) == size and size >= 14, 'Truncated Ethernet frame'
            packets += 1
            protocol, offset = int.from_bytes(frame[12:14],'big'), 14
            if protocol == 0x8100:
                assert len(frame) >= 18, 'Truncated VLAN frame'
                protocol, offset = int.from_bytes(frame[16:18],'big'), 18
            if protocol == 0x0800:
                assert len(frame) >= offset+20, 'Truncated IPv4 frame'
                ip_packets += 1
                tcp = offset+(frame[offset]&15)*4
                if frame[offset+9] == 6 and len(frame) >= tcp+4:
                    destinations.add(int.from_bytes(frame[tcp+2:tcp+4],'big'))
            elif protocol == 0x86DD:
                assert len(frame) >= offset+40, 'Truncated IPv6 frame'
                ip_packets += 1
                if frame[offset+6] == 6 and len(frame) >= offset+44:
                    destinations.add(int.from_bytes(frame[offset+42:offset+44],'big'))
    assert ip_packets, 'No actual guest IP traffic recorded'
    assert 8080 in destinations and 2222 in destinations, 'Capture did not observe direct HTTP and SSH'
    result = {'recorder':'android-emulator-native-pcap','packets':packets,'ip_packets':ip_packets,
              'scope':'emulator-external-interface','loopback_admission_observable':False,
              'observed_direct_http':True,'observed_ssh':True}
    (Path(artifacts) / 'network-audit.json').write_text(json.dumps(result, indent=2))
