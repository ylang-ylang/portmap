"""Foreground runner lifecycle regressions."""
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import agent, lifecycle
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


@pytest.fixture
def state_dir(settings):
    directory = settings.client_state_dir
    directory.mkdir(parents=True, mode=0o700)
    return directory


def save_connection(state_dir: Path) -> None:
    (state_dir / "connections.json").write_text(json.dumps({
        "version": 1,
        "connections": {
            "saved.portmap": {"domain": "saved.portmap", "via": "ssh", "target": "user@saved-host"},
        },
    }), encoding="utf-8")


def read_connection(state_dir: Path) -> dict:
    return json.loads((state_dir / "connections.json").read_text(encoding="utf-8"))["connections"]


def test_second_runner_conflicts_without_touching_the_active_one(settings, state_dir, monkeypatch):
    handle = (state_dir / "runner.lock").open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    lifecycle._claim_runner(state_dir)
    attempts = []
    monkeypatch.setattr(lifecycle, "setup_client", lambda *args, **kwargs: attempts.append("setup") or {})

    with pytest.raises(PortmapError, match=r"already active \(pid \d+\)"):
        lifecycle.run_foreground(settings)

    assert attempts == []
    assert lifecycle.runner_owner_path(state_dir).exists()
    handle.close()


def test_foreground_run_serves_controls_and_stops_cleanly(settings, state_dir, monkeypatch):
    save_connection(state_dir)
    seen = {}

    def fake_setup_client(resolved, *, settings=None, use_sudo=True, http_port=None):
        seen["state_dir"] = resolved
        seen["use_sudo"] = use_sudo
        # The runner must already serve working agent controls for this state
        # directory while setup runs; readiness is never assumed before it.
        seen["health"] = agent_health(socket_path=settings.agent_socket, timeout=2)
        seen["worker_pid"] = agent.read_pid(settings.agent_pid_file)
        return {"gateway": {"address": "127.0.0.1", "http_port": 8080, "running": True}}

    def stop_once_ready(_summary, _stream):
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(lifecycle, "setup_client", fake_setup_client)
    monkeypatch.setattr(lifecycle, "_report_ready", stop_once_ready)
    reaped = []
    monkeypatch.setattr(lifecycle, "stop_tracked_tunnels", lambda: reaped.append("reaped") or [])
    retired = []
    monkeypatch.setattr(
        lifecycle, "retire_stopped_transports",
        lambda state_dir, *, use_sudo=True: retired.append((str(state_dir), use_sudo)) or [],
    )

    assert lifecycle.run_foreground(settings, use_sudo=False, http_port=8080, stream=sys.stdout) == 0

    assert seen["state_dir"] == state_dir.resolve()
    assert seen["use_sudo"] is False
    assert seen["health"]["ok"] is True
    # The runner serves through its owned worker process, not this process.
    assert seen["health"]["pid"] != os.getpid()
    assert seen["health"]["pid"] == seen["worker_pid"]
    assert seen["health"]["access_state_dir"] == str(state_dir.resolve())
    assert reaped == ["reaped"]
    # Stopped masters must not keep advertised DNS/routes, while the shared
    # resolver and every saved profile survive the shutdown.
    assert retired == [(str(state_dir.resolve()), False)]
    # Only owned Unix/PID metadata is removed; the saved connection survives.
    assert not settings.agent_socket.exists()
    assert not settings.agent_pid_file.exists()
    assert not (settings.agent_runtime_dir / "agent-owner.json").exists()
    assert not lifecycle.runner_owner_path(state_dir).exists()
    assert read_connection(state_dir)["saved.portmap"]["target"] == "user@saved-host"


def test_foreground_run_failure_retires_its_agent_and_keeps_connections(settings, state_dir, monkeypatch):
    save_connection(state_dir)

    def failing_setup(resolved, *, settings=None, **kwargs):
        assert agent_health(socket_path=settings.agent_socket, timeout=2)["ok"] is True
        raise PortmapError("infrastructure unavailable")

    monkeypatch.setattr(lifecycle, "setup_client", failing_setup)
    monkeypatch.setattr(lifecycle, "retire_stopped_transports", lambda *args, **kwargs: [])
    with pytest.raises(PortmapError, match="infrastructure unavailable"):
        lifecycle.run_foreground(settings)

    assert not settings.agent_socket.exists()
    assert not settings.agent_pid_file.exists()
    assert not lifecycle.runner_owner_path(state_dir).exists()
    assert read_connection(state_dir)["saved.portmap"]["via"] == "ssh"


@pytest.mark.parametrize("failure_stage", ["stop_tracked_tunnels", "retire_stopped_transports"])
def test_shutdown_failure_is_reported_without_losing_connections(settings, state_dir, monkeypatch, failure_stage):
    save_connection(state_dir)

    def ready_then_stop(*args, **kwargs):
        signal.raise_signal(signal.SIGTERM)
        return {"gateway": {"http_port": 80}}

    monkeypatch.setattr(lifecycle, "setup_client", ready_then_stop)
    monkeypatch.setattr(lifecycle, "stop_tracked_tunnels", lambda: [])
    monkeypatch.setattr(lifecycle, "retire_stopped_transports", lambda *args, **kwargs: [])
    monkeypatch.setattr(lifecycle, failure_stage, lambda *args, **kwargs: ["saved.portmap: unavailable"])
    assert lifecycle.run_foreground(settings, stream=sys.stdout) == 1
    assert read_connection(state_dir)["saved.portmap"]["target"] == "user@saved-host"
    assert not settings.agent_socket.exists()


def test_foreground_lifetime_covers_operations_after_setup(settings, state_dir, monkeypatch):
    from portmap import client_runtime

    monkeypatch.setattr(client_runtime, "_persistent_scope", None)
    monkeypatch.setattr(client_runtime, "_disposable_user_scope", lambda: "/user.slice/user@1000.service")
    monkeypatch.setattr(client_runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(lifecycle, "setup_client", lambda *args, **kwargs: {"gateway": {"http_port": 80}})
    monkeypatch.setattr(lifecycle, "stop_tracked_tunnels", lambda: [])
    monkeypatch.setattr(lifecycle, "retire_stopped_transports", lambda *args, **kwargs: [])

    def ready_in_foreground(summary, stream):
        # An operation after initialization must not need sudo to escape
        # the caller's cgroup. This is also the scope used by API threads.
        client_runtime.ensure_persistent_scope(use_sudo=False)
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(lifecycle, "_report_ready", ready_in_foreground)
    assert lifecycle.run_foreground(settings, use_sudo=False) == 0
    assert client_runtime._persistent_scope is None
    assert not settings.agent_socket.exists()


def test_foreground_shutdown_is_bounded_with_a_hung_worker_descendant(settings, state_dir, monkeypatch):
    save_connection(state_dir)
    monkeypatch.setattr(lifecycle, "setup_client", lambda *args, **kwargs: {"gateway": {"http_port": 80}})
    monkeypatch.setattr(lifecycle, "stop_tracked_tunnels", lambda: [])
    monkeypatch.setattr(lifecycle, "retire_stopped_transports", lambda *args, **kwargs: [])
    stubborn = {}

    def ready_with_hung_descendant(_summary, _stream):
        worker_pid = agent.read_pid(settings.agent_pid_file)
        assert worker_pid not in (None, os.getpid())
        stubborn["child"] = subprocess.Popen(
            [sys.executable, "-c",
             "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "print('armed', flush=True); time.sleep(300)"],
            stdout=subprocess.PIPE,
            process_group=worker_pid,
        )
        assert stubborn["child"].stdout.readline().strip() == b"armed"
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(lifecycle, "_report_ready", ready_with_hung_descendant)

    started = time.monotonic()
    assert lifecycle.run_foreground(settings, use_sudo=False, stream=sys.stdout) == 0
    elapsed = time.monotonic() - started

    # Bounded shutdown even with a group member that ignores SIGTERM.
    assert agent.FOREGROUND_STOP_GRACE - 1 <= elapsed < agent.FOREGROUND_STOP_GRACE + 5
    assert stubborn["child"].wait(timeout=5) == -signal.SIGKILL
    assert not settings.agent_socket.exists()
    assert not settings.agent_pid_file.exists()
    assert not (settings.agent_runtime_dir / "agent-owner.json").exists()
    assert not lifecycle.runner_owner_path(state_dir).exists()
    assert read_connection(state_dir)["saved.portmap"]["target"] == "user@saved-host"


def test_stop_signal_during_a_blocked_setup_cancels_and_cleans_up(settings, state_dir, monkeypatch):
    save_connection(state_dir)
    entered = threading.Event()
    release = threading.Event()

    def blocked_setup(*args, **kwargs):
        entered.set()
        # An unbounded infrastructure/setup call on the main thread.
        release.wait(timeout=30)
        raise AssertionError("a cancelled initialization must never complete")

    monkeypatch.setattr(lifecycle, "setup_client", blocked_setup)
    monkeypatch.setattr(lifecycle, "stop_tracked_tunnels", lambda: [])
    monkeypatch.setattr(lifecycle, "retire_stopped_transports", lambda *args, **kwargs: [])

    def stop_once_initialization_blocks():
        # Signal only after the blocked setup is really running, so slow
        # worker startup cannot move the cancellation out of the test's way.
        if not entered.wait(timeout=10):
            return
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)

    started = time.monotonic()
    signalling = threading.Timer(0.3, stop_once_initialization_blocks)
    try:
        signalling.start()
        assert lifecycle.run_foreground(settings, use_sudo=False, stream=sys.stdout) == 0
    finally:
        # The timer must never outlive the runner's own signal handlers.
        signalling.cancel()
        signalling.join(timeout=12)
        release.set()
    elapsed = time.monotonic() - started

    # The signal cancelled the blocked initialization instead of waiting for
    # it, and the normal cleanup still removed every owned worker artifact.
    assert entered.is_set()
    assert elapsed < 10
    assert not settings.agent_socket.exists()
    assert not settings.agent_pid_file.exists()
    assert not (settings.agent_runtime_dir / "agent-owner.json").exists()
    assert not lifecycle.runner_owner_path(state_dir).exists()
    assert read_connection(state_dir)["saved.portmap"]["target"] == "user@saved-host"


def test_selected_settings_replaces_only_explicit_backends(monkeypatch, tmp_path):
    from portmap import access_cli

    base = load_portmap_settings(environ={
        "PORTMAP_ROOT": str(tmp_path),
        "PORTMAP_STATE_DIR": str(tmp_path / "state"),
        "PORTMAP_RUNTIME": "docker",
    })
    monkeypatch.setattr(access_cli, "load_portmap_settings", lambda: base)

    only_resolver = access_cli.selected_settings(SimpleNamespace(resolver="forward", ingress=None))
    assert only_resolver.resolver_backend == "forward"
    assert only_resolver.ingress_backend == base.ingress_backend
    assert only_resolver is not base

    untouched = access_cli.selected_settings(SimpleNamespace(resolver=None, ingress=None))
    assert untouched is base


