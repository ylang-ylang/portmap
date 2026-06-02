from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .errors import ComposeError
from .model import ComposeConfig, ComposePort, ComposeService


def load_compose_config(
    *,
    compose_file: Path | None,
    compose_json_file: Path | None,
    project_directory: Path,
) -> ComposeConfig:
    if compose_file and compose_json_file:
        raise ComposeError("use either compose_file or compose_json_file, not both")
    if compose_json_file:
        return parse_compose_config(json.loads(compose_json_file.read_text(encoding="utf-8")))
    if not compose_file:
        raise ComposeError("compose_file is required")
    return parse_compose_config(render_compose_json(compose_file, project_directory))


def render_compose_json(compose_file: Path, project_directory: Path) -> dict[str, Any]:
    cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "config",
        "--format",
        "json",
    ]
    result = subprocess.run(
        cmd,
        cwd=project_directory,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise ComposeError(result.stderr.strip() or "docker compose config failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ComposeError(f"docker compose produced invalid JSON: {exc}") from exc
    return payload


def parse_compose_config(payload: dict[str, Any]) -> ComposeConfig:
    raw_services = payload.get("services")
    if not isinstance(raw_services, dict):
        raise ComposeError("compose config does not contain a services object")

    services: dict[str, ComposeService] = {}
    for name, raw in raw_services.items():
        if not isinstance(raw, dict):
            continue
        services[name] = parse_service(name, raw)

    networks = parse_networks(payload.get("networks", {}))
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        name = None
    return ComposeConfig(name=name, services=services, networks=networks, raw=payload)


def parse_service(name: str, raw: dict[str, Any]) -> ComposeService:
    return ComposeService(
        name=name,
        expose=tuple(str(item) for item in raw.get("expose", []) or []),
        ports=tuple(parse_port(item) for item in raw.get("ports", []) or [] if isinstance(item, dict)),
        labels=parse_labels(raw.get("labels")),
        networks=parse_service_networks(raw.get("networks")),
        network_mode=raw.get("network_mode") if isinstance(raw.get("network_mode"), str) else None,
    )


def parse_port(raw: dict[str, Any]) -> ComposePort:
    target = raw.get("target")
    if not isinstance(target, int):
        raise ComposeError(f"compose port target must be an integer: {raw!r}")
    published = raw.get("published")
    protocol = raw.get("protocol", "tcp")
    host_ip = raw.get("host_ip")
    return ComposePort(
        target=target,
        published=str(published) if published is not None else None,
        protocol=str(protocol or "tcp").lower(),
        host_ip=str(host_ip) if host_ip is not None else None,
    )


def parse_labels(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, list):
        return tuple(str(item) for item in raw)
    if isinstance(raw, dict):
        return tuple(f"{key}={value}" for key, value in raw.items())
    return ()


def parse_service_networks(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, list):
        return tuple(str(item) for item in raw)
    if isinstance(raw, dict):
        return tuple(str(key) for key in raw.keys())
    return ()


def parse_networks(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    networks: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            networks[str(key)] = value["name"]
        else:
            networks[str(key)] = str(key)
    return networks


def choose_traefik_network(compose: ComposeConfig, service_name: str) -> str:
    service = compose.services.get(service_name)
    if service is None:
        raise ComposeError(f"endpoint references unknown service: {service_name}")
    if service.network_mode == "host":
        raise ComposeError(f"service uses network_mode=host and cannot be managed: {service_name}")
    network_key = service.networks[0] if service.networks else next(iter(compose.networks), "default")
    if network_key in compose.networks:
        return compose.networks[network_key]
    if compose.name:
        return f"{compose.name}_{network_key}"
    return network_key

