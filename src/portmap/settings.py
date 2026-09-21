from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import PortmapError


CONFIG_FILE_NAME = "portmap.toml"
DEFAULT_DNS_DOMAIN = "debug.lan"
DEFAULT_GATEWAY_NETWORK = "portmap_gateway"
AGENT_CONTAINER_SOCKET = "/run/portmap/agent.sock"

RUNTIME_AUTO = "auto"
RUNTIME_DOCKER = "docker"
RUNTIME_PODMAN = "podman"
RUNTIME_BACKENDS = (RUNTIME_AUTO, RUNTIME_DOCKER, RUNTIME_PODMAN)
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_PODMAN_SOCKET = "/run/podman/podman.sock"


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
    dns_forward: str
    gateway_network: str
    tcp_port_start: int
    udp_port_start: int
    range_port_start: int
    state_dir: Path
    host_ip: str
    agent_runtime_dir: Path
    runtime: str
    runtime_socket: str

    @property
    def docker_host(self) -> str:
        if "://" in self.runtime_socket:
            # Remote engine endpoint (tcp://, ssh://); pass through verbatim.
            return self.runtime_socket
        return f"unix://{self.runtime_socket}"

    @property
    def allocation_state_file(self) -> Path:
        return self.state_dir / "allocations.json"

    @property
    def agent_socket(self) -> Path:
        return self.agent_runtime_dir / "agent.sock"

    @property
    def agent_pid_file(self) -> Path:
        return self.agent_runtime_dir / "agent.pid"

    @property
    def agent_log_file(self) -> Path:
        return self.state_dir / "agent.log"

    @property
    def effective_dns_bind(self) -> str:
        if self.dns_bind in {"", "0.0.0.0", "::"}:
            return self.host_ip
        return self.dns_bind

    def gateway_env(self) -> dict[str, str]:
        return {
            "DOCKER_HOST": self.docker_host,
            "PORTMAP_RUNTIME": self.runtime,
            "PORTMAP_RUNTIME_SOCKET": self.runtime_socket if "://" not in self.runtime_socket else "",
            "PORTMAP_DOCKER_SOCKET": self.runtime_socket if "://" not in self.runtime_socket else DEFAULT_DOCKER_SOCKET,
            "PORTMAP_HTTP_BIND": self.http_bind,
            "PORTMAP_HTTP_PORT": str(self.http_port),
            "PORTMAP_CATALOG_BIND": self.catalog_bind,
            "PORTMAP_CATALOG_PORT": str(self.catalog_port),
            "PORTMAP_DNS_BIND": self.effective_dns_bind,
            "PORTMAP_DNS_PORT": str(self.dns_port),
            "PORTMAP_DNS_DOMAIN": self.dns_domain,
            "PORTMAP_DNS_TARGET_IP": self.host_ip,
            "PORTMAP_DNS_FORWARD": self.dns_forward,
            "PORTMAP_GATEWAY_NETWORK": self.gateway_network,
            "PORTMAP_STATE_DIR": str(self.state_dir),
            "PORTMAP_AGENT_RUNTIME_HOST_DIR": str(self.agent_runtime_dir),
            "PORTMAP_AGENT_SOCKET": AGENT_CONTAINER_SOCKET,
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
    agent = table(config, "agent")
    runtime_config = table(config, "runtime")

    runtime, runtime_socket = resolve_runtime(env, runtime_config)

    host_ip = string_env(env, "PORTMAP_HOST_IP") or string_env(env, "PORTMAP_DNS_TARGET_IP") or detect_host_ip()
    state_dir = expand_path(
        string_env(env, "PORTMAP_STATE_DIR")
        or string_value(state, "dir", "~/.local/state/portmap"),
        root=resolved_root,
    )
    agent_runtime_dir = expand_path(
        string_env(env, "PORTMAP_AGENT_RUNTIME_DIR")
        or string_env(env, "PORTMAP_AGENT_RUNTIME_HOST_DIR")
        or string_value(agent, "runtime_dir", default_agent_runtime_dir(env)),
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
        dns_forward=string_env(env, "PORTMAP_DNS_FORWARD") or string_value(gateway, "dns_forward", "/etc/resolv.conf"),
        gateway_network=(
            string_env(env, "PORTMAP_GATEWAY_NETWORK")
            or string_value(gateway, "network", DEFAULT_GATEWAY_NETWORK)
        ),
        tcp_port_start=int_env(env, "PORTMAP_TCP_PORT_START", int_value(ports, "tcp_start", 18000)),
        udp_port_start=int_env(env, "PORTMAP_UDP_PORT_START", int_value(ports, "udp_start", 19000)),
        range_port_start=int_env(env, "PORTMAP_RANGE_PORT_START", int_value(ports, "range_start", 49160)),
        state_dir=state_dir,
        host_ip=host_ip,
        agent_runtime_dir=agent_runtime_dir,
        runtime=runtime,
        runtime_socket=runtime_socket,
    )


def resolve_runtime(
    environ: Mapping[str, str],
    config: Mapping[str, Any],
) -> tuple[str, str]:
    """Resolve the container runtime name and its API socket path.

    Precedence:
    1. explicit socket (PORTMAP_RUNTIME_SOCKET env or [runtime] socket)
    2. explicit backend (PORTMAP_RUNTIME env or [runtime] backend)
    3. DOCKER_HOST env (unix sockets give a path; tcp/ssh/npipe values
       pass through verbatim so remote engines keep working)
    4. auto-detect by probing well-known sockets for liveness
    5. docker default
    """
    backend = (
        string_env(environ, "PORTMAP_RUNTIME")
        or string_value(config, "backend", RUNTIME_AUTO)
    ).strip().lower()
    if backend not in RUNTIME_BACKENDS:
        raise PortmapError(
            f"invalid runtime backend {backend!r}; expected one of: {', '.join(RUNTIME_BACKENDS)}"
        )

    explicit_socket = string_env(environ, "PORTMAP_RUNTIME_SOCKET") or (
        str(config["socket"]) if config.get("socket") is not None else None
    )
    if explicit_socket:
        socket_path = normalize_socket_path(explicit_socket)
        name = backend if backend != RUNTIME_AUTO else runtime_name_for_socket(socket_path)
        return name, socket_path

    if backend == RUNTIME_DOCKER:
        return RUNTIME_DOCKER, docker_host_value(environ) or DEFAULT_DOCKER_SOCKET
    if backend == RUNTIME_PODMAN:
        existing = podman_socket_path(environ, probe=True)
        return RUNTIME_PODMAN, existing or podman_socket_path(environ)

    detected = docker_host_value(environ)
    if detected:
        return runtime_name_for_socket(detected), detected
    if socket_alive(DEFAULT_DOCKER_SOCKET):
        return RUNTIME_DOCKER, DEFAULT_DOCKER_SOCKET
    podman_socket = podman_socket_path(environ, probe=True)
    if podman_socket:
        return RUNTIME_PODMAN, podman_socket
    return RUNTIME_DOCKER, DEFAULT_DOCKER_SOCKET


def normalize_socket_path(value: str) -> str:
    """Accept plain paths and the unix:// / unix: forms users copy from DOCKER_HOST."""
    raw = value.strip()
    for prefix in ("unix://", "unix:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return str(Path(raw).expanduser())


def docker_host_value(environ: Mapping[str, str]) -> str | None:
    """DOCKER_HOST as a socket path, or the raw value for remote engines."""
    value = string_env(environ, "DOCKER_HOST")
    if value is None:
        return None
    if value.startswith(("unix://", "unix:")):
        return normalize_socket_path(value)
    return value


def socket_alive(path: str, *, timeout: float = 0.3) -> bool:
    """True when a unix socket path accepts a connection.

    Stale socket files (e.g. dockerd stopped but /var/run/docker.sock
    left behind) must not count as a live runtime.
    """
    if not Path(path).exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout)
            probe.connect(path)
        return True
    except OSError:
        return False


def podman_socket_path(environ: Mapping[str, str], *, probe: bool = False) -> str | None:
    candidates = [DEFAULT_PODMAN_SOCKET]
    runtime_dir = string_env(environ, "XDG_RUNTIME_DIR")
    if runtime_dir:
        candidates.append(str(Path(runtime_dir) / "podman" / "podman.sock"))
    for candidate in candidates:
        if not probe or socket_alive(candidate):
            return candidate
    return candidates[0] if not probe else None


def runtime_name_for_socket(socket_path: str) -> str:
    return RUNTIME_PODMAN if "podman" in socket_path else RUNTIME_DOCKER


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


def default_agent_runtime_dir(env: Mapping[str, str]) -> str:
    if runtime_dir := string_env(env, "XDG_RUNTIME_DIR"):
        return str(Path(runtime_dir) / "portmap")
    return f"/tmp/portmap-{os.getuid()}"
