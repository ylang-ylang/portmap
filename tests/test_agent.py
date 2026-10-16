"""Background and foreground agent startup regressions."""
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from portmap import agent, client_runtime
from portmap.agent_client import agent_health
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


def test_start_foreground_agent_reuses_a_healthy_running_agent(settings, monkeypatch):
    handle = agent.start_foreground_agent(settings)
    assert handle is not None
    worker_pid = handle.process.pid

    def must_not_touch(*args, **kwargs):
        raise AssertionError("a reused agent must never be stopped or replaced")

    monkeypatch.setattr(agent, "_stop_agent", must_not_touch)
    monkeypatch.setattr(agent, "_spawn_foreground_worker", must_not_touch)
    try:
        assert agent.start_foreground_agent(settings) is None
        health = agent_health(socket_path=settings.agent_socket, timeout=2)
        assert health["pid"] == worker_pid != os.getpid()
        # The reused worker and its metadata were never touched.
        assert agent.read_pid(settings.agent_pid_file) == worker_pid
        assert settings.agent_pid_file.exists()
    finally:
        handle.stop()

    assert not settings.agent_pid_file.exists()


def test_start_foreground_agent_refuses_a_foreign_state_directory(settings, monkeypatch):
    monkeypatch.setattr(agent, "agent_status", lambda _settings: _status(settings, running=True))
    monkeypatch.setattr(agent, "agent_health", lambda **kwargs: {
        "protocol": agent.AGENT_PROTOCOL,
        "version": agent.__version__,
        "access_state_dir": "/srv/other-client-state",
    })
    with pytest.raises(PortmapError, match="different client state directory"):
        agent.start_foreground_agent(settings)


def test_start_foreground_agent_retires_a_mismatched_agent_under_lock(settings, monkeypatch):
    probes = {"count": 0, "health": 0}

    def fading_status(_settings):
        probes["count"] += 1
        return _status(settings, running=probes["count"] == 1)

    real_agent_health = agent.agent_health

    def stale_then_real_health(**kwargs):
        probes["health"] += 1
        if probes["health"] > 1:
            return real_agent_health(**kwargs)
        return {
            "protocol": agent.AGENT_PROTOCOL,
            "version": "0.0.0",
            "access_state_dir": str(settings.client_state_dir.resolve()),
        }

    monkeypatch.setattr(agent, "agent_status", fading_status)
    monkeypatch.setattr(agent, "agent_health", stale_then_real_health)
    retirements = []
    monkeypatch.setattr(agent, "_stop_agent", lambda _settings: retirements.append("stopped") or _status(settings, running=False))
    handle = agent.start_foreground_agent(settings)
    try:
        assert handle is not None
        assert handle.process.pid != os.getpid()
        assert retirements == ["stopped"]
        assert agent_health(socket_path=settings.agent_socket, timeout=2)["version"] == agent.__version__
    finally:
        handle.stop()


def test_foreground_agent_serves_controls_and_cleans_owned_metadata(settings):
    settings.client_state_dir.mkdir(parents=True, mode=0o700)
    handle = agent.start_foreground_agent(settings)
    assert handle is not None
    worker_pid = handle.process.pid
    health = agent_health(socket_path=settings.agent_socket, timeout=2)
    assert health["ok"] is True
    assert health["pid"] == worker_pid != os.getpid()
    assert health["access_state_dir"] == str(settings.client_state_dir.resolve())
    assert agent.read_pid(settings.agent_pid_file) == worker_pid
    assert os.getpgid(worker_pid) == worker_pid
    assert os.getsid(worker_pid) == os.getsid(0)

    handle.stop()

    assert handle.process.returncode is not None
    assert not settings.agent_socket.exists()
    assert not settings.agent_pid_file.exists()
    assert not (settings.agent_runtime_dir / "agent-owner.json").exists()


def _stubborn_group_member(group_pid: int) -> subprocess.Popen:
    """A worker-group member that ignores the graceful SIGTERM."""
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
         "print('armed', flush=True); time.sleep(300)"],
        stdout=subprocess.PIPE,
        process_group=group_pid,
    )
    assert child.stdout.readline().strip() == b"armed"
    return child


def test_foreground_worker_stop_is_bounded_and_kills_the_whole_owned_group(settings):
    settings.client_state_dir.mkdir(parents=True, mode=0o700)
    handle = agent.start_foreground_agent(settings)
    assert handle is not None
    worker_pid = handle.process.pid

    # An indefinitely blocked control request: the worker's handler thread
    # waits for a request body that never arrives.
    blocked = socket.socket(socket.AF_UNIX)
    blocked.connect(str(settings.agent_socket))
    blocked.sendall(
        b"POST /access/connections HTTP/1.1\r\nHost: agent\r\n"
        b"content-length: 1073741824\r\n\r\n"
    )
    # Other requests still answer while that handler thread is stuck.
    assert agent_health(socket_path=settings.agent_socket, timeout=2)["pid"] == worker_pid
    stubborn = _stubborn_group_member(worker_pid)

    started = time.monotonic()
    handle.stop()
    elapsed = time.monotonic() - started

    # Bounded shutdown; the stubborn member forces the full graceful grace.
    assert agent.FOREGROUND_STOP_GRACE - 1 <= elapsed < agent.FOREGROUND_STOP_GRACE + 2
    assert handle.process.returncode is not None
    assert stubborn.wait(timeout=5) == -signal.SIGKILL
    assert not settings.agent_socket.exists()
    assert not settings.agent_pid_file.exists()
    assert not (settings.agent_runtime_dir / "agent-owner.json").exists()
    blocked.close()


def test_foreground_startup_failure_cleans_owned_child_group_and_files(settings, monkeypatch):
    settings.client_state_dir.mkdir(parents=True, mode=0o700)
    spawned = {}

    def broken_worker(broken_settings, _access):
        process = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(3)"],
            cwd=broken_settings.state_dir,
            process_group=0,
            start_new_session=False,
        )
        spawned["process"] = process
        return process, broken_settings.agent_log_file

    monkeypatch.setattr(agent, "_spawn_foreground_worker", broken_worker)
    with pytest.raises(PortmapError, match="exited during startup"):
        agent.start_foreground_agent(settings)

    assert spawned["process"].returncode == 3
    assert not settings.agent_socket.exists()
    assert not settings.agent_pid_file.exists()
    assert not (settings.agent_runtime_dir / "agent-owner.json").exists()


def test_foreground_spawn_cleanup_on_cancellation(settings, monkeypatch):
    settings.client_state_dir.mkdir(parents=True, mode=0o700)
    spawned = {}
    real_spawn = agent._spawn_foreground_worker

    def tracking_spawn(spawn_settings, access):
        process, log_file = real_spawn(spawn_settings, access)
        spawned["process"] = process
        return process, log_file

    def cancelled(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(agent, "_spawn_foreground_worker", tracking_spawn)
    monkeypatch.setattr(agent, "_await_foreground_worker", cancelled)
    with pytest.raises(KeyboardInterrupt):
        agent.start_foreground_agent(settings)

    # The owned child and its group died and were reaped before the raise.
    assert spawned["process"].returncode is not None
    assert not settings.agent_socket.exists()
    assert not settings.agent_pid_file.exists()
    assert not (settings.agent_runtime_dir / "agent-owner.json").exists()
