"""Background and foreground agent startup regressions."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import threading

import pytest

from portmap import agent, agent_client, client, client_runtime
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


def test_owned_agent_keeps_http_controls_responsive_and_pins_state_during_outage(settings, tmp_path, monkeypatch):
    selected = tmp_path / "selected-client"
    selected.mkdir(mode=0o700)
    other = tmp_path / "other-client"
    monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(selected))
    entered = threading.Event()
    finished = threading.Event()
    published = []

    def outage(state_dir, stop_event):
        entered.set()
        # Model an unavailable remote waiting for its cancellable retry.
        if not stop_event.wait(timeout=10):
            published.append(state_dir)
        finished.set()

    monkeypatch.setattr(client, "run_connection_maintenance", outage)
    monkeypatch.setattr(client, "connections", lambda *args, **kwargs: {"connections": []})
    server = agent._bind_agent_server(settings.agent_socket, settings.agent_pid_file)
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    serving.start()
    try:
        assert entered.wait(timeout=2)
        # Even an environment change cannot send HTTP mutations to a different
        # store from the worker bound to this owned socket.
        monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(other))
        assert agent_health(socket_path=settings.agent_socket)["access_state_dir"] == str(selected)
        status, payload = agent_client.request_json_status(
            "POST", "/access/connections", socket_path=settings.agent_socket, timeout=1,
            payload={"action": "save", "target": "user@offline-host", "via": "ssh"},
        )
        assert status == 200
        assert payload["connections"][0]["target"] == "user@offline-host"
        stored = json.loads((selected / "connection-targets.json").read_text())
        assert stored
        assert not (other / "connection-targets.json").exists()
    finally:
        server.shutdown()
        serving.join(timeout=2)
        agent._retire_agent_server(server)

    assert finished.is_set()
    assert published == []
    assert not settings.agent_socket.exists()
    assert not settings.agent_pid_file.exists()


def test_owned_agent_retirement_does_not_wait_forever_for_a_stuck_worker(settings, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(agent, "MAINTENANCE_STOP_TIMEOUT", 0.05)

    def stuck_worker(state_dir, stop_event):
        entered.set()
        release.wait(timeout=10)

    monkeypatch.setattr(client, "run_connection_maintenance", stuck_worker)
    server = agent._bind_agent_server(settings.agent_socket, settings.agent_pid_file)
    try:
        assert entered.wait(timeout=2)
        started = time.monotonic()
        agent._retire_agent_server(server)
        assert time.monotonic() - started < 1
        assert server.maintenance_stop.is_set()
        assert not settings.agent_socket.exists()
        assert not settings.agent_pid_file.exists()
    finally:
        release.set()
        server.maintenance_thread.join(timeout=2)


_OUTAGE_AGENT = """
import sys
from portmap import agent, client

def outage(state_dir, stop_event):
    journal = state_dir / "maintenance-journal"
    with journal.open("a") as output:
        output.write("started\\n")
    if not stop_event.wait(timeout=30):
        (state_dir / "late-reconnect").write_text("unexpected")
    with journal.open("a") as output:
        output.write("stopped\\n")

client.run_connection_maintenance = outage
client.connections = lambda *args, **kwargs: {"connections": []}
raise SystemExit(agent.main(sys.argv[1:]))
"""


@pytest.mark.parametrize("foreground", [False, True], ids=["background", "foreground"])
@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_agent_signal_stops_maintenance_and_reuse_never_duplicates_it(settings, tmp_path, foreground, stop_signal):
    selected = tmp_path / "selected-client"
    selected.mkdir(mode=0o700)
    journal = selected / "maintenance-journal"
    command = [
        sys.executable, "-c", _OUTAGE_AGENT, "serve",
        "--socket", str(settings.agent_socket), "--pid-file", str(settings.agent_pid_file),
    ]
    if foreground:
        command.append("--foreground-lifetime")
    process = subprocess.Popen(
        command, env=agent._agent_worker_environment(settings, selected),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, process_group=0,
    )
    blocked = socket.socket(socket.AF_UNIX)
    try:
        agent._await_foreground_worker(settings, process, selected)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if journal.exists() and journal.read_text() == "started\n":
                break
            time.sleep(0.01)
        assert journal.read_text() == "started\n"
        assert agent.start_foreground_agent(settings, access_state_dir=selected) is None
        assert agent.ensure_agent_started(settings, use_sudo=False, access_state_dir=selected).pid == process.pid
        assert agent_health(socket_path=settings.agent_socket, timeout=1)["access_state_dir"] == str(selected)
        status, body = agent_client.request_json_status(
            "POST", "/access/connections", socket_path=settings.agent_socket, timeout=1,
            payload={"action": "save", "target": "user@unreachable", "via": "ssh"},
        )
        assert status == 200
        assert body["connections"][0]["target"] == "user@unreachable"
        # A hung independent HTTP operation must not delay cancellation or
        # prevent other controls answering during the maintenance outage.
        blocked.connect(str(settings.agent_socket))
        blocked.sendall(b"POST /access/connections HTTP/1.1\r\nHost: agent\r\ncontent-length: 65536\r\n\r\n")
        assert agent_health(socket_path=settings.agent_socket, timeout=1)["pid"] == process.pid
        started = time.monotonic()
        process.send_signal(stop_signal)
        assert process.wait(timeout=5) == 0
        assert time.monotonic() - started < 5
        assert journal.read_text() == "started\nstopped\n"
        assert not (selected / "late-reconnect").exists()
        assert (selected / "connection-targets.json").exists()
        assert not settings.agent_socket.exists()
        assert not settings.agent_pid_file.exists()
        assert not (settings.agent_runtime_dir / "agent-owner.json").exists()
    finally:
        blocked.close()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_agent_stop_during_bind_cancels_before_any_reconnect(settings, monkeypatch):
    reconnects = []
    completed = threading.Event()
    original_bind = agent._bind_agent_server
    previous_handler = signal.getsignal(signal.SIGTERM)

    def stopping_bind(*args, **kwargs):
        signal.raise_signal(signal.SIGTERM)
        return original_bind(*args, **kwargs)

    def maintenance(state_dir, stop_event):
        if not stop_event.is_set():
            reconnects.append(state_dir)
        completed.set()

    monkeypatch.setattr(agent, "_bind_agent_server", stopping_bind)
    monkeypatch.setattr(client, "run_connection_maintenance", maintenance)
    agent.run_agent_server(settings.agent_socket, pid_file=settings.agent_pid_file)

    assert completed.is_set()
    assert reconnects == []
    assert signal.getsignal(signal.SIGTERM) == previous_handler
    assert not settings.agent_socket.exists()
    assert not settings.agent_pid_file.exists()
    assert not (settings.agent_runtime_dir / "agent-owner.json").exists()
