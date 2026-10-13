"""One configuration model for Docker and Podman infrastructure."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from .client_dns import DNS_ADDRESS, render_corefile
from .client_runtime import frozen
from .container_runtime import ContainerRuntime, OWNER_LABEL, STATE_LABEL, UID_LABEL
from .errors import PortmapError
from .settings import pythonpath_dir

IMAGES = {
    "gateway": "traefik:v3.6",
    "dns": "coredns/coredns:1.12.1",
    "catalog": "python:3.12-slim-bookworm",
    "relay": "haproxy:3.2-alpine",
}
HEALTH_HOST = "portmap-gateway.invalid"
RELAY_HEALTH_PORT = 19000
RELAY_PORT_START = 20000
RELAY_PORT_COUNT = 45000
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+portmap\Z")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def resource_labels(state: Mapping[str, Any]) -> dict[str, str]:
    return {OWNER_LABEL: state["owner"], STATE_LABEL: state["state_dir"], UID_LABEL: str(state["uid"]), "portmap.gateway": "true"}


def static_config(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "global": {"checkNewVersion": False, "sendAnonymousUsage": False},
        "entryPoints": {"access": {"address": ":80"}, "web": {"address": ":8080"}, "traefik": {"address": ":8083"}},
        "api": {"insecure": True, "dashboard": False},
        "providers": {
            "providersThrottleDuration": "100ms",
            "file": {"directory": "/config/dynamic", "watch": True},
            "docker": {"endpoint": "unix:///var/run/docker.sock", "exposedByDefault": False, "network": state["network"]["name"], "constraints": "Label(`portmap.managed`,`true`)"},
        },
        "log": {"level": "INFO", "format": "json"},
    }


def compose_config(state: dict[str, Any], runtime: ContainerRuntime) -> dict[str, Any]:
    directory = Path(state["instance_dir"])
    labels = resource_labels(state)
    network = state["network"]["name"]
    common: dict[str, Any] = {
        "labels": labels, "restart": "no",
        "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
    }
    config_mount = {"type": "bind", "source": str(directory / "config"), "target": "/config", "read_only": True}
    socket_mount = {"type": "bind", "source": runtime.socket_path, "target": "/var/run/docker.sock", "read_only": True}
    services = {role: {**common, "image": image, "container_name": state["names"][role], "networks": {"gateway": {"ipv4_address": state["addresses"][role]}}} for role, image in IMAGES.items()}
    services["gateway"].update({
        "command": ["--configFile=/config/traefik.yaml"], "cap_add": ["NET_BIND_SERVICE"],
        "volumes": [config_mount, socket_mount],
        "ports": [f"127.0.0.1:{state['listen_port']}:80", f"127.0.0.1:{state['public_backend_port']}:8080", f"127.0.0.1:{state['management_port']}:8083"],
    })
    services["dns"].update({
        "command": ["-conf", "/config/Corefile"], "cap_add": ["NET_BIND_SERVICE"], "volumes": [config_mount],
        "ports": [f"{DNS_ADDRESS}:{state['dns_port']}:1053/udp", f"{DNS_ADDRESS}:{state['dns_port']}:1053/tcp"],
    })
    public_dns = state.get("public_dns")
    if public_dns and public_dns.get("publish"):
        published = public_dns["publish"]
        address = ipaddress.ip_address(published["address"])
        if type(published["port"]) is not int or not 1 <= published["port"] <= 65535:
            raise PortmapError("invalid recorded public DNS port")
        for protocol in ("udp", "tcp"):
            services["dns"]["ports"].append({"target": 1054, "published": str(published["port"]), "host_ip": str(address), "protocol": protocol})
    services["relay"].update({
        "user": runtime.file_user, "command": ["haproxy", "-W", "-db", "-f", "/config/relay.cfg"],
        "volumes": [config_mount, {"type": "bind", "source": state["socket_dir"], "target": "/run/portmap-ssh", "read_only": True}],
    })
    catalog_environment = {
        "PORTMAP_CATALOG_LISTEN_HOST": "0.0.0.0", "PORTMAP_CATALOG_LISTEN_PORT": "8081",
        "PORTMAP_CATALOG_PUBLIC_LISTEN_PORT": "8082", "PORTMAP_ACCESS_CONFIG": "/config/catalog-access.json",
        "PORTMAP_HTTP_PORT": str(state["http_port"]), "PORTMAP_DNS_DOMAIN": state["domain"],
        "PORTMAP_DNS_SERVER": DNS_ADDRESS, "PORTMAP_DOCKER_SOCKET": "/var/run/docker.sock",
        "PORTMAP_AGENT_SOCKET": "/run/portmap/agent.sock", "PORTMAP_STATE_DIR": "/state",
        "PORTMAP_DOWNLOAD_DIR": "/downloads",
        "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/state",
    }
    catalog_volumes = [config_mount, socket_mount,
        {"type": "bind", "source": str(directory / "catalog"), "target": "/state"},
        {"type": "bind", "source": state["download_cache_dir"], "target": "/downloads"},
        {"type": "bind", "source": state["agent_runtime_dir"], "target": "/run/portmap", "read_only": True},
    ]
    if frozen():
        catalog_command = ["/opt/portmap/portmap", "_serve-catalog"]
        catalog_volumes.append({"type": "bind", "source": str(Path(sys.executable).resolve().parent), "target": "/opt/portmap", "read_only": True})
    else:
        catalog_command = ["python", "-m", "portmap.catalog"]
        catalog_environment["PYTHONPATH"] = "/workspace/pythonpath"
        source = pythonpath_dir()
        identity = source.stat()
        # A reinstall may replace this directory without changing its path.
        # Compose must remount the new object before the old keg is removed.
        catalog_environment["PORTMAP_SOURCE_GENERATION"] = f"{identity.st_dev}:{identity.st_ino}"
        catalog_volumes.append({"type": "bind", "source": str(source), "target": "/workspace/pythonpath", "read_only": True})
    services["catalog"].update({"command": catalog_command, "user": runtime.file_user, "environment": catalog_environment, "volumes": catalog_volumes, "tmpfs": ["/tmp"]})
    if not runtime.rootless:
        services["catalog"]["group_add"] = [str(Path(runtime.socket_path).stat().st_gid)]
    return {"name": state["project"], "services": services, "networks": {"gateway": {"name": network, "external": True}}}


def _domain(value: object) -> str:
    if not isinstance(value, str) or len(value) > 253 or not _DOMAIN.fullmatch(value):
        raise PortmapError("infrastructure domains must be lowercase host subdomains of .portmap")
    return value


def relay_ports(profiles: Mapping[str, dict[str, Any]], previous: Mapping[str, int]) -> dict[str, int]:
    active = {domain for domain, profile in profiles.items() if profile.get("via") == "ssh" and profile.get("available")}
    ports = {domain: port for domain, port in previous.items() if domain in active}
    if any(type(port) is not int or not RELAY_PORT_START <= port < RELAY_PORT_START + RELAY_PORT_COUNT for port in ports.values()) or len(set(ports.values())) != len(ports):
        raise PortmapError("invalid recorded SSH relay ports")
    used = set(ports.values())
    for domain in sorted(active - ports.keys()):
        seed = int.from_bytes(hashlib.sha256(domain.encode()).digest()[:4], "big") % RELAY_PORT_COUNT
        for offset in range(RELAY_PORT_COUNT):
            candidate = RELAY_PORT_START + (seed + offset) % RELAY_PORT_COUNT
            if candidate not in used:
                ports[domain] = candidate
                used.add(candidate)
                break
        else:
            raise PortmapError("no internal SSH relay port remains available")
    return ports


def relay_config(state: dict[str, Any], profiles: Mapping[str, dict[str, Any]]) -> tuple[str, str]:
    forwards = []
    for domain, port in sorted(state["relay_ports"].items()):
        path = Path(profiles[domain]["http_socket"])
        if path.parent != Path(state["socket_dir"]) or not re.fullmatch(r"[0-9a-f]{24}\.sock", path.name):
            raise PortmapError("SSH relay socket is outside the owned socket directory")
        forwards.append((domain, port, path.name))
    revision = hashlib.sha256(json_bytes({"owner": state["owner"], "forwards": forwards})).hexdigest()
    health = json.dumps({"owner": state["owner"], "revision": revision}, separators=(",", ":"))
    lines = ["global", "    log stdout format raw local0", "defaults", "    mode tcp", "    timeout connect 5s", "    timeout client 1h", "    timeout server 1h", "frontend relay_health", f"    bind :{RELAY_HEALTH_PORT}", "    mode http", f"    http-request return status 200 content-type application/json string '{health}'"]
    for domain, port, name in forwards:
        identifier = hashlib.sha256(domain.encode()).hexdigest()[:20]
        lines.extend([f"listen ssh_{identifier}", f"    bind :{port}", f"    server upstream unix@/run/portmap-ssh/{name}"])
    return revision, "\n".join(lines) + "\n"


def access_snapshot(state: dict[str, Any], profiles: Mapping[str, dict[str, Any]], local: dict[str, Any] | None) -> dict[str, Any]:
    machines = {}
    for domain, profile in profiles.items():
        _domain(domain)
        if domain == state["domain"]:
            raise PortmapError(f"{domain} belongs to this machine's portal and cannot also identify a remote")
        via = profile.get("via")
        if via not in {"direct", "ssh"}:
            raise PortmapError("unknown saved remote transport")
        available = via == "direct" or bool(profile.get("available"))
        row = {key: profile[key] for key in ("domain", "via", "target", "address", "http_port", "remote_http_port", "tcp_address", "tcp_ports", "include_tcp") if key in profile}
        row.update({"available": available, "http_port": profile["remote_http_port"] if via == "direct" else state["http_port"], "remote_http_port": profile.get("remote_http_port", 80)})
        if via == "direct":
            address = ipaddress.ip_address(profile["address"])
            authority = f"[{address}]" if address.version == 6 else str(address)
            row["backend_url"] = f"http://{authority}:{profile['remote_http_port']}"
        elif available:
            row["backend_url"] = f"http://{state['addresses']['relay']}:{state['relay_ports'][domain]}"
        machines[domain] = row
    return {"version": 1, "owner": state["owner"], "dns": {"address": DNS_ADDRESS, "port": state["dns_port"]}, "local": {"domain": state["domain"], "http_port": state["http_port"], "enabled": local is not None}, "machines": machines}


def dns_config(state: dict[str, Any], profiles: Mapping[str, dict[str, Any]]) -> tuple[str, str]:
    host = {state["domain"]: "127.0.0.1"}
    containers = {state["domain"]: state["addresses"]["gateway"]}
    for domain, profile in profiles.items():
        _domain(domain)
        if profile.get("via") == "direct":
            host[domain] = containers[domain] = str(ipaddress.ip_address(profile["address"]))
        elif profile.get("available"):
            host[domain] = "127.0.0.1"
            containers[domain] = state["addresses"]["gateway"]
    public = state.get("public_dns")
    revision = hashlib.sha256(json_bytes({"host": host, "containers": containers, "forward": state["dns_forward"], "public": public})).hexdigest()[:24]
    marker = f"{state['owner']}:{revision}"
    return marker, render_corefile(host, containers, marker, forward=state["dns_forward"], public=public)


def routes_config(state: dict[str, Any], snapshot: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    catalog = state["names"]["catalog"]
    relay = state["names"]["relay"]
    services = {
        "catalog-private": {"loadBalancer": {"servers": [{"url": f"http://{catalog}:8081"}], "passHostHeader": True}},
        "catalog-public": {"loadBalancer": {"servers": [{"url": f"http://{catalog}:8082"}], "passHostHeader": True}},
        "local-web": {"loadBalancer": {"servers": [{"url": "http://127.0.0.1:8080"}], "passHostHeader": True}},
        "relay-health": {"loadBalancer": {"servers": [{"url": f"http://{relay}:{RELAY_HEALTH_PORT}"}], "passHostHeader": True}},
    }
    portal_rule = "Host(`127.0.0.1`) || Host(`localhost`)"
    if not snapshot["local"]["enabled"]:
        portal_rule += f" || Host(`{state['domain']}`)"
    routers = {"portal": {"entryPoints": ["access"], "rule": portal_rule, "service": "catalog-private", "priority": 100},
        "relay-health": {"entryPoints": ["access"], "rule": f"Host(`{HEALTH_HOST}`) && Path(`/__portmap_relay/{state['owner']}`)", "service": "relay-health", "priority": 400}}
    if snapshot["local"]["enabled"]:
        domain = _domain(state["domain"])
        routers["local-domain"] = {"entryPoints": ["access"], "rule": f"Host(`{domain}`) || HostRegexp(`^.+\\.{re.escape(domain)}$`)", "service": "local-web", "priority": 1000 + len(domain)}
        # The private self-hop is a connection from THIS container's loopback,
        # not a forwarded header. Public ingress never has this source address.
        # Both fallbacks remain below existing application Docker-provider rules.
        routers["catalog-private-local"] = {"entryPoints": ["web"], "rule": "ClientIP(`127.0.0.1/32`)", "service": "catalog-private", "priority": 2}
        routers["catalog-public-local"] = {"entryPoints": ["web"], "rule": "HostRegexp(`.+`)", "service": "catalog-public", "priority": 1}
    for domain, profile in snapshot["machines"].items():
        if profile["via"] != "ssh" or not profile["available"]:
            continue
        name = hashlib.sha256(domain.encode()).hexdigest()[:20]
        service = f"ssh-{name}"
        services[service] = {"loadBalancer": {"servers": [{"url": profile["backend_url"]}], "passHostHeader": True}}
        routers[service] = {"entryPoints": ["access"], "rule": f"Host(`{domain}`) || HostRegexp(`^.+\\.{re.escape(domain)}$`)", "service": service, "priority": 1000 + len(domain)}
        routers[f"catalog-{name}"] = {"entryPoints": ["access"], "rule": f"Host(`{domain}`)", "service": "catalog-private", "priority": 2000 + len(domain)}
    config = {"http": {"routers": routers, "services": services, "middlewares": {"readiness-path": {"replacePath": {"path": "/healthz"}}}}}
    revision = hashlib.sha256(json_bytes({"owner": state["owner"], "config": config})).hexdigest()
    routers[f"ready-{revision}"] = {"entryPoints": ["access"], "rule": f"Host(`{HEALTH_HOST}`) && Path(`/__portmap_ready/{state['owner']}/{revision}`)", "service": "catalog-private", "middlewares": ["readiness-path"], "priority": 400}
    return revision, config
