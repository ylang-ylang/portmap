import os

from portmap import infrastructure_config
from portmap.container_runtime import ContainerRuntime


def test_replaced_source_directory_recreates_only_the_catalog(monkeypatch, tmp_path):
    source = tmp_path / "site-packages"
    source.mkdir()
    monkeypatch.setattr(infrastructure_config, "frozen", lambda: False)
    monkeypatch.setattr(infrastructure_config, "pythonpath_dir", lambda: source)
    roles = list(infrastructure_config.IMAGES)
    state = {
        "owner": "test-owner",
        "uid": os.getuid(),
        "state_dir": str(tmp_path / "state"),
        "instance_dir": str(tmp_path / "instance"),
        "network": {"name": "test-gateway"},
        "project": "test-portmap",
        "names": {role: f"test-{role}" for role in roles},
        "addresses": {role: f"172.19.0.{index + 2}" for index, role in enumerate(roles)},
        "listen_port": 18080,
        "public_backend_port": 18081,
        "management_port": 18082,
        "dns_port": 1053,
        "http_port": 80,
        "domain": "test.portmap",
        "socket_dir": str(tmp_path / "ssh"),
        "agent_runtime_dir": str(tmp_path / "agent"),
        "download_cache_dir": str(tmp_path / "downloads"),
    }
    runtime = ContainerRuntime("podman", str(tmp_path / "podman.sock"), True)
    before = infrastructure_config.compose_config(state, runtime)
    assert infrastructure_config.compose_config(state, runtime) == before

    # Brew retains the old keg while installing another directory at exactly
    # the same pathname. A bind-mounted old inode must not survive cleanup.
    source.rename(tmp_path / "previous-keg")
    source.mkdir()
    after = infrastructure_config.compose_config(state, runtime)
    assert before["services"]["catalog"]["volumes"] == after["services"]["catalog"]["volumes"]
    changed = {
        role for role in roles
        if before["services"][role] != after["services"][role]
    }
    assert changed == {"catalog"}
