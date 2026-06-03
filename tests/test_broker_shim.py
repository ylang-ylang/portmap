import json
import os
import stat
from pathlib import Path

import pytest

from portmap.broker_shim import (
    SHIM_MARKER,
    broker_shim_status,
    compose_plugin_shim_path,
    find_real_compose_plugin,
    install_compose_plugin_shim,
    is_portmap_shim,
    render_compose_plugin_shim,
    uninstall_compose_plugin_shim,
)
from portmap.cli import main
from portmap.errors import PortmapError


def test_render_compose_plugin_shim_contains_required_guards(tmp_path: Path) -> None:
    real = tmp_path / "real-compose"
    root = tmp_path / "portmap-root"

    shim = render_compose_plugin_shim(real_compose=real, portmap_root=root)

    assert SHIM_MARKER in shim
    assert 'if [ "${1:-}" = "docker-cli-plugin-metadata" ]; then' in shim
    assert 'if [ "${1:-}" = "compose" ]; then' in shim
    assert "unset DOCKER_CLI_PLUGIN_ORIGINAL_CLI_COMMAND" in shim
    assert "unset DOCKER_CLI_PLUGIN_SOCKET" in shim
    assert 'PORTMAP_BROKER_BYPASS=1' in shim
    assert (
        'env -u VIRTUAL_ENV PORTMAP_ROOT="$PORTMAP_ROOT" PORTMAP_BROKER_BYPASS=1 '
        'uv run --project "$PORTMAP_ROOT" portmap docker-compose -- "$@"'
    ) in shim


def test_install_compose_plugin_shim_writes_executable_plugin(tmp_path: Path) -> None:
    docker_config = tmp_path / "docker-config"
    real = tmp_path / "real-compose"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    root = tmp_path / "portmap-root"
    root.mkdir()

    status = install_compose_plugin_shim(docker_config=docker_config, real_compose=real, portmap_root=root)

    shim = compose_plugin_shim_path(docker_config)
    assert status.installed is True
    assert status.shim_path == shim
    assert is_portmap_shim(shim) is True
    assert shim.stat().st_mode & stat.S_IXUSR
    assert broker_shim_status(docker_config=docker_config).real_compose == real
    assert broker_shim_status(docker_config=docker_config).portmap_root == root


def test_install_refuses_to_overwrite_non_portmap_plugin(tmp_path: Path) -> None:
    docker_config = tmp_path / "docker-config"
    shim = compose_plugin_shim_path(docker_config)
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
    real = tmp_path / "real-compose"
    real.write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(PortmapError):
        install_compose_plugin_shim(docker_config=docker_config, real_compose=real, portmap_root=tmp_path)


def test_uninstall_removes_only_portmap_shim(tmp_path: Path) -> None:
    docker_config = tmp_path / "docker-config"
    real = tmp_path / "real-compose"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    install_compose_plugin_shim(docker_config=docker_config, real_compose=real, portmap_root=tmp_path)

    status = uninstall_compose_plugin_shim(docker_config=docker_config)

    assert status.installed is False
    assert not compose_plugin_shim_path(docker_config).exists()


def test_find_real_compose_plugin_skips_current_shim(tmp_path: Path) -> None:
    shim = tmp_path / "shim"
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    candidate = tmp_path / "real-compose"
    candidate.write_text("#!/bin/sh\n", encoding="utf-8")

    assert find_real_compose_plugin(shim_path=shim, candidates=(shim, candidate)) == candidate


def test_broker_cli_status_and_install(tmp_path: Path, capsys) -> None:
    docker_config = tmp_path / "docker-config"
    real = tmp_path / "real-compose"
    real.write_text("#!/bin/sh\n", encoding="utf-8")

    assert main(
        [
            "broker",
            "install",
            "--docker-config",
            str(docker_config),
            "--real-compose",
            str(real),
            "--portmap-root",
            str(tmp_path),
        ]
    ) == 0
    install_payload = json.loads(capsys.readouterr().out)
    assert install_payload["installed"] is True

    assert main(["broker", "status", "--docker-config", str(docker_config)]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["installed"] is True


def test_generated_shim_forwards_metadata_and_real_compose(tmp_path: Path) -> None:
    real_log = tmp_path / "real.log"
    real = tmp_path / "real-compose"
    real.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"docker-cli-plugin-metadata\" ]; then\n"
        "  echo '{\"SchemaVersion\":\"0.1.0\",\"Vendor\":\"Test\",\"Version\":\"1\",\"ShortDescription\":\"Test Compose\"}'\n"
        "  exit 0\n"
        "fi\n"
        "env | sort | grep '^DOCKER_CLI_PLUGIN' >> \"$REAL_LOG\" || true\n"
        "printf '%s\\n' \"$@\" >> \"$REAL_LOG\"\n",
        encoding="utf-8",
    )
    real.chmod(real.stat().st_mode | stat.S_IXUSR)
    shim = tmp_path / "docker-compose"
    shim.write_text(render_compose_plugin_shim(real_compose=real, portmap_root=tmp_path), encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    metadata = os.popen(f"{shim} docker-cli-plugin-metadata").read()
    assert '"SchemaVersion"' in metadata

    env = os.environ.copy()
    env["REAL_LOG"] = str(real_log)
    env["PORTMAP_COMPOSE_TAKEOVER"] = "0"
    env["DOCKER_CLI_PLUGIN_ORIGINAL_CLI_COMMAND"] = "docker"
    env["DOCKER_CLI_PLUGIN_SOCKET"] = "socket"
    assert os.spawnve(os.P_WAIT, str(shim), [str(shim), "compose", "version"], env) == 0

    log = real_log.read_text(encoding="utf-8")
    assert "version\n" in log
    assert "DOCKER_CLI_PLUGIN_SOCKET" not in log
