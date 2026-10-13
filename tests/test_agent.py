"""Background agent startup regressions."""
import pytest

from portmap import agent, client_runtime
from portmap.errors import PortmapError
from portmap.settings import load_portmap_settings


@pytest.fixture
def settings(tmp_path):
    return load_portmap_settings(environ={
        "PORTMAP_ROOT": str(tmp_path),
        "PORTMAP_STATE_DIR": str(tmp_path / "state"),
        "PORTMAP_AGENT_RUNTIME_DIR": str(tmp_path / "agent"),
        "PORTMAP_RUNTIME": "docker",
    })


def _status(settings, *, running):
    return agent.AgentProcessStatus(
        running=running,
        socket=settings.agent_socket,
        pid_file=settings.agent_pid_file,
        message="" if running else "agent socket is not answering",
    )


def test_agent_start_refuses_session_escape_without_sudo_before_spawn(settings, monkeypatch):
    monkeypatch.setattr(client_runtime.sys, "platform", "linux")
    monkeypatch.setattr(client_runtime, "_persistent_scope", None)
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: ["/user.slice/user-1000.slice/session-1.scope"])
    monkeypatch.setattr(client_runtime.os, "geteuid", lambda: 1000)

    def must_not_spawn(*args, **kwargs):
        raise AssertionError("no agent worker may be spawned")

    monkeypatch.setattr(agent, "agent_status", lambda _settings: _status(settings, running=False))
    monkeypatch.setattr(agent.subprocess, "Popen", must_not_spawn)

    with pytest.raises(PortmapError):
        agent.ensure_agent_started(settings, use_sudo=False)

    assert not settings.agent_log_file.exists()


def test_agent_upgrade_preserves_live_worker_when_scope_permission_is_denied(settings, monkeypatch):
    monkeypatch.setattr(client_runtime.sys, "platform", "linux")
    monkeypatch.setattr(client_runtime, "_persistent_scope", None)
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: [
        "/user.slice/user-1000.slice/user@1000.service/app.slice/portmap-brew-setup.service",
    ])
    monkeypatch.setattr(client_runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(agent, "agent_status", lambda _settings: _status(settings, running=True))
    monkeypatch.setattr(agent, "agent_health", lambda **kwargs: {
        "protocol": agent.AGENT_PROTOCOL, "version": "0.0.0",
    })

    def must_not_stop(*args, **kwargs):
        raise AssertionError("a working old agent must survive failed upgrade preflight")

    monkeypatch.setattr(agent, "_stop_agent", must_not_stop)
    with pytest.raises(PortmapError, match="--no-sudo"):
        agent.ensure_agent_started(settings, use_sudo=False)
