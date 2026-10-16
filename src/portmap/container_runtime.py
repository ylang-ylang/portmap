"""Local Docker/Podman operations shared by the infrastructure controller.

The compatibility API covers resource operations, not host networking or user
namespaces. Runtime identity and container UID mapping are resolved explicitly.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .client_runtime import native_environment
from .errors import PortmapError
from .settings import PortmapSettings

OWNER_LABEL = "portmap.infrastructure.owner"
STATE_LABEL = "portmap.infrastructure.state"
UID_LABEL = "portmap.infrastructure.uid"


@lru_cache(maxsize=1)
def compose_entrypoint() -> tuple[str, ...]:
    """Select the Docker Compose CLI once per process.

    The ``docker compose`` plugin is preferred when it is usable. Hosts whose
    package manager installs the standalone ``docker-compose`` client without
    wiring ``~/.docker/cli-plugins`` (a plain Homebrew install without the
    setup helper, for example) get that client instead -- no user-level docker
    configuration is ever written here. The selection is made from version
    probes only: a selected command that later fails on real compose work
    raises exactly that failure and is never retried on the other command.
    """
    environment = native_environment()
    if shutil.which("docker") is not None:
        probe = subprocess.run(
            ["docker", "compose", "version", "--short"],
            capture_output=True, text=True, env=environment, check=False,
        )
        if probe.returncode == 0:
            return ("docker", "compose")
    standalone = shutil.which("docker-compose")
    if standalone is not None:
        probe = subprocess.run(
            [standalone, "version", "--short"],
            capture_output=True, text=True, env=environment, check=False,
        )
        if probe.returncode == 0:
            return (standalone,)
    raise PortmapError(
        "portmap setup requires a usable Docker Compose CLI: neither the "
        "`docker compose` plugin nor a standalone docker-compose client is available"
    )

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


@dataclass(frozen=True)
class ContainerRuntime:
    backend: str
    socket_path: str
    rootless: bool

    @classmethod
    def connect(cls, settings: PortmapSettings) -> ContainerRuntime:
        if "://" in settings.runtime_socket or not Path(settings.runtime_socket).is_absolute():
            raise PortmapError("portmap infrastructure requires a local Docker/Podman socket; use connect for remote machines")
        endpoint = cls(settings.runtime, settings.runtime_socket, False)
        info = endpoint.request("GET", "/info")
        if not isinstance(info, dict):
            raise PortmapError("the container runtime returned invalid engine information")
        if settings.runtime == "podman":
            # Libpod reports the actual security mode; socket spelling alone
            # cannot distinguish rootful and rootless services.
            native = endpoint.request("GET", "/v4.0.0/libpod/info")
            try:
                rootless = native["host"]["security"]["rootless"]
            except (KeyError, TypeError) as exc:
                raise PortmapError("Podman did not report its rootless security mode") from exc
            if type(rootless) is not bool:
                raise PortmapError("Podman returned an invalid rootless security mode")
        else:
            rootless = any("rootless" in option for option in info.get("SecurityOptions", []) if isinstance(option, str))
        return cls(settings.runtime, settings.runtime_socket, rootless)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ContainerRuntime:
        backend, path, rootless = record.get("backend"), record.get("socket_path"), record.get("rootless")
        if backend not in {"docker", "podman"} or not isinstance(path, str) or not Path(path).is_absolute() or type(rootless) is not bool:
            raise PortmapError("invalid recorded container runtime")
        return cls(backend, path, rootless)

    def record(self) -> dict[str, Any]:
        return {"backend": self.backend, "socket_path": self.socket_path, "rootless": self.rootless}

    @property
    def file_user(self) -> str:
        # Root inside a rootless user namespace maps to the invoking host UID;
        # Docker/rootful Podman instead need that UID explicitly for private
        # host sockets and files. Neither case runs a host worker as root.
        return "0:0" if self.rootless else f"{os.getuid()}:{os.getgid()}"

    @cached_property
    def supports_network_mac_address(self) -> bool:
        version = self.request("GET", "/version")
        value = version.get("ApiVersion") if isinstance(version, dict) else None
        parts = value.split(".") if isinstance(value, str) else []
        if len(parts) != 2 or not all(part.isdecimal() for part in parts):
            raise PortmapError("the container runtime did not report a valid Docker API version")
        return tuple(map(int, parts)) >= (1, 44)

    def request(self, method: str, path: str, payload: Any = None, *, statuses: tuple[int, ...] = (200,), timeout: float = 15) -> Any:
        connection = _UnixConnection(self.socket_path, timeout)
        body = None if payload is None else json.dumps(payload).encode()
        headers = {} if body is None else {"Content-Type": "application/json"}
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            if response.status not in statuses:
                try:
                    detail = json.loads(raw).get("message", "")
                except (ValueError, AttributeError):
                    detail = raw.decode(errors="replace")
                raise PortmapError(f"{self.backend} API {method} {path} returned {response.status}: {str(detail)[:2000]}")
            return json.loads(raw) if raw else None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise PortmapError(f"cannot use {self.backend} at {self.socket_path}: {exc}; start the configured container runtime before portmap setup") from exc
        finally:
            connection.close()

    def inspect_container(self, identity: str) -> dict[str, Any] | None:
        result = self.request("GET", f"/containers/{quote(identity, safe='')}/json", statuses=(200, 404))
        return result if isinstance(result, dict) and result.get("Id") else None

    def inspect_network(self, identity: str) -> dict[str, Any] | None:
        result = self.request("GET", f"/networks/{quote(identity, safe='')}", statuses=(200, 404))
        return result if isinstance(result, dict) and result.get("Id") else None

    def ensure_network(self, name: str, labels: dict[str, str]) -> tuple[dict[str, Any], bool]:
        existing = self.inspect_network(name)
        created = existing is None
        if created:
            result = self.request("POST", "/networks/create", {"Name": name, "Driver": "bridge", "EnableIPv6": True, "CheckDuplicate": True, "Labels": labels}, statuses=(201,))
            existing = self.inspect_network(result["Id"])
        if not existing or existing.get("Name") != name or existing.get("Driver") != "bridge":
            raise PortmapError(f"the configured gateway network {name!r} must be a local bridge network")
        return existing, created

    def reserve_addresses(self, network: Mapping[str, Any], roles: Mapping[str, Any]) -> dict[str, str]:
        """Pick fixed high-end addresses; the engine arbitrates creation races."""
        subnet = None
        used: set[ipaddress.IPv4Address] = set()
        try:
            for entry in network.get("IPAM", {}).get("Config", []):
                candidate = ipaddress.ip_network(entry["Subnet"])
                if candidate.version == 4 and subnet is None:
                    subnet = candidate
                    if entry.get("Gateway"):
                        used.add(ipaddress.IPv4Address(entry["Gateway"]))
                    for value in (entry.get("AuxiliaryAddresses") or {}).values():
                        used.add(ipaddress.IPv4Address(value))
            for endpoint in (network.get("Containers") or {}).values():
                if endpoint.get("IPv4Address"):
                    used.add(ipaddress.IPv4Interface(endpoint["IPv4Address"]).ip)
            for container in self.network_containers(network["Id"]):
                endpoint = (container.get("NetworkSettings", {}).get("Networks") or {}).get(network["Name"], {})
                for value in (endpoint.get("IPAddress"), (endpoint.get("IPAMConfig") or {}).get("IPv4Address")):
                    if value:
                        used.add(ipaddress.IPv4Address(value))
        except (ValueError, KeyError, TypeError) as exc:
            raise PortmapError("the runtime returned invalid gateway-network address allocations") from exc
        if subnet is None or subnet.num_addresses < len(roles) + 3:
            raise PortmapError("the gateway bridge needs an IPv4 subnet with room for portmap infrastructure")
        result = {}
        address = int(subnet.broadcast_address) - 1
        for role in roles:
            while address > int(subnet.network_address) and ipaddress.IPv4Address(address) in used:
                address -= 1
            if address <= int(subnet.network_address):
                raise PortmapError("no free gateway-network address remains for portmap infrastructure")
            result[role] = str(ipaddress.IPv4Address(address))
            used.add(ipaddress.IPv4Address(address))
            address -= 1
        return result

    def network_containers(self, identity: str) -> list[dict[str, Any]]:
        filters = quote(json.dumps({"network": [identity]}, separators=(",", ":")), safe="")
        result = self.request("GET", f"/containers/json?all=1&filters={filters}")
        if not isinstance(result, list):
            raise PortmapError("the runtime returned an invalid network consumer inventory")
        return result

    def owned_containers(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        filters = quote(json.dumps({"label": [f"{key}={value}" for key, value in labels.items()]}, separators=(",", ":")), safe="")
        containers = self.request("GET", f"/containers/json?all=1&filters={filters}")
        if not isinstance(containers, list):
            raise PortmapError("container runtime returned an invalid container inventory")
        return [item for item in containers if all((item.get("Labels") or {}).get(key) == value for key, value in labels.items())]

    def require_owned(self, identity: str, labels: Mapping[str, str]) -> dict[str, Any] | None:
        container = self.inspect_container(identity)
        if container is not None and any((container.get("Config", {}).get("Labels") or {}).get(key) != value for key, value in labels.items()):
            raise PortmapError(f"refusing to change container {identity}: infrastructure ownership does not match")
        return container

    def remove_owned(self, identity: str, labels: Mapping[str, str]) -> None:
        if self.require_owned(identity, labels) is not None:
            self.request("DELETE", f"/containers/{quote(identity, safe='')}?force=1&v=0", statuses=(204, 404), timeout=30)

    def signal_owned(self, identity: str, labels: Mapping[str, str], signal: str) -> None:
        if self.require_owned(identity, labels) is None:
            raise PortmapError("the owned container disappeared before its configuration reload")
        self.request("POST", f"/containers/{quote(identity, safe='')}/kill?signal={quote(signal, safe='')}", statuses=(204,))

    def remove_empty_network(self, identity: str, labels: Mapping[str, str]) -> bool:
        network = self.inspect_network(identity)
        if network is None:
            return True
        if any((network.get("Labels") or {}).get(key) != value for key, value in labels.items()):
            return False
        if network.get("Containers") or self.network_containers(identity):
            return False
        self.request("DELETE", f"/networks/{quote(identity, safe='')}", statuses=(204, 404))
        return True

    def compose(self, path: Path, arguments: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
        entrypoint = compose_entrypoint()
        environment = native_environment()
        environment.pop("DOCKER_CONTEXT", None)
        environment.update({"DOCKER_HOST": f"unix://{self.socket_path}", "PORTMAP_BROKER_BYPASS": "1"})
        try:
            result = subprocess.run([*entrypoint, "-f", str(path), *arguments], env=environment, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None, check=False)
        except OSError as exc:
            raise PortmapError(f"cannot run Docker Compose for {self.backend}: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise PortmapError(f"container infrastructure compose {' '.join(arguments)} failed" + (f": {detail}" if detail else ""))
        return result


def container_address(container: Mapping[str, Any], network: str) -> str:
    try:
        address = ipaddress.IPv4Address(container["NetworkSettings"]["Networks"][network]["IPAddress"])
    except (KeyError, TypeError, ipaddress.AddressValueError) as exc:
        raise PortmapError(f"container has no IPv4 address on gateway network {network}") from exc
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise PortmapError("the container runtime returned an unusable gateway address")
    return str(address)
