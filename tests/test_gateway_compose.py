from pathlib import Path


def test_gateway_compose_has_transparent_defaults() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert ":?" not in compose
    assert 'PORTMAP_STATE_DIR: "${PORTMAP_STATE_CONTAINER_DIR:-/state}"' in compose
    assert '"${PORTMAP_STATE_DIR:-./.portmap-state}:${PORTMAP_STATE_CONTAINER_DIR:-/state}"' in compose
    assert "${PORTMAP_HTTP_PORT:-8080}:80" in compose
