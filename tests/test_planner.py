import json
from pathlib import Path

from portmap.model import GenerateRequest
from portmap.planner import generate_plan


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
    assert "traefik.http.routers.sample-feat-example-frontend.entrypoints=web" in frontend_labels
    assert (
        "traefik.http.routers.sample-feat-example-frontend.rule="
        "Host(`frontend.feat-example.sample.debug.lan`)"
    ) in frontend_labels
    assert (
        "traefik.http.services.sample-feat-example-frontend.loadbalancer.server.port=5173"
    ) in frontend_labels
    assert "traefik.http.routers.sample-feat-example-frontend.service=sample-feat-example-frontend" in frontend_labels
    assert "traefik.http.routers.sample-feat-example-frontend.middlewares=sample-feat-example-frontend-host" in frontend_labels
    assert (
        "traefik.http.middlewares.sample-feat-example-frontend-host."
        "headers.customrequestheaders.Host=127.0.0.1:5173"
    ) in frontend_labels
    assert "portmap.endpoints.sample-feat-example-frontend.name=frontend" in frontend_labels
    assert "portmap.endpoints.sample-feat-example-frontend.kind=http" in frontend_labels
    assert "portmap.endpoints.sample-feat-example-frontend.container_port=5173" in frontend_labels
    assert "portmap.endpoints.sample-feat-example-frontend.preserve_host=false" in frontend_labels
    assert "portmap.endpoints.sample-feat-example-frontend.upstream_host=127.0.0.1:5173" in frontend_labels
    assert (
        "portmap.endpoints.sample-feat-example-frontend.url="
        "http://frontend.feat-example.sample.debug.lan:28081"
    ) in frontend_labels

    mqtt_labels = plan.compose_override["services"]["mqtt"]["labels"]
    assert "traefik.enable=true" not in mqtt_labels
    assert not any(label.startswith("traefik.tcp.") for label in mqtt_labels)
    assert "portmap.endpoints.sample-feat-example-mqtt.kind=tcp" in mqtt_labels
    assert "portmap.endpoints.sample-feat-example-mqtt.host_port=28831" in mqtt_labels
    assert plan.compose_override["services"]["mqtt"]["ports"] == ["28831:1883/tcp"]

    udp_labels = plan.compose_override["services"]["udp-echo"]["labels"]
    assert "traefik.enable=true" not in udp_labels
    assert not any(label.startswith("traefik.udp.") for label in udp_labels)
    assert "portmap.endpoints.sample-feat-example-udp-echo.kind=udp" in udp_labels
    assert "portmap.endpoints.sample-feat-example-udp-echo.host_port=29991" in udp_labels
    assert plan.compose_override["services"]["udp-echo"]["ports"] == ["29991:9999/udp"]

    turn_override = plan.compose_override["services"]["turn"]
    turn_labels = turn_override["labels"]
    assert "traefik.enable=true" not in turn_labels
    assert "traefik.docker.network=portmap_gateway" not in turn_labels
    assert "traefik.tcp.routers.sample-feat-example-turn.entrypoints" not in turn_labels
    assert "portmap.endpoints.sample-feat-example-turn.kind=range" in turn_labels
    assert "portmap.endpoints.sample-feat-example-turn.host_port=34781" in turn_labels
    assert "portmap.endpoints.sample-feat-example-turn.range_start=49160" in turn_labels
    assert "portmap.endpoints.sample-feat-example-turn.range_end=49199" in turn_labels
    assert turn_override["ports"] == [
        "34781:3478/udp",
        "49160-49199:49160-49199/udp",
    ]

    assert plan.compose_override["services"]["frontend"]["networks"] == {
        "default": None,
        "portmap_gateway": None,
    }
    assert "networks" not in plan.compose_override["services"]["mqtt"]
    assert "networks" not in plan.compose_override["services"]["udp-echo"]
    assert "networks" not in plan.compose_override["services"]["turn"]
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
    assert instance["endpoints"]["mqtt"]["port"] == 28831
    assert instance["endpoints"]["udp_echo"]["port"] == 29991
    assert instance["endpoints"]["turn"]["port"] == 34781
    assert instance["endpoints"]["turn"]["range"] == {
        "min_port": 49160,
        "max_port": 49199,
    }


def test_http_endpoint_can_preserve_original_host_header(tmp_path: Path) -> None:
    plan = generate_plan(make_preserve_host_request(tmp_path))

    frontend_labels = plan.compose_override["services"]["frontend"]["labels"]
    assert "traefik.http.routers.sample-feat-example-frontend.middlewares=sample-feat-example-frontend-host" not in frontend_labels
    assert not any("headers.customrequestheaders.Host" in label for label in frontend_labels)
    assert "portmap.endpoints.sample-feat-example-frontend.preserve_host=true" in frontend_labels


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

    override = (out_dir / "docker-compose.override.generated.yml").read_text(encoding="utf-8")
    assert "traefik.http.routers.sample-feat-example-frontend.rule" in override
