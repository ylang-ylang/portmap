from pathlib import Path
from types import SimpleNamespace

from portmap.cli import main


def test_portmap_up_starts_agent_before_gateway(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "portmap.toml").write_text("", encoding="utf-8")
    calls = []

    def fake_ensure_agent_started(settings):
        calls.append(("agent-start", settings.agent_socket))
        return SimpleNamespace(running=True, as_dict=lambda: {"running": True})

    def fake_run(command, *, check, env):
        calls.append(("compose", command, env))
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("PORTMAP_ROOT", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr("portmap.cli.ensure_agent_started", fake_ensure_agent_started)
    monkeypatch.setattr("portmap.cli.subprocess.run", fake_run)

    assert main(["up"]) == 0

    assert calls[0][0] == "agent-start"
    assert calls[1][0] == "compose"
    assert calls[1][1] == ["docker", "compose", "-f", str(tmp_path / "docker-compose.yml"), "up", "-d"]
    assert calls[1][2]["PORTMAP_AGENT_RUNTIME_HOST_DIR"] == str(tmp_path / "runtime" / "portmap")
    assert calls[1][2]["PORTMAP_AGENT_SOCKET"] == "/run/portmap/agent.sock"


def test_portmap_down_stops_gateway_and_agent(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "portmap.toml").write_text("", encoding="utf-8")
    calls = []

    def fake_stop_agent(settings):
        calls.append(("agent-stop", settings.agent_socket))
        return SimpleNamespace(running=False, as_dict=lambda: {"running": False})

    def fake_run(command, *, check, env):
        calls.append(("compose", command, env))
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("PORTMAP_ROOT", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr("portmap.cli.stop_agent", fake_stop_agent)
    monkeypatch.setattr("portmap.cli.subprocess.run", fake_run)

    assert main(["down"]) == 0

    assert calls[0][0] == "compose"
    assert calls[0][1] == ["docker", "compose", "-f", str(tmp_path / "docker-compose.yml"), "down"]
    assert calls[1][0] == "agent-stop"
