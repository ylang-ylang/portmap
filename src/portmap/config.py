from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .model import EndpointDeclaration, EndpointKind


def load_endpoint_declarations(path: Path | None) -> list[EndpointDeclaration]:
    if path is None:
        raise ConfigError("endpoint config is required for generate")
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_endpoints = payload.get("endpoints")
    if not isinstance(raw_endpoints, dict):
        raise ConfigError("config must contain [endpoints.<name>] tables")
    endpoints = [
        parse_endpoint(name, value, order=index)
        for index, (name, value) in enumerate(raw_endpoints.items())
    ]
    if not endpoints:
        raise ConfigError("config contains no endpoints")
    return endpoints


def parse_endpoint(name: str, raw: Any, *, order: int) -> EndpointDeclaration:
    if not isinstance(raw, dict):
        raise ConfigError(f"endpoint {name!r} must be a table")
    kind_value = raw.get("kind")
    service = raw.get("service")
    container_port = raw.get("container_port")
    if not isinstance(kind_value, str):
        raise ConfigError(f"endpoint {name!r} missing string kind")
    if not isinstance(service, str):
        raise ConfigError(f"endpoint {name!r} missing string service")
    if not isinstance(container_port, int):
        raise ConfigError(f"endpoint {name!r} missing integer container_port")
    try:
        kind = EndpointKind(kind_value)
    except ValueError as exc:
        raise ConfigError(f"endpoint {name!r} has unsupported kind: {kind_value}") from exc

    host = raw.get("host")
    host_port = raw.get("host_port")
    protocol = raw.get("protocol")
    range_size = raw.get("range_size")
    range_start = raw.get("range_start")
    preserve_host = raw.get("preserve_host", False)
    upstream_host = raw.get("upstream_host")
    if protocol is not None and str(protocol).lower() not in {"tcp", "udp"}:
        raise ConfigError(f"endpoint {name!r} protocol must be tcp or udp")
    if kind == EndpointKind.RANGE:
        if not isinstance(range_size, int) or range_size <= 0:
            raise ConfigError(f"endpoint {name!r} range_size must be a positive integer")
        if range_start is not None and (not isinstance(range_start, int) or range_start <= 0):
            raise ConfigError(f"endpoint {name!r} range_start must be a positive integer")
    if not isinstance(preserve_host, bool):
        raise ConfigError(f"endpoint {name!r} preserve_host must be a boolean")
    if upstream_host is not None and not isinstance(upstream_host, str):
        raise ConfigError(f"endpoint {name!r} upstream_host must be a string")
    return EndpointDeclaration(
        name=name,
        order=order,
        kind=kind,
        service=service,
        container_port=container_port,
        protocol=str(protocol).lower() if protocol is not None else None,
        host=str(host) if host is not None else None,
        host_port=host_port if isinstance(host_port, int) else None,
        range_size=range_size if isinstance(range_size, int) else None,
        range_start=range_start if isinstance(range_start, int) else None,
        preserve_host=preserve_host,
        upstream_host=upstream_host.strip() if isinstance(upstream_host, str) and upstream_host.strip() else None,
    )
