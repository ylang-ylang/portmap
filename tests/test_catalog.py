import pytest

from portmap.catalog import (
    compose_down_project,
    container_to_service,
    parse_host_rule,
    read_static_asset,
    select_dns_server,
)


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
    assert service["endpoints"] == [
        {
            "id": "sample-feat-example-frontend",
            "name": "frontend",
            "kind": "http",
            "container_port": 5173,
            "url": "http://frontend.feat-example.sample.debug.lan:8080",
        }
    ]


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
    assert select_dns_server("192.168.201.52", "192.168.201.52") == "192.168.201.52"
    assert select_dns_server("0.0.0.0", "192.168.201.52") == "192.168.201.52"
    assert select_dns_server("127.0.0.1", "10.0.0.5") == "10.0.0.5"


def test_catalog_static_frontend_uses_registry_and_dns_probe() -> None:
    index = read_static_asset("index.html")
    script = read_static_asset("catalog.js")
    dns_check = read_static_asset("dns-check.svg")

    assert index is not None
    assert script is not None
    assert dns_check is not None

    index_body, index_type = index
    script_body, script_type = script
    dns_body, dns_type = dns_check

    assert index_type == "text/html; charset=utf-8"
    assert script_type == "application/javascript; charset=utf-8"
    assert dns_type == "image/svg+xml"
    assert b'data-catalog-tree' in index_body
    assert b'/assets/catalog.js' in index_body
    assert b'split-dns-unset' in index_body
    assert b'Test command' not in index_body
    assert b'fetch("/registry.json")' in script_body
    assert b'/assets/dns-check.svg' in script_body
    assert b'buildCatalogTree' in script_body
    assert b'resolvectl revert "$DNS_IFACE"' in script_body
    assert b'split-dns-test' not in script_body
    assert b'<svg ' in dns_body


def test_catalog_static_asset_rejects_unknown_paths() -> None:
    assert read_static_asset("../catalog.py") is None
    assert read_static_asset("missing.js") is None


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
