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
        "Host(`frontend.feat-example.sample.debug.local`)"
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
        "http://frontend.feat-example.sample.debug.local:28081"
    ) in frontend_labels

    mqtt_labels = plan.compose_override["services"]["mqtt"]["labels"]
    assert "traefik.tcp.routers.sample-feat-example-mqtt.entrypoints=mqtt-sample-feat-example" in mqtt_labels
    assert "traefik.tcp.routers.sample-feat-example-mqtt.rule=HostSNI(`*`)" in mqtt_labels
    assert "traefik.tcp.routers.sample-feat-example-mqtt.service=sample-feat-example-mqtt" in mqtt_labels
    assert "traefik.tcp.services.sample-feat-example-mqtt.loadbalancer.server.port=1883" in mqtt_labels
    assert "portmap.endpoints.sample-feat-example-mqtt.kind=tcp" in mqtt_labels
    assert "portmap.endpoints.sample-feat-example-mqtt.host_port=28831" in mqtt_labels

    udp_labels = plan.compose_override["services"]["udp-echo"]["labels"]
    assert "traefik.udp.routers.sample-feat-example-udp-echo.entrypoints=udp-echo-sample-feat-example" in udp_labels
    assert "traefik.udp.routers.sample-feat-example-udp-echo.service=sample-feat-example-udp-echo" in udp_labels
    assert "traefik.udp.services.sample-feat-example-udp-echo.loadbalancer.server.port=9999" in udp_labels
    assert "portmap.endpoints.sample-feat-example-udp-echo.kind=udp" in udp_labels
    assert "portmap.endpoints.sample-feat-example-udp-echo.host_port=29991" in udp_labels

    assert plan.compose_override["services"]["frontend"]["networks"] == {
        "default": None,
        "portmap_gateway": None,
    }
    assert plan.compose_override["networks"]["portmap_gateway"] == {
        "external": True,
        "name": "portmap_gateway",
    }

    assert plan.traefik_static == {
        "entryPoints": {
            "mqtt-sample-feat-example": {"address": ":28831/tcp"},
            "udp-echo-sample-feat-example": {"address": ":29991/udp"},
        }
    }

    instance = plan.registry["repos"]["sample-repo"]["instances"]["feat-example"]
    assert instance["compose_project"] == "sample_project"
    assert instance["endpoints"]["frontend"]["url"] == "http://frontend.feat-example.sample.debug.local:28081"
    assert instance["endpoints"]["mqtt"]["port"] == 28831
    assert instance["endpoints"]["udp_echo"]["port"] == 29991


def test_http_endpoint_can_preserve_original_host_header(tmp_path: Path) -> None:
    plan = generate_plan(make_preserve_host_request(tmp_path))

    frontend_labels = plan.compose_override["services"]["frontend"]["labels"]
    assert "traefik.http.routers.sample-feat-example-frontend.middlewares=sample-feat-example-frontend-host" not in frontend_labels
    assert not any("headers.customrequestheaders.Host" in label for label in frontend_labels)
    assert "portmap.endpoints.sample-feat-example-frontend.preserve_host=true" in frontend_labels


def test_write_keeps_project_artifacts_minimal_for_http_only_project(tmp_path: Path) -> None:
    out_dir = tmp_path / ".portmap"
    out_dir.mkdir()
    (out_dir / "registry.json").write_text("{}\n", encoding="utf-8")
    (out_dir / "state.json").write_text(
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
    assert not (out_dir / "state.json").exists()

    override = (out_dir / "docker-compose.override.generated.yml").read_text(encoding="utf-8")
    assert "traefik.http.routers.sample-feat-example-frontend.rule" in override
