import json
import shutil
from pathlib import Path

import pytest

from portmap.catalog import container_to_service
from portmap.server import run_gateway_compose
from portmap.settings import load_portmap_settings


@pytest.fixture
def gateway_config(tmp_path: Path, capfd):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is required to render gateway configuration")
    root = Path(__file__).resolve().parents[1]
    shutil.copyfile(root / "docker-compose.yml", tmp_path / "docker-compose.yml")

    def render(config: str = "", **environ: str) -> dict:
        (tmp_path / "portmap.toml").write_text(config, encoding="utf-8")
        settings = load_portmap_settings(
            root=tmp_path,
            environ={"PORTMAP_HOST_IP": "127.0.0.1", **environ},
        )
        result = run_gateway_compose(settings, ["config", "--format", "json"], backend_port=47123)
        captured = capfd.readouterr()
        assert result == 0, captured.err
        return json.loads(captured.out)

    return render


def test_http_backend_stays_private_when_public_http_uses_port_80(gateway_config) -> None:
    services = gateway_config('[gateway]\nhttp_bind = "0.0.0.0"\nhttp_port = 80\n')["services"]
    ports = services["traefik"]["ports"]
    assert {(port["host_ip"], port["published"]) for port in ports} == {("127.0.0.1", "47123")}
    assert not services["catalog"].get("ports")
    assert services["catalog"]["environment"]["PORTMAP_HTTP_PORT"] == "80"


def test_catalog_backend_is_not_listed_as_a_managed_project(gateway_config) -> None:
    catalog = gateway_config()["services"]["catalog"]

    assert container_to_service({"Names": ["/portmap-catalog"], "Labels": catalog["labels"]}) is None