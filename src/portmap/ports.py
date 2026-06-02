from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PortAllocator:
    state_file: Path
    host_ip: str = "127.0.0.1"
    state: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        else:
            self.state = {"allocations": {"tcp": {}, "udp": {}}}

    def allocate(self, *, protocol: str, key: str, preferred: int | None, start: int) -> int:
        protocol = protocol.lower()
        allocations = self.state.setdefault("allocations", {}).setdefault(protocol, {})
        if preferred is not None:
            allocations[key] = preferred
            return preferred
        current = allocations.get(key)
        if isinstance(current, int):
            return current
        used = {value for value in allocations.values() if isinstance(value, int)}
        port = start
        while port in used or not port_available(self.host_ip, port, protocol):
            port += 1
        allocations[key] = port
        return port

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def has_allocations(self) -> bool:
        allocations = self.state.get("allocations", {})
        return any(bool(allocations.get(protocol)) for protocol in ("tcp", "udp"))

    def delete_if_empty(self) -> None:
        if not self.has_allocations():
            self.state_file.unlink(missing_ok=True)


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
