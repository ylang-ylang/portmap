from portmap.catalog import (
    build_catalog_tree,
    container_to_service,
    parse_host_rule,
    render_html,
    select_dns_server,
    split_dns_test_command,
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


def test_split_dns_test_command_uses_portmap_itself() -> None:
    command = split_dns_test_command(
        {
            "dns_domain": "debug.lan",
            "services": [
                {
                    "endpoints": [
                        {
                            "kind": "http",
                            "url": "http://external-project.debug.lan:8080",
                        }
                    ]
                }
            ],
        }
    )

    assert 'resolvectl query "portmap.debug.lan"' in command
    assert 'curl -I "http://portmap.debug.lan/"' in command
    assert "external-project" not in command


def test_build_catalog_tree_groups_by_directory_repo_and_branch() -> None:
    tree = build_catalog_tree(
        {
            "services": [
                {
                    "worktree": "/repo-a/worktree-dev",
                    "repo_id": "repo-a-id",
                    "repo_name": "repo-a",
                    "branch": "dev",
                    "compose_service": "frontend",
                    "container": "repo-a-frontend-1",
                    "endpoints": [{"name": "frontend", "kind": "http"}],
                },
                {
                    "worktree": "/repo-a/worktree-dev",
                    "repo_id": "repo-a-id",
                    "repo_name": "repo-a",
                    "branch": "feat-a",
                    "compose_service": "backend",
                    "container": "repo-a-backend-1",
                    "endpoints": [{"name": "backend", "kind": "http"}],
                },
                {
                    "worktree": "/repo-b/worktree-main",
                    "repo_id": "repo-b-id",
                    "repo_name": "repo-b",
                    "branch": "main",
                    "compose_service": "api",
                    "container": "repo-b-api-1",
                    "endpoints": [{"name": "api", "kind": "http"}],
                },
            ]
        }
    )

    assert [directory["worktree"] for directory in tree] == [
        "/repo-a/worktree-dev",
        "/repo-b/worktree-main",
    ]
    repo_a = tree[0]["repos"][0]
    assert repo_a["repo_id"] == "repo-a-id"
    assert [branch["branch"] for branch in repo_a["branches"]] == ["dev", "feat-a"]


def test_render_html_uses_work_tree_repo_branch_hierarchy() -> None:
    html = render_html(
        {
            "generated_at": "2026-06-02T00:00:00+00:00",
            "http_port": 8080,
            "dns_domain": "debug.lan",
            "dns_server": "192.168.201.52",
            "services": [
                {
                    "worktree": "/repo/sample",
                    "repo_id": "sample-id",
                    "repo_name": "sample",
                    "branch": "feat-a",
                    "compose_service": "frontend",
                    "container": "sample-frontend-1",
                    "image": "sample:latest",
                    "endpoints": [
                        {
                            "name": "frontend",
                            "kind": "http",
                            "container_port": 5173,
                            "url": "http://frontend.feat-a.sample.debug.lan:8080",
                        }
                    ],
                }
            ],
        }
    )

    assert "<h2>Work Tree</h2>" in html
    assert "/repo/sample" in html
    assert "repo_id: <code>sample-id</code>" in html
    assert "Branch: <code>feat-a</code>" in html
    assert "<th>Repo</th>" not in html
    assert html.index("<h2>Work Tree</h2>") < html.index("<summary>Split DNS quick setup</summary>")
    assert '<details class="quick-setup">' in html
