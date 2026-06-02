import pytest

from portmap.errors import PortmapError
from portmap.registry import get_instance, list_repos, registry_from_catalog


def test_list_repos_returns_display_name_and_instances() -> None:
    registry = {
        "repos": {
            "abc123": {
                "display_name": "sample",
                "instances": {
                    "dev": {},
                    "feat-a": {},
                },
            }
        }
    }

    assert list_repos(registry) == [
        {
            "repo_id": "abc123",
            "display_name": "sample",
            "instances": ["dev", "feat-a"],
        }
    ]


def test_get_instance_accepts_repo_id_or_display_name() -> None:
    instance = {"endpoints": {"frontend": {"kind": "http"}}}
    registry = {
        "repos": {
            "abc123": {
                "display_name": "sample",
                "instances": {
                    "dev": instance,
                },
            }
        }
    }

    assert get_instance(registry, "abc123", "dev") == instance
    assert get_instance(registry, "sample", "dev") == instance


def test_get_instance_reports_missing_repo() -> None:
    with pytest.raises(PortmapError):
        get_instance({"repos": {}}, "missing", "dev")


def test_registry_from_catalog_groups_services_by_repo_and_branch() -> None:
    registry = registry_from_catalog(
        {
            "services": [
                {
                    "repo_id": "sample-repo",
                    "repo_name": "sample",
                    "branch": "feat-a",
                    "worktree": "/repo/sample",
                    "compose_project": "sample",
                    "compose_service": "frontend",
                    "endpoints": [
                        {
                            "name": "frontend",
                            "kind": "http",
                            "url": "http://frontend.feat-a.sample.debug.lan:8080",
                        }
                    ],
                },
                {
                    "repo_id": "sample-repo",
                    "repo_name": "sample",
                    "branch": "feat-a",
                    "worktree": "/repo/sample",
                    "compose_project": "sample",
                    "compose_service": "mqtt",
                    "endpoints": [
                        {
                            "name": "mqtt",
                            "kind": "tcp",
                            "host": "127.0.0.1",
                            "host_port": 18831,
                        }
                    ],
                },
            ]
        }
    )

    instance = get_instance(registry, "sample", "feat-a")
    assert instance["worktree"] == "/repo/sample"
    assert sorted(instance["endpoints"]) == ["frontend", "mqtt"]
    assert instance["endpoints"]["frontend"]["url"] == "http://frontend.feat-a.sample.debug.lan:8080"
    assert instance["endpoints"]["mqtt"]["host_port"] == 18831
