from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


CONFIG_FILE_NAME = "portmap.toml"
DEFAULT_DNS_DOMAIN = "debug.lan"
DEFAULT_GATEWAY_NETWORK = "portmap_gateway"


@dataclass(frozen=True)
class PortmapSettings:
    root: Path
    http_bind: str
    http_port: int
    catalog_bind: str
    catalog_port: int
    dns_bind: str
    dns_port: int
    dns_domain: str
    gateway_network: str
    tcp_port_start: int
    udp_port_start: int
    range_port_start: int
    state_dir: Path
    host_ip: str

    @property
    def allocation_state_file(self) -> Path:
        return self.state_dir / "allocations.json"

    @property
    def effective_dns_bind(self) -> str:
        if self.dns_bind in {"", "0.0.0.0", "::"}:
            return self.host_ip
        return self.dns_bind

    def gateway_env(self) -> dict[str, str]:
        return {
            "PORTMAP_HTTP_BIND": self.http_bind,
            "PORTMAP_HTTP_PORT": str(self.http_port),
            "PORTMAP_CATALOG_BIND": self.catalog_bind,
            "PORTMAP_CATALOG_PORT": str(self.catalog_port),
            "PORTMAP_DNS_BIND": self.effective_dns_bind,
            "PORTMAP_DNS_PORT": str(self.dns_port),
            "PORTMAP_DNS_DOMAIN": self.dns_domain,
            "PORTMAP_DNS_TARGET_IP": self.host_ip,
            "PORTMAP_GATEWAY_NETWORK": self.gateway_network,
            "PORTMAP_STATE_DIR": str(self.state_dir),
        }


def load_portmap_settings(
    *,
    environ: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> PortmapSettings:
    env = os.environ if environ is None else environ
    resolved_root = resolve_portmap_root(environ=env, root=root)
    config = load_config_file(resolved_root / CONFIG_FILE_NAME)
    gateway = table(config, "gateway")
    ports = table(config, "ports")
    state = table(config, "state")

    host_ip = string_env(env, "PORTMAP_HOST_IP") or string_env(env, "PORTMAP_DNS_TARGET_IP") or detect_host_ip()
    state_dir = expand_path(
        string_env(env, "PORTMAP_STATE_DIR")
        or string_value(state, "dir", "~/.local/state/portmap"),
        root=resolved_root,
    )
    return PortmapSettings(
        root=resolved_root,
        http_bind=string_env(env, "PORTMAP_HTTP_BIND") or string_value(gateway, "http_bind", "0.0.0.0"),
        http_port=int_env(env, "PORTMAP_HTTP_PORT", int_value(gateway, "http_port", 8080)),
        catalog_bind=string_env(env, "PORTMAP_CATALOG_BIND") or string_value(gateway, "catalog_bind", "0.0.0.0"),
        catalog_port=int_env(env, "PORTMAP_CATALOG_PORT", int_value(gateway, "catalog_port", 80)),
        dns_bind=string_env(env, "PORTMAP_DNS_BIND") or string_value(gateway, "dns_bind", "0.0.0.0"),
        dns_port=int_env(env, "PORTMAP_DNS_PORT", int_value(gateway, "dns_port", 53)),
        dns_domain=(
            string_env(env, "PORTMAP_DOMAIN_SUFFIX")
            or string_env(env, "PORTMAP_DNS_DOMAIN")
            or string_value(gateway, "dns_domain", DEFAULT_DNS_DOMAIN)
        ).strip("."),
        gateway_network=(
            string_env(env, "PORTMAP_GATEWAY_NETWORK")
            or string_value(gateway, "network", DEFAULT_GATEWAY_NETWORK)
        ),
        tcp_port_start=int_env(env, "PORTMAP_TCP_PORT_START", int_value(ports, "tcp_start", 18000)),
        udp_port_start=int_env(env, "PORTMAP_UDP_PORT_START", int_value(ports, "udp_start", 19000)),
        range_port_start=int_env(env, "PORTMAP_RANGE_PORT_START", int_value(ports, "range_start", 49160)),
        state_dir=state_dir,
        host_ip=host_ip,
    )


def resolve_portmap_root(
    *,
    environ: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> Path:
    if root is not None:
        return root.expanduser().resolve()
    env = os.environ if environ is None else environ
    if raw_root := string_env(env, "PORTMAP_ROOT"):
        return Path(raw_root).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def load_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    return payload if isinstance(payload, dict) else {}


def detect_host_ip() -> str:
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((host, 53))
                candidate = str(sock.getsockname()[0])
                if candidate and not candidate.startswith("127."):
                    return candidate
        except OSError:
            continue
    try:
        candidate = socket.gethostbyname(socket.gethostname())
        if candidate and not candidate.startswith("127."):
            return candidate
    except OSError:
        pass
    return "127.0.0.1"


def table(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key)
    return value if isinstance(value, Mapping) else {}


def string_value(config: Mapping[str, Any], key: str, default: str) -> str:
    value = config.get(key)
    if value is None:
        return default
    return str(value)


def int_value(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key)
    if value is None:
        return default
    return int(value)


def string_env(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None or value.strip() == "":
        return None
    return value


def int_env(env: Mapping[str, str], key: str, default: int) -> int:
    value = string_env(env, key)
    if value is None:
        return default
    return int(value)


def expand_path(value: str, *, root: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return root / path
