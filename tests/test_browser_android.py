"""Real-import lifecycle regressions; browser acceptance remains a separate suite."""
import importlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest
from selenium.common.exceptions import WebDriverException


BROWSER_SOURCE = Path(__file__).parent / "browser"


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(BROWSER_SOURCE))
    monkeypatch.setenv("ANDROID_FIREFOX_CHANNEL", "nightly")
    module = importlib.import_module("android")
    assert Path(module.__file__).resolve() == (BROWSER_SOURCE / "android.py").resolve()
    assert Path(module.runner.__file__).resolve() == (BROWSER_SOURCE / "runner.py").resolve()
    monkeypatch.setattr(module.runner, "ARTIFACTS", tmp_path)
    monkeypatch.setattr(module, "PROCESSES", [])
    monkeypatch.setattr(module, "ADB_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(module, "EMULATOR_STOP_TIMEOUT_SECONDS", 0.3)
    yield module
    module.cleanup()


@pytest.fixture
def adb_program(tmp_path, monkeypatch):
    invocations = tmp_path / "adb-invocations.jsonl"
    monkeypatch.setenv("ADB_INVOCATIONS", str(invocations))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    def install(body):
        executable = tmp_path / "adb"
        executable.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "os.umask(0o077)\n"
            "with Path(os.environ['ADB_INVOCATIONS']).open('a') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            + textwrap.dedent(body))
        executable.chmod(0o700)
        return invocations

    return install


def records(lifecycle):
    path = lifecycle.runner.ARTIFACTS / "android-lifecycle-failures.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def browser_without_start(lifecycle, driver, screenshot=lambda name: None):
    browser = object.__new__(lifecycle.AndroidBrowser)
    browser.driver = driver
    browser.screenshot = screenshot
    return browser


def test_hanging_adb_is_bounded_and_payload_is_not_copied_to_failure_evidence(lifecycle, adb_program):
    invocations = adb_program("time.sleep(60)\n")
    secret = "private-test-shell-payload"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        lifecycle.adb("shell", secret)
    assert time.monotonic() - started < 3
    assert invocations.exists()
    event = records(lifecycle)[0]
    assert event["operation"] == "adb:shell" and event["errorType"] == "TimeoutExpired"
    assert secret not in (lifecycle.runner.ARTIFACTS / "android-lifecycle-failures.jsonl").read_text()


def test_failed_adb_keeps_native_exit_status_and_failure_evidence(lifecycle, adb_program):
    adb_program("raise SystemExit(23)\n")
    with pytest.raises(subprocess.CalledProcessError) as failure:
        lifecycle.adb("shell", "id", capture_output=True)
    assert failure.value.returncode == 23
    event = records(lifecycle)[0]
    assert event["errorType"] == "CalledProcessError" and event["returncode"] == 23


def test_exited_emulator_prevents_starting_another_adb_wait(lifecycle, adb_program):
    invocations = adb_program("raise SystemExit(0)\n")
    process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(17)"])
    assert process.wait(timeout=3) == 17
    lifecycle.PROCESSES.append(process)
    with pytest.raises(RuntimeError, match="emulator exited"):
        lifecycle.adb("shell", "getprop")
    assert not invocations.exists()
    assert records(lifecycle)[0]["emulatorReturnCodes"] == [17]


def test_close_is_idempotent_and_retains_previous_native_logcat(lifecycle, adb_program):
    invocations = adb_program("sys.stdout.buffer.write(b'current logcat\\n')\n")
    log = lifecycle.runner.ARTIFACTS / "android-logcat.txt"
    log.write_bytes(b"previous logcat\n")
    disposed = []
    browser = browser_without_start(lifecycle, SimpleNamespace(quit=lambda: disposed.append(True)))
    browser.close()
    browser.close()
    assert browser.driver is None and disposed == [True]
    assert len(invocations.read_text().splitlines()) == 1
    assert log.read_bytes() == b"previous logcat\ncurrent logcat\n"


def test_close_keeps_native_capture_timeout_when_disposal_also_fails(lifecycle, adb_program):
    invocations = adb_program("time.sleep(60)\n")

    def quit_driver():
        raise OSError("disposal failed")

    browser = browser_without_start(lifecycle, SimpleNamespace(quit=quit_driver))
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        browser.close()
    assert browser.driver is None
    assert "OSError" in " ".join(failure.value.__notes__)
    browser.close()
    assert len(invocations.read_text().splitlines()) == 1
    assert [row["operation"] for row in records(lifecycle)] == ["adb:logcat", "close-capture", "webdriver-quit"]


def test_close_preserves_existing_case_failure_without_more_native_ui_commands(lifecycle, adb_program):
    invocations = adb_program("raise SystemExit(99)\n")

    def quit_driver():
        raise OSError("disposal failed")

    def screenshot(name):
        pytest.fail("A failed browser must not receive another screenshot command")

    browser = browser_without_start(lifecycle, SimpleNamespace(quit=quit_driver), screenshot)
    primary = ValueError("actual case failure")
    with pytest.raises(ValueError) as observed:
        try:
            raise primary
        finally:
            browser.close()
    assert observed.value is primary and browser.driver is None
    assert not invocations.exists()
    assert "OSError" in " ".join(primary.__notes__)
    assert [row["operation"] for row in records(lifecycle)] == ["close-after-primary-failure", "webdriver-quit"]


def test_disposal_failure_without_an_earlier_error_is_not_suppressed(lifecycle, adb_program):
    adb_program("raise SystemExit(0)\n")
    failure = OSError("actual disposal failure")

    def quit_driver():
        raise failure

    browser = browser_without_start(lifecycle, SimpleNamespace(quit=quit_driver))
    with pytest.raises(OSError) as observed:
        browser.close()
    assert observed.value is failure and browser.driver is None


@pytest.mark.parametrize("reported", [
    "/storage/emulated/0/Android/data/org.mozilla.fenix/files/test_root/org.mozilla.fenix-geckodriver-profile",
    "/sdcard/Android/data/org.mozilla.fenix/files/test_root/org.mozilla.fenix-geckodriver-profile",
    "/data/local/tmp/org.mozilla.fenix-geckodriver-profile",
])
def test_restart_retains_the_driver_reported_native_profile(lifecycle, adb_program, reported):
    invocations = adb_program("raise SystemExit(0)\n")
    browser = browser_without_start(lifecycle, SimpleNamespace(capabilities={"moz:profile": reported}))
    assert browser.locate_native_profile() == reported
    assert browser.native_profile == reported
    assert len(invocations.read_text().splitlines()) == 2


@pytest.mark.parametrize("reported", [None, "", "/tmp/desktop-profile", 1])
def test_restart_does_not_change_guest_state_for_an_unproven_profile(lifecycle, adb_program, reported):
    invocations = adb_program("raise SystemExit(0)\n")
    browser = browser_without_start(lifecycle, SimpleNamespace(capabilities={"moz:profile": reported}))
    with pytest.raises(AssertionError, match="actual native Android profile"):
        browser.locate_native_profile()
    assert not hasattr(browser, "native_profile") and not invocations.exists()


def test_missing_native_profile_is_not_replaced_by_a_search_or_reseed(lifecycle, adb_program):
    invocations = adb_program("raise SystemExit(1)\n")
    browser = browser_without_start(lifecycle, SimpleNamespace(capabilities={"moz:profile": "/storage/emulated/0/missing"}))
    with pytest.raises(AssertionError, match="does not exist"):
        browser.locate_native_profile()
    assert not hasattr(browser, "native_profile")
    assert len(invocations.read_text().splitlines()) == 1


def test_real_selenium_reports_native_driver_startup_failure(lifecycle, tmp_path, monkeypatch):
    executable = tmp_path / "geckodriver"
    executable.write_text("#!" + sys.executable + "\nraise SystemExit(23)\n")
    executable.chmod(0o700)
    monkeypatch.setenv("SE_GECKODRIVER", str(executable))
    profile = tmp_path / "fresh-profile"
    profile.mkdir()
    browser = object.__new__(lifecycle.AndroidBrowser)
    browser.profile, browser.signed, browser.driver = profile, False, None
    with pytest.raises(WebDriverException, match="23"):
        browser.start()
    assert records(lifecycle)[0]["operation"] == "webdriver-start"


def test_cleanup_kills_and_reaps_a_stuck_owned_native_process(lifecycle):
    process = subprocess.Popen([
        sys.executable, "-c",
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(60)",
    ], stdout=subprocess.PIPE, text=True)
    lifecycle.PROCESSES.append(process)
    assert select.select([process.stdout], [], [], 3)[0]
    assert process.stdout.readline().strip() == "ready"
    started = time.monotonic()
    lifecycle.cleanup()
    assert time.monotonic() - started < 3
    assert process.returncode == -signal.SIGKILL and lifecycle.PROCESSES == []
    process.stdout.close()


def test_cleanup_accepts_an_already_exited_owned_process(lifecycle):
    process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    assert process.wait(timeout=3) == 0
    lifecycle.PROCESSES.append(process)
    lifecycle.cleanup()
    assert process.returncode == 0 and lifecycle.PROCESSES == []


def test_missing_diagnostic_directory_does_not_replace_original_error(lifecycle, tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle.runner, "ARTIFACTS", tmp_path / "missing-directory")
    error = RuntimeError("original failure")
    lifecycle.lifecycle_failure("test", error)
    assert "FileNotFoundError" in " ".join(error.__notes__)
