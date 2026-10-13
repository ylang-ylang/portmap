import json
from dataclasses import replace
from pathlib import Path

import pytest

from portmap.model import GenerateRequest
from portmap.planner import generate_plan


CONTAINER_DNS_SERVER = "portmap-runtime-dns"


def write_compose_json(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "name": "sample_project",
                "networks": {
                    "default": {
                        "name": "sample_project_default",
                    }
                },
                "services": {
                    "frontend": {
                        "expose": ["5173"],
                        "networks": {"default": None},
                    },
                    "mqtt": {
                        "expose": ["1883"],
                        "networks": {"default": None},
                    },
                    "udp-echo": {
                        "expose": ["9999/udp"],
                        "networks": {"default": None},
                    },
                    "turn": {
                        "expose": ["3478/udp"],
                        "networks": {"default": None},
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def write_endpoint_config(path: Path) -> None:
    path.write_text(
        """
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 5173

[endpoints.mqtt]
kind = "tcp"
service = "mqtt"
container_port = 1883
host_port = 28831

[endpoints.udp_echo]
kind = "udp"
service = "udp-echo"
container_port = 9999
host_port = 29991

[endpoints.turn]
kind = "range"
service = "turn"
container_port = 3478
protocol = "udp"
host_port = 34781
range_start = 49160
range_size = 40
""".strip()
        + "\n",
        encoding="utf-8",
    )


def write_http_endpoint_config(path: Path) -> None:
    path.write_text(
        """
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 5173
""".strip()
        + "\n",
        encoding="utf-8",
    )


def write_preserve_host_endpoint_config(path: Path) -> None:
    path.write_text(
        """
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 5173
preserve_host = true
""".strip()
        + "\n",
        encoding="utf-8",
    )


def write_auto_range_endpoint_config(path: Path) -> None:
    path.write_text(
        """
[endpoints.turn]
kind = "range"
service = "turn"
container_port = 3478
protocol = "udp"
range_size = 3
""".strip()
        + "\n",
        encoding="utf-8",
    )


def make_request(tmp_path: Path, *, branch: str = "feat/example") -> GenerateRequest:
    compose_json = tmp_path / "compose.json"
    config = tmp_path / "endpoints.toml"
    write_compose_json(compose_json)
    write_endpoint_config(config)
    return GenerateRequest(
        compose_file=None,
        compose_json_file=compose_json,
        project_directory=tmp_path,
        out_dir=tmp_path / ".portmap",
        config_file=config,
        branch=branch,
        repo_id="sample-repo",
        repo_name="sample",
        http_port=28081,
        tcp_port_start=28800,
        udp_port_start=29900,
        domain_suffix="debug.lan",
        container_dns_server=CONTAINER_DNS_SERVER,
    )


def make_auto_range_request(tmp_path: Path, *, branch: str) -> GenerateRequest:
    compose_json = tmp_path / "compose.json"
    config = tmp_path / "endpoints-auto-range.toml"
    write_compose_json(compose_json)
    write_auto_range_endpoint_config(config)
    return GenerateRequest(
        compose_file=None,
        compose_json_file=compose_json,
        project_directory=tmp_path,
        out_dir=tmp_path / ".portmap",
        config_file=config,
        branch=branch,
        repo_id="sample-repo",
        repo_name="sample",
        udp_port_start=30000,
        range_port_start=40000,
    )


def make_http_request(tmp_path: Path, *, branch: str = "feat/example") -> GenerateRequest:
    compose_json = tmp_path / "compose.json"
    config = tmp_path / "endpoints-http.toml"
    write_compose_json(compose_json)
    write_http_endpoint_config(config)
    return GenerateRequest(
        compose_file=None,
        compose_json_file=compose_json,
        project_directory=tmp_path,
        out_dir=tmp_path / ".portmap",
        config_file=config,
        branch=branch,
        repo_id="sample-repo",
        repo_name="sample",
        http_port=28081,
        tcp_port_start=28800,
        udp_port_start=29900,
        container_dns_server=CONTAINER_DNS_SERVER,
    )


def make_preserve_host_request(tmp_path: Path, *, branch: str = "feat/example") -> GenerateRequest:
    compose_json = tmp_path / "compose.json"
    config = tmp_path / "endpoints-preserve-host.toml"
    write_compose_json(compose_json)
    write_preserve_host_endpoint_config(config)
    return GenerateRequest(
        compose_file=None,
        compose_json_file=compose_json,
        project_directory=tmp_path,
        out_dir=tmp_path / ".portmap",
        config_file=config,
        branch=branch,
        repo_id="sample-repo",
        repo_name="sample",
        http_port=28081,
        tcp_port_start=28800,
        udp_port_start=29900,
        container_dns_server=CONTAINER_DNS_SERVER,
    )


def label_resources(labels: list[str], prefix: str) -> dict[str, dict[str, str]]:
    resources: dict[str, dict[str, str]] = {}
    for label in labels:
        key, value = label.split("=", 1)
        if key.startswith(prefix):
            name, field = key.removeprefix(prefix).split(".", 1)
            resources.setdefault(name, {})[field] = value
    return resources


def endpoint_labels(labels: list[str], name: str) -> dict[str, str]:
    return next(
        endpoint
        for endpoint in label_resources(labels, "portmap.endpoints.").values()
        if endpoint["name"] == name
    )


def test_generate_plan_builds_traefik_labels_and_registry(tmp_path: Path) -> None:
    plan = generate_plan(make_request(tmp_path))

    frontend_labels = plan.compose_override["services"]["frontend"]["labels"]
    assert "traefik.enable=true" in frontend_labels
    assert "traefik.docker.network=portmap_gateway" in frontend_labels
    assert "portmap.managed=true" in frontend_labels
    assert "portmap.repo_id=sample-repo" in frontend_labels
    assert "portmap.repo_name=sample" in frontend_labels
    assert "portmap.branch=feat-example" in frontend_labels
    router, = label_resources(frontend_labels, "traefik.http.routers.").values()
    assert router["entrypoints"] == "web"
    assert router["rule"] == "Host(`frontend.feat-example.sample.debug.lan`)"
    services = label_resources(frontend_labels, "traefik.http.services.")
    assert services[router["service"]]["loadbalancer.server.port"] == "5173"
    middlewares = label_resources(frontend_labels, "traefik.http.middlewares.")
    assert middlewares[router["middlewares"]]["headers.customrequestheaders.Host"] == "127.0.0.1:5173"
    frontend = endpoint_labels(frontend_labels, "frontend")
    assert frontend["order"] == "0"
    assert frontend["kind"] == "http"
    assert frontend["container_port"] == "5173"
    assert frontend["preserve_host"] == "false"
    assert frontend["upstream_host"] == "127.0.0.1:5173"
    assert frontend["url"] == "http://frontend.feat-example.sample.debug.lan:28081"

    mqtt_labels = plan.compose_override["services"]["mqtt"]["labels"]
    assert "traefik.enable=true" not in mqtt_labels
    assert not any(label.startswith("traefik.tcp.") for label in mqtt_labels)
    mqtt = endpoint_labels(mqtt_labels, "mqtt")
    assert mqtt["kind"] == "tcp"
    assert mqtt["order"] == "1"
    assert mqtt["host_port"] == "28831"
    assert plan.compose_override["services"]["mqtt"]["ports"] == ["28831:1883/tcp"]

    udp_labels = plan.compose_override["services"]["udp-echo"]["labels"]
    assert "traefik.enable=true" not in udp_labels
    assert not any(label.startswith("traefik.udp.") for label in udp_labels)
    udp = endpoint_labels(udp_labels, "udp_echo")
    assert udp["kind"] == "udp"
    assert udp["order"] == "2"
    assert udp["host_port"] == "29991"
    assert plan.compose_override["services"]["udp-echo"]["ports"] == ["29991:9999/udp"]

    turn_override = plan.compose_override["services"]["turn"]
    turn_labels = turn_override["labels"]
    assert "traefik.enable=true" not in turn_labels
    assert "traefik.docker.network=portmap_gateway" not in turn_labels
    assert not any(label.startswith("traefik.tcp.") for label in turn_labels)
    turn = endpoint_labels(turn_labels, "turn")
    assert turn["kind"] == "range"
    assert turn["order"] == "3"
    assert turn["host_port"] == "34781"
    assert turn["range_start"] == "49160"
    assert turn["range_end"] == "49199"
    assert turn_override["ports"] == [
        "34781:3478/udp",
        "49160-49199:49160-49199/udp",
    ]

    for service in ("frontend", "mqtt", "udp-echo", "turn"):
        override = plan.compose_override["services"][service]
        assert "portmap_gateway" in override["networks"]
        assert override["dns"] == [CONTAINER_DNS_SERVER]
    assert plan.compose_override["services"]["frontend"]["environment"]["PORTMAP_TURN_PORT"] == "34781"
    assert plan.compose_override["services"]["frontend"]["environment"]["PORTMAP_TURN_RANGE_MIN_PORT"] == "49160"
    assert plan.compose_override["services"]["frontend"]["environment"]["PORTMAP_TURN_RANGE_MAX_PORT"] == "49199"
    assert plan.compose_override["services"]["turn"]["environment"]["PORTMAP_FRONTEND_URL"] == (
        "http://frontend.feat-example.sample.debug.lan:28081"
    )
    assert plan.compose_override["networks"]["portmap_gateway"] == {
        "external": True,
        "name": "portmap_gateway",
    }

    assert plan.traefik_static == {"entryPoints": {}}

    instance = plan.registry["repos"]["sample-repo"]["instances"]["feat-example"]
    assert instance["compose_project"] == plan.compose_project
    assert plan.compose_project is not None
    assert plan.compose_project.startswith("sample_feat_example_")
    assert instance["endpoints"]["frontend"]["url"] == "http://frontend.feat-example.sample.debug.lan:28081"
    assert instance["endpoints"]["frontend"]["order"] == 0
    assert instance["endpoints"]["mqtt"]["port"] == 28831
    assert instance["endpoints"]["mqtt"]["order"] == 1
    assert instance["endpoints"]["udp_echo"]["port"] == 29991
    assert instance["endpoints"]["udp_echo"]["order"] == 2
    assert instance["endpoints"]["turn"]["port"] == 34781
    assert instance["endpoints"]["turn"]["order"] == 3
    assert instance["endpoints"]["turn"]["range"] == {
        "min_port": 49160,
        "max_port": 49199,
    }


@pytest.mark.parametrize(
    ("repo_id", "worktree"),
    [
        pytest.param("other-repo", "first", id="different-repositories"),
        pytest.param("sample-repo", "second", id="different-worktrees"),
        pytest.param("other-repo", "second", id="different-repositories-and-worktrees"),
    ],
)
def test_same_named_projects_have_independent_routing_resources(
    tmp_path: Path, repo_id: str, worktree: str
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / worktree
    first_dir.mkdir()
    second_dir.mkdir(exist_ok=True)
    first_request = replace(make_http_request(first_dir), domain_suffix="qa101.portmap")
    second_request = replace(
        make_http_request(second_dir),
        repo_id=repo_id,
        domain_suffix="qa-legacy.portmap",
    )
    first_plan = generate_plan(first_request)
    second_plan = generate_plan(second_request)

    for request, plan in ((first_request, first_plan), (second_request, second_plan)):
        assert generate_plan(request).compose_override == plan.compose_override
        endpoint, = plan.endpoints
        host = f"frontend.feat-example.sample.{request.domain_suffix}"
        url = f"http://{host}:28081"
        assert endpoint.host == host
        assert endpoint.url == url
        labels = plan.compose_override["services"]["frontend"]["labels"]
        router, = label_resources(labels, "traefik.http.routers.").values()
        assert router["rule"] == f"Host(`{host}`)"
        assert endpoint_labels(labels, "frontend")["url"] == url

    first_labels = first_plan.compose_override["services"]["frontend"]["labels"]
    second_labels = second_plan.compose_override["services"]["frontend"]["labels"]
    for prefix in (
        "traefik.http.routers.",
        "traefik.http.services.",
        "traefik.http.middlewares.",
        "portmap.endpoints.",
    ):
        first_resources = label_resources(first_labels, prefix)
        second_resources = label_resources(second_labels, prefix)
        assert len(first_resources) == len(second_resources) == 1
        assert first_resources.keys().isdisjoint(second_resources)


def test_http_endpoint_can_preserve_original_host_header(tmp_path: Path) -> None:
    plan = generate_plan(make_preserve_host_request(tmp_path))

    frontend_labels = plan.compose_override["services"]["frontend"]["labels"]
    assert all(
        "middlewares" not in router
        for router in label_resources(frontend_labels, "traefik.http.routers.").values()
    )
    assert not any("headers.customrequestheaders.Host" in label for label in frontend_labels)
    assert endpoint_labels(frontend_labels, "frontend")["preserve_host"] == "true"


def test_range_endpoint_auto_allocation_uses_non_overlapping_state(tmp_path: Path) -> None:
    dev_plan = generate_plan(make_auto_range_request(tmp_path, branch="dev"))
    feat_plan = generate_plan(make_auto_range_request(tmp_path, branch="feat/example"))

    dev_turn = dev_plan.registry["repos"]["sample-repo"]["instances"]["dev"]["endpoints"]["turn"]
    feat_turn = feat_plan.registry["repos"]["sample-repo"]["instances"]["feat-example"]["endpoints"]["turn"]
    assert dev_turn["port"] == 30000
    assert dev_turn["range"] == {"min_port": 40000, "max_port": 40002}
    assert feat_turn["port"] == 30001
    assert feat_turn["range"] == {"min_port": 40003, "max_port": 40005}

    assert feat_plan.compose_override["services"]["turn"]["ports"] == [
        "30001:3478/udp",
        "40003-40005:40003-40005/udp",
    ]
    assert "networks" not in feat_plan.compose_override
    assert "networks" not in feat_plan.compose_override["services"]["turn"]


def test_global_allocation_file_allocates_across_worktrees(tmp_path: Path) -> None:
    allocation_state_file = tmp_path / "global" / "allocations.json"
    dev_dir = tmp_path / "sample@dev"
    feat_dir = tmp_path / "sample@feat"
    dev_dir.mkdir()
    feat_dir.mkdir()
    write_compose_json(dev_dir / "compose.json")
    write_auto_range_endpoint_config(dev_dir / "endpoints.toml")
    write_compose_json(feat_dir / "compose.json")
    write_auto_range_endpoint_config(feat_dir / "endpoints.toml")

    dev_plan = generate_plan(
        GenerateRequest(
            compose_file=None,
            compose_json_file=dev_dir / "compose.json",
            project_directory=dev_dir,
            out_dir=dev_dir / ".portmap",
            config_file=dev_dir / "endpoints.toml",
            branch="feat/example",
            repo_id="sample-repo",
            repo_name="sample",
            udp_port_start=30000,
            range_port_start=40000,
            allocation_state_file=allocation_state_file,
        )
    )
    feat_plan = generate_plan(
        GenerateRequest(
            compose_file=None,
            compose_json_file=feat_dir / "compose.json",
            project_directory=feat_dir,
            out_dir=feat_dir / ".portmap",
            config_file=feat_dir / "endpoints.toml",
            branch="feat/example",
            repo_id="sample-repo",
            repo_name="sample",
            udp_port_start=30000,
            range_port_start=40000,
            allocation_state_file=allocation_state_file,
        )
    )

    dev_turn = dev_plan.registry["repos"]["sample-repo"]["instances"]["feat-example"]["endpoints"]["turn"]
    feat_turn = feat_plan.registry["repos"]["sample-repo"]["instances"]["feat-example"]["endpoints"]["turn"]
    assert dev_turn["port"] == 30000
    assert feat_turn["port"] == 30001
    assert dev_turn["range"] == {"min_port": 40000, "max_port": 40002}
    assert feat_turn["range"] == {"min_port": 40003, "max_port": 40005}
    assert dev_plan.compose_project != feat_plan.compose_project


def test_write_keeps_project_artifacts_minimal_for_http_only_project(tmp_path: Path) -> None:
    out_dir = tmp_path / ".portmap"
    out_dir.mkdir()
    (out_dir / "registry.json").write_text("{}\n", encoding="utf-8")
    (out_dir / "allocations.json").write_text(
        '{"allocations": {"tcp": {"stale": 18831}, "udp": {"stale": 19991}}}\n',
        encoding="utf-8",
    )
    (out_dir / "traefik.generated.yml").write_text("entryPoints:\n", encoding="utf-8")

    dev_plan = generate_plan(make_http_request(tmp_path, branch="dev"))
    dev_plan.write(out_dir)

    feat_plan = generate_plan(make_http_request(tmp_path, branch="feat/example"))
    feat_plan.write(out_dir)

    assert not (out_dir / "registry.json").exists()
    assert not (out_dir / "traefik.generated.yml").exists()
    assert not (out_dir / "allocations.json").exists()
    assert (out_dir / "state.json").exists()
    assert (out_dir / "docker-compose.override.generated.yml").exists()
