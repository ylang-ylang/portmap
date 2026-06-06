import http.client
import json
import os
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap.catalog import (
    CatalogHandler,
    collect_catalog,
    compose_down_project,
    compose_restart_project,
    compose_up_worktree,
    container_to_service,
    parse_host_rule,
    read_static_asset,
    select_dns_server,
)


@pytest.fixture(autouse=True)
def missing_agent_socket(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(tmp_path / "missing-agent.sock"))


def request_catalog(
    path: str,
    *,
    method: str = "GET",
    body: str | None = None,
) -> tuple[int, http.client.HTTPMessage, bytes]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        headers = {"content-type": "application/x-www-form-urlencoded"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def write_git_compose_repo(path: Path, *, with_compose: bool = True, with_endpoints: bool = True) -> None:
    path.mkdir(parents=True)
    run(["git", "init", "-b", "dev"], cwd=path)
    run(["git", "config", "user.email", "portmap-test@example.invalid"], cwd=path)
    run(["git", "config", "user.name", "portmap test"], cwd=path)
    (path / "README.md").write_text("sample\n", encoding="utf-8")
    if with_compose:
        (path / "docker-compose.yml").write_text(
            """
services:
  frontend:
    image: python:3.12-alpine
    expose:
      - "8000"
""".lstrip(),
            encoding="utf-8",
        )
    if with_endpoints:
        (path / ".portmap").mkdir()
        (path / ".portmap" / "endpoints.toml").write_text(
            """
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 8000
""".lstrip(),
            encoding="utf-8",
        )
    run(["git", "add", "."], cwd=path)
    run(["git", "commit", "-m", "init sample compose repo"], cwd=path)


def run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    result = subprocess.run(
        args,
        cwd=cwd,
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(args)}\n"
            f"cwd: {cwd}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def test_container_to_service_uses_portmap_labels() -> None:
    service = container_to_service(
        {
            "Names": ["/sample-frontend-1"],
            "Image": "sample:latest",
            "Labels": {
                "com.docker.compose.project": "sample",
                "com.docker.compose.service": "frontend",
                "com.docker.compose.project.working_dir": "/tmp/sample",
                "traefik.enable": "true",
                "traefik.docker.network": "portmap_gateway",
                "portmap.managed": "true",
                "portmap.repo_id": "sample-repo",
                "portmap.repo_name": "sample",
                "portmap.branch": "feat-example",
                "portmap.worktree": "/repo/sample",
                "portmap.endpoints.sample-feat-example-frontend.name": "frontend",
                "portmap.endpoints.sample-feat-example-frontend.order": "0",
                "portmap.endpoints.sample-feat-example-frontend.kind": "http",
                "portmap.endpoints.sample-feat-example-frontend.container_port": "5173",
                "portmap.endpoints.sample-feat-example-frontend.url": (
                    "http://frontend.feat-example.sample.debug.lan:8080"
                ),
            },
        }
    )

    assert service is not None
    assert service["repo_name"] == "sample"
    assert service["branch"] == "feat-example"
    assert service["compose_service"] == "frontend"
    assert service["portmap_order"] == 0
    assert service["endpoints"] == [
        {
            "id": "sample-feat-example-frontend",
            "name": "frontend",
            "order": 0,
            "kind": "http",
            "container_port": 5173,
            "url": "http://frontend.feat-example.sample.debug.lan:8080",
        }
    ]


def test_container_to_service_orders_portmap_endpoints_by_toml_order() -> None:
    service = container_to_service(
        {
            "Names": ["/sample-api-1"],
            "Image": "sample:latest",
            "Labels": {
                "com.docker.compose.project": "sample",
                "com.docker.compose.service": "api",
                "traefik.enable": "true",
                "portmap.managed": "true",
                "portmap.repo_name": "sample",
                "portmap.branch": "dev",
                "portmap.worktree": "/repo/sample",
                "portmap.endpoints.sample-dev-zeta.name": "zeta",
                "portmap.endpoints.sample-dev-zeta.order": "2",
                "portmap.endpoints.sample-dev-zeta.kind": "http",
                "portmap.endpoints.sample-dev-zeta.container_port": "9002",
                "portmap.endpoints.sample-dev-alpha.name": "alpha",
                "portmap.endpoints.sample-dev-alpha.order": "0",
                "portmap.endpoints.sample-dev-alpha.kind": "http",
                "portmap.endpoints.sample-dev-alpha.container_port": "9000",
                "portmap.endpoints.sample-dev-beta.name": "beta",
                "portmap.endpoints.sample-dev-beta.order": "1",
                "portmap.endpoints.sample-dev-beta.kind": "http",
                "portmap.endpoints.sample-dev-beta.container_port": "9001",
            },
        }
    )

    assert service is not None
    assert service["portmap_order"] == 0
    assert [endpoint["name"] for endpoint in service["endpoints"]] == ["alpha", "beta", "zeta"]


def test_container_to_service_marks_deleted_worktree(tmp_path: Path) -> None:
    missing_worktree = tmp_path / "deleted-worktree"

    service = container_to_service(
        {
            "Names": ["/sample-api-1"],
            "Image": "sample:latest",
            "Labels": {
                "com.docker.compose.project": "sample",
                "com.docker.compose.service": "api",
                "traefik.enable": "true",
                "portmap.managed": "true",
                "portmap.repo_name": "sample",
                "portmap.branch": "feat-deleted",
                "portmap.worktree": str(missing_worktree),
                "portmap.endpoints.sample-feat-deleted-api.name": "api",
                "portmap.endpoints.sample-feat-deleted-api.order": "0",
                "portmap.endpoints.sample-feat-deleted-api.kind": "http",
                "portmap.endpoints.sample-feat-deleted-api.container_port": "9000",
            },
        }
    )

    assert service is not None
    assert service["worktree"] == str(missing_worktree)
    assert service["worktree_exists"] is False
    assert service["worktree_status"] == "deleted"
    assert service["worktree_status_message"] == "worktree directory not found"
    assert service["worktree_root"] is None


def test_container_to_service_marks_submodule_worktree(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child_source = tmp_path / "browserdeck-source"
    submodule_path = parent / "modules" / "browserdeck"

    child_source.mkdir()
    run(["git", "init", "-b", "main"], cwd=child_source)
    run(["git", "config", "user.email", "portmap-test@example.invalid"], cwd=child_source)
    run(["git", "config", "user.name", "portmap test"], cwd=child_source)
    (child_source / "README.md").write_text("child\n", encoding="utf-8")
    run(["git", "add", "."], cwd=child_source)
    run(["git", "commit", "-m", "init child"], cwd=child_source)

    parent.mkdir()
    run(["git", "init", "-b", "main"], cwd=parent)
    run(["git", "config", "user.email", "portmap-test@example.invalid"], cwd=parent)
    run(["git", "config", "user.name", "portmap test"], cwd=parent)
    (parent / "README.md").write_text("parent\n", encoding="utf-8")
    run(["git", "add", "."], cwd=parent)
    run(["git", "commit", "-m", "init parent"], cwd=parent)
    run(["git", "-c", "protocol.file.allow=always", "submodule", "add", str(child_source), "modules/browserdeck"], cwd=parent)
    run(["git", "commit", "-m", "add browserdeck submodule"], cwd=parent)

    service = container_to_service(
        {
            "Names": ["/browserdeck-api-1"],
            "Image": "sample:latest",
            "Labels": {
                "com.docker.compose.project": "browserdeck",
                "com.docker.compose.service": "api",
                "traefik.enable": "true",
                "portmap.managed": "true",
                "portmap.repo_name": "browserdeck",
                "portmap.branch": "case-comap-frontend-debug",
                "portmap.worktree": str(submodule_path),
                "portmap.endpoints.browserdeck-case-api.name": "api",
                "portmap.endpoints.browserdeck-case-api.order": "0",
                "portmap.endpoints.browserdeck-case-api.kind": "http",
                "portmap.endpoints.browserdeck-case-api.container_port": "9000",
            },
        }
    )

    assert service is not None
    assert service["worktree_exists"] is True
    assert service["worktree_status"] == "submodule"
    assert service["worktree_superproject"] == str(parent.resolve())
    assert service["worktree_status_message"] == f"submodule under {parent.resolve()}"


def test_container_to_service_can_fallback_to_traefik_labels() -> None:
    service = container_to_service(
        {
            "Names": ["/legacy-frontend-1"],
            "Image": "legacy:latest",
            "Labels": {
                "com.docker.compose.project": "legacy",
                "com.docker.compose.service": "frontend",
                "traefik.enable": "true",
                "traefik.http.routers.legacy-frontend.rule": "Host(`frontend.legacy.debug.lan`)",
                "traefik.http.routers.legacy-frontend.entrypoints": "web",
                "traefik.http.routers.legacy-frontend.service": "legacy-frontend",
                "traefik.http.services.legacy-frontend.loadbalancer.server.port": "5173",
            },
        }
    )

    assert service is not None
    assert service["endpoints"][0]["host"] == "frontend.legacy.debug.lan"
    assert service["endpoints"][0]["container_port"] == 5173


def test_parse_host_rule() -> None:
    assert parse_host_rule("Host(`frontend.example.test`)") == "frontend.example.test"
    assert parse_host_rule("PathPrefix(`/`)") is None


def test_select_dns_server_prefers_external_bind_ip() -> None:
    external_bind = "external-bind"
    external_target = "external-target"

    assert select_dns_server(external_bind, external_target) == external_bind
    assert select_dns_server("0.0.0.0", external_target) == external_target
    assert select_dns_server("127.0.0.1", external_target) == external_target


def test_collect_catalog_uses_agent_worktrees_to_enrich_running_services(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=0":
            return [
                {
                    "Names": ["/sample-frontend-1"],
                    "Image": "sample:latest",
                    "Labels": {
                        "com.docker.compose.project": "sample_dev",
                        "com.docker.compose.service": "frontend",
                        "traefik.enable": "true",
                        "portmap.managed": "true",
                        "portmap.repo_id": "sample-repo",
                        "portmap.repo_name": "sample",
                        "portmap.branch": "dev",
                        "portmap.worktree": "/repo/sample@dev",
                        "portmap.endpoints.sample-dev-frontend.name": "frontend",
                        "portmap.endpoints.sample-dev-frontend.order": "0",
                        "portmap.endpoints.sample-dev-frontend.kind": "http",
                        "portmap.endpoints.sample-dev-frontend.container_port": "5173",
                    },
                }
            ]
        raise AssertionError(path)

    def fake_agent_worktrees():
        return {
            "ok": True,
            "generated_at": "now",
            "worktrees": [
                {
                    "repo_id": "sample-repo",
                    "repo_name": "sample",
                    "branch": "dev",
                    "worktree": "/repo/sample@dev",
                    "worktree_title": "sample@dev",
                    "worktree_root": "/repo/linked-git/sample.git",
                    "worktree_root_title": "sample linked .git",
                    "worktree_exists": True,
                    "worktree_status": "ok",
                    "worktree_status_message": "",
                    "worktree_superproject": None,
                    "compose_project": "sample_dev",
                    "running": True,
                    "startable": True,
                },
                {
                    "repo_id": "sample-repo",
                    "repo_name": "sample",
                    "branch": "feat-example",
                    "worktree": "/repo/sample@feat-example",
                    "worktree_title": "sample@feat-example",
                    "worktree_root": "/repo/linked-git/sample.git",
                    "worktree_root_title": "sample linked .git",
                    "compose_project": "sample_feat_example",
                    "running": False,
                    "startable": True,
                },
            ],
        }

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)
    monkeypatch.setattr("portmap.catalog.agent_worktrees", fake_agent_worktrees)

    catalog = collect_catalog()

    assert catalog["agent"]["available"] is True
    assert len(catalog["worktrees"]) == 2
    assert catalog["services"][0]["worktree_root"] == "/repo/linked-git/sample.git"
    assert catalog["services"][0]["worktree_root_title"] == "sample linked .git"
    assert catalog["services"][0]["worktree_exists"] is True
    assert catalog["services"][0]["worktree_status"] == "ok"
    assert catalog["services"][0]["worktree_status_message"] == ""


def test_collect_catalog_orders_services_by_portmap_endpoint_order(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=0":
            return [
                {
                    "Names": ["/sample-worker-1"],
                    "Image": "sample:latest",
                    "Labels": {
                        "com.docker.compose.project": "sample_dev",
                        "com.docker.compose.service": "worker",
                        "traefik.enable": "true",
                        "portmap.managed": "true",
                        "portmap.repo_id": "sample-repo",
                        "portmap.repo_name": "sample",
                        "portmap.branch": "dev",
                        "portmap.worktree": "/repo/sample@dev",
                        "portmap.endpoints.sample-dev-worker.name": "worker",
                        "portmap.endpoints.sample-dev-worker.order": "2",
                        "portmap.endpoints.sample-dev-worker.kind": "http",
                        "portmap.endpoints.sample-dev-worker.container_port": "9002",
                    },
                },
                {
                    "Names": ["/sample-frontend-1"],
                    "Image": "sample:latest",
                    "Labels": {
                        "com.docker.compose.project": "sample_dev",
                        "com.docker.compose.service": "frontend",
                        "traefik.enable": "true",
                        "portmap.managed": "true",
                        "portmap.repo_id": "sample-repo",
                        "portmap.repo_name": "sample",
                        "portmap.branch": "dev",
                        "portmap.worktree": "/repo/sample@dev",
                        "portmap.endpoints.sample-dev-frontend.name": "frontend",
                        "portmap.endpoints.sample-dev-frontend.order": "0",
                        "portmap.endpoints.sample-dev-frontend.kind": "http",
                        "portmap.endpoints.sample-dev-frontend.container_port": "9000",
                    },
                },
            ]
        raise AssertionError(path)

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)

    catalog = collect_catalog()

    assert [service["compose_service"] for service in catalog["services"]] == ["frontend", "worker"]
    assert [service["portmap_order"] for service in catalog["services"]] == [0, 2]


def test_catalog_static_frontend_uses_registry_and_dns_probe() -> None:
    index = read_static_asset("index.html")
    script = read_static_asset("assets/catalog.js")
    stylesheet = read_static_asset("assets/catalog.css")
    dns_check = read_static_asset("assets/dns-check.svg")
    favicon = read_static_asset("favicon.svg")

    assert index is not None
    assert script is not None
    assert stylesheet is not None
    assert dns_check is not None
    assert favicon is not None

    index_body, index_type = index
    script_body, script_type = script
    stylesheet_body, stylesheet_type = stylesheet
    dns_body, dns_type = dns_check
    favicon_body, favicon_type = favicon

    assert index_type == "text/html; charset=utf-8"
    assert script_type == "application/javascript; charset=utf-8"
    assert stylesheet_type == "text/css; charset=utf-8"
    assert dns_type == "image/svg+xml"
    assert favicon_type == "image/svg+xml"
    assert b'id="root"' in index_body
    assert b'/favicon.svg' in index_body
    assert b'/assets/catalog.js' in index_body
    assert b'Test command' not in index_body
    assert b'/registry.json' in script_body
    assert b'/assets/dns-check.svg' in script_body
    assert b'data-catalog-tree' in script_body
    assert b'data-project-group' in script_body
    assert b'data-worktree-group' in script_body
    assert b'data-branch-group' in script_body
    assert b'data-dead-panel' in script_body
    assert b'dead-menu' in script_body
    assert b'running-menu' in script_body
    assert b'branch_tip_epoch' in script_body
    assert b'portmap_order' in script_body
    assert b'worktree-status-badge' in script_body
    assert b'branch-name-deleted' in script_body
    assert b'deleted' in script_body
    assert b'submodule' in script_body
    assert b'is-empty' in script_body
    assert b'has-items' in script_body
    assert b'History' not in script_body
    assert b'dns-status' in script_body
    assert b'Action log' in script_body
    assert b'action-log' in script_body
    assert b'split-dns-unset' in script_body
    assert b'noopener noreferrer' in script_body
    assert b'/actions/compose-' in script_body
    assert b'resolvectl revert "$DNS_IFACE"' in script_body
    assert b'split-dns-test' not in script_body
    assert b'succ' in script_body
    assert b'/actions/compose-up' in script_body
    assert b'Start' in script_body
    assert b'.project-group' in stylesheet_body
    assert b'.worktree-group' in stylesheet_body
    assert b'.branch-group' in stylesheet_body
    assert b'.dead-menu' in stylesheet_body
    assert b'.running-menu' in stylesheet_body
    assert b'.running-action' in stylesheet_body
    assert b'gap: 8px' in stylesheet_body
    assert b'.running-menu-button.is-empty' in stylesheet_body
    assert b'.dead-menu-button.is-empty' in stylesheet_body
    assert b'inline-size: 144px' in stylesheet_body
    assert b'.branch-running-name' in stylesheet_body
    assert b'.branch-name-deleted' in stylesheet_body
    assert b'.worktree-status-deleted' in stylesheet_body
    assert b'.worktree-status-submodule' in stylesheet_body
    assert b'.history-panel' not in stylesheet_body
    assert stylesheet_body.count(b"\n") > 20
    assert b'.dns-status' in stylesheet_body
    assert b'.dns-status-ok' in stylesheet_body
    assert b'.dns-status-failed' in stylesheet_body
    assert b'.action-log' in stylesheet_body
    assert b'<svg ' in dns_body
    assert b'pM' in favicon_body


def test_catalog_static_asset_rejects_unknown_paths() -> None:
    assert read_static_asset("../catalog.py") is None
    assert read_static_asset("assets/../catalog.py") is None
    assert read_static_asset("missing.js") is None


def test_catalog_http_serves_vite_public_root_static_assets() -> None:
    status, headers, body = request_catalog("/favicon.svg")

    assert status == 200
    assert headers["content-type"] == "image/svg+xml"
    assert b"pM" in body


def test_catalog_http_rejects_missing_public_root_static_assets() -> None:
    status, headers, body = request_catalog("/missing.svg")

    assert status == 404
    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert body == b"not found\n"


def test_collect_catalog_includes_running_worktree_and_writes_history(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    history = tmp_path / "history.json"
    write_git_compose_repo(repo)
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(history))

    def fake_docker_get(path: str):
        if path == "/containers/json?all=0":
            return [
                {
                    "Names": ["/sample-frontend-1"],
                    "Image": "sample:latest",
                    "Labels": {
                        "com.docker.compose.project": "sample_dev",
                        "com.docker.compose.service": "frontend",
                        "traefik.enable": "true",
                        "portmap.managed": "true",
                        "portmap.repo_name": "sample",
                        "portmap.branch": "dev",
                        "portmap.worktree": str(repo),
                        "portmap.endpoints.frontend.name": "frontend",
                        "portmap.endpoints.frontend.kind": "http",
                        "portmap.endpoints.frontend.container_port": "8000",
                        "portmap.endpoints.frontend.url": "http://frontend.dev.sample.debug.lan:8080",
                    },
                }
            ]
        raise AssertionError(path)

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)

    catalog = collect_catalog()

    assert catalog["services"][0]["worktree"] == str(repo)
    assert catalog["services"][0]["worktree_root"] == str(repo / ".git")
    assert catalog["worktrees"][0]["worktree"] == str(repo)
    assert catalog["worktrees"][0]["worktree_root"] == str(repo / ".git")
    assert catalog["worktrees"][0]["branch"] == "dev"
    assert catalog["worktrees"][0]["running"] is True
    assert catalog["worktrees"][0]["startable"] is True
    assert catalog["worktrees"][0]["source"] == "current"

    payload = json.loads(history.read_text(encoding="utf-8"))
    assert payload["worktrees"][0]["worktree"] == str(repo)
    assert payload["worktrees"][0]["worktree_root"] == str(repo / ".git")


def test_collect_catalog_orders_worktrees_by_branch_tip_descending(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    feat = tmp_path / "sample@feat-new"
    write_git_compose_repo(repo)

    (repo / "dev.txt").write_text("dev\n", encoding="utf-8")
    run(["git", "add", "dev.txt"], cwd=repo)
    run(
        ["git", "commit", "-m", "dev tip"],
        cwd=repo,
        env={
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        },
    )
    run(["git", "worktree", "add", "-b", "feat-new", str(feat)], cwd=repo)
    (feat / "feat.txt").write_text("feat\n", encoding="utf-8")
    run(["git", "add", "feat.txt"], cwd=feat)
    run(
        ["git", "commit", "-m", "feat tip"],
        cwd=feat,
        env={
            "GIT_AUTHOR_DATE": "2026-02-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-02-01T00:00:00+00:00",
        },
    )

    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(tmp_path))
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    catalog = collect_catalog()

    assert [worktree["branch"] for worktree in catalog["worktrees"]] == ["feat-new", "dev"]
    assert catalog["worktrees"][0]["branch_tip_epoch"] > catalog["worktrees"][1]["branch_tip_epoch"]
    assert catalog["worktrees"][0]["branch_tip_time"] == "2026-02-01T00:00:00+00:00"


def test_collect_catalog_discovers_startable_submodules_across_superproject_worktrees(
    tmp_path: Path,
    monkeypatch,
) -> None:
    child_source = tmp_path / "browserdeck-source"
    parent = tmp_path / "comap@dev"
    sibling = tmp_path / "comap@feat"
    submodule_relative = Path("3rdparty/browserdeck")

    write_git_compose_repo(child_source)

    parent.mkdir()
    run(["git", "init", "-b", "dev"], cwd=parent)
    run(["git", "config", "user.email", "portmap-test@example.invalid"], cwd=parent)
    run(["git", "config", "user.name", "portmap test"], cwd=parent)
    (parent / "README.md").write_text("parent\n", encoding="utf-8")
    run(["git", "add", "."], cwd=parent)
    run(["git", "commit", "-m", "init parent"], cwd=parent)
    run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(child_source),
            submodule_relative.as_posix(),
        ],
        cwd=parent,
    )
    run(["git", "commit", "-m", "add browserdeck submodule"], cwd=parent)
    run(["git", "worktree", "add", "-b", "feat-sibling", str(sibling)], cwd=parent)
    run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"], cwd=sibling)

    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(parent))
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    catalog = collect_catalog()

    submodule_worktrees = {
        worktree["worktree"]: worktree
        for worktree in catalog["worktrees"]
        if worktree["repo_name"] == "browserdeck"
    }
    expected_paths = {
        str((parent / submodule_relative).resolve()),
        str((sibling / submodule_relative).resolve()),
    }
    assert set(submodule_worktrees) == expected_paths
    assert {worktree["repo_id"] for worktree in submodule_worktrees.values()} == {
        next(iter(submodule_worktrees.values()))["repo_id"]
    }
    assert all(worktree["startable"] is True for worktree in submodule_worktrees.values())
    assert all(worktree["worktree_status"] == "submodule" for worktree in submodule_worktrees.values())
    assert {
        worktree["worktree_superproject"]
        for worktree in submodule_worktrees.values()
    } == {str(parent.resolve()), str(sibling.resolve())}
    assert {worktree["display_worktree_root"] for worktree in submodule_worktrees.values()} == {
        next(iter(submodule_worktrees.values()))["display_worktree_root"]
    }
    assert {worktree["display_worktree_root_title"] for worktree in submodule_worktrees.values()} == {
        "comap / 3rdparty/browserdeck"
    }
    assert submodule_worktrees[str((parent / submodule_relative).resolve())]["display_branch"] == "dev"
    assert submodule_worktrees[str((sibling / submodule_relative).resolve())]["display_branch"] == "feat-sibling"
    assert all(worktree["submodule_branch"] for worktree in submodule_worktrees.values())
    assert all(worktree["submodule_sha"] for worktree in submodule_worktrees.values())


def test_collect_catalog_restores_existing_worktree_from_history(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    history = tmp_path / "history.json"
    write_git_compose_repo(repo)
    history.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-06-01T00:00:00+00:00",
                "worktrees": [
                    {
                        "repo_id": "old",
                        "repo_name": "sample",
                        "branch": "dev",
                        "worktree": str(repo),
                        "worktree_title": repo.name,
                        "last_seen_at": "2026-06-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(history))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    catalog = collect_catalog()

    assert catalog["services"] == []
    assert catalog["worktrees"][0]["worktree"] == str(repo)
    assert catalog["worktrees"][0]["running"] is False
    assert catalog["worktrees"][0]["startable"] is True
    assert catalog["worktrees"][0]["source"] == "history"


def test_collect_catalog_drops_history_branches_not_checked_out(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    history = tmp_path / "history.json"
    write_git_compose_repo(repo)
    history.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-06-01T00:00:00+00:00",
                "worktrees": [
                    {
                        "repo_name": "sample",
                        "branch": "dev",
                        "worktree": str(repo),
                        "worktree_title": repo.name,
                        "last_seen_at": "2026-06-01T00:00:00+00:00",
                    },
                    {
                        "repo_name": "sample",
                        "branch": "feat-old",
                        "worktree": str(repo),
                        "worktree_title": repo.name,
                        "last_seen_at": "2026-06-02T00:00:00+00:00",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(history))
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(tmp_path))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    catalog = collect_catalog()

    by_branch = {worktree["branch"]: worktree for worktree in catalog["worktrees"]}
    assert set(by_branch) == {"dev"}
    assert by_branch["dev"]["source"] == "current"
    assert by_branch["dev"]["startable"] is True

    payload = json.loads(history.read_text(encoding="utf-8"))
    assert [entry["branch"] for entry in payload["worktrees"]] == ["dev"]


def test_collect_catalog_drops_non_startable_history(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    history = tmp_path / "history.json"
    write_git_compose_repo(repo, with_endpoints=False)
    history.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-06-01T00:00:00+00:00",
                "worktrees": [
                    {
                        "repo_name": "sample",
                        "branch": "dev",
                        "worktree": str(repo),
                        "worktree_title": repo.name,
                        "last_seen_at": "2026-06-01T00:00:00+00:00",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(history))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    catalog = collect_catalog()

    assert catalog["services"] == []
    assert catalog["worktrees"] == []


def test_catalog_compose_up_action_uses_agent(monkeypatch) -> None:
    calls = {}

    def fake_agent_compose_up(worktree: str):
        calls["worktree"] = worktree
        return {"ok": True, "message": "started by agent", "worktree": worktree}

    monkeypatch.setattr("portmap.catalog.agent_compose_up_worktree", fake_agent_compose_up)

    status, _, body = request_catalog(
        "/actions/compose-up",
        method="POST",
        body="worktree=%2Frepo%2Fsample%40dev",
    )

    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert calls["worktree"] == "/repo/sample@dev"
    assert payload["message"] == "started by agent"


def test_compose_up_worktree_runs_generated_compose_command(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo)
    calls = {}
    real_run = subprocess.run

    def fake_ensure(args, *, cwd, environ):
        calls["ensure"] = (args, cwd, environ)

    def fake_plan(args, *, cwd):
        calls["plan"] = (args, cwd)
        return SimpleNamespace(
            command=["docker", "compose", "-f", str(repo / "docker-compose.yml"), "up", "-d"],
            injected=True,
            compose_project="sample_dev",
        )

    def fake_run(command, *, cwd, env=None, text=True, stdout=None, stderr=None, check=False):
        if command and command[0] == "git":
            return real_run(
                command,
                cwd=cwd,
                env=env,
                text=text,
                stdout=stdout,
                stderr=stderr,
                check=check,
            )
        calls["run"] = {
            "command": command,
            "cwd": cwd,
            "env": env,
            "text": text,
            "stdout": stdout,
            "stderr": stderr,
            "check": check,
        }
        return SimpleNamespace(returncode=0, stdout="started\n", stderr="")

    monkeypatch.setattr("portmap.catalog.ensure_generated_override", fake_ensure)
    monkeypatch.setattr("portmap.catalog.plan_docker_compose_command", fake_plan)
    monkeypatch.setattr("portmap.catalog.subprocess.run", fake_run)

    result = compose_up_worktree(str(repo))

    assert calls["ensure"][0] == ["up", "-d"]
    assert calls["ensure"][1] == repo
    assert calls["plan"] == (["up", "-d"], repo)
    assert calls["run"]["command"] == ["docker", "compose", "-f", str(repo / "docker-compose.yml"), "up", "-d"]
    assert calls["run"]["cwd"] == repo
    assert calls["run"]["env"]["PORTMAP_BROKER_BYPASS"] == "1"
    assert result["ok"] is True
    assert result["compose_project"] == "sample_dev"


def test_compose_up_worktree_rejects_non_startable_repo(tmp_path: Path) -> None:
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo, with_endpoints=False)

    with pytest.raises(ValueError, match="missing .portmap/endpoints.toml"):
        compose_up_worktree(str(repo))


def test_compose_down_project_removes_portmap_managed_project(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=1":
            return [
                {
                    "Id": "container-one",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                        "portmap.managed": "true",
                    },
                },
                {
                    "Id": "container-two",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                    },
                },
                {
                    "Id": "container-other",
                    "Labels": {
                        "com.docker.compose.project": "other_project",
                        "portmap.managed": "true",
                    },
                },
            ]
        if path == "/networks":
            return [
                {
                    "Id": "network-one",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                    },
                },
                {
                    "Id": "network-other",
                    "Labels": {
                        "com.docker.compose.project": "other_project",
                    },
                },
            ]
        raise AssertionError(path)

    calls = []

    def fake_docker_request(method: str, path: str, *, ok_statuses=None):
        calls.append((method, path, ok_statuses))
        return b""

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)
    monkeypatch.setattr("portmap.catalog.docker_request", fake_docker_request)

    result = compose_down_project("sample_project")

    assert result["ok"] is True
    assert result["containers_removed"] == 2
    assert result["networks_removed"] == 1
    assert ("POST", "/containers/container-one/stop?t=10", {204, 304, 404, 409}) in calls
    assert ("DELETE", "/containers/container-one?v=0&force=1", {204, 404, 409}) in calls
    assert ("POST", "/containers/container-two/stop?t=10", {204, 304, 404, 409}) in calls
    assert ("DELETE", "/containers/container-two?v=0&force=1", {204, 404, 409}) in calls
    assert ("DELETE", "/networks/network-one", {204, 404}) in calls


def test_compose_down_project_rejects_unmanaged_project(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=1":
            return [
                {
                    "Id": "container-one",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                    },
                },
            ]
        raise AssertionError(path)

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)

    with pytest.raises(ValueError, match="not portmap-managed"):
        compose_down_project("sample_project")


def test_compose_restart_project_restarts_portmap_managed_project(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=1":
            return [
                {
                    "Id": "container-one",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                        "portmap.managed": "true",
                    },
                },
                {
                    "Id": "container-two",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                    },
                },
                {
                    "Id": "container-other",
                    "Labels": {
                        "com.docker.compose.project": "other_project",
                        "portmap.managed": "true",
                    },
                },
            ]
        raise AssertionError(path)

    calls = []

    def fake_docker_request(method: str, path: str, *, ok_statuses=None):
        calls.append((method, path, ok_statuses))
        return b""

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)
    monkeypatch.setattr("portmap.catalog.docker_request", fake_docker_request)

    result = compose_restart_project("sample_project")

    assert result["ok"] is True
    assert result["containers_restarted"] == 2
    assert ("POST", "/containers/container-one/restart?t=10", {204, 404}) in calls
    assert ("POST", "/containers/container-two/restart?t=10", {204, 404}) in calls


def test_compose_restart_project_rejects_unmanaged_project(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=1":
            return [
                {
                    "Id": "container-one",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                    },
                },
            ]
        raise AssertionError(path)

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)

    with pytest.raises(ValueError, match="not portmap-managed"):
        compose_restart_project("sample_project")
