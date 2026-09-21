import json
import shutil
from pathlib import Path

import pytest

from portmap.catalog import container_to_service
from portmap.cli import run_gateway_compose
from portmap.settings import load_portmap_settings


@pytest.fixture
def gateway_config(tmp_path: Path, capfd):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is required to render gateway configuration")
    root = Path(__file__).resolve().parents[1]
    for name in ("docker-compose.yml", "docker-compose.single-port.yml"):
        shutil.copyfile(root / name, tmp_path / name)

    def render(config: str = "", **environ: str) -> dict:
        (tmp_path / "portmap.toml").write_text(config, encoding="utf-8")
        settings = load_portmap_settings(
            root=tmp_path,
            environ={"PORTMAP_HOST_IP": "127.0.0.1", **environ},
        )
        result = run_gateway_compose(settings, ["config", "--format", "json"])
        captured = capfd.readouterr()
        assert result == 0, captured.err
        return json.loads(captured.out)

    return render


def test_gateway_keeps_separate_default_ports(gateway_config) -> None:
    services = gateway_config()["services"]

    assert services["traefik"]["ports"] == [
        {"mode": "ingress", "host_ip": "0.0.0.0", "target": 80, "published": "8080", "protocol": "tcp"}
    ]
    assert services["catalog"]["ports"] == [
        {"mode": "ingress", "host_ip": "0.0.0.0", "target": 8081, "published": "80", "protocol": "tcp"}
    ]


@pytest.mark.parametrize("port", [80, 18080])
def test_equal_http_and_catalog_ports_publish_only_traefik(gateway_config, port: int) -> None:
    services = gateway_config(f"[gateway]\nhttp_port = {port}\ncatalog_port = {port}\n")["services"]

    assert services["traefik"]["ports"][0]["published"] == str(port)
    assert not services["catalog"].get("ports")
    assert services["catalog"]["environment"]["PORTMAP_HTTP_PORT"] == str(port)


def test_gateway_preserves_custom_separate_catalog_binding(gateway_config) -> None:
    services = gateway_config(
        '[gateway]\nhttp_port = 18080\ncatalog_port = 18081\ncatalog_bind = "127.0.0.1"\n'
    )["services"]

    assert services["traefik"]["ports"][0]["published"] == "18080"
    catalog_port = services["catalog"]["ports"][0]
    assert (catalog_port["host_ip"], catalog_port["published"], catalog_port["target"]) == (
        "127.0.0.1", "18081", 8081
    )


def test_gateway_coalesces_ports_after_environment_overrides(gateway_config) -> None:
    services = gateway_config(
        "[gateway]\nhttp_port = 18080\ncatalog_port = 18081\n",
        PORTMAP_HTTP_PORT="80",
        PORTMAP_CATALOG_PORT="80",
    )["services"]

    assert services["traefik"]["ports"][0]["published"] == "80"
    assert not services["catalog"].get("ports")


def test_catalog_fallback_uses_shared_network_and_lowest_router_priority(gateway_config) -> None:
    config = gateway_config('[gateway]\nnetwork = "custom_gateway"\n')
    catalog = config["services"]["catalog"]
    labels = catalog["labels"]
    router = "traefik.http.routers.portmap-catalog"

    assert labels["traefik.enable"] == "true"
    assert labels[f"{router}.rule"] == "HostRegexp(`.+`)"
    assert labels[f"{router}.entrypoints"] == "web"
    assert 0 < int(labels[f"{router}.priority"]) < len("Host(`a`)")
    service = labels[f"{router}.service"]
    assert labels[f"traefik.http.services.{service}.loadbalancer.server.port"] == "8081"
    assert labels["traefik.docker.network"] == config["networks"]["portmap_gateway"]["name"] == "custom_gateway"
    assert "portmap_gateway" in catalog["networks"]



def test_catalog_backend_is_not_listed_as_a_managed_project(gateway_config) -> None:
    catalog = gateway_config()["services"]["catalog"]

    assert container_to_service({"Names": ["/portmap-catalog"], "Labels": catalog["labels"]}) is None