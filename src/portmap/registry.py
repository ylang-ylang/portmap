from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .errors import PortmapError


def read_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise PortmapError(f"registry does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return normalize_registry(payload, source=str(path))


def read_catalog(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise PortmapError(f"cannot read catalog {url}: {exc}") from exc
    return normalize_registry(payload, source=url)


def normalize_registry(payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("repos"), dict):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("services"), list):
        return registry_from_catalog(payload)
    raise PortmapError(f"invalid registry/catalog: {source}")


def registry_from_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    repos: dict[str, Any] = {}
    for service in catalog.get("services", []):
        if not isinstance(service, dict):
            continue
        repo_id = str(service.get("repo_id") or service.get("repo_name") or "").strip()
        branch = str(service.get("branch") or "").strip()
        if not repo_id or not branch:
            continue

        repo = repos.setdefault(
            repo_id,
            {
                "display_name": service.get("repo_name") or repo_id,
                "instances": {},
            },
        )
        instance = repo["instances"].setdefault(
            branch,
            {
                "worktree": service.get("worktree"),
                "compose_project": service.get("compose_project"),
                "endpoints": {},
            },
        )
        endpoints = instance.setdefault("endpoints", {})
        for endpoint in service.get("endpoints", []):
            if not isinstance(endpoint, dict):
                continue
            name = endpoint.get("name") or endpoint.get("id")
            if not name:
                continue
            endpoints[str(name)] = endpoint
    return {"repos": repos}


def list_repos(registry: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for repo_id, repo in sorted(registry.get("repos", {}).items()):
        instances = repo.get("instances", {}) if isinstance(repo, dict) else {}
        rows.append(
            {
                "repo_id": repo_id,
                "display_name": repo.get("display_name", repo_id) if isinstance(repo, dict) else repo_id,
                "instances": sorted(instances.keys()) if isinstance(instances, dict) else [],
            }
        )
    return rows


def get_instance(registry: dict[str, Any], repo: str, instance: str) -> dict[str, Any]:
    repos = registry.get("repos", {})
    repo_payload = repos.get(repo)
    if repo_payload is None:
        repo_payload = next(
            (
                value
                for value in repos.values()
                if isinstance(value, dict) and value.get("display_name") == repo
            ),
            None,
        )
    if not isinstance(repo_payload, dict):
        raise PortmapError(f"repo not found: {repo}")
    instances = repo_payload.get("instances", {})
    if not isinstance(instances, dict) or instance not in instances:
        raise PortmapError(f"instance not found: {repo} {instance}")
    payload = instances[instance]
    if not isinstance(payload, dict):
        raise PortmapError(f"invalid instance payload: {repo} {instance}")
    return payload
