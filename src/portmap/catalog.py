from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import re
import socket
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any


DOCKER_SOCKET = os.environ.get("PORTMAP_DOCKER_SOCKET", "/var/run/docker.sock")
HTTP_PORT = int(os.environ.get("PORTMAP_HTTP_PORT", "8080"))
DNS_DOMAIN = os.environ.get("PORTMAP_DNS_DOMAIN", "debug.lan").strip(".")
DNS_BIND = os.environ.get("PORTMAP_DNS_BIND", "127.0.0.1")
DNS_TARGET_IP = os.environ.get("PORTMAP_DNS_TARGET_IP", "127.0.0.1")

PORTMAP_ENDPOINT_RE = re.compile(r"^portmap\.endpoints\.([^.]+)\.([^.]+)$")
TRAEFIK_ROUTER_RE = re.compile(r"^traefik\.(http|tcp|udp)\.routers\.([^.]+)\.([^.]+)$")
TRAEFIK_SERVICE_PORT_RE = re.compile(
    r"^traefik\.(http|tcp|udp)\.services\.([^.]+)\.loadbalancer\.server\.port$"
)
HTTP_HOST_RE = re.compile(r"Host\(`([^`]+)`\)")
NETWORK_REMOVE_ATTEMPTS = 10
NETWORK_REMOVE_DELAY_SECONDS = 0.2
STATIC_ROOT = Path(__file__).with_name("catalog_static")
STATIC_CONTENT_TYPES_BY_SUFFIX = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


def select_dns_server(bind_ip: str, target_ip: str) -> str:
    for candidate in (bind_ip, target_ip):
        candidate = candidate.strip()
        if candidate and candidate not in {"0.0.0.0", "::", "127.0.0.1", "localhost"}:
            return candidate
    return target_ip.strip() or bind_ip.strip() or "127.0.0.1"


DNS_SERVER = os.environ.get("PORTMAP_DNS_SERVER") or select_dns_server(DNS_BIND, DNS_TARGET_IP)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        self.sock = sock


def docker_get(path: str) -> Any:
    payload = docker_request("GET", path)
    return json.loads(payload.decode("utf-8"))


def docker_request(method: str, path: str, *, ok_statuses: set[int] | None = None) -> bytes:
    expected = ok_statuses or {200}
    connection = UnixHTTPConnection(DOCKER_SOCKET)
    connection.request(method, path)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    if response.status not in expected:
        raise RuntimeError(f"Docker API returned {response.status}: {payload.decode(errors='replace')}")
    return payload


def collect_catalog() -> dict[str, Any]:
    containers = docker_get("/containers/json?all=0")
    services = [
        service
        for container in containers
        if (service := container_to_service(container)) is not None
    ]
    services.sort(
        key=lambda item: (
            item.get("repo_name") or "",
            item.get("branch") or "",
            item.get("compose_service") or "",
            item.get("container") or "",
        )
    )
    return {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "http_port": HTTP_PORT,
        "dns_domain": DNS_DOMAIN,
        "dns_server": DNS_SERVER,
        "services": services,
    }


def container_to_service(container: dict[str, Any]) -> dict[str, Any] | None:
    labels = container.get("Labels") or {}
    if labels.get("traefik.enable") != "true" and labels.get("portmap.managed") != "true":
        return None

    endpoints = parse_portmap_endpoints(labels)
    if not endpoints:
        endpoints = parse_traefik_endpoints(labels)
    if not endpoints:
        return None

    container_name = first_container_name(container)
    return {
        "container": container_name,
        "image": container.get("Image"),
        "repo_id": labels.get("portmap.repo_id"),
        "repo_name": labels.get("portmap.repo_name"),
        "branch": labels.get("portmap.branch"),
        "worktree": labels.get("portmap.worktree")
        or labels.get("com.docker.compose.project.working_dir"),
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
        "docker_network": labels.get("traefik.docker.network"),
        "endpoints": endpoints,
    }


def first_container_name(container: dict[str, Any]) -> str:
    names = container.get("Names") or []
    if not names:
        return container.get("Id", "")[:12]
    return str(names[0]).lstrip("/")


def parse_portmap_endpoints(labels: dict[str, str]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for key, value in labels.items():
        match = PORTMAP_ENDPOINT_RE.match(key)
        if match is None:
            continue
        endpoint_id, field = match.groups()
        grouped.setdefault(endpoint_id, {"id": endpoint_id})[field] = normalize_label_value(field, value)

    endpoints = list(grouped.values())
    endpoints.sort(key=lambda item: str(item.get("name") or item.get("id") or ""))
    return endpoints


def normalize_label_value(field: str, value: str) -> Any:
    if field in {"container_port", "host_port", "range_start", "range_end", "range_size"}:
        try:
            return int(value)
        except ValueError:
            return value
    return value


def parse_traefik_endpoints(labels: dict[str, str]) -> list[dict[str, Any]]:
    routers: dict[tuple[str, str], dict[str, Any]] = {}
    service_ports: dict[tuple[str, str], int] = {}

    for key, value in labels.items():
        router_match = TRAEFIK_ROUTER_RE.match(key)
        if router_match is not None:
            kind, router, field = router_match.groups()
            routers.setdefault((kind, router), {"id": router, "kind": kind, "router": router})[field] = value
            continue

        service_match = TRAEFIK_SERVICE_PORT_RE.match(key)
        if service_match is not None:
            kind, service_name = service_match.groups()
            try:
                service_ports[(kind, service_name)] = int(value)
            except ValueError:
                pass

    endpoints: list[dict[str, Any]] = []
    for (kind, router), values in routers.items():
        service_name = values.get("service")
        endpoint: dict[str, Any] = {
            "id": router,
            "name": router,
            "kind": kind,
            "router": router,
            "entrypoint": values.get("entrypoints"),
            "traefik_service": service_name,
            "container_port": service_ports.get((kind, service_name)),
        }
        if kind == "http":
            host = parse_host_rule(str(values.get("rule", "")))
            endpoint["host"] = host
            if host:
                endpoint["url"] = f"http://{host}:{HTTP_PORT}"
        endpoints.append(endpoint)

    endpoints.sort(key=lambda item: str(item.get("name") or item.get("id") or ""))
    return endpoints


def parse_host_rule(rule: str) -> str | None:
    match = HTTP_HOST_RE.search(rule)
    if match is None:
        return None
    return match.group(1)


def compose_down_project(compose_project: str) -> dict[str, Any]:
    project = compose_project.strip()
    if not project:
        raise ValueError("compose_project is required")

    containers = compose_project_containers(project)
    if not containers:
        raise ValueError(f"no containers found for compose project: {project}")
    if not any(is_portmap_managed(container) for container in containers):
        raise ValueError(f"compose project is not portmap-managed: {project}")

    result: dict[str, Any] = {
        "ok": True,
        "compose_project": project,
        "message": "compose project stopped",
        "containers_removed": 0,
        "networks_removed": 0,
        "errors": [],
    }

    for container in containers:
        container_id = str(container.get("Id") or "")
        if not container_id:
            continue
        try:
            stop_container(container_id)
            remove_container(container_id)
            result["containers_removed"] += 1
        except Exception as exc:  # pragma: no cover - depends on Docker daemon race behavior.
            result["ok"] = False
            result["errors"].append(f"container {container_id[:12]}: {exc}")

    for network in compose_project_networks(project):
        network_id = str(network.get("Id") or network.get("Name") or "")
        if not network_id:
            continue
        try:
            remove_network(network_id)
            result["networks_removed"] += 1
        except Exception as exc:  # pragma: no cover - depends on Docker daemon race behavior.
            result["ok"] = False
            result["errors"].append(f"network {network_id}: {exc}")

    if not result["ok"]:
        result["message"] = "compose project partially stopped"
    return result


def compose_restart_project(compose_project: str) -> dict[str, Any]:
    project = compose_project.strip()
    if not project:
        raise ValueError("compose_project is required")

    containers = compose_project_containers(project)
    if not containers:
        raise ValueError(f"no containers found for compose project: {project}")
    if not any(is_portmap_managed(container) for container in containers):
        raise ValueError(f"compose project is not portmap-managed: {project}")

    result: dict[str, Any] = {
        "ok": True,
        "compose_project": project,
        "message": "compose project restarted",
        "containers_restarted": 0,
        "errors": [],
    }
    for container in containers:
        container_id = str(container.get("Id") or "")
        if not container_id:
            continue
        try:
            restart_container(container_id)
            result["containers_restarted"] += 1
        except Exception as exc:  # pragma: no cover - depends on Docker daemon race behavior.
            result["ok"] = False
            result["errors"].append(f"container {container_id[:12]}: {exc}")

    if not result["ok"]:
        result["message"] = "compose project partially restarted"
    return result


def compose_project_containers(compose_project: str) -> list[dict[str, Any]]:
    containers = docker_get("/containers/json?all=1")
    return [
        container
        for container in containers
        if (container.get("Labels") or {}).get("com.docker.compose.project") == compose_project
    ]


def compose_project_networks(compose_project: str) -> list[dict[str, Any]]:
    networks = docker_get("/networks")
    return [
        network
        for network in networks
        if (network.get("Labels") or {}).get("com.docker.compose.project") == compose_project
    ]


def is_portmap_managed(container: dict[str, Any]) -> bool:
    return (container.get("Labels") or {}).get("portmap.managed") == "true"


def stop_container(container_id: str) -> None:
    escaped = urllib.parse.quote(container_id, safe="")
    docker_request("POST", f"/containers/{escaped}/stop?t=10", ok_statuses={204, 304, 404, 409})


def restart_container(container_id: str) -> None:
    escaped = urllib.parse.quote(container_id, safe="")
    docker_request("POST", f"/containers/{escaped}/restart?t=10", ok_statuses={204, 404})


def remove_container(container_id: str) -> None:
    escaped = urllib.parse.quote(container_id, safe="")
    docker_request("DELETE", f"/containers/{escaped}?v=0&force=1", ok_statuses={204, 404, 409})


def remove_network(network_id: str) -> None:
    escaped = urllib.parse.quote(network_id, safe="")
    for attempt in range(NETWORK_REMOVE_ATTEMPTS):
        try:
            docker_request("DELETE", f"/networks/{escaped}", ok_statuses={204, 404})
            return
        except RuntimeError:
            if attempt == NETWORK_REMOVE_ATTEMPTS - 1:
                raise
            time.sleep(NETWORK_REMOVE_DELAY_SECONDS)


def first_form_value(form: dict[str, list[str]], name: str) -> str:
    values = form.get(name) or [""]
    return values[0]


def safe_static_path(asset_path: str) -> Path | None:
    normalized = PurePosixPath(asset_path)
    if normalized.is_absolute() or ".." in normalized.parts or not normalized.parts:
        return None
    return STATIC_ROOT.joinpath(*normalized.parts)


def static_content_type(asset_path: str) -> str | None:
    return STATIC_CONTENT_TYPES_BY_SUFFIX.get(PurePosixPath(asset_path).suffix)


def read_static_asset(asset_path: str) -> tuple[bytes, str] | None:
    path = safe_static_path(asset_path)
    content_type = static_content_type(asset_path)
    if path is None or content_type is None:
        return None
    try:
        return path.read_bytes(), content_type
    except FileNotFoundError:
        return None


class CatalogHandler(BaseHTTPRequestHandler):
    server_version = "portmap-catalog/0.1"

    def do_GET(self) -> None:
        self.handle_request(send_body=True)

    def do_HEAD(self) -> None:
        self.handle_request(send_body=False)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/actions/compose-down":
            self.handle_compose_down()
            return
        if parsed.path == "/actions/compose-restart":
            self.handle_compose_restart()
            return
        self.send_response(404)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found\n")

    def handle_request(self, *, send_body: bool) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/healthz", "/readyz"}:
            self.write_text("ok\n", content_type="text/plain", send_body=send_body)
            return

        if parsed.path in {"/", "/index.html"}:
            self.write_static("index.html", send_body=send_body)
            return

        if parsed.path.startswith("/assets/"):
            asset_path = parsed.path.removeprefix("/")
            if self.write_static(asset_path, send_body=send_body):
                return
            self.write_not_found(send_body=send_body)
            return

        if parsed.path == "/registry.json":
            try:
                catalog = collect_catalog()
            except Exception as exc:  # pragma: no cover - exercised through container runtime.
                self.send_response(500)
                self.send_header("content-type", "text/plain; charset=utf-8")
                self.end_headers()
                if send_body:
                    self.wfile.write(f"failed to read Docker catalog: {exc}\n".encode("utf-8"))
                return
            self.write_json(catalog, send_body=send_body)
            return

        self.write_not_found(send_body=send_body)

    def handle_compose_down(self) -> None:
        length = min(int(self.headers.get("content-length", "0") or "0"), 8192)
        payload = self.rfile.read(length).decode("utf-8", errors="replace")
        form = urllib.parse.parse_qs(payload)
        compose_project = first_form_value(form, "compose_project")
        try:
            result = compose_down_project(compose_project)
            self.write_json(result, send_body=True)
        except Exception as exc:  # pragma: no cover - exercised through container runtime.
            self.write_json(
                {
                    "compose_project": compose_project,
                    "ok": False,
                    "message": str(exc),
                    "containers_removed": 0,
                    "networks_removed": 0,
                    "errors": [],
                },
                status=400,
                send_body=True,
            )

    def handle_compose_restart(self) -> None:
        length = min(int(self.headers.get("content-length", "0") or "0"), 8192)
        payload = self.rfile.read(length).decode("utf-8", errors="replace")
        form = urllib.parse.parse_qs(payload)
        compose_project = first_form_value(form, "compose_project")
        try:
            result = compose_restart_project(compose_project)
            self.write_json(result, send_body=True)
        except Exception as exc:  # pragma: no cover - exercised through container runtime.
            self.write_json(
                {
                    "compose_project": compose_project,
                    "ok": False,
                    "message": str(exc),
                    "containers_restarted": 0,
                    "errors": [],
                },
                status=400,
                send_body=True,
            )

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def write_json(self, payload: dict[str, Any], *, send_body: bool, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def write_static(self, filename: str, *, send_body: bool) -> bool:
        asset = read_static_asset(filename)
        if asset is None:
            return False
        body, content_type = asset
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)
        return True

    def write_not_found(self, *, send_body: bool) -> None:
        self.send_response(404)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.end_headers()
        if send_body:
            self.wfile.write(b"not found\n")

    def write_text(self, body: str, *, content_type: str, send_body: bool) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", f"{content_type}; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if send_body:
            self.wfile.write(payload)


def main() -> None:
    host = os.environ.get("PORTMAP_CATALOG_LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("PORTMAP_CATALOG_LISTEN_PORT", "8081"))
    server = ThreadingHTTPServer((host, port), CatalogHandler)
    print(f"portmap catalog listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
