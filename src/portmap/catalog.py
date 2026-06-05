from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import re
import socket
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

from .broker import ENDPOINT_CONFIG, ensure_generated_override
from .compose_takeover import find_compose_file, generated_compose_project, plan_docker_compose_command
from .repo_identity import git, git_branch, resolve_repo_identity, stable_hash
from .settings import load_portmap_settings
from .slug import slugify


DOCKER_SOCKET = os.environ.get("PORTMAP_DOCKER_SOCKET", "/var/run/docker.sock")
HTTP_PORT = int(os.environ.get("PORTMAP_HTTP_PORT", "8080"))
DNS_DOMAIN = os.environ.get("PORTMAP_DNS_DOMAIN", "debug.lan").strip(".")
DNS_BIND = os.environ.get("PORTMAP_DNS_BIND", "127.0.0.1")
DNS_TARGET_IP = os.environ.get("PORTMAP_DNS_TARGET_IP", "127.0.0.1")
CATALOG_WORKTREE_ROOTS = os.environ.get("PORTMAP_CATALOG_WORKTREE_ROOTS", "")
CATALOG_HISTORY_FILE_NAME = "catalog-worktrees.json"

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
    ".ico": "image/x-icon",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
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
    generated_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    worktrees = discover_catalog_worktrees(services)
    worktrees = merge_catalog_history(worktrees, services, generated_at=generated_at)
    services.sort(
        key=lambda item: (
            item.get("repo_name") or "",
            item.get("branch") or "",
            item.get("compose_service") or "",
            item.get("container") or "",
        )
    )
    return {
        "generated_at": generated_at,
        "http_port": HTTP_PORT,
        "dns_domain": DNS_DOMAIN,
        "dns_server": DNS_SERVER,
        "services": services,
        "worktrees": worktrees,
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
    worktree = labels.get("portmap.worktree") or labels.get("com.docker.compose.project.working_dir")
    root, root_title = worktree_root_from_raw(worktree)
    return {
        "container": container_name,
        "image": container.get("Image"),
        "repo_id": labels.get("portmap.repo_id"),
        "repo_name": labels.get("portmap.repo_name"),
        "branch": labels.get("portmap.branch"),
        "worktree": worktree,
        "worktree_root": root,
        "worktree_root_title": root_title,
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


def discover_catalog_worktrees(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    running_by_worktree = running_services_by_worktree(services)
    candidates: dict[str, Path] = {}

    for worktree in running_by_worktree:
        path = Path(worktree)
        add_existing_worktree_candidate(candidates, path)
        for sibling in git_worktree_paths(path):
            add_existing_worktree_candidate(candidates, sibling)

    for root in catalog_worktree_roots():
        for candidate in root_worktree_candidates(root):
            add_existing_worktree_candidate(candidates, candidate)
            for sibling in git_worktree_paths(candidate):
                add_existing_worktree_candidate(candidates, sibling)

    worktrees = []
    for path in candidates.values():
        worktree = catalog_worktree_from_path(path, running_by_worktree)
        if worktree is None:
            continue
        if not worktree.get("running") and not worktree.get("startable"):
            continue
        worktrees.append(worktree)
    worktrees.sort(
        key=lambda item: (
            item.get("repo_name") or "",
            item.get("repo_id") or "",
            item.get("worktree_title") or "",
            item.get("branch") or "",
            item.get("worktree") or "",
        )
    )
    return worktrees


def merge_catalog_history(
    current_worktrees: list[dict[str, Any]],
    services: list[dict[str, Any]],
    *,
    generated_at: str,
) -> list[dict[str, Any]]:
    running_by_worktree = running_services_by_worktree(services)
    merged = {
        worktree_instance_key(worktree): dict(worktree, source="current")
        for worktree in current_worktrees
    }
    current_worktree_paths = {str(worktree.get("worktree") or "") for worktree in current_worktrees}
    previous = read_catalog_history()

    for historical in previous:
        raw_worktree = str(historical.get("worktree") or "").strip()
        if not raw_worktree:
            continue
        record = catalog_worktree_from_path(Path(raw_worktree), running_by_worktree)
        if record is None or not record.get("startable"):
            continue
        historical_branch = slugify(str(historical.get("branch") or record["branch"]))
        if historical_branch != record["branch"]:
            continue
        key = worktree_instance_key(record)
        if key in merged:
            continue
        record["source"] = "history" if record["worktree"] not in current_worktree_paths else "current"
        record["last_seen_at"] = historical.get("last_seen_at") or historical.get("updated_at") or ""
        merged[key] = record

    worktrees = list(merged.values())
    for worktree in worktrees:
        if worktree.get("source") == "current":
            worktree["last_seen_at"] = generated_at

    worktrees.sort(
        key=lambda item: (
            item.get("repo_name") or "",
            item.get("repo_id") or "",
            item.get("worktree_title") or "",
            item.get("branch") or "",
            item.get("worktree") or "",
        )
    )
    write_catalog_history(worktrees, generated_at=generated_at)
    return worktrees


def worktree_instance_key(worktree: dict[str, Any]) -> str:
    return "|".join(
        [
            str(worktree.get("repo_id") or worktree.get("repo_name") or ""),
            str(worktree.get("worktree") or ""),
            str(worktree.get("branch") or ""),
        ]
    )


def catalog_history_file() -> Path:
    explicit = os.environ.get("PORTMAP_CATALOG_HISTORY_FILE")
    if explicit:
        return Path(explicit).expanduser()
    state_dir = os.environ.get("PORTMAP_STATE_DIR")
    if state_dir:
        return Path(state_dir).expanduser() / CATALOG_HISTORY_FILE_NAME
    return load_portmap_settings(environ=os.environ).state_dir / CATALOG_HISTORY_FILE_NAME


def read_catalog_history() -> list[dict[str, Any]]:
    path = catalog_history_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    worktrees = payload.get("worktrees") if isinstance(payload, dict) else None
    if not isinstance(worktrees, list):
        return []
    return [item for item in worktrees if isinstance(item, dict)]


def write_catalog_history(worktrees: list[dict[str, Any]], *, generated_at: str) -> None:
    path = catalog_history_file()
    entries = []
    for worktree in worktrees:
        raw_path = str(worktree.get("worktree") or "").strip()
        if not raw_path:
            continue
        if not worktree.get("startable"):
            continue
        entries.append(
            {
                "repo_id": worktree.get("repo_id"),
                "repo_name": worktree.get("repo_name"),
                "branch": worktree.get("branch"),
                "worktree": raw_path,
                "worktree_title": worktree.get("worktree_title"),
                "worktree_root": worktree.get("worktree_root"),
                "worktree_root_title": worktree.get("worktree_root_title"),
                "compose_project": worktree.get("compose_project"),
                "startable": bool(worktree.get("startable")),
                "last_seen_at": worktree.get("last_seen_at") or generated_at,
            }
        )
    payload = {
        "version": 1,
        "updated_at": generated_at,
        "worktrees": entries,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return


def running_services_by_worktree(services: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for service in services:
        raw_worktree = str(service.get("worktree") or "").strip()
        if not raw_worktree:
            continue
        key = canonical_worktree_path(Path(raw_worktree))
        grouped.setdefault(key, []).append(service)
    return grouped


def catalog_worktree_roots() -> list[Path]:
    raw = os.environ.get("PORTMAP_CATALOG_WORKTREE_ROOTS", CATALOG_WORKTREE_ROOTS)
    roots: list[Path] = []
    for item in re.split(rf"[{re.escape(os.pathsep)},]", raw):
        value = item.strip()
        if value:
            roots.append(Path(value).expanduser())
    return roots


def root_worktree_candidates(root: Path) -> list[Path]:
    try:
        resolved = root.expanduser().resolve()
    except OSError:
        return []
    if not resolved.exists() or not resolved.is_dir():
        return []

    candidates = [resolved]
    try:
        children = [child for child in resolved.iterdir() if child.is_dir()]
    except OSError:
        return candidates

    candidates.extend(children)
    wt_containers = [resolved, *children] if looks_like_worktree_container(resolved) else children
    for child in wt_containers:
        if not looks_like_worktree_container(child):
            continue
        try:
            candidates.extend(grandchild for grandchild in child.iterdir() if grandchild.is_dir())
        except OSError:
            continue
    return candidates


def looks_like_worktree_container(path: Path) -> bool:
    return path.name.endswith(("@wt", "@worktree"))


def add_existing_worktree_candidate(candidates: dict[str, Path], path: Path) -> None:
    top_level = git_top_level(path)
    if top_level is None or not top_level.exists():
        return
    candidates[str(top_level)] = top_level


def canonical_worktree_path(path: Path) -> str:
    top_level = git_top_level(path)
    if top_level is not None:
        return str(top_level)
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path.expanduser())


def git_top_level(path: Path) -> Path | None:
    if not path.exists():
        return None
    result = git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


def git_common_dir(path: Path) -> Path | None:
    if not path.exists():
        return None
    result = git(path, "rev-parse", "--git-common-dir")
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    common_dir = Path(value).expanduser()
    if not common_dir.is_absolute():
        common_dir = path / common_dir
    try:
        return common_dir.resolve()
    except OSError:
        return common_dir


def worktree_root_from_raw(raw_worktree: str | None) -> tuple[str | None, str | None]:
    if not raw_worktree:
        return None, None
    top_level = git_top_level(Path(raw_worktree).expanduser())
    if top_level is None:
        return None, None
    return worktree_root_from_top_level(top_level)


def worktree_root_from_top_level(top_level: Path) -> tuple[str, str]:
    common_dir = git_common_dir(top_level)
    if common_dir is None:
        return str(top_level), top_level.name
    title = common_dir.parent.name if common_dir.name == ".git" else common_dir.name
    return str(common_dir), title


def git_worktree_paths(path: Path) -> list[Path]:
    if git_top_level(path) is None:
        return []
    result = git(path, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return []
    return parse_git_worktree_porcelain(result.stdout)


def parse_git_worktree_porcelain(output: str) -> list[Path]:
    paths: list[Path] = []
    for line in output.splitlines():
        if line.startswith("worktree "):
            value = line.removeprefix("worktree ").strip()
            if value:
                paths.append(Path(value).expanduser())
    return paths


def catalog_worktree_from_path(
    path: Path,
    running_by_worktree: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    top_level = git_top_level(path)
    if top_level is None:
        return None

    identity = resolve_repo_identity(
        project_directory=top_level,
        repo_root=top_level,
        repo_id=None,
        repo_name=None,
    )
    branch = worktree_branch(top_level)
    worktree_root, worktree_root_title = worktree_root_from_top_level(top_level)
    running_services = running_by_worktree.get(str(top_level), [])
    compose_project = generated_compose_project(top_level) or first_service_value(running_services, "compose_project")
    compose_file = find_compose_file(top_level)
    endpoint_config = top_level / ENDPOINT_CONFIG
    startable = compose_file is not None and endpoint_config.exists()
    start_error = ""
    if compose_file is None:
        start_error = "missing compose file"
    elif not endpoint_config.exists():
        start_error = f"missing {ENDPOINT_CONFIG}"

    endpoint_total = sum(len(service.get("endpoints") or []) for service in running_services)
    return {
        "id": stable_hash(str(top_level)),
        "repo_id": identity.repo_id,
        "repo_name": identity.display_name,
        "branch": branch,
        "worktree": str(top_level),
        "worktree_title": top_level.name,
        "worktree_root": worktree_root,
        "worktree_root_title": worktree_root_title,
        "compose_project": compose_project,
        "running": bool(running_services),
        "status": "running" if running_services else "stopped",
        "startable": startable,
        "start_error": start_error,
        "service_count": len(running_services),
        "endpoint_count": endpoint_total,
    }


def worktree_branch(path: Path) -> str:
    branch = git_branch(path)
    if branch:
        return slugify(branch)
    result = git(path, "rev-parse", "--short", "HEAD")
    if result.returncode == 0 and result.stdout.strip():
        return slugify(f"detached-{result.stdout.strip()}")
    return "detached"


def first_service_value(services: list[dict[str, Any]], key: str) -> str | None:
    for service in services:
        value = service.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def compose_up_worktree(worktree: str) -> dict[str, Any]:
    raw_worktree = worktree.strip()
    if not raw_worktree:
        raise ValueError("worktree is required")

    path = Path(raw_worktree).expanduser().resolve()
    record = catalog_worktree_from_path(path, {})
    if record is None:
        raise ValueError(f"not a git worktree: {path}")
    if not record["startable"]:
        raise ValueError(f"worktree is not startable: {record['start_error']}")

    ensure_generated_override(["up", "-d"], cwd=path, environ=os.environ)
    plan = plan_docker_compose_command(["up", "-d"], cwd=path)
    if not plan.injected:
        raise RuntimeError("generated compose override was not available for docker compose up")

    env = os.environ.copy()
    env["PORTMAP_BROKER_BYPASS"] = "1"
    result = subprocess.run(
        plan.command,
        cwd=path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"docker compose up failed: {detail}")

    return {
        "ok": True,
        "message": "compose project started",
        "worktree": str(path),
        "compose_project": plan.compose_project or generated_compose_project(path),
        "command": plan.command,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


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


def vite_public_root_asset(request_path: str) -> str | None:
    if not request_path.startswith("/"):
        return None
    normalized = PurePosixPath(request_path.removeprefix("/"))
    if len(normalized.parts) != 1:
        return None
    asset_path = normalized.as_posix()
    if asset_path in {"", ".", "index.html"}:
        return None
    if static_content_type(asset_path) is None:
        return None
    return asset_path


class CatalogHandler(BaseHTTPRequestHandler):
    server_version = "portmap-catalog/0.1"

    def do_GET(self) -> None:
        self.handle_request(send_body=True)

    def do_HEAD(self) -> None:
        self.handle_request(send_body=False)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/actions/compose-up":
            self.handle_compose_up()
            return
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

        public_asset = vite_public_root_asset(parsed.path)
        if public_asset is not None:
            # Vite serves files from frontend/public at the site root in mock
            # mode; the packaged Python server must expose the same build
            # output shape for production.
            if self.write_static(public_asset, send_body=send_body):
                return
            self.write_not_found(send_body=send_body)
            return

        self.write_not_found(send_body=send_body)

    def read_form(self) -> dict[str, list[str]]:
        length = min(int(self.headers.get("content-length", "0") or "0"), 8192)
        payload = self.rfile.read(length).decode("utf-8", errors="replace")
        return urllib.parse.parse_qs(payload)

    def handle_compose_up(self) -> None:
        form = self.read_form()
        worktree = first_form_value(form, "worktree")
        try:
            result = compose_up_worktree(worktree)
            self.write_json(result, send_body=True)
        except Exception as exc:  # pragma: no cover - exercised through container runtime.
            self.write_json(
                {
                    "worktree": worktree,
                    "ok": False,
                    "message": str(exc),
                    "errors": [],
                },
                status=400,
                send_body=True,
            )

    def handle_compose_down(self) -> None:
        form = self.read_form()
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
        form = self.read_form()
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
