from __future__ import annotations

from . import __version__

import datetime as dt
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .agent_client import (
    AgentRequestError,
    AgentUnavailable,
    agent_compose_up_worktree,
    agent_connection_targets,
    agent_mutate_connection,
    agent_update_agent_dispatch,
    agent_update_credentials,
    agent_update_job,
    agent_update_start,
    agent_update_status,
    agent_update_token,
    agent_update_version,
    agent_worktrees,
)
from .update_control import (
    NONCE_HEADER,
    RESPONSE_SIGNATURE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
)
from .broker import ENDPOINT_CONFIG, ensure_generated_override
from .catalog_access import (
    LOCAL_MACHINE_ID,
    AccessSnapshot,
    MachineAccess,
    host_selected_machine_id,
    load_access_snapshot,
    local_descriptor,
    machine_descriptor,
    machines_payload,
)
from .client_dns import probe_marker
from .client_discovery import probe_gateway
from .client_runtime import native_environment
from .client_view import project_catalog, request_host
from .downloads import (
    ARCHIVE_CONTENT_TYPE,
    DOWNLOADS_PREFIX,
    INSTALL_SCRIPT_CONTENT_TYPE,
    DownloadUnavailable,
    download_info,
    install_script_body,
    resolve_archive,
)
from .errors import PortmapError
from .compose_takeover import find_compose_file, generated_compose_project, plan_docker_compose_command
from .repo_identity import git, git_branch, resolve_repo_identity, stable_hash
from .settings import load_portmap_settings
from .slug import slugify
from .web_static import StaticHandler, vite_public_root_asset


DOCKER_SOCKET = os.environ.get("PORTMAP_DOCKER_SOCKET", "/var/run/docker.sock")
HTTP_PORT = int(os.environ.get("PORTMAP_HTTP_PORT", "80"))
DNS_DOMAIN = os.environ.get("PORTMAP_DNS_DOMAIN", "portmap").strip(".")
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
MISSING_ORDER = 1_000_000
AGENT_AUTHORITATIVE_WORKTREE_KEYS = {
    "branch_tip_epoch",
    "branch_tip_time",
    "branch_tip_sha",
    "display_branch",
    "display_worktree_root",
    "display_worktree_root_title",
    "submodule_branch",
    "submodule_relative_path",
    "submodule_sha",
    "superproject_branch",
    "superproject_repo_id",
    "superproject_repo_name",
    "superproject_worktree",
    "worktree_exists",
    "worktree_root",
    "worktree_root_title",
    "worktree_status",
    "worktree_status_message",
    "worktree_superproject",
}


def select_dns_server(bind_ip: str, target_ip: str) -> str:
    for candidate in (bind_ip, target_ip):
        candidate = candidate.strip()
        if candidate and candidate not in {"0.0.0.0", "::", "127.0.0.1", "localhost"}:
            return candidate
    return target_ip.strip() or bind_ip.strip() or "127.0.0.1"


HOST_DNS_ENV = "PORTMAP_HOST_DNS_ENABLED"
HTTP_ADDRESS_ENV = "PORTMAP_HTTP_ADDRESS"


def host_dns_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the controller publishes host DNS for this catalog.

    Absent keeps the legacy default (enabled); the controller's explicit
    ``PORTMAP_HOST_DNS_ENABLED=0`` marks a server-only publication. Any
    value other than ``1`` fails closed to disabled.
    """

    env = os.environ if environ is None else environ
    return env.get(HOST_DNS_ENV, "1") == "1"


def advertised_dns_server(environ: Mapping[str, str] | None = None) -> str | None:
    """The host DNS listener this catalog may advertise, if any.

    Disabled host DNS must not fall back to DNS_BIND/DNS_TARGET_IP and
    advertise a server that nobody publishes.
    """

    env = os.environ if environ is None else environ
    if not host_dns_enabled(env):
        return None
    explicit = env.get("PORTMAP_DNS_SERVER")
    if explicit:
        return explicit
    return select_dns_server(
        env.get("PORTMAP_DNS_BIND", DNS_BIND), env.get("PORTMAP_DNS_TARGET_IP", DNS_TARGET_IP)
    )


def advertised_http_address(environ: Mapping[str, str] | None = None) -> str | None:
    """The published entry address for HTTP bootstrap, when configured.

    This is the honest address of the entry the catalog is served through;
    clients must never reuse the DNS server address as an HTTP address.
    Omitted when the controller does not record one.
    """

    env = os.environ if environ is None else environ
    address = (env.get(HTTP_ADDRESS_ENV) or "").strip()
    if not address:
        return None
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        pass
    if len(address) <= 253 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in address.split(".")
    ):
        return address
    raise PortmapError(f"{HTTP_ADDRESS_ENV} must be an IP address or DNS name")


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
    agent = {"available": False, "message": "agent unavailable"}
    # Worktree data comes exclusively from the host agent; the container has
    # no host filesystem access for in-process git scanning.
    try:
        agent_payload = agent_worktrees()
    except Exception as exc:  # AgentUnavailable and agent protocol errors.
        worktrees = []
        agent["message"] = str(exc)
    else:
        raw_worktrees = agent_payload.get("worktrees")
        worktrees = raw_worktrees if isinstance(raw_worktrees, list) else []
        agent = {
            "available": True,
            "message": str(agent_payload.get("message") or "agent ready"),
            "generated_at": str(agent_payload.get("generated_at") or ""),
        }
    enrich_services_from_worktrees(services, worktrees)
    services.sort(
        key=lambda item: (
            item.get("repo_name") or "",
            item.get("branch") or "",
            endpoint_sort_order(item),
            item.get("compose_service") or "",
            item.get("container") or "",
        )
    )
    catalog = {
        "generated_at": generated_at,
        "http_port": HTTP_PORT,
        "dns_domain": DNS_DOMAIN,
        "agent": agent,
        "services": services,
        "worktrees": worktrees,
    }
    dns_server = advertised_dns_server()
    if dns_server is not None:
        catalog["dns_server"] = dns_server
    http_address = advertised_http_address()
    if http_address is not None:
        catalog["http_address"] = http_address
    return catalog


def enrich_services_from_worktrees(services: list[dict[str, Any]], worktrees: list[dict[str, Any]]) -> None:
    by_worktree = {
        str(worktree.get("worktree") or ""): worktree
        for worktree in worktrees
        if str(worktree.get("worktree") or "").strip()
    }
    for service in services:
        worktree = by_worktree.get(str(service.get("worktree") or ""))
        if not worktree:
            continue
        for key in (
            "repo_id",
            "repo_name",
            "branch",
            "branch_tip_epoch",
            "branch_tip_time",
            "branch_tip_sha",
            "display_branch",
            "display_worktree_root",
            "display_worktree_root_title",
            "submodule_branch",
            "submodule_relative_path",
            "submodule_sha",
            "superproject_branch",
            "superproject_repo_id",
            "superproject_repo_name",
            "superproject_worktree",
            "worktree_exists",
            "worktree_root",
            "worktree_root_title",
            "worktree_status",
            "worktree_status_message",
            "worktree_superproject",
            "compose_project",
        ):
            if key in AGENT_AUTHORITATIVE_WORKTREE_KEYS:
                if key in worktree:
                    service[key] = worktree[key]
                continue
            if missing_catalog_value(service.get(key)) and not missing_catalog_value(worktree.get(key)):
                service[key] = worktree[key]


def missing_catalog_value(value: Any) -> bool:
    return value is None or value == ""


def container_to_service(container: dict[str, Any]) -> dict[str, Any] | None:
    labels = container.get("Labels") or {}
    if labels.get("portmap.gateway") == "true":
        return None
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
    status = worktree_status_from_raw(worktree)
    return {
        "container": container_name,
        "image": container.get("Image"),
        "repo_id": labels.get("portmap.repo_id"),
        "repo_name": labels.get("portmap.repo_name"),
        "branch": labels.get("portmap.branch"),
        "worktree": worktree,
        **status,
        "worktree_root": root,
        "worktree_root_title": root_title,
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
        "docker_network": labels.get("traefik.docker.network"),
        "portmap_order": endpoint_sort_order({"endpoints": endpoints}),
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
    endpoints.sort(key=endpoint_sort_key)
    return endpoints


def normalize_label_value(field: str, value: str) -> Any:
    if field in {"container_port", "host_port", "order", "range_start", "range_end", "range_size"}:
        try:
            return int(value)
        except ValueError:
            return value
    return value


def endpoint_sort_key(endpoint: dict[str, Any]) -> tuple[int, str, str]:
    order = endpoint.get("order")
    numeric_order = order if isinstance(order, int) else MISSING_ORDER
    return (numeric_order, str(endpoint.get("name") or ""), str(endpoint.get("id") or ""))


def endpoint_sort_order(service: dict[str, Any]) -> int:
    orders = [
        endpoint.get("order")
        for endpoint in service.get("endpoints", [])
        if isinstance(endpoint.get("order"), int)
    ]
    return min(orders) if orders else MISSING_ORDER


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

    add_submodule_worktree_candidates(candidates)

    worktrees = []
    for path in candidates.values():
        worktree = catalog_worktree_from_path(path, running_by_worktree)
        if worktree is None:
            continue
        if not worktree.get("running") and not worktree.get("startable"):
            continue
        worktrees.append(worktree)
    worktrees.sort(key=catalog_worktree_sort_key)
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

    worktrees.sort(key=catalog_worktree_sort_key)
    write_catalog_history(worktrees, generated_at=generated_at)
    return worktrees


def catalog_worktree_sort_key(item: dict[str, Any]) -> tuple[str, str, str, int, str, str]:
    return (
        str(item.get("repo_name") or ""),
        str(item.get("repo_id") or ""),
        str(item.get("worktree_root_title") or item.get("worktree_title") or ""),
        -int_value(item.get("branch_tip_epoch")),
        str(item.get("branch") or ""),
        str(item.get("worktree") or ""),
    )


def int_value(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


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


def add_submodule_worktree_candidates(candidates: dict[str, Path]) -> None:
    seed_paths = list(candidates.values())
    for path in seed_paths:
        for candidate in submodule_worktree_candidates(path):
            add_existing_worktree_candidate(candidates, candidate)


def submodule_worktree_candidates(path: Path) -> list[Path]:
    top_level = git_top_level(path)
    if top_level is None:
        return []

    superproject = git_superproject(top_level)
    if superproject is not None:
        relative = relative_path_under(top_level, superproject)
        if relative is None:
            return []
        return [
            sibling / relative
            for sibling in worktree_self_and_siblings(superproject)
            if (sibling / relative).exists()
        ]

    candidates: list[Path] = []
    for superproject_worktree in worktree_self_and_siblings(top_level):
        for submodule_path in git_submodule_paths(superproject_worktree):
            candidate = superproject_worktree / submodule_path
            if candidate.exists():
                candidates.append(candidate)
    return candidates


def worktree_self_and_siblings(path: Path) -> list[Path]:
    top_level = git_top_level(path)
    if top_level is None:
        return []
    siblings = [top_level, *git_worktree_paths(top_level)]
    deduped: dict[str, Path] = {}
    for sibling in siblings:
        sibling_top_level = git_top_level(sibling)
        if sibling_top_level is not None:
            deduped[str(sibling_top_level)] = sibling_top_level
    return list(deduped.values())


def git_submodule_paths(path: Path) -> list[Path]:
    top_level = git_top_level(path)
    if top_level is None:
        return []
    gitmodules = top_level / ".gitmodules"
    if not gitmodules.exists():
        return []
    result = git(top_level, "config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$")
    if result.returncode != 0:
        return []
    return parse_git_submodule_paths(result.stdout)


def parse_git_submodule_paths(output: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        relative = clean_submodule_path(parts[1].strip())
        if relative is None:
            continue
        key = relative.as_posix()
        if key in seen:
            continue
        seen.add(key)
        paths.append(relative)
    return paths


def clean_submodule_path(value: str) -> Path | None:
    if not value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return Path(*path.parts)


def relative_path_under(child: Path, parent: Path) -> Path | None:
    try:
        relative = child.relative_to(parent)
    except ValueError:
        return None
    if not relative.parts:
        return None
    return relative


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


def git_superproject(path: Path) -> Path | None:
    if not path.exists():
        return None
    result = git(path, "rev-parse", "--show-superproject-working-tree")
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


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


def worktree_status_from_raw(raw_worktree: str | None, *, top_level: Path | None = None) -> dict[str, Any]:
    if not raw_worktree:
        return {
            "worktree_exists": None,
            "worktree_status": "",
            "worktree_status_message": "",
            "worktree_superproject": None,
        }

    path = Path(raw_worktree).expanduser()
    if not path.exists():
        return {
            "worktree_exists": False,
            "worktree_status": "deleted",
            "worktree_status_message": "worktree directory not found",
            "worktree_superproject": None,
        }

    resolved_top_level = top_level or git_top_level(path)
    superproject = git_superproject(resolved_top_level or path)
    if superproject is not None:
        return {
            "worktree_exists": True,
            "worktree_status": "submodule",
            "worktree_status_message": f"submodule under {superproject}",
            "worktree_superproject": str(superproject),
        }

    return {
        "worktree_exists": True,
        "worktree_status": "ok",
        "worktree_status_message": "",
        "worktree_superproject": None,
    }


def submodule_display_metadata(
    *,
    top_level: Path,
    child_repo_id: str,
    branch: str,
    tip: dict[str, int | str],
) -> dict[str, Any]:
    superproject = git_superproject(top_level)
    if superproject is None:
        return {}

    relative = relative_path_under(top_level, superproject)
    if relative is None:
        return {}

    superproject_identity = resolve_repo_identity(
        project_directory=superproject,
        repo_root=superproject,
        repo_id=None,
        repo_name=None,
    )
    superproject_branch = worktree_branch(superproject)
    relative_text = relative.as_posix()
    display_root = f"submodule:{child_repo_id}:{superproject_identity.repo_id}:{relative_text}"
    return {
        "display_branch": branch,
        "display_worktree_root": display_root,
        "display_worktree_root_title": f"{superproject_identity.display_name} / {relative_text}",
        "submodule_branch": branch,
        "submodule_relative_path": relative_text,
        "submodule_sha": str(tip.get("sha") or ""),
        "superproject_branch": superproject_branch,
        "superproject_repo_id": superproject_identity.repo_id,
        "superproject_repo_name": superproject_identity.display_name,
        "superproject_worktree": str(superproject),
    }


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
    tip = worktree_tip(top_level)
    worktree_root, worktree_root_title = worktree_root_from_top_level(top_level)
    status = worktree_status_from_raw(str(top_level), top_level=top_level)
    display = submodule_display_metadata(
        top_level=top_level,
        child_repo_id=identity.repo_id,
        branch=branch,
        tip=tip,
    )
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
        "display_branch": display.get("display_branch", branch),
        "branch_tip_epoch": tip["epoch"],
        "branch_tip_time": tip["time"],
        "branch_tip_sha": tip["sha"],
        "worktree": str(top_level),
        "worktree_title": top_level.name,
        **status,
        **display,
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


def worktree_tip(path: Path) -> dict[str, int | str]:
    result = git(path, "log", "-1", "--format=%ct%x00%cI%x00%H")
    if result.returncode != 0:
        return {"epoch": 0, "time": "", "sha": ""}
    parts = result.stdout.rstrip("\n").split("\x00")
    if len(parts) != 3:
        return {"epoch": 0, "time": "", "sha": ""}
    try:
        epoch = int(parts[0])
    except ValueError:
        epoch = 0
    return {"epoch": epoch, "time": parts[1], "sha": parts[2]}


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

    env = native_environment()
    env["PORTMAP_BROKER_BYPASS"] = "1"
    # Read at call time: the host agent receives the runtime socket through
    # PORTMAP_DOCKER_SOCKET; the in-container fallback uses the mount target.
    socket_path = os.environ.get("PORTMAP_DOCKER_SOCKET", DOCKER_SOCKET)
    env["DOCKER_HOST"] = f"unix://{socket_path}"
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


ACTION_PATHS = ("/actions/compose-up", "/actions/compose-down", "/actions/compose-restart")
CONNECTION_CONTROL_PATH = "/api/connections"
CONNECTION_JSON_MAX_BYTES = 16384
UPDATES_API_PATH = "/api/updates"
UPDATES_JOBS_PREFIX = "/api/updates/jobs/"
UPDATES_TOKEN_PATH = "/api/updates/token"
UPDATES_CREDENTIALS_PATH = "/api/updates/credentials"
UPDATE_AGENT_PREFIX = "/api/update-agent/"
UPDATE_AGENT_PAIR_PATH = "/api/update-agent/pair"
VERSION_API_PATH = "/api/version"
UPDATES_JSON_MAX_BYTES = 16384
UPDATE_AGENT_MAX_BYTES = 16384
UPDATE_JOB_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
REMOTE_PROBE_TIMEOUT = 5.0
REMOTE_ACTION_TIMEOUT = 30.0
REMOTE_ACTION_MAX_BYTES = 262144
MAX_FORM_BYTES = 8192
# The readiness gate probes the container-scope DNS listener inside the
# bridge network; no host DNS port is published or probed for health.
READY_DNS_PORT = 53
READY_DNS_TIMEOUT = 0.4


class MachineRequestError(Exception):
    """A machine selection that must fail before any data or mutation."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RemoteActionError(RuntimeError):
    """A remote machine action could not be forwarded or understood."""


class RequestBodyError(Exception):
    """A malformed or oversized JSON request body."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _single_check_flag(query: str) -> bool | None:
    """True/False for an optional exactly-``check=1`` flag; None when invalid."""
    values = urllib.parse.parse_qs(query, keep_blank_values=True).get("check", [])
    if not values:
        return False
    if len(values) == 1 and values[0] == "1":
        return True
    return None


def forward_remote_action(machine: MachineAccess, action_path: str, body: bytes) -> tuple[int, dict[str, Any]]:
    """Forward one allowlisted action form to the machine's saved origin.

    The path is allowlisted and the origin is controller-owned. The fixed
    localhost Host selects that remote's own portal, never one of its apps.
    Redirects are not followed, responses are bounded, and transport failures raise
    RemoteActionError for the caller to sanitize.
    """
    parsed = urllib.parse.urlsplit(machine.backend_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=REMOTE_ACTION_TIMEOUT)
    try:
        connection.request(
            "POST",
            action_path,
            body=body,
            headers={
                "host": "localhost",
                "content-type": "application/x-www-form-urlencoded",
                "content-length": str(len(body)),
                "accept": "application/json",
                "connection": "close",
            },
        )
        response = connection.getresponse()
        raw = response.read(REMOTE_ACTION_MAX_BYTES + 1)
        if len(raw) > REMOTE_ACTION_MAX_BYTES or 300 <= response.status < 400:
            raise RemoteActionError("remote action response is not usable")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, http.client.HTTPException, UnicodeError, ValueError) as exc:
        raise RemoteActionError("remote action failed") from exc
    finally:
        connection.close()
    if not isinstance(payload, dict):
        raise RemoteActionError("remote action returned an unexpected body")
    return response.status, payload


def dns_marker_verified(snapshot: AccessSnapshot) -> bool:
    """Whether the snapshot's DNS container answers with its own marker.

    Readiness is proven over the wire at the container-scope listener; a
    missing container address or marker is never accepted as verified.
    """

    if snapshot.dns_container_address is None or snapshot.dns_marker is None:
        return False
    return probe_marker(snapshot.dns_container_address, READY_DNS_PORT, timeout=READY_DNS_TIMEOUT) == snapshot.dns_marker


class CatalogServer(ThreadingHTTPServer):
    """One catalog process; the server-level scope decides remote access."""

    def __init__(self, address: tuple[str, int], scope: str = "private") -> None:
        if scope not in {"private", "public"}:
            raise ValueError(f"unknown catalog scope: {scope}")
        self.catalog_scope: str = scope
        super().__init__(address, CatalogHandler)


class CatalogHandler(StaticHandler):
    server: CatalogServer
    server_version = f"portmap-catalog/{__version__}"

    @property
    def private_scope(self) -> bool:
        # Server-level scope only; CatalogServer is the sole supported host.
        return self.server.catalog_scope == "private"

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == CONNECTION_CONTROL_PATH:
            # Connection control is private-scope only; the public/LAN
            # listener denies it before any IPC or mutation.
            if not self.private_scope:
                self.write_not_found(send_body=True)
                return
            self.handle_connections_post()
            return
        if parsed.path in {UPDATES_TOKEN_PATH, UPDATES_CREDENTIALS_PATH}:
            # Capability reveal and pairing are the most sensitive update
            # actions; they never exist on the public listener.
            if not self.private_scope:
                self.write_not_found(send_body=True)
                return
            if parsed.path == UPDATES_TOKEN_PATH:
                self.handle_updates_token_post()
            else:
                self.handle_updates_credentials_post()
            return
        if parsed.path == UPDATES_API_PATH:
            if not self.private_scope:
                self.write_not_found(send_body=True)
                return
            self.handle_updates_post()
            return
        if parsed.path.startswith(UPDATE_AGENT_PREFIX):
            self.handle_update_agent_post(self.path)
            return
        if parsed.path not in ACTION_PATHS:
            self.send_response(404)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found\n")
            return
        self.handle_action(parsed.path)

    def handle_action(self, action_path: str) -> None:
        snapshot = self._fail_closed_snapshot(send_body=True)
        if snapshot is None:
            return
        try:
            machine = self._resolve_machine(snapshot)
        except MachineRequestError as exc:
            self.write_json({"error": exc.message}, status=exc.status, send_body=True)
            return
        if machine is not None:
            self.forward_action(machine, action_path)
            return
        if action_path == "/actions/compose-up":
            self.handle_compose_up()
        elif action_path == "/actions/compose-down":
            self.handle_compose_down()
        else:
            self.handle_compose_restart()

    def forward_action(self, machine: MachineAccess, action_path: str) -> None:
        body = self.read_form_body()
        try:
            status, payload = forward_remote_action(machine, action_path, body)
        except RemoteActionError:
            # Never surface backend origins or transport diagnostics.
            self.write_json({"error": "remote action failed; check the remote machine"}, status=502, send_body=True)
            return
        self.write_json(payload, status=status, send_body=True)

    def _request_host(self) -> str | None:
        values = self.headers.get_all("Host", [])
        return request_host(values[0]) if len(values) == 1 else None

    def _fail_closed_snapshot(self, *, send_body: bool) -> AccessSnapshot | None:
        """Load the controller snapshot or answer a sanitized 503.

        There is no local-only mode: without controller-owned access data
        the catalog must not serve or mutate any machine's projects.
        """
        try:
            return load_access_snapshot()
        except PortmapError:
            self.write_json({"error": "catalog access snapshot is unavailable"}, status=503, send_body=send_body)
            return None

    def _machine_id(self) -> str | None:
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query,
            keep_blank_values=True,
        )
        values = query.get("machine", [])
        if not values:
            return None
        if len(values) > 1:
            raise MachineRequestError(400, "duplicate machine parameter")
        if not values[0]:
            raise MachineRequestError(400, "empty machine parameter")
        return values[0]

    def _resolve_machine(self, snapshot: AccessSnapshot) -> MachineAccess | None:
        """Resolve the target machine; None means the local machine.

        Unknown, duplicate, and empty selections fail here, as does any
        non-local machine outside the private scope, before registry data
        is fetched or an action mutates anything.
        """
        machine_id = self._machine_id()
        if machine_id is None:
            machine_id = host_selected_machine_id(snapshot, self._request_host())
        if machine_id == LOCAL_MACHINE_ID:
            return None
        machine = snapshot.machines.get(machine_id)
        if machine is None or not self.private_scope:
            raise MachineRequestError(404, "unknown machine")
        if not machine.available:
            raise MachineRequestError(503, f"machine {machine.id} is unavailable; choose another machine")
        return machine

    def handle_machines(self, *, send_body: bool) -> None:
        snapshot = self._fail_closed_snapshot(send_body=send_body)
        if snapshot is None:
            return
        selected = host_selected_machine_id(snapshot, self._request_host())
        if not self.private_scope and selected != LOCAL_MACHINE_ID:
            selected = LOCAL_MACHINE_ID
        payload = machines_payload(snapshot, selected=selected, private=self.private_scope)
        # Server scope alone decides whether the portal may manage host
        # connections; a public catalog can neither expose nor mutate them.
        payload["can_manage_connections"] = self.private_scope
        self.write_json(payload, send_body=send_body)

    def handle_connections_get(self, *, send_body: bool) -> None:
        if self._fail_closed_snapshot(send_body=send_body) is None:
            return
        check = _single_check_flag(urllib.parse.urlparse(self.path).query)
        if check is None:
            self.write_json({"error": "check must be exactly 1"}, status=400, send_body=send_body)
            return
        try:
            payload = agent_connection_targets(check=check)
        except AgentUnavailable:
            self.write_json(
                {"error": "host agent unavailable; connection control requires the portmap agent"},
                status=502,
                send_body=send_body,
            )
            return
        except AgentRequestError as exc:
            self.write_json({"error": str(exc)}, status=self._agent_error_status(exc.status), send_body=send_body)
            return
        self.write_json(payload, send_body=send_body)

    def handle_connections_post(self) -> None:
        if self._fail_closed_snapshot(send_body=True) is None:
            return
        if not self._same_origin():
            self.write_json({"error": "connection control requires a same-origin request"}, status=403, send_body=True)
            return
        try:
            payload = self._read_json_body()
        except RequestBodyError as exc:
            self.write_json({"error": exc.message}, status=exc.status, send_body=True)
            return
        try:
            status, result = agent_mutate_connection(payload)
        except AgentUnavailable:
            self.write_json(
                {"error": "host agent unavailable; connection control requires the portmap agent"},
                status=502,
                send_body=True,
            )
            return
        except AgentRequestError as exc:
            self.write_json({"error": str(exc)}, status=self._agent_error_status(exc.status), send_body=True)
            return
        self.write_json(result, status=status, send_body=True)

    def handle_updates_get(self, *, send_body: bool) -> None:
        if self._fail_closed_snapshot(send_body=send_body) is None:
            return
        check = _single_check_flag(urllib.parse.urlparse(self.path).query)
        if check is None:
            self.write_json({"error": "check must be exactly 1"}, status=400, send_body=send_body)
            return
        try:
            payload = agent_update_status(check=check)
        except AgentUnavailable:
            self.write_json(
                {"error": "host agent unavailable; update control requires the portmap agent"},
                status=502,
                send_body=send_body,
            )
            return
        except AgentRequestError as exc:
            self.write_json({"error": str(exc)}, status=self._agent_error_status(exc.status), send_body=send_body)
            return
        self.write_json(payload, send_body=send_body)

    def handle_updates_job(self, batch_id: str, *, send_body: bool) -> None:
        try:
            status, body = agent_update_job(batch_id)
        except AgentUnavailable:
            self.write_json(
                {"error": "host agent unavailable; update control requires the portmap agent"},
                status=502,
                send_body=send_body,
            )
            return
        except AgentRequestError as exc:
            self.write_json({"error": str(exc)}, status=self._agent_error_status(exc.status), send_body=send_body)
            return
        self.write_json(body, status=status, send_body=send_body)

    def handle_updates_post(self) -> None:
        if self._fail_closed_snapshot(send_body=True) is None:
            return
        if not self._same_origin():
            self.write_json({"error": "update control requires a same-origin request"}, status=403, send_body=True)
            return
        try:
            payload = self._read_updates_json_body()
        except RequestBodyError as exc:
            self.write_json({"error": exc.message}, status=exc.status, send_body=True)
            return
        try:
            status, body = agent_update_start(payload)
        except AgentUnavailable:
            self.write_json(
                {"error": "host agent unavailable; update control requires the portmap agent"},
                status=502,
                send_body=True,
            )
            return
        except AgentRequestError as exc:
            self.write_json({"error": str(exc)}, status=self._agent_error_status(exc.status), send_body=True)
            return
        self.write_json(body, status=status, send_body=True)

    def handle_updates_token_post(self) -> None:
        if not self._same_origin():
            self.write_json({"error": "capability reveal requires a same-origin request"}, status=403, send_body=True)
            return
        self._relay_updates_json(agent_update_token)

    def handle_updates_credentials_post(self) -> None:
        if not self._same_origin():
            self.write_json({"error": "capability pairing requires a same-origin request"}, status=403, send_body=True)
            return
        self._relay_updates_json(agent_update_credentials)

    def _relay_updates_json(self, forward: Any) -> None:
        try:
            payload = self._read_updates_json_body()
        except RequestBodyError as exc:
            self.write_json({"error": exc.message}, status=exc.status, send_body=True)
            return
        try:
            status, body = forward(payload)
        except AgentUnavailable:
            self.write_json(
                {"error": "host agent unavailable; update control requires the portmap agent"},
                status=502,
                send_body=True,
            )
            return
        except AgentRequestError as exc:
            self.write_json({"error": str(exc)}, status=self._agent_error_status(exc.status), send_body=True)
            return
        self.write_json(body, status=status, send_body=True)

    def _read_updates_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("content-length")
        if raw_length is None:
            raise RequestBodyError(400, "update control requires a JSON request body with a content length")
        try:
            length = int(raw_length)
        except ValueError:
            raise RequestBodyError(400, "invalid content length") from None
        if length <= 0:
            raise RequestBodyError(400, "update control requires a JSON request body")
        if length > UPDATES_JSON_MAX_BYTES:
            raise RequestBodyError(413, "update control request body is too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise RequestBodyError(400, "update control request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise RequestBodyError(400, "update control request body must be a JSON object")
        return payload

    def handle_version(self, *, send_body: bool) -> None:
        try:
            payload = agent_update_version()
        except (AgentUnavailable, AgentRequestError, RuntimeError):
            payload = {"version": __version__, "build_id": None, "update_api": True}
        payload["catalog_version"] = __version__
        self.write_json(payload, send_body=send_body)

    def handle_update_agent_get(self, path: str, *, send_body: bool) -> None:
        self._relay_update_agent("GET", path, b"", send_body=send_body)

    def handle_update_agent_post(self, path: str) -> None:
        parsed = urllib.parse.urlparse(path)
        if parsed.path == UPDATE_AGENT_PAIR_PATH:
            if not self.private_scope or not self._same_origin():
                self.write_json(
                    {"error": "capability pairing requires a same-origin private request"},
                    status=403, send_body=True,
                )
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self.write_json(
                    {"error": "capability pairing requires application/json"},
                    status=415, send_body=True,
                )
                return
        try:
            body = self._read_raw_body(UPDATE_AGENT_MAX_BYTES)
        except RequestBodyError as exc:
            self.write_json({"error": exc.message}, status=exc.status, send_body=True)
            return
        self._relay_update_agent("POST", path, body, send_body=True)

    def _read_raw_body(self, max_bytes: int) -> bytes:
        raw_length = self.headers.get("content-length")
        if raw_length is None:
            raise RequestBodyError(400, "update-agent request requires a content length")
        try:
            length = int(raw_length)
        except ValueError:
            raise RequestBodyError(400, "invalid content length") from None
        if length < 0:
            raise RequestBodyError(400, "invalid content length")
        if length > max_bytes:
            raise RequestBodyError(413, "update-agent request body is too large")
        if length == 0:
            return b""
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestBodyError(400, "incomplete update-agent request body")
        return body

    def _relay_update_agent(self, method: str, path: str, body: bytes, *, send_body: bool) -> None:
        try:
            text = body.decode("utf-8")
        except UnicodeError:
            self.write_json({"error": "update-agent request body must be UTF-8"}, status=400, send_body=send_body)
            return
        payload = {
            "method": method,
            "path": path,
            "body": text,
            "scope": "private" if self.private_scope else "public",
            "signature": self.headers.get(SIGNATURE_HEADER),
            "timestamp": self.headers.get(TIMESTAMP_HEADER),
            "nonce": self.headers.get(NONCE_HEADER),
        }
        try:
            status, envelope = agent_update_agent_dispatch(payload)
        except AgentUnavailable:
            self.write_json(
                {"error": "update agent unavailable on this machine"},
                status=502,
                send_body=send_body,
            )
            return
        except AgentRequestError as exc:
            self.write_json({"error": str(exc)}, status=self._agent_error_status(exc.status), send_body=send_body)
            return
        if status >= 400 or not isinstance(envelope.get("body"), str):
            self.write_json({"error": "update agent dispatch failed"}, status=502, send_body=send_body)
            return
        response_body = envelope["body"].encode("utf-8")
        headers = envelope.get("headers") if isinstance(envelope.get("headers"), dict) else {}
        self.send_response(int(envelope.get("status") or 200))
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(response_body)))
        for key, value in headers.items():
            if isinstance(key, str) and isinstance(value, str):
                self.send_header(key, value)
        self.end_headers()
        if send_body:
            self.wfile.write(response_body)

    def _same_origin(self) -> bool:
        """Browsers send Origin on same-origin JSON mutations; all else is foreign."""
        origins = self.headers.get_all("Origin", [])
        hosts = self.headers.get_all("Host", [])
        if len(origins) != 1 or len(hosts) != 1 or request_host(hosts[0]) is None:
            return False
        try:
            parsed = urllib.parse.urlsplit(origins[0])
            host_parts = urllib.parse.urlsplit(f"//{hosts[0]}")
            origin_port = parsed.port
            host_port = host_parts.port
        except ValueError:
            return False
        if parsed.scheme.lower() != "http" or parsed.path or parsed.query or parsed.fragment:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        return parsed.hostname == host_parts.hostname and (
            origin_port if origin_port is not None else 80
        ) == (host_port if host_port is not None else 80)

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("content-length")
        if raw_length is None:
            raise RequestBodyError(400, "connection control requires a JSON request body with a content length")
        try:
            length = int(raw_length)
        except ValueError:
            raise RequestBodyError(400, "invalid content length") from None
        if length <= 0:
            raise RequestBodyError(400, "connection control requires a JSON request body")
        if length > CONNECTION_JSON_MAX_BYTES:
            raise RequestBodyError(413, "connection control request body is too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise RequestBodyError(400, "connection control request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise RequestBodyError(400, "connection control request body must be a JSON object")
        return payload

    @staticmethod
    def _agent_error_status(status: int) -> int:
        return 502 if status >= 500 else status

    def handle_request(self, *, send_body: bool) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/healthz", "/readyz"}:
            if self.private_scope:
                # Controller readiness only: the private scope cannot claim
                # ready without a valid snapshot, an owner, and the container
                # DNS actually serving the snapshot's ownership marker. The
                # public scope keeps ordinary liveness and never exposes
                # private config or readiness data.
                try:
                    snapshot = load_access_snapshot()
                except PortmapError:
                    snapshot = None
                if snapshot is not None and snapshot.owner and dns_marker_verified(snapshot):
                    # The reported marker lets the controller compare the
                    # served revision against its own desired revision.
                    self.write_json(
                        {"ok": True, "owner": snapshot.owner, "dns_marker": snapshot.dns_marker},
                        send_body=send_body,
                    )
                else:
                    self.write_json({"ok": False}, status=503, send_body=send_body)
            else:
                self.write_text("ok\n", content_type="text/plain", send_body=send_body)
            return

        if parsed.path == "/api/machines":
            self.handle_machines(send_body=send_body)
            return

        if parsed.path == CONNECTION_CONTROL_PATH:
            if not self.private_scope:
                self.write_not_found(send_body=send_body)
                return
            self.handle_connections_get(send_body=send_body)
            return

        if parsed.path == VERSION_API_PATH:
            # Minimal running identity on both scopes; never management
            # state, jobs, or secrets.
            self.handle_version(send_body=send_body)
            return

        if parsed.path.startswith(UPDATE_AGENT_PREFIX):
            # Authenticated remote update operations; the agent verifies the
            # signature so the capability never has to exist here.
            self.handle_update_agent_get(self.path, send_body=send_body)
            return

        if parsed.path == UPDATES_API_PATH:
            if not self.private_scope:
                self.write_not_found(send_body=send_body)
                return
            self.handle_updates_get(send_body=send_body)
            return

        if parsed.path.startswith(UPDATES_JOBS_PREFIX):
            if not self.private_scope:
                self.write_not_found(send_body=send_body)
                return
            batch_id = parsed.path[len(UPDATES_JOBS_PREFIX):]
            if UPDATE_JOB_ID_RE.fullmatch(batch_id) is None:
                self.write_not_found(send_body=send_body)
                return
            self.handle_updates_job(batch_id, send_body=send_body)
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
            self.handle_registry(send_body=send_body)
            return

        if parsed.path == "/api/downloads":
            self.write_json(download_info(), send_body=send_body)
            return

        if parsed.path == "/install.sh":
            self.handle_install_script(send_body=send_body)
            return

        if parsed.path.startswith(DOWNLOADS_PREFIX):
            requested = parsed.path[len(DOWNLOADS_PREFIX):]
            self.handle_download(urllib.parse.unquote(requested), send_body=send_body)
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

    def handle_registry(self, *, send_body: bool) -> None:
        snapshot = self._fail_closed_snapshot(send_body=send_body)
        if snapshot is None:
            return
        try:
            machine = self._resolve_machine(snapshot)
        except MachineRequestError as exc:
            self.write_json({"error": exc.message}, status=exc.status, send_body=send_body)
            return
        if machine is None:
            try:
                catalog = collect_catalog()
            except Exception as exc:  # pragma: no cover - exercised through container runtime.
                self.send_response(500)
                self.send_header("content-type", "text/plain; charset=utf-8")
                self.end_headers()
                if send_body:
                    self.wfile.write(f"failed to read Docker catalog: {exc}\n".encode("utf-8"))
                return
            catalog["machine"] = local_descriptor(snapshot)
            self.write_json(catalog, send_body=send_body)
            return
        try:
            info = probe_gateway(machine.backend_url, expected_domain=machine.domain, timeout=REMOTE_PROBE_TIMEOUT, http_host="localhost")
            projected = project_catalog(
                info.catalog,
                machine.profile(),
                machine.http_port,
                dns_address=snapshot.dns_address,
                dns_port=snapshot.dns_port,
            )
        except Exception:
            # Probe and projection errors carry remote transport details.
            self.write_json({"error": "remote catalog unavailable; check portmap connections"}, status=502, send_body=send_body)
            return
        projected["machine"] = machine_descriptor(machine)
        self.write_json(projected, send_body=send_body)

    def read_form_body(self) -> bytes:
        try:
            length = min(int(self.headers.get("content-length", "0") or "0"), MAX_FORM_BYTES)
        except ValueError:
            length = 0
        return self.rfile.read(length)

    def read_form(self) -> dict[str, list[str]]:
        payload = self.read_form_body().decode("utf-8", errors="replace")
        return urllib.parse.parse_qs(payload)

    def handle_compose_up(self) -> None:
        form = self.read_form()
        worktree = first_form_value(form, "worktree")
        status = 200
        try:
            result = agent_compose_up_worktree(worktree)
        except AgentUnavailable as exc:
            # No in-container Compose execution: host starts belong to the
            # host agent only.
            status = 400
            result = {
                "worktree": worktree,
                "ok": False,
                "message": f"agent unavailable: {exc}",
                "errors": [],
            }
        except Exception as exc:  # pragma: no cover - exercised through container runtime.
            status = 400
            result = {
                "worktree": worktree,
                "ok": False,
                "message": str(exc),
                "errors": [],
            }
        self.write_json(result, status=status, send_body=True)

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

    def handle_install_script(self, *, send_body: bool) -> None:
        try:
            body = install_script_body()
        except DownloadUnavailable as exc:
            self.write_json({"error": exc.message}, status=exc.status, send_body=send_body)
            return
        self.send_response(200)
        self.send_header("content-type", INSTALL_SCRIPT_CONTENT_TYPE)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def handle_download(self, filename: str, *, send_body: bool) -> None:
        try:
            archive = resolve_archive(filename)
        except DownloadUnavailable as exc:
            self.write_json({"error": exc.message}, status=exc.status, send_body=send_body)
            return
        size = archive.stat().st_size
        self.send_response(200)
        self.send_header("content-type", ARCHIVE_CONTENT_TYPE)
        self.send_header("content-length", str(size))
        self.end_headers()
        if not send_body:
            return
        with archive.open("rb") as handle:
            while chunk := handle.read(65536):
                self.wfile.write(chunk)



def main() -> None:
    host = os.environ.get("PORTMAP_CATALOG_LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("PORTMAP_CATALOG_LISTEN_PORT", "8081"))
    public_port = int(os.environ.get("PORTMAP_CATALOG_PUBLIC_LISTEN_PORT", "8082"))
    server = CatalogServer((host, port), scope="private")
    print(f"portmap catalog listening on {host}:{port}")
    if public_port > 0:
        public = CatalogServer((host, public_port), scope="public")
        print(f"portmap catalog listening on {host}:{public_port} (public)")
        threading.Thread(target=public.serve_forever, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
