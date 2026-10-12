import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import client_runtime
from portmap.errors import PortmapError


def simulate_frozen(monkeypatch, bundle: Path):
    executable = bundle / "portmap"
    executable.write_text("#!/bin/sh\n:", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    return executable


def test_state_dir_prefers_explicit_isolation_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(tmp_path / "isolated"))
    monkeypatch.setenv("PORTMAP_STATE_DIR", str(tmp_path / "shared"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert client_runtime.client_state_dir() == (tmp_path / "isolated").resolve()


def test_state_dir_uses_shared_root_then_xdg_then_home_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PORTMAP_CLIENT_STATE_DIR", raising=False)
    monkeypatch.setenv("PORTMAP_STATE_DIR", str(tmp_path / "shared"))
    assert client_runtime.client_state_dir() == (tmp_path / "shared" / "client").resolve()
    monkeypatch.delenv("PORTMAP_STATE_DIR")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert client_runtime.client_state_dir() == (tmp_path / "xdg" / "portmap" / "client").resolve()
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert client_runtime.client_state_dir() == (tmp_path / "home" / ".local" / "state" / "portmap" / "client").resolve()


def test_legacy_state_dir_is_the_standalone_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert client_runtime.legacy_client_state_dir() == (tmp_path / "xdg" / "portmap-client").resolve()
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert client_runtime.legacy_client_state_dir() == (tmp_path / "home" / ".local" / "state" / "portmap-client").resolve()


def test_bundled_binary_wins_over_path_and_requires_executable_bit(monkeypatch, tmp_path):
    monkeypatch.setattr(client_runtime, "BUNDLED_BIN_DIR", tmp_path / "native_bin")
    (tmp_path / "native_bin").mkdir()
    bundled = tmp_path / "native_bin" / "coredns"
    bundled.write_text("#!/bin/sh\n:", encoding="utf-8")
    bundled.chmod(0o755)
    on_path = tmp_path / "on-path"
    on_path.write_text("#!/bin/sh\n:", encoding="utf-8")
    on_path.chmod(0o755)
    monkeypatch.setattr(client_runtime.shutil, "which", lambda _name: str(on_path))
    assert client_runtime.find_native_binary("coredns") == bundled

    bundled.chmod(0o644)
    assert client_runtime.find_native_binary("coredns") == on_path
    assert client_runtime.bundled_binary("../escape") is None


def test_native_environment_is_untouched_in_source_mode(monkeypatch):
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/custom-libs")
    assert client_runtime.native_environment()["LD_LIBRARY_PATH"] == "/opt/custom-libs"


def test_native_environment_drops_only_bundle_loader_entries(monkeypatch, tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    executable = simulate_frozen(monkeypatch, app)
    monkeypatch.setattr(sys, "_MEIPASS", str(app / "_internal"), raising=False)
    user_lib = tmp_path / "user-libs"
    monkeypatch.setenv("LD_LIBRARY_PATH", os.pathsep.join([str(app / "lib"), str(user_lib), ""]))
    monkeypatch.delenv("DYLD_LIBRARY_PATH", raising=False)
    monkeypatch.setenv("DYLD_FALLBACK_LIBRARY_PATH", str(app))
    environment = client_runtime.native_environment()
    assert environment["LD_LIBRARY_PATH"] == str(user_lib)
    assert "DYLD_FALLBACK_LIBRARY_PATH" not in environment
    # Everything unrelated to the loader passes through.
    assert environment["HOME"] == os.environ["HOME"]


def test_worker_environment_resets_bootloader_only_when_frozen(monkeypatch, tmp_path):
    monkeypatch.delenv("PYINSTALLER_RESET_ENVIRONMENT", raising=False)
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in client_runtime.worker_environment()
    simulate_frozen(monkeypatch, tmp_path)
    assert client_runtime.worker_environment()["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


@pytest.mark.parametrize(
    "command",
    [
        ["/usr/bin/portmap-client", "_serve-catalog", "--state-dir", "/s", "--port", "1", "--owner", "o"],
        ["/usr/bin/python3", "-m", "portmap.client_view", "--state-dir", "/s", "--port", "1", "--owner", "o"],
    ],
)
def test_serve_catalog_arguments_parses_both_recorded_forms(command):
    assert client_runtime.serve_catalog_arguments(command) == {"state_dir": "/s", "port": "1", "owner": "o"}


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/portmap-client _serve-catalog --state-dir /s",
        ["/usr/bin/portmap-client", "_serve-catalog", "--state-dir", "/s", "--port", "1"],
        ["/usr/bin/portmap-client", "_serve-catalog", "--state-dir", "/s", "--port", "abc", "--owner", "o"],
        ["/usr/bin/portmap-client", "-m", "portmap.server_view", "--state-dir", "/s", "--port", "1", "--owner", "o"],
        ["/bin/sh", "-c", "portmap.client_view --state-dir /s --port 1 --owner o"],
        ["relative/portmap-client", "_serve-catalog", "--state-dir", "/s", "--port", "1", "--owner", "o"],
    ],
)
def test_serve_catalog_arguments_rejects_foreign_commands(command):
    assert client_runtime.serve_catalog_arguments(command) is None


SESSION = "/user.slice/user-1000.slice/user@1000.service/session-9.scope"


def reset_scope_state(monkeypatch):
    monkeypatch.setattr(client_runtime, "_persistent_scope", None)


def forbid_busctl(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no session escape may be attempted")

    monkeypatch.setattr(client_runtime.subprocess, "run", forbidden)


def test_login_session_scope_matches_only_session_scopes(monkeypatch):
    legacy = "/user.slice/user-1000.slice/session-c2.scope"
    monkeypatch.setattr(
        client_runtime, "_cgroup_paths",
        lambda: ["", "/system.slice/portmap.service", "/user.slice/user-1000.slice/user@1000.service/app.slice/x.scope"],
    )
    assert client_runtime._login_session_scope() is None
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: [SESSION])
    assert client_runtime._login_session_scope() == SESSION
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: [legacy])
    assert client_runtime._login_session_scope() == legacy


def test_persistent_scope_does_nothing_outside_a_login_session(monkeypatch):
    reset_scope_state(monkeypatch)
    forbid_busctl(monkeypatch)
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: ["/system.slice/portmap.service"])
    assert client_runtime.ensure_persistent_scope(use_sudo=False) is None
    assert client_runtime._persistent_scope == ""


def test_persistent_scope_does_nothing_off_linux(monkeypatch):
    reset_scope_state(monkeypatch)
    forbid_busctl(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: [SESSION])
    assert client_runtime.ensure_persistent_scope(use_sudo=False) is None


def test_persistent_scope_refuses_without_sudo_before_any_worker(monkeypatch):
    reset_scope_state(monkeypatch)
    forbid_busctl(monkeypatch)
    monkeypatch.setattr(client_runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: [SESSION])
    with pytest.raises(PortmapError, match="--no-sudo"):
        client_runtime.ensure_persistent_scope(use_sudo=False)
    assert client_runtime._persistent_scope is None


def adopt_scope(monkeypatch, *, euid):
    commands = []
    paths = [SESSION]

    def run(command, **kwargs):
        commands.append(list(command))
        name = next(part for part in command if part.endswith(".scope"))
        paths[:] = [f"/system.slice/{name}"]
        assert kwargs["capture_output"] is True, "host output must not reach CLI stdout"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(client_runtime.subprocess, "run", run)
    monkeypatch.setattr(client_runtime.os, "geteuid", lambda: euid)
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: list(paths))
    return commands


@pytest.mark.parametrize("euid", [0, 1000])
def test_persistent_scope_adopts_once_per_cli_process(monkeypatch, euid):
    reset_scope_state(monkeypatch)
    commands = adopt_scope(monkeypatch, euid=euid)
    scope = client_runtime.ensure_persistent_scope(use_sudo=euid != 0)
    assert client_runtime._login_session_scope() is None
    assert client_runtime.ensure_persistent_scope(use_sudo=False) == scope
    assert len(commands) == 1

def test_persistent_scope_failure_surfaces_busctl_output(monkeypatch):
    reset_scope_state(monkeypatch)
    monkeypatch.setattr(client_runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: [SESSION])
    monkeypatch.setattr(
        client_runtime.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="start request rejected"),
    )
    with pytest.raises(PortmapError, match="start request rejected"):
        client_runtime.ensure_persistent_scope(use_sudo=True)
    assert client_runtime._persistent_scope is None


def test_persistent_scope_adoption_wait_is_bounded(monkeypatch):
    reset_scope_state(monkeypatch)
    monkeypatch.setattr(client_runtime, "_SCOPE_ADOPT_WAIT", 0.01)
    monkeypatch.setattr(client_runtime, "_SCOPE_ADOPT_POLL", 0.001)
    monkeypatch.setattr(client_runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(client_runtime, "_cgroup_paths", lambda: [SESSION])
    monkeypatch.setattr(
        client_runtime.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    with pytest.raises(PortmapError, match="did not enter transient scope"):
        client_runtime.ensure_persistent_scope(use_sudo=True)
