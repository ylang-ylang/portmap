from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class EndpointKind(StrEnum):
    HTTP = "http"
    TCP = "tcp"
    UDP = "udp"
    RANGE = "range"


@dataclass(frozen=True)
class EndpointDeclaration:
    name: str
    order: int
    kind: EndpointKind
    service: str
    container_port: int
    protocol: str | None = None
    host: str | None = None
    host_port: int | None = None
    range_size: int | None = None
    range_start: int | None = None
    preserve_host: bool = False
    upstream_host: str | None = None


@dataclass(frozen=True)
class ComposePort:
    target: int
    published: str | None
    protocol: str
    host_ip: str | None = None


@dataclass(frozen=True)
class ComposeService:
    name: str
    expose: tuple[str, ...] = ()
    ports: tuple[ComposePort, ...] = ()
    labels: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    network_mode: str | None = None


@dataclass(frozen=True)
class ComposeConfig:
    name: str | None
    services: dict[str, ComposeService]
    networks: dict[str, str]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerateRequest:
    compose_file: Path | None
    compose_json_file: Path | None
    project_directory: Path
    out_dir: Path
    config_file: Path | None
    branch: str | None = None
    repo_root: Path | None = None
    repo_id: str | None = None
    repo_name: str | None = None
    http_port: int = 8080
    tcp_port_start: int = 18000
    udp_port_start: int = 19000
    range_port_start: int = 49160
    host_ip: str = "127.0.0.1"
    domain_suffix: str = "debug.lan"
    gateway_network: str = "portmap_gateway"
    container_dns_server: str | None = None
    allocation_state_file: Path | None = None
    compose_project: str | None = None
