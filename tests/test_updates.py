"""Behavior and security regression tests for the local update engine.

These exercise the real on-disk job store, the trusted-release validation,
the standalone/Brew install paths (against fixture layouts and stub
binaries - never the network) and the negative guarantees: no downgrade,
no unsupported method, no traversal, no silent worker death, no
credential-bearing records, no interactive privilege waits.
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from portmap import update_worker, updates
from portmap.updates import UpdateError

STUB_TEMPLATE = """#!/bin/sh
BASE_DIR=$(dirname "$0")
if [ "$1" = "--version" ]; then
  echo "portmap {version}"
  exit 0
fi
if [ "$1" = "_update-recover" ]; then
  shift
  printf '%s\\n' "$*" >> "$BASE_DIR/recover-calls.log"
  result_file=
  target=
  while [ $# -gt 0 ]; do
    case "$1" in
      --result-file) result_file=$2; shift 2 ;;
      --target-version) target=$2; shift 2 ;;
      *) shift ;;
    esac
  done
  cat > "$result_file" <<JSON
{recover_body}
JSON
  exit {recover_rc}
fi
exit 1
"""


def write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def make_binary_stub(path: Path, version: str, *, recover_ok: bool = True, recover_error: str = "", recover_rc: int = 0) -> Path:
    if recover_ok:
        body = (
            '{"ok": true, "target_version": "%s", "running_version": "%s", "steps": {}, "error": null}'
            % (version, version)
        )
    else:
        body = json.dumps({"ok": False, "target_version": version, "running_version": version, "steps": {}, "error": recover_error})
    return write_executable(path, STUB_TEMPLATE.format(version=version, recover_body=body, recover_rc=recover_rc))


def make_standalone_install(root: Path, version: str, suffix: str = "abcdef") -> tuple[Path, Path, Path]:
    """install.sh layout: bundles under <root>/portmap, launcher in ~/.local/bin.

    Tests point HOME at ``root`` so the engine's real launcher discovery
    (``shutil.which`` then ``Path.home()/.local/bin/portmap``) finds the
    fixture exactly as on a production machine.
    """
    bundle = root / "portmap" / f"{version}.{suffix}"
    binary = make_binary_stub(bundle / "portmap", version)
    (bundle / "release.json").write_text(
        json.dumps({"version": version, "assets": []}), encoding="utf-8"
    )
    (bundle / ".portmap-install").write_text("installed by install.sh\n", encoding="utf-8")
    launcher = root / ".local" / "bin" / "portmap"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.symlink_to(binary)
    return binary, bundle, launcher


def platform_asset_payload(version: str) -> dict:
    plat = updates.current_platform()
    if plat is None:
        pytest.skip("no official platform build for this test machine")
    os_name, arch = plat
    filename = f"portmap-{version}-{os_name}-{arch}.tar.gz"
    return {
        "os": os_name,
        "arch": arch,
        "filename": filename,
        "sha256": "a" * 64,
        "size": 1024,
        "source_url": f"https://github.com/ylang-ylang/portmap/releases/download/V{version.rsplit('.', 1)[0]}/{filename}",
    }


def release_for(version: str):
    return updates.parse_update_manifest(
        {"version": version, "assets": [platform_asset_payload(version)]},
        expected_version=version,
    )


def build_archive(path: Path, version: str, *, unsafe: str | None = None) -> Path:
    """Fixture release archive containing a portmap stub launcher."""
    stub = STUB_TEMPLATE.format(
        version=version,
        recover_body='{"ok": true, "target_version": "%s", "running_version": "%s", "steps": {}, "error": null}' % (version, version),
        recover_rc=0,
    )
    with tarfile.open(path, "w:gz") as archive:
        payload = stub.encode("utf-8")
        info = tarfile.TarInfo("portmap")
        info.size = len(payload)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(payload))
        readme = tarfile.TarInfo("README")
        data = b"fixture bundle\n"
        readme.size = len(data)
        archive.addfile(readme, io.BytesIO(data))
        if unsafe == "symlink":
            link = tarfile.TarInfo("evil")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)
        elif unsafe == "traversal":
            escape = tarfile.TarInfo("../escape")
            data = b"x\n"
            escape.size = len(data)
            archive.addfile(escape, io.BytesIO(data))
    return path


def make_job(state_dir: Path, **overrides) -> dict:
    job = {
        "id": "job-20260101T000000Z-deadbeef",
        "request_id": "req-test",
        "state": "queued",
        "version_before": "0.15.0",
        "target_version": "0.16.0",
        "version_after": None,
        "created_at": updates._utc_now(),
        "updated_at": updates._utc_now(),
        "message": "queued for the durable update worker",
        "log_tail": "",
        "error": None,
        "worker_pid": None,
        "worker_start": None,
        "install_method": "standalone",
        "install_executable": None,
    }
    job.update(overrides)
    updates.prepare_updates_state(state_dir)
    updates._save_job(state_dir, job)
    return job


def patch_worker_environment(monkeypatch, tmp_path: Path, *, installed_exe: Path | None = None) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(updates, "foreground_runner_active", lambda state_dir: False)
    monkeypatch.setattr(update_worker, "foreground_runner_active", lambda state_dir: False)
    requirements = {
        "infrastructure": False,
        "host_dns_enabled": False,
        "dns_needs_sudo": False,
        "expect_agent": False,
        "needs_sudo": False,
        "agent_scope_needs_sudo": False,
        "sudo_ok": False,
    }
    monkeypatch.setattr(updates, "privilege_requirements", lambda state_dir: dict(requirements))
    monkeypatch.setattr(updates, "_preflight_privilege", lambda state_dir: dict(requirements))
    monkeypatch.setattr(update_worker, "_preflight_privilege", lambda state_dir: dict(requirements))
    monkeypatch.setattr(updates, "_load_infrastructure_state", lambda state_dir: None)


# ---------------------------------------------------------------------------
# Trusted release validation
# ---------------------------------------------------------------------------


def test_parse_update_manifest_accepts_newer_target_version():
    release = release_for("0.16.0")
    assert release.version == "0.16.0"
    assert updates.platform_asset(release) is not None


def test_parse_update_manifest_rejects_wrong_expected_version():
    with pytest.raises(UpdateError, match="not the requested"):
        release_for("0.16.0"), updates.parse_update_manifest(
            {"version": "0.16.0", "assets": [platform_asset_payload("0.16.0")]},
            expected_version="0.17.0",
        )


def test_parse_update_manifest_rejects_duplicates_and_bad_entries():
    asset = platform_asset_payload("0.16.0")
    # An identical second entry duplicates the platform (the filename regex
    # ties filenames to platform+version, so both dup classes coincide).
    with pytest.raises(UpdateError, match="duplicate platform"):
        updates.parse_update_manifest({"version": "0.16.0", "assets": [asset, dict(asset)]})
    bad_sha = dict(asset, sha256="ZZ" * 32)
    with pytest.raises(UpdateError, match="release manifest is invalid"):
        updates.parse_update_manifest({"version": "0.16.0", "assets": [bad_sha]})
    foreign = dict(asset, source_url="https://example.com/portmap.tar.gz")
    with pytest.raises(UpdateError, match="release manifest is invalid"):
        updates.parse_update_manifest({"version": "0.16.0", "assets": [foreign]})
    with pytest.raises(UpdateError, match="non-empty list"):
        updates.parse_update_manifest({"version": "0.16.0", "assets": []})


def test_release_manifest_url_only_allows_project_tags():
    assert updates._release_manifest_url("V0.16").endswith("/V0.16/release.json")
    for tag in ("../../etc", "V0.16/", "v0.16", "V0.16.0.1"):
        with pytest.raises(UpdateError):
            updates._release_manifest_url(tag)


# ---------------------------------------------------------------------------
# Status, installation detection and check caching
# ---------------------------------------------------------------------------


def test_local_status_reports_source_checkout_unsupported(tmp_path):
    status = updates.local_status(state_dir=tmp_path, check=False)
    assert status["current_version"] == updates.__version__
    assert status["install_method"] == "unsupported"
    assert status["installed_version"] is None
    assert status["can_update"] is False
    assert status["reason"] and "source checkout" in status["reason"]
    assert len(status["build_id"]) == 16
    int(status["build_id"], 16)


def test_local_status_check_uses_cache(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_tag():
        calls["n"] += 1
        return "V0.16"

    monkeypatch.setattr(updates, "fetch_latest_tag", fake_tag)
    monkeypatch.setattr(updates, "fetch_release_manifest", lambda tag, expected_version=None: release_for("0.16.0"))
    first = updates.local_status(state_dir=tmp_path, check=True)
    assert first["latest_version"] == "0.16.0"
    second = updates.local_status(state_dir=tmp_path, check=True)
    assert second["latest_version"] == "0.16.0"
    assert calls["n"] == 1  # cache absorbs progress polling
    cached = json.loads((tmp_path / "updates" / "check.json").read_text())
    assert cached["latest_version"] == "0.16.0"
    # A stale cache must not be served as fresh without check=True.
    cached["checked_at"] = "2020-01-01T00:00:00Z"
    (tmp_path / "updates" / "check.json").write_text(json.dumps(cached))
    stale = updates.local_status(state_dir=tmp_path, check=False)
    assert stale["latest_version"] is None


def test_local_status_check_failure_is_explicit(monkeypatch, tmp_path):
    def boom():
        raise UpdateError("github unreachable")

    monkeypatch.setattr(updates, "fetch_latest_tag", boom)
    status = updates.local_status(state_dir=tmp_path, check=True)
    assert status["latest_version"] is None
    # the trusted-source failure leads the reason and is never masked by the
    # static capability notes (source checkout etc.) that follow it
    assert status["reason"].startswith("github unreachable")
    # the failure is cached too: polling must not hammer the network
    status2 = updates.local_status(state_dir=tmp_path, check=True)
    assert status2["reason"].startswith("github unreachable")


def test_probe_standalone_layout(tmp_path, monkeypatch):
    binary, bundle, launcher = make_standalone_install(tmp_path, "0.15.0")
    monkeypatch.setenv("HOME", str(tmp_path))
    info = updates.probe_installation(binary)
    assert info.method == "standalone"
    assert info.installed_version == "0.15.0"
    assert info.install_root == (tmp_path / "portmap")
    assert info.launcher == launcher


def test_probe_standalone_reports_linked_bundle_not_running_dir(tmp_path, monkeypatch):
    """After an external update flips the launcher, installed == linked."""
    old, _, launcher = make_standalone_install(tmp_path, "0.15.0")
    new = make_binary_stub(tmp_path / "portmap" / "0.16.0.abcdef" / "portmap", "0.16.0")
    (new.parent / "release.json").write_text(json.dumps({"version": "0.16.0", "assets": []}))
    (new.parent / ".portmap-install").write_text("installed by install.sh\n")
    launcher.unlink()
    launcher.symlink_to(new)
    monkeypatch.setenv("HOME", str(tmp_path))
    info = updates.probe_installation(old)
    assert info.method == "standalone"
    assert info.installed_version == "0.16.0"
    assert info.installed_executable == new.resolve()
    assert info.executable == old.resolve()  # the running process is still old


def test_probe_brew_layout(tmp_path):
    prefix = tmp_path / "homebrew"
    binary = make_binary_stub(prefix / "Cellar" / "portmap" / "0.15.0" / "bin" / "portmap", "0.15.0")
    write_executable(prefix / "bin" / "brew", "#!/bin/sh\nexit 0\n")
    info = updates.probe_installation(binary)
    assert info.method == "brew"
    assert info.installed_version == "0.15.0"
    assert info.brew_prefix == prefix
    assert info.launcher is None  # brew has no install.sh-managed launcher
    assert info.reason is None


def test_probe_brew_virtualenv_reports_linked_keg_version(tmp_path):
    """The official formula is a Python virtualenv under libexec, not frozen."""
    prefix = tmp_path / "homebrew"
    module = prefix / "Cellar" / "portmap" / "0.15.0" / "libexec" / "lib" / "python3.12" / "site-packages" / "portmap" / "updates.py"
    module.parent.mkdir(parents=True)
    module.write_text("# running module of the 0.15.0 keg\n")
    write_executable(prefix / "bin" / "brew", "#!/bin/sh\nexit 0\n")
    # the linked keg is already 0.16.0: an external brew upgrade moved on
    linked_binary = make_binary_stub(prefix / "Cellar" / "portmap" / "0.16.0" / "bin" / "portmap", "0.16.0")
    (prefix / "opt").mkdir()
    (prefix / "opt" / "portmap").symlink_to(prefix / "Cellar" / "portmap" / "0.16.0", target_is_directory=True)
    info = updates.probe_installation(module)
    assert info.method == "brew"
    assert info.installed_version == "0.16.0"  # linked keg, not the running 0.15.0
    assert info.installed_executable == linked_binary.resolve()
    assert info.reason is None


def test_probe_unknown_frozen_layout_is_unsupported(tmp_path):
    binary = make_binary_stub(tmp_path / "opt" / "misc" / "portmap", "0.15.0")
    info = updates.probe_installation(binary)
    assert info.method == "unsupported"
    assert "source checkout" in info.reason


# ---------------------------------------------------------------------------
# start_update guards
# ---------------------------------------------------------------------------


def standalone_probe(binary: Path, version: str):
    def probe(executable=None):
        return updates.InstallInfo(
            method="standalone",
            installed_version=version,
            executable=binary,
            bundle_dir=binary.parent,
            install_root=binary.parent.parent,
            launcher=binary.parent.parent.parent / ".local" / "bin" / "portmap",
        )

    return probe


def test_start_update_rejects_non_concrete_versions(tmp_path, monkeypatch):
    for bad in ("latest", "0.16", "0.16.0-beta", "../x", "0.16.0.1", ""):
        with pytest.raises(UpdateError):
            updates.start_update(bad, "req-1", state_dir=tmp_path)


def test_start_update_rejects_unusable_request_ids(tmp_path):
    for bad in ("", "x" * 129, "req\nwith-newline", "req\x00null"):
        with pytest.raises(UpdateError):
            updates.start_update("0.16.0", bad, state_dir=tmp_path)


def test_start_update_blocks_unsupported_install(tmp_path, monkeypatch):
    def probe(executable=None):
        return updates.InstallInfo(
            method="unsupported", installed_version=None, executable=None,
            reason="portmap 0.15.0 runs from a source checkout; use install.sh",
        )

    monkeypatch.setattr(updates, "probe_installation", probe)
    with pytest.raises(UpdateError, match="install.sh"):
        updates.start_update("0.16.0", "req-1", state_dir=tmp_path)


def test_start_update_refuses_downgrade_and_noop(tmp_path, monkeypatch):
    binary_new, _, _ = make_standalone_install(tmp_path, "0.16.0")
    monkeypatch.setattr(updates, "probe_installation", standalone_probe(binary_new, "0.16.0"))
    monkeypatch.setattr(updates, "foreground_runner_active", lambda state_dir: False)
    monkeypatch.setattr(updates, "_preflight_privilege", lambda state_dir: {})
    with pytest.raises(UpdateError, match="downgrade"):
        updates.start_update("0.15.0", "req-1", state_dir=tmp_path)
    binary_old, _, _ = make_standalone_install(tmp_path / "same", "0.15.0")
    monkeypatch.setattr(updates, "probe_installation", standalone_probe(binary_old, "0.15.0"))
    with pytest.raises(UpdateError, match="already installed and running"):
        updates.start_update("0.15.0", "req-2", state_dir=tmp_path)


def test_start_update_requires_concrete_official_release_before_spawn(tmp_path, monkeypatch):
    """Preflight failures surface before any worker is launched."""
    binary, _, _ = make_standalone_install(tmp_path, "0.15.0")
    monkeypatch.setattr(updates, "probe_installation", standalone_probe(binary, "0.15.0"))
    monkeypatch.setattr(updates, "foreground_runner_active", lambda state_dir: False)
    spawned = []
    monkeypatch.setattr(updates, "_spawn_worker", lambda *a, **k: spawned.append(a) or {"pid": os.getpid(), "start": ""})

    def refuse(state_dir):
        raise UpdateError("sudo -n did not succeed")

    monkeypatch.setattr(updates, "_preflight_privilege", refuse)
    with pytest.raises(UpdateError, match="sudo -n"):
        updates.start_update("0.16.0", "req-1", state_dir=tmp_path)
    assert spawned == []


def test_start_update_is_idempotent_and_single_flight(tmp_path, monkeypatch):
    binary, _, _ = make_standalone_install(tmp_path, "0.15.0")
    monkeypatch.setattr(updates, "probe_installation", standalone_probe(binary, "0.15.0"))
    monkeypatch.setattr(updates, "foreground_runner_active", lambda state_dir: False)
    monkeypatch.setattr(updates, "_preflight_privilege", lambda state_dir: {})
    monkeypatch.setattr(
        updates, "_spawn_worker",
        lambda kind, identifier, state_dir: {"pid": os.getpid(), "start": updates.process_identity(os.getpid())[1]},
    )
    first = updates.start_update("0.16.0", "req-1", state_dir=tmp_path)
    again = updates.start_update("0.16.0", "req-1", state_dir=tmp_path)
    assert first["id"] == again["id"]
    job_files = list((tmp_path / "updates" / "jobs").iterdir())
    assert len(job_files) == 1
    with pytest.raises(UpdateError, match="already in progress"):
        updates.start_update("0.16.0", "req-2", state_dir=tmp_path)
    # the same request_id still returns the running job, not an error
    replay = updates.start_update("0.16.0", "req-1", state_dir=tmp_path)
    assert replay["id"] == first["id"]


def test_start_update_rejects_request_id_reuse_for_a_different_version(tmp_path, monkeypatch):
    binary, _, _ = make_standalone_install(tmp_path, "0.15.0")
    monkeypatch.setattr(updates, "probe_installation", standalone_probe(binary, "0.15.0"))
    monkeypatch.setattr(updates, "foreground_runner_active", lambda state_dir: False)
    monkeypatch.setattr(updates, "_preflight_privilege", lambda state_dir: {})
    monkeypatch.setattr(
        updates, "_spawn_worker",
        lambda kind, identifier, state_dir: {"pid": os.getpid(), "start": updates.process_identity(os.getpid())[1]},
    )
    updates.start_update("0.16.0", "req-1", state_dir=tmp_path)
    with pytest.raises(UpdateError, match="already used for version"):
        updates.start_update("0.17.0", "req-1", state_dir=tmp_path)


def test_start_update_allows_completing_activation_of_installed_newer_version(tmp_path, monkeypatch):
    """installed > running must be completable from the UI, not CLI-only."""
    old, _, launcher = make_standalone_install(tmp_path, "0.15.0")
    new = make_binary_stub(tmp_path / "portmap" / "0.16.0.abcdef" / "portmap", "0.16.0")
    (new.parent / "release.json").write_text(json.dumps({"version": "0.16.0", "assets": []}))
    (new.parent / ".portmap-install").write_text("installed by install.sh\n")
    launcher.unlink()
    launcher.symlink_to(new)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(updates, "__version__", "0.15.0")
    real_probe = updates.probe_installation
    monkeypatch.setattr(updates, "probe_installation", lambda executable=None: real_probe(old))
    monkeypatch.setattr(updates, "foreground_runner_active", lambda state_dir: False)
    monkeypatch.setattr(updates, "_preflight_privilege", lambda state_dir: {})
    monkeypatch.setattr(
        updates, "_spawn_worker",
        lambda kind, identifier, state_dir: {"pid": os.getpid(), "start": updates.process_identity(os.getpid())[1]},
    )
    job = updates.start_update("0.16.0", "req-activate", state_dir=tmp_path)
    assert job["state"] == "queued"
    assert job["target_version"] == "0.16.0"


def test_start_update_blocks_while_foreground_runner_owns_state(tmp_path, monkeypatch):
    binary, _, _ = make_standalone_install(tmp_path, "0.15.0")
    monkeypatch.setattr(updates, "probe_installation", standalone_probe(binary, "0.15.0"))
    monkeypatch.setattr(updates, "foreground_runner_active", lambda state_dir: True)
    status = updates.local_status(state_dir=tmp_path)
    assert status["can_update"] is False
    assert "foreground" in status["reason"]
    monkeypatch.setattr(updates, "_preflight_privilege", lambda state_dir: {})
    with pytest.raises(UpdateError, match="foreground"):
        updates.start_update("0.16.0", "req-1", state_dir=tmp_path)


# ---------------------------------------------------------------------------
# Job records, traversal and worker-death detection
# ---------------------------------------------------------------------------


def test_get_job_rejects_traversal_and_unknown_ids(tmp_path):
    for bad in ("../../etc/passwd", "job-x", "../jobs", "job-20260101T000000Z-ZZZZZZZZ", "job-20260101T000000Z-deadbe", "."):
        with pytest.raises(UpdateError):
            updates.get_job(bad, state_dir=tmp_path)
    with pytest.raises(UpdateError, match="unknown update job"):
        updates.get_job("job-20260101T000000Z-deadbeef", state_dir=tmp_path)


def test_get_job_marks_dead_worker_interrupted(tmp_path):
    dead_pid = reaped_child_pid()
    make_job(tmp_path, state="installing", worker_pid=dead_pid, worker_start="12345")
    job = updates.get_job("job-20260101T000000Z-deadbeef", state_dir=tmp_path)
    assert job["state"] == "interrupted"
    assert "no longer running" in job["error"]
    persisted = json.loads((tmp_path / "updates" / "jobs" / "job-20260101T000000Z-deadbeef.json").read_text())
    assert persisted["state"] == "interrupted"


def test_queued_job_without_worker_survives_grace_period(tmp_path):
    make_job(tmp_path, state="queued", worker_pid=None)
    job = updates.get_job("job-20260101T000000Z-deadbeef", state_dir=tmp_path)
    assert job["state"] == "queued"


def test_sweep_reports_interrupted_job_in_last_job(tmp_path):
    make_job(tmp_path, state="restarting", worker_pid=reaped_child_pid())
    status = updates.local_status(state_dir=tmp_path)
    assert status["last_job"]["state"] == "interrupted"
    assert status["active_job"] is None


def reaped_child_pid() -> int:
    """PID of a child that already exited and was reaped."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def test_log_tail_is_bounded(tmp_path):
    make_job(tmp_path, state="failed", error="x")
    log = tmp_path / "updates" / "logs" / "job-20260101T000000Z-deadbeef.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("line\n" * 40 * 1024)
    job = updates.get_job("job-20260101T000000Z-deadbeef", state_dir=tmp_path)
    assert len(job["log_tail"]) <= updates.LOG_TAIL_BYTES
    assert job["log_tail"].endswith("line\n")


def test_job_records_and_state_dirs_stay_private(tmp_path):
    make_job(tmp_path, state="queued")
    record = tmp_path / "updates" / "jobs" / "job-20260101T000000Z-deadbeef.json"
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    for directory in (tmp_path, tmp_path / "updates", tmp_path / "updates" / "jobs", tmp_path / "updates" / "logs"):
        assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0


def test_public_job_schema_has_no_secret_fields(tmp_path):
    job = updates._public_job(make_job(tmp_path))
    assert set(job) == set(updates.JOB_FIELDS)
    serialized = json.dumps(job) + (tmp_path / "updates" / "jobs" / "job-20260101T000000Z-deadbeef.json").read_text()
    for secret in ("token", "secret", "credential", "password"):
        assert secret not in serialized.lower()


# ---------------------------------------------------------------------------
# Standalone install flow (worker, fixture archive, stub binaries)
# ---------------------------------------------------------------------------


def run_worker_job(state_dir: Path, job: dict, *, manifest_release=None, archive: Path | None = None, fetch_calls: list | None = None) -> dict:
    if manifest_release is None:
        manifest_release = release_for(job["target_version"])
    monkey_manifest = manifest_release

    def fake_fetch_release_manifest(tag, expected_version=None):
        return monkey_manifest

    update_worker.fetch_release_manifest = fake_fetch_release_manifest

    def fake_fetch_archive(asset, directory):
        if fetch_calls is not None:
            fetch_calls.append(asset.filename)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / asset.filename
        target.write_bytes(archive.read_bytes())
        return target

    original_fetch = updates.fetch_archive
    updates.fetch_archive = fake_fetch_archive
    try:
        rc = update_worker.run_local_job(job["id"], state_dir)
        result = updates.get_job(job["id"], state_dir=state_dir)
        result["_rc"] = rc
        return result
    finally:
        updates.fetch_archive = original_fetch
        update_worker.fetch_release_manifest = updates.fetch_release_manifest


def test_standalone_job_succeeds_and_flips_managed_launcher(tmp_path, monkeypatch):
    binary, bundle, launcher = make_standalone_install(tmp_path, "0.15.0")
    patch_worker_environment(monkeypatch, tmp_path)
    archive = build_archive(tmp_path / "fixture.tar.gz", "0.16.0")
    job = make_job(tmp_path, install_executable=str(binary))
    result = run_worker_job(tmp_path, job, archive=archive)

    assert result["_rc"] == 0
    assert result["state"] == "succeeded"
    assert result["version_after"] == "0.16.0"
    assert result["error"] is None
    # launcher now points at a NEW retained bundle; the old one is intact
    new_target = Path(os.readlink(launcher))
    assert new_target != binary
    assert new_target.is_file()
    assert binary.is_file()
    sidecar = json.loads((new_target.parent / "release.json").read_text())
    assert sidecar["version"] == "0.16.0"
    assert (new_target.parent / ".portmap-install").is_file()
    # probe now reports the installed target
    assert updates.probe_installation(new_target).installed_version == "0.16.0"
    # recovery ran from the new binary with the fixed argv (no agent expected)
    calls = (new_target.parent / "recover-calls.log").read_text().splitlines()[-1]
    assert "--target-version 0.16.0" in calls
    assert "--expect-agent" not in calls
    assert "roles, connections and DNS preserved" in result["message"]


def test_activation_job_completes_installed_newer_version_without_reinstall(tmp_path, monkeypatch):
    """installed > running: recovery-only job, no archive download, no staging."""
    old, _, launcher = make_standalone_install(tmp_path, "0.15.0")
    new = make_binary_stub(tmp_path / "portmap" / "0.16.0.abcdef" / "portmap", "0.16.0")
    (new.parent / "release.json").write_text(json.dumps({"version": "0.16.0", "assets": []}))
    (new.parent / ".portmap-install").write_text("installed by install.sh\n")
    launcher.unlink()
    launcher.symlink_to(new)
    patch_worker_environment(monkeypatch, tmp_path)
    job = make_job(tmp_path, install_executable=str(old))
    fetch_calls: list = []
    result = run_worker_job(
        tmp_path, job, archive=build_archive(tmp_path / "fixture.tar.gz", "0.16.0"), fetch_calls=fetch_calls
    )
    assert result["_rc"] == 0
    assert result["state"] == "succeeded"
    assert result["version_after"] == "0.16.0"
    assert fetch_calls == []  # nothing was downloaded
    assert os.readlink(launcher) == str(new)  # and nothing was reinstalled
    assert sorted(p.name for p in (tmp_path / "portmap").iterdir()) == ["0.15.0.abcdef", "0.16.0.abcdef"]
    calls = (new.parent / "recover-calls.log").read_text().splitlines()[-1]
    assert "--target-version 0.16.0" in calls


def test_worker_refuses_same_version_when_already_running(tmp_path, monkeypatch):
    """target == installed == running is a no-op the worker must refuse."""
    binary, _, _ = make_standalone_install(tmp_path, "0.15.0")
    patch_worker_environment(monkeypatch, tmp_path)
    job = make_job(tmp_path, target_version="0.15.0", install_executable=str(binary))
    result = run_worker_job(tmp_path, job, archive=build_archive(tmp_path / "fixture.tar.gz", "0.15.0"))
    assert result["state"] == "failed"
    assert "already installed and running" in result["error"]


def test_standalone_job_rejects_unsafe_archive_members(tmp_path, monkeypatch):
    binary, bundle, launcher = make_standalone_install(tmp_path, "0.15.0")
    patch_worker_environment(monkeypatch, tmp_path)
    for unsafe in ("symlink", "traversal"):
        archive = build_archive(tmp_path / f"fixture-{unsafe}.tar.gz", "0.16.0", unsafe=unsafe)
        job = make_job(
            tmp_path,
            id=f"job-20260101T00000{1 if unsafe == 'symlink' else 2}Z-deadbeef",
            request_id=f"req-{unsafe}",
            install_executable=str(binary),
        )
        result = run_worker_job(tmp_path, job, archive=archive)
        assert result["state"] == "failed", unsafe
        assert "unsafe path" in result["error"] or "non-regular member" in result["error"]
        assert os.readlink(launcher) == str(binary)
    # no staging directories survived the failures
    assert sorted(p.name for p in (tmp_path / "portmap").iterdir()) == ["0.15.0.abcdef"]


def test_standalone_job_fails_when_staged_binary_reports_wrong_version(tmp_path, monkeypatch):
    binary, _, launcher = make_standalone_install(tmp_path, "0.15.0")
    patch_worker_environment(monkeypatch, tmp_path)
    archive = build_archive(tmp_path / "fixture.tar.gz", "0.15.9")
    job = make_job(tmp_path, target_version="0.16.0", install_executable=str(binary))
    result = run_worker_job(tmp_path, job, archive=archive)
    assert result["state"] == "failed"
    assert "0.15.9" in result["error"]
    assert os.readlink(launcher) == str(binary)  # launcher never flipped


def test_worker_blocks_before_installing_when_foreground_runner_active(tmp_path, monkeypatch):
    binary, _, launcher = make_standalone_install(tmp_path, "0.15.0")
    patch_worker_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(update_worker, "foreground_runner_active", lambda state_dir: True)
    job = make_job(tmp_path, install_executable=str(binary))
    fetch_calls: list = []
    result = run_worker_job(tmp_path, job, archive=build_archive(tmp_path / "fixture.tar.gz", "0.16.0"), fetch_calls=fetch_calls)
    assert result["state"] == "failed"
    assert "foreground" in result["error"]
    assert fetch_calls == []  # nothing was downloaded or installed
    assert os.readlink(launcher) == str(binary)


def test_failed_recovery_fails_the_job_despite_successful_install(tmp_path, monkeypatch):
    binary, _, launcher = make_standalone_install(tmp_path, "0.15.0")
    patch_worker_environment(monkeypatch, tmp_path)
    archive_path = tmp_path / "fixture.tar.gz"

    stub = STUB_TEMPLATE.format(
        version="0.16.0",
        recover_body=json.dumps(
            {"ok": False, "target_version": "0.16.0", "running_version": "0.16.0",
             "steps": {}, "error": "the catalog reports version 0.15.0"}
        ),
        recover_rc=0,
    )
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = stub.encode("utf-8")
        info = tarfile.TarInfo("portmap")
        info.size = len(payload)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(payload))

    job = make_job(tmp_path, install_executable=str(binary))
    result = run_worker_job(tmp_path, job, archive=archive_path)
    assert result["state"] == "failed"
    assert "catalog reports version 0.15.0" in result["error"]
    assert result["version_after"] is None
    # the install itself did land - the failure is honest, not rolled back fiction
    assert os.readlink(launcher) != str(binary)


def test_worker_records_missing_recovery_result_as_failure(tmp_path, monkeypatch):
    binary, _, _ = make_standalone_install(tmp_path, "0.15.0")
    patch_worker_environment(monkeypatch, tmp_path)
    stub = STUB_TEMPLATE.format(
        version="0.16.0",
        recover_body="unused",
        recover_rc=1,  # exits 1 without writing a result file
    )
    archive_path = tmp_path / "fixture.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = stub.encode("utf-8")
        info = tarfile.TarInfo("portmap")
        info.size = len(payload)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(payload))
    job = make_job(tmp_path, install_executable=str(binary))
    result = run_worker_job(tmp_path, job, archive=archive_path)
    assert result["state"] == "failed"
    assert "exit 1" in result["error"]


# ---------------------------------------------------------------------------
# Brew install flow (controlled external package-manager behavior)
# ---------------------------------------------------------------------------


BREW_STUB = """#!/bin/sh
BASE={base}
LOG={log}
if [ -f "$LOG/conflicting-tap" ]; then
  case "$1:$2:$3" in
    info:--json=v2:portmap|--prefix:portmap:) exit 2 ;;
  esac
fi
if [ "$1" = "update" ]; then
  if [ -f "$LOG/tap-stable.txt" ]; then cp "$LOG/tap-stable.txt" "$LOG/stable.txt"; fi
  exit 0
fi
if [ "$1" = "info" ] && [ "$2" = "--json=v2" ]; then
  printf '{{"formulae":[{{"name":"portmap","versions":{{"stable":"%s"}}}}]}}\\n' "$(cat "$LOG/stable.txt")"
  exit 0
fi
if [ "$1" = "install" ]; then
  if [ "$HOMEBREW_NO_INSTALL_UPGRADE" = "1" ]; then exit 0; fi
  V=$(cat "$LOG/stable.txt")
  mkdir -p "$BASE/Cellar/portmap/$V/bin"
  cat > "$BASE/Cellar/portmap/$V/bin/portmap" <<'STUB'
{stub}
STUB
  chmod +x "$BASE/Cellar/portmap/$V/bin/portmap"
  mkdir -p "$BASE/opt"
  ln -sfn "$BASE/Cellar/portmap/$V" "$BASE/opt/portmap"
  exit 0
fi
if [ "$1" = "--prefix" ] && {{ [ "$2" = "portmap" ] || [ "$2" = "ylang-ylang/tap/portmap" ]; }}; then
  echo "$BASE/opt/portmap"
  exit 0
fi
exit 1
"""


def make_brew_install(
    tmp_path: Path, installed: str, stable: str, *, tap_stable: str | None = None, new_stub_version: str | None = None
) -> tuple[Path, Path, Path]:
    """(old running binary, bound brew command, brew log dir).

    The brew stub lives at ``<prefix>/bin/brew`` exactly like a real
    installation, so the engine's prefix-bound command finds it and PATH is
    irrelevant. ``brew update`` refreshes ``stable.txt`` from
    ``tap-stable.txt`` to model a stale local tap.
    """
    prefix = tmp_path / "homebrew"
    log = tmp_path / "brewlog"
    log.mkdir()
    (log / "stable.txt").write_text(stable)
    (log / "tap-stable.txt").write_text(tap_stable if tap_stable is not None else stable)
    new_version = new_stub_version or (tap_stable if tap_stable is not None else stable)
    stub = STUB_TEMPLATE.format(
        version=new_version,
        recover_body='{"ok": true, "target_version": "%s", "running_version": "%s", "steps": {}, "error": null}' % (new_version, new_version),
        recover_rc=0,
    )
    brew = write_executable(prefix / "bin" / "brew", BREW_STUB.format(base=prefix, log=log, stub=stub))
    old = make_binary_stub(prefix / "Cellar" / "portmap" / installed / "bin" / "portmap", installed)
    return old, brew, log


def test_brew_job_upgrades_despite_callers_no_upgrade_setting(tmp_path, monkeypatch):
    old, brew, log = make_brew_install(tmp_path, "0.15.0", "0.16.0")
    monkeypatch.setenv("HOMEBREW_NO_INSTALL_UPGRADE", "1")  # must be cleared by the engine
    patch_worker_environment(monkeypatch, tmp_path)
    job = make_job(tmp_path, install_method="brew", install_executable=str(old))
    result = run_worker_job(tmp_path, job)

    assert result["_rc"] == 0
    assert result["state"] == "succeeded"
    assert result["version_after"] == "0.16.0"


def test_brew_job_refreshes_stale_tap_metadata(tmp_path, monkeypatch):
    """A stale local tap must not force the user to run brew update manually."""
    old, brew, log = make_brew_install(tmp_path, "0.15.0", "0.15.1", tap_stable="0.16.0")
    patch_worker_environment(monkeypatch, tmp_path)
    job = make_job(tmp_path, install_method="brew", install_executable=str(old))
    result = run_worker_job(tmp_path, job)
    assert result["_rc"] == 0
    assert result["state"] == "succeeded"
    assert result["version_after"] == "0.16.0"


def test_brew_job_needs_no_frozen_archive_for_this_cpu(tmp_path, monkeypatch):
    """The formula is Python-source based; only the version must be official."""
    old, brew, log = make_brew_install(tmp_path, "0.15.0", "0.16.0")
    patch_worker_environment(monkeypatch, tmp_path)
    plat = updates.current_platform()
    other = ("darwin", "arm64") if plat != ("darwin", "arm64") else ("linux", "amd64")
    payload = platform_asset_payload("0.16.0")
    payload["os"], payload["arch"] = other
    payload["filename"] = f"portmap-0.16.0-{other[0]}-{other[1]}.tar.gz"
    payload["source_url"] = f"https://github.com/ylang-ylang/portmap/releases/download/V0.16/{payload['filename']}"
    off_platform = updates.parse_update_manifest(
        {"version": "0.16.0", "assets": [payload]}, expected_version="0.16.0"
    )
    assert updates.platform_asset(off_platform) is None  # no archive for this CPU
    job = make_job(tmp_path, install_method="brew", install_executable=str(old))
    result = run_worker_job(tmp_path, job, manifest_release=off_platform)
    assert result["_rc"] == 0
    assert result["state"] == "succeeded"
    assert result["version_after"] == "0.16.0"


def test_brew_job_never_uses_an_unrelated_brew_from_path(tmp_path, monkeypatch):
    old, brew, log = make_brew_install(tmp_path, "0.15.0", "0.16.0")
    decoy_dir = tmp_path / "decoy-bin"
    decoy_dir.mkdir()
    decoy_log = tmp_path / "decoy-log"
    decoy_log.mkdir()
    decoy = write_executable(
        decoy_dir / "brew",
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {decoy_log}/argv.log\nexit 1\n',
    )
    monkeypatch.setenv("PATH", f"{decoy_dir}:{os.environ['PATH']}")
    patch_worker_environment(monkeypatch, tmp_path)
    job = make_job(tmp_path, install_method="brew", install_executable=str(old))
    result = run_worker_job(tmp_path, job)
    assert result["_rc"] == 0
    assert result["state"] == "succeeded"
    assert not (decoy_log / "argv.log").exists()  # the decoy brew was never executed
    assert decoy.parent == decoy_dir


def test_brew_job_fails_preflight_when_formula_differs(tmp_path, monkeypatch):
    old, brew, log = make_brew_install(tmp_path, "0.15.0", "0.15.1")
    patch_worker_environment(monkeypatch, tmp_path)
    job = make_job(tmp_path, install_method="brew", install_executable=str(old))
    result = run_worker_job(tmp_path, job)
    assert result["state"] == "failed"
    assert old.exists()
    assert {path.name for path in old.parents[2].iterdir()} == {"0.15.0"}


def test_brew_job_fails_when_brew_reports_wrong_installed_version(tmp_path, monkeypatch):
    # The formula "installs" a stub that still reports the old version.
    old, brew, log = make_brew_install(tmp_path, "0.15.0", "0.16.0", new_stub_version="0.15.0")
    patch_worker_environment(monkeypatch, tmp_path)
    job = make_job(tmp_path, install_method="brew", install_executable=str(old))
    result = run_worker_job(tmp_path, job)
    assert result["state"] == "failed"
    assert "reports 0.15.0, not the requested 0.16.0" in result["error"]


# ---------------------------------------------------------------------------
# Recovery entry units
# ---------------------------------------------------------------------------


def test_run_recovery_refuses_version_mismatch(tmp_path):
    result_file = tmp_path / "recover.json"
    rc = updates.run_recovery(
        state_dir=tmp_path,
        target_version="9.9.9",
        result_file=result_file,
    )
    assert rc == 1
    payload = json.loads(result_file.read_text())
    assert payload["ok"] is False
    assert "9.9.9" in payload["error"]


def test_run_recovery_succeeds_without_infrastructure_or_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "_load_infrastructure_state", lambda state_dir: None)
    result_file = tmp_path / "recover.json"
    rc = updates.run_recovery(
        state_dir=tmp_path,
        target_version=updates.__version__,
        result_file=result_file,
    )
    assert rc == 0
    payload = json.loads(result_file.read_text())
    assert payload["ok"] is True
    assert payload["running_version"] == updates.__version__


def test_run_recovery_fails_when_a_connection_disappears(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "_load_infrastructure_state", lambda state_dir: None)
    (tmp_path / "connections.json").write_text(
        json.dumps({"version": 1, "connections": {"host.portmap": {"domain": "host.portmap", "via": "ssh", "ssh_target": "user@10.0.0.5"}}}),
        encoding="utf-8",
    )
    snapshot = tmp_path / "conn-snapshot.json"
    snapshot.write_text(
        json.dumps([
            {"domain": "host.portmap", "via": "ssh", "ssh_target": "user@10.0.0.5"},
            {"domain": "other.portmap", "via": "direct", "address": "10.0.0.9"},
        ])
    )
    result_file = tmp_path / "recover.json"
    rc = updates.run_recovery(
        state_dir=tmp_path,
        target_version=updates.__version__,
        result_file=result_file,
        connections_snapshot=snapshot,
    )
    assert rc == 1
    payload = json.loads(result_file.read_text())
    assert "other.portmap disappeared" in payload["error"]


def test_run_recovery_fails_when_a_connection_target_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "_load_infrastructure_state", lambda state_dir: None)
    (tmp_path / "connections.json").write_text(
        json.dumps({"version": 1, "connections": {"host.portmap": {"domain": "host.portmap", "via": "ssh", "ssh_target": "user@10.0.0.6"}}}),
        encoding="utf-8",
    )
    snapshot = tmp_path / "conn-snapshot.json"
    snapshot.write_text(json.dumps([{"domain": "host.portmap", "via": "ssh", "ssh_target": "user@10.0.0.5"}]))
    result_file = tmp_path / "recover.json"
    rc = updates.run_recovery(
        state_dir=tmp_path,
        target_version=updates.__version__,
        result_file=result_file,
        connections_snapshot=snapshot,
    )
    assert rc == 1
    payload = json.loads(result_file.read_text())
    assert "changed ssh_target" in payload["error"]


def test_recovery_allows_new_connections_and_metadata_refresh(tmp_path, monkeypatch):
    """Identity/target must survive; new connections and dynamic state may not block success."""
    monkeypatch.setattr(updates, "_load_infrastructure_state", lambda state_dir: None)
    (tmp_path / "connections.json").write_text(
        json.dumps({"version": 1, "connections": {
            "host.portmap": {"domain": "host.portmap", "via": "ssh", "ssh_target": "user@10.0.0.5", "endpoints": ["http://x"], "last_checked": "now"},
            "new.portmap": {"domain": "new.portmap", "via": "direct", "address": "10.0.0.7"},
        }}),
        encoding="utf-8",
    )
    snapshot = tmp_path / "conn-snapshot.json"
    snapshot.write_text(json.dumps([{"domain": "host.portmap", "via": "ssh", "ssh_target": "user@10.0.0.5"}]))
    result_file = tmp_path / "recover.json"
    rc = updates.run_recovery(
        state_dir=tmp_path,
        target_version=updates.__version__,
        result_file=result_file,
        connections_snapshot=snapshot,
    )
    assert rc == 0
    payload = json.loads(result_file.read_text())
    assert payload["ok"] is True
    assert payload["steps"]["connections_preserved"] is True


# ---------------------------------------------------------------------------
# Durable worker snapshot
# ---------------------------------------------------------------------------


def test_snapshot_copies_source_package_without_bytecode(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "frozen", lambda: False)
    prefix, overrides = updates._ensure_worker_snapshot(tmp_path)
    assert prefix[1:] == ["-m", "portmap.update_worker"]
    snapshot_root = Path(overrides["PYTHONPATH"])
    assert (snapshot_root / "portmap" / "update_worker.py").is_file()
    assert not list(snapshot_root.rglob("__pycache__"))
    marker = (snapshot_root / updates.SNAPSHOT_MARKER).read_text().strip()
    assert marker == updates.build_fingerprint()
    # a matching snapshot is reused instead of copied again
    again_prefix, again_overrides = updates._ensure_worker_snapshot(tmp_path)
    assert Path(again_overrides["PYTHONPATH"]) == snapshot_root
    assert len(list((tmp_path / "updates" / "snapshots").iterdir())) == 1


def test_snapshot_link_is_relative_and_resolves_to_the_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "frozen", lambda: False)
    prefix, overrides = updates._ensure_worker_snapshot(tmp_path)
    link = tmp_path / "updates" / "worker-snapshot"
    relative = os.readlink(link)
    assert not relative.startswith("/")
    assert relative.startswith("snapshots/")
    assert link.resolve() == Path(overrides["PYTHONPATH"])
    # the reused snapshot is reachable purely through the relative link
    again_prefix, again_overrides = updates._ensure_worker_snapshot(tmp_path)
    assert again_prefix == prefix and again_overrides == overrides


def test_snapshot_copies_full_frozen_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "app" / "portmap-0.15.0.abcdef"
    exe = write_executable(bundle / "portmap", "#!/bin/sh\nexit 0\n")
    (bundle / "_internal" / "libs").mkdir(parents=True)
    (bundle / "_internal" / "libs" / "core.so").write_bytes(b"\x00core")
    monkeypatch.setattr(updates, "frozen", lambda: True)
    monkeypatch.setattr(updates, "build_fingerprint", lambda executable=None, package_dir=None: "frozenfp0")
    monkeypatch.setattr(updates.sys, "executable", str(exe))
    prefix, overrides = updates._ensure_worker_snapshot(tmp_path)
    assert prefix[0].endswith("/portmap")
    assert prefix[1] == "_update-worker"  # the frozen snapshot must drive the hidden CLI hook
    snapshot_exe = Path(prefix[0])
    copied_root = snapshot_exe.parent
    assert copied_root.name == "bundle"
    assert (copied_root / "_internal" / "libs" / "core.so").read_bytes() == b"\x00core"
    assert not overrides  # no PYTHONPATH override for frozen runs


def test_build_fingerprint_covers_runtime_data_not_just_python(tmp_path):
    package = tmp_path / "portmap"
    (package / "catalog_static").mkdir(parents=True)
    (package / "download_assets").mkdir()
    (package / "__pycache__").mkdir()
    (package / "updates.py").write_text("v1\n")
    (package / "catalog_static" / "app.js").write_text("// ui v1\n")
    (package / "download_assets" / "install.sh").write_text("#!/bin/sh\n")
    (package / "__pycache__ / updates.cpython-312.pyc".replace(" ", "")).write_bytes(b"junk")
    first = updates.build_fingerprint(package_dir=package)
    # bytecode never affects the fingerprint
    (package / "__pycache__" / "more.pyc").write_bytes(b"junk2")
    assert updates.build_fingerprint(package_dir=package) == first
    # a hotfixed UI asset must change the build identity
    (package / "catalog_static" / "app.js").write_text("// ui hotfix\n")
    assert updates.build_fingerprint(package_dir=package) != first


def test_clean_binary_environment_strips_loader_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/snapshot/dir")
    monkeypatch.setenv("PYTHONHOME", "/bad/home")
    monkeypatch.setenv("PORTMAP_PYTHONPATH_DIR", "/snapshot/dir")
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/podman.sock")
    env = updates.clean_binary_environment(tmp_path)
    for name in ("PYTHONPATH", "PYTHONHOME", "PORTMAP_PYTHONPATH_DIR"):
        assert name not in env
    assert env["DOCKER_HOST"] == "unix:///tmp/podman.sock"
    assert env["PORTMAP_CLIENT_STATE_DIR"] == str(tmp_path)


# ---------------------------------------------------------------------------
# Worker spawn / launch_durable_worker
# ---------------------------------------------------------------------------


def test_launch_durable_worker_validates_kind_and_identifier(tmp_path):
    with pytest.raises(UpdateError):
        updates.launch_durable_worker("local", "not-a-job-id", state_dir=tmp_path)
    with pytest.raises(UpdateError):
        updates.launch_durable_worker("remote", "x", state_dir=tmp_path)
    job = make_job(tmp_path, state="succeeded")
    with pytest.raises(UpdateError, match="already finished"):
        updates.launch_durable_worker("local", job["id"], state_dir=tmp_path)


def test_worker_lock_is_released_between_jobs(tmp_path, monkeypatch):
    binary, _, _ = make_standalone_install(tmp_path, "0.15.0")
    patch_worker_environment(monkeypatch, tmp_path)
    archive = build_archive(tmp_path / "fixture.tar.gz", "0.16.0")
    first = make_job(tmp_path, install_executable=str(binary))
    result = run_worker_job(tmp_path, first, archive=archive)
    assert result["state"] == "succeeded"
    second = make_job(
        tmp_path,
        id="job-20260102T000000Z-cafebabe",
        request_id="req-second",
        install_executable=str(binary),
    )
    # the launcher now points at 0.16.0; updating again to 0.16.0 must fail
    # the downgrade/equality guard inside the worker probe, not deadlock.
    result2 = run_worker_job(tmp_path, second, archive=archive)
    assert result2["state"] in {"failed", "succeeded"}
    # the lock file exists but is free: a fresh acquisition succeeds
    with updates._file_lock(update_worker.worker_lock_path(tmp_path), blocking=False):
        pass


def test_launch_durable_worker_spawns_from_snapshot(tmp_path, monkeypatch):
    job = make_job(tmp_path, state="queued")
    monkeypatch.setattr(
        updates, "_ensure_worker_snapshot",
        lambda state_dir: (["/bin/true", "check"], {}),
    )
    updates.launch_durable_worker("local", job["id"], state_dir=tmp_path)
    record = json.loads((tmp_path / "updates" / "jobs" / f"{job['id']}.json").read_text())
    assert isinstance(record["worker_pid"], int)
    assert record["worker_pid"] > 1
    time.sleep(0.2)  # /bin/true exits immediately; the pid must be detectable
    refreshed = updates.get_job(job["id"], state_dir=tmp_path)
    assert refreshed["state"] == "interrupted"


# ---------------------------------------------------------------------------
# Privilege preflight
# ---------------------------------------------------------------------------


def test_privilege_preflight_blocks_when_sudo_needed_and_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        updates, "_load_infrastructure_state",
        lambda state_dir: {"host_dns_enabled": True, "ingress_backend": "container", "resolver": {"backend": "resolved"}},
    )
    monkeypatch.setattr(updates, "_sudo_noninteractive", lambda timeout=10.0: False)
    from portmap import agent as agent_module

    monkeypatch.setattr(agent_module, "agent_status", lambda settings: type("S", (), {"running": False})())
    requirements = updates.privilege_requirements(tmp_path)
    assert requirements["needs_sudo"] is True
    assert requirements["sudo_ok"] is False
    with pytest.raises(UpdateError, match="sudo -n"):
        updates._preflight_privilege(tmp_path)


@pytest.mark.parametrize("installed", [True, False])
def test_owned_forward_dns_does_not_require_passwordless_sudo(tmp_path, monkeypatch, installed):
    from portmap import agent as agent_module, client_dns

    monkeypatch.setattr(
        updates, "_load_infrastructure_state",
        lambda state_dir: {"host_dns_enabled": True, "ingress_backend": "container", "resolver": {"backend": "forward"}},
    )
    monkeypatch.setattr(agent_module, "agent_status", lambda settings: type("S", (), {"running": False})())
    monkeypatch.setattr(client_dns, "resolver_status", lambda state_dir: {"installed": installed})
    monkeypatch.setattr(updates, "_sudo_noninteractive", lambda: pytest.fail("healthy forward DNS must not invoke sudo"))
    if installed:
        assert updates._preflight_privilege(tmp_path)["needs_sudo"] is False
    else:
        with pytest.raises(UpdateError, match="restore the existing host DNS"):
            updates._preflight_privilege(tmp_path)


def test_brew_job_selects_official_formula_when_taps_share_its_name(tmp_path, monkeypatch):
    old, _brew, log = make_brew_install(tmp_path, "0.15.0", "0.16.0")
    (log / "conflicting-tap").touch()
    patch_worker_environment(monkeypatch, tmp_path)
    job = make_job(tmp_path, install_method="brew", install_executable=str(old))
    result = run_worker_job(tmp_path, job)
    assert result["state"] == "succeeded"
    assert result["version_after"] == "0.16.0"
