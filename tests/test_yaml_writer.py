from portmap.yaml_writer import dump_yaml


def test_dump_yaml_handles_nested_compose_like_values() -> None:
    assert dump_yaml(
        {
            "services": {
                "frontend": {
                    "labels": [
                        "traefik.enable=true",
                        "traefik.http.routers.frontend.rule=Host(`frontend.local`)",
                    ]
                }
            }
        }
    ) == (
        'services:\n'
        '  frontend:\n'
        '    labels:\n'
        '      - "traefik.enable=true"\n'
        '      - "traefik.http.routers.frontend.rule=Host(`frontend.local`)"\n'
    )

