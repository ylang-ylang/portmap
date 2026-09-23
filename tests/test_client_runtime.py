import os
import sys
from pathlib import Path

import pytest

from portmap import client_runtime


def simulate_frozen(monkeypatch, bundle: Path):
    executable = bundle / "portmap-client"
    executable.write_text("#!/bin/sh\n:", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    return executable


def test_state_dir_prefers_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(tmp_path / "custom"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert client_runtime.client_state_dir() == (tmp_path / "custom").resolve()


def test_state_dir_uses_xdg_then_home_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PORTMAP_CLIENT_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert client_runtime.client_state_dir() == (tmp_path / "xdg" / "portmap-client").resolve()
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert client_runtime.client_state_dir() == (tmp_path / "home" / ".local" / "state" / "portmap-client").resolve()


def test_bundled_binary_wins_over_path_and_requires_executable_bit(monkeypatch, tmp_path):
    monkeypatch.setattr(client_runtime, "BUNDLED_BIN_DIR", tmp_path / "client_bin")
    (tmp_path / "client_bin").mkdir()
    bundled = tmp_path / "client_bin" / "coredns"
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


def test_serve_catalog_command_matches_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    source = client_runtime.serve_catalog_command(state_dir="/state", port=8080, owner="owner")
    assert source == [sys.executable, "-m", "portmap.client_view", "--state-dir", "/state", "--port", "8080", "--owner", "owner"]
    simulate_frozen(monkeypatch, tmp_path)
    frozen = client_runtime.serve_catalog_command(state_dir="/state", port=8080, owner="owner")
    assert frozen == [sys.executable, "_serve-catalog", "--state-dir", "/state", "--port", "8080", "--owner", "owner"]


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
