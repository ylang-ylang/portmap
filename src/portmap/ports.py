from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path

from .errors import PortAllocationError


@dataclass
class PortAllocator:
    state_file: Path
    host_ip: str = "127.0.0.1"
    state: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        else:
            self.state = {}
        self.state.setdefault("allocations", {}).setdefault("tcp", {})
        self.state.setdefault("allocations", {}).setdefault("udp", {})
        self.state.setdefault("ranges", {}).setdefault("tcp", {})
        self.state.setdefault("ranges", {}).setdefault("udp", {})

    def allocate(self, *, protocol: str, key: str, preferred: int | None, start: int) -> int:
        protocol = protocol.lower()
        allocations = self.state.setdefault("allocations", {}).setdefault(protocol, {})
        if preferred is not None:
            allocations[key] = preferred
            return preferred
        current = allocations.get(key)
        if isinstance(current, int):
            return current
        used = self.used_ports(protocol)
        port = start
        while port in used or not port_available(self.host_ip, port, protocol):
            port += 1
        allocations[key] = port
        return port

    def allocate_range(
        self,
        *,
        protocol: str,
        key: str,
        preferred_start: int | None,
        start: int,
        size: int,
    ) -> tuple[int, int]:
        protocol = protocol.lower()
        ranges = self.state.setdefault("ranges", {}).setdefault(protocol, {})
        if preferred_start is not None:
            ranges[key] = {"start": preferred_start, "size": size}
            return preferred_start, preferred_start + size - 1

        current = ranges.get(key)
        if isinstance(current, dict):
            current_start = current.get("start")
            current_size = current.get("size")
            if isinstance(current_start, int) and current_size == size:
                return current_start, current_start + size - 1

        port = start
        while True:
            end = port + size - 1
            candidates = range(port, end + 1)
            if self.range_available(protocol, candidates):
                ranges[key] = {"start": port, "size": size}
                return port, end
            port += size
            if port > 65535:
                raise PortAllocationError(
                    f"unable to allocate {size} contiguous {protocol} ports from {start}"
                )

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def has_allocations(self) -> bool:
        allocations = self.state.get("allocations", {})
        ranges = self.state.get("ranges", {})
        return any(bool(allocations.get(protocol)) or bool(ranges.get(protocol)) for protocol in ("tcp", "udp"))

    def delete_if_empty(self) -> None:
        if not self.has_allocations():
            self.state_file.unlink(missing_ok=True)

    def used_ports(self, protocol: str) -> set[int]:
        allocations = self.state.setdefault("allocations", {}).setdefault(protocol, {})
        used = {value for value in allocations.values() if isinstance(value, int)}
        ranges = self.state.setdefault("ranges", {}).setdefault(protocol, {})
        for value in ranges.values():
            if not isinstance(value, dict):
                continue
            start = value.get("start")
            size = value.get("size")
            if isinstance(start, int) and isinstance(size, int):
                used.update(range(start, start + size))
        return used

    def range_available(self, protocol: str, ports: range) -> bool:
        used = self.used_ports(protocol)
        return all(port not in used and port_available(self.host_ip, port, protocol) for port in ports)


def port_available(host: str, port: int, protocol: str) -> bool:
    if protocol == "udp":
        return udp_port_available(host, port)
    return tcp_port_available(host, port)


def tcp_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.1)
        return sock.connect_ex((host, port)) != 0


def udp_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True
