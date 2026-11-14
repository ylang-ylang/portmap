"""Inspect actual traced syscalls or native emulator packets, never browser source."""
import json
from pathlib import Path
import struct

SENTINEL = "192.0.2.1"
SENTINEL_BYTES = bytes((192, 0, 2, 1))


def desktop(artifacts):
    traces = sorted(Path(artifacts).glob("desktop-network.*"))
    assert traces, "No actual desktop network syscall capture"
    events = 0
    observed_proxy = False
    for trace in traces:
        for line in trace.read_text(errors="replace").splitlines():
            events += 1
            # AF_UNIX IPC carries the actual ProxyInfo object too. Inspect
            # decoded network sockaddr fields, never arbitrary message bodies.
            assert ('sin_addr=inet_addr("' + SENTINEL + '")') not in line and ('inet_pton(AF_INET6, "::ffff:' + SENTINEL + '",') not in line, "Firefox attempted admission sentinel connection: " + line
            observed_proxy |= line.startswith("connect(") and "sin_port=htons(18080)" in line
    assert observed_proxy, "Syscall recorder did not observe actual node proxy connection"
    result = {"recorder": "strace-network-syscalls", "events": events, "sentinel_attempts": 0}
    (Path(artifacts) / "network-audit.json").write_text(json.dumps(result, indent=2))


def android(artifacts):
    packets = ip_packets = 0
    with (Path(artifacts) / "android-network.pcap").open("rb") as capture:
        header = capture.read(24)
        assert len(header) == 24, "Native emulator packet capture is missing its header"
        endian = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">", b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">"}.get(header[:4])
        assert endian, "Unknown native packet capture format"
        assert struct.unpack(endian + "I", header[20:24])[0] == 1, "Native packet recorder must capture Ethernet"
        while entry := capture.read(16):
            assert len(entry) == 16, "Truncated native packet record"
            size = struct.unpack(endian + "IIII", entry)[2]
            frame = capture.read(size)
            assert len(frame) == size and size >= 14, "Truncated native packet frame"
            packets += 1
            protocol = int.from_bytes(frame[12:14], "big")
            offset = 14
            if protocol == 0x8100:
                assert len(frame) >= 18, "Truncated VLAN frame"
                protocol, offset = int.from_bytes(frame[16:18], "big"), 18
            if protocol == 0x0800:
                assert len(frame) >= offset + 20, "Truncated IPv4 packet"
                ip_packets += 1
                assert SENTINEL_BYTES not in (frame[offset + 12:offset + 16], frame[offset + 16:offset + 20]), "Actual Android packet reached admission sentinel"
            elif protocol == 0x86DD:
                assert len(frame) >= offset + 40, "Truncated IPv6 packet"
                ip_packets += 1
                mapped = b"\0" * 10 + b"\xff\xff" + SENTINEL_BYTES
                assert mapped not in (frame[offset + 8:offset + 24], frame[offset + 24:offset + 40]), "Actual Android IPv6 packet reached admission sentinel"
            elif protocol == 0x0806:
                assert len(frame) >= offset + 28, "Truncated ARP packet"
                assert frame[offset + 24:offset + 28] != SENTINEL_BYTES, "Actual Android ARP attempted admission sentinel"
    assert ip_packets, "Native packet capture did not observe actual IP traffic"
    result = {"recorder": "android-emulator-native-pcap", "packets": packets, "ip_packets": ip_packets, "sentinel_attempts": 0}
    (Path(artifacts) / "network-audit.json").write_text(json.dumps(result, indent=2))
