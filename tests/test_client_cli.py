import subprocess
import sys
from pathlib import Path

import pytest

import portmap
from portmap import cli as server_cli
from portmap import client_cli
from portmap.errors import PortmapError


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    state = tmp_path / "client-state"
    monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(state))
    return state


def test_version_reports_package_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        client_cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"portmap-client {portmap.__version__}"


def test_setup_uses_client_owned_state_dir(state_dir, monkeypatch, capsys):
    captured = {}

    def fake_setup(path, *, use_sudo, http_port):
        captured.update(path=path, use_sudo=use_sudo, http_port=http_port)
        return {"state_dir": str(path)}

    monkeypatch.setattr(client_cli, "setup_client", fake_setup)
    assert client_cli.main(["setup", "--no-sudo", "--http-port", "18080"]) == 0
    assert captured == {"path": state_dir, "use_sudo": False, "http_port": 18080}
    assert '"state_dir"' in capsys.readouterr().out


def test_command_dispatch_reaches_client_module(state_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(client_cli, "connections", lambda path, check: calls.append((path, check)) or {"connections": []})
    monkeypatch.setattr(client_cli, "teardown_client", lambda path, use_sudo: calls.append(("teardown", use_sudo)) or {})
    monkeypatch.setattr(client_cli, "discover", lambda path, target, ssh_config, timeout: calls.append(("discover", target)) or {})
    monkeypatch.setattr(client_cli, "disconnect", lambda path, host: calls.append(("disconnect", host)) or {})
    assert client_cli.main(["connections", "--check"]) == 0
    assert client_cli.main(["status"]) == 0
    assert client_cli.main(["teardown"]) == 0
    assert client_cli.main(["discover", "192.0.2.10"]) == 0
    assert client_cli.main(["disconnect", "remote"]) == 0
    assert calls == [
        (state_dir, True),
        (state_dir, False),
        ("teardown", True),
        ("discover", "192.0.2.10"),
        ("disconnect", "remote"),
    ]


def test_connect_passes_selection_options(state_dir, monkeypatch):
    captured = {}

    def fake_connect(path, target, **options):
        captured.update(target=target, options=options)
        return {"domain": "remote.portmap"}

    monkeypatch.setattr(client_cli, "connect", fake_connect)
    ssh_config = Path("/custom/ssh-config")
    assert client_cli.main([
        "connect", "remote.portmap", "--via", "ssh", "--ssh-target", "user@host",
        "--ssh-config", str(ssh_config), "--remote-port", "8081", "--tcp", "--timeout", "9",
    ]) == 0
    assert captured["target"] == "remote.portmap"
    assert captured["options"] == {
        "via": "ssh", "ssh_target": "user@host", "ssh_config": ssh_config,
        "remote_port": 8081, "include_tcp": True, "timeout": 9.0,
    }


def test_errors_are_reported_with_client_prefix(state_dir, monkeypatch, capsys):
    def failing_connections(path, check):
        raise PortmapError("resolver missing")

    monkeypatch.setattr(client_cli, "connections", failing_connections)
    assert client_cli.main(["status"]) == 2
    assert capsys.readouterr().err.strip() == "portmap-client: resolver missing"


def test_hidden_serve_catalog_delegates_to_view_worker(monkeypatch):
    captured = []

    def fake_view_main(argv):
        captured.append(list(argv))

    monkeypatch.setattr("portmap.client_view.main", fake_view_main)
    assert client_cli.main(["_serve-catalog", "--state-dir", "/state", "--port", "8080", "--owner", "o"]) == 0
    assert captured == [["--state-dir", "/state", "--port", "8080", "--owner", "o"]]


def test_help_documents_public_commands_only(capsys):
    with pytest.raises(SystemExit):
        client_cli.main(["--help"])
    help_text = capsys.readouterr().out
    for command in ("setup", "teardown", "status", "discover", "connect", "connections", "disconnect"):
        assert command in help_text
    # The worker stays undocumented: it may appear in argparse's generated
    # usage choices, but never as a described command.
    assert not any(line.lstrip().startswith("_serve-catalog") for line in help_text.splitlines())


def test_server_cli_no_longer_offers_client_commands():
    for argv in (["client"], ["client", "setup"], ["discover"], ["connect", "remote"], ["connections"], ["disconnect", "remote"]):
        with pytest.raises(SystemExit) as exit_info:
            server_cli.main(argv)
        assert exit_info.value.code == 2


def test_client_cli_imports_no_server_runtime():
    program = (
        "import sys; import portmap.client_cli, portmap.client, portmap.client_view;"
        "leaked = sorted(name for name in sys.modules if name.startswith('portmap.') "
        "and any(name == f'portmap.{part}' or name.startswith(f'portmap.{part}.') "
        "for part in ('settings', 'catalog', 'agent', 'planner', 'broker', 'compose', 'demo')));"
        "print(','.join(leaked))"
    )
    environment = {"PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, env=environment, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
