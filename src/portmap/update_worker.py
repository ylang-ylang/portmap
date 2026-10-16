"""Durable detached worker executing portmap update jobs.

The worker is spawned by :func:`portmap.updates.launch_durable_worker` from
a private code snapshot, so it keeps running with the exact code that
accepted the job even while the installed package (or the whole agent) is
replaced underneath it. It owns one local job end to end:

``queued -> preflight -> installing -> restarting -> verifying -> succeeded``

with every transition persisted atomically. Any failure - including the
worker itself being stopped - leaves an explicit ``failed``/``interrupted``
record; success is only ever recorded after the *new* binary's own recovery
entry proved the observed agent and catalog versions.

``kind='batch'`` workers delegate to :mod:`portmap.update_control`
(``run_batch``) for persistent multi-machine orchestration.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .errors import PortmapError
from .updates import (
    RECOVERY_TIMEOUT,
    TRANSIENT_STATES,
    UpdateError,
    _atomic_write_json,
    _connection_summary,
    _file_lock,
    _jobs_dir,
    _load_job_file,
    _preflight_privilege,
    _save_job,
    _worker_alive,
    bound_brew_command,
    brew_available_version,
    clean_binary_environment,
    fetch_release_manifest,
    foreground_runner_active,
    install_brew,
    install_standalone,
    job_log_path,
    observed_version,
    platform_asset,
    probe_installation,
    process_identity,
    refresh_brew_metadata,
    tag_for_version,
    updates_dir,
    version_tuple,
)

WORKER_LOCK_WAIT_SECONDS = 60.0
POLL_INTERVAL = 1.0
HEARTBEAT_INTERVAL = 5.0


class WorkerStopped(BaseException):
    """The worker received a stop signal; the job must be interrupted."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="portmap update worker")
    parser.add_argument("--kind", choices=("local", "batch"), required=True)
    parser.add_argument("--identifier", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    state_dir = args.state_dir.expanduser().resolve()
    if args.kind == "local":
        return run_local_job(args.identifier, state_dir)
    return run_batch_worker(args.identifier, state_dir)


def run_batch_worker(identifier: str, state_dir: Path) -> int:
    """Run one persistent batch through update_control (control-owned)."""
    try:
        from . import update_control
    except ImportError as exc:  # pragma: no cover - shipped together in practice
        print(f"portmap update worker: update_control is unavailable: {exc}", file=sys.stderr)
        return 3
    update_control.run_batch(identifier, state_dir=state_dir)
    return 0


def worker_lock_path(state_dir: Path) -> Path:
    return updates_dir(state_dir) / "worker.lock"


def run_local_job(job_id: str, state_dir: Path) -> int:
    from .updates import prepare_updates_state

    prepare_updates_state(state_dir)
    try:
        job = _load_job_file(_jobs_dir(state_dir) / f"{job_id}.json")
    except UpdateError as exc:
        print(f"portmap update worker: {exc}", file=sys.stderr)
        return 1
    log_stream = job_log_path(state_dir, job_id).open("ab", buffering=0)

    def log(message: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log_stream.write(f"{stamp} {message}\n".encode("utf-8"))

    def save(**fields: Any) -> None:
        job.update(fields)
        _save_job(state_dir, job)

    def fail(error: str) -> int:
        save(state="failed", error=error[:2000], message="update failed")
        log(f"FAILED: {error}")
        return 1

    def stop_handlers() -> None:
        def handler(signum: int, _frame: Any) -> None:
            raise WorkerStopped(f"worker received signal {signum}")

        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, handler)

    deadline = time.monotonic() + WORKER_LOCK_WAIT_SECONDS
    while True:
        try:
            lock = _file_lock(worker_lock_path(state_dir), blocking=False, message="update worker lock")
            lock.__enter__()
            break
        except UpdateError:
            if time.monotonic() >= deadline:
                log_stream.close()
                return fail_after_lock_timeout(job_id, state_dir, log)
            time.sleep(POLL_INTERVAL)

    stop_handlers()
    try:
        if job.get("state") not in TRANSIENT_STATES:
            log(f"job {job_id} already finished ({job.get('state')}); nothing to do")
            return 0
        if job.get("worker_pid") is not None and _worker_alive(job):
            # A live worker already owns this job (we were a duplicate spawn).
            if job.get("worker_pid") != os.getpid():
                log(f"job {job_id} is owned by live worker {job.get('worker_pid')}")
                return 0
        identity = process_identity(os.getpid())
        save(worker_pid=os.getpid(), worker_start=identity[1] if identity else "", message="update worker started")

        target = job.get("target_version")
        if not isinstance(target, str):
            return fail("job record has no concrete target version")
        installed_exe = job.get("install_executable")
        installed_exe = Path(installed_exe) if isinstance(installed_exe, str) else None

        # ------------------------------------------------ preflight --------
        save(state="preflight", message=f"verifying installation and official release {target}")
        info = probe_installation(installed_exe)
        if not info.supported:
            return fail(info.reason or f"unsupported installation method: {info.method}")
        if info.reason:
            return fail(info.reason)
        if foreground_runner_active(state_dir):
            return fail(
                "a foreground `portmap run` session owns this machine; managed updates never "
                "kill it. Stop the session (Ctrl-C) and retry from a background agent session"
            )
        running_before = str(job.get("version_before") or "")
        activation_only = bool(
            info.installed_version == target
            and running_before
            and version_tuple(target) > version_tuple(running_before)
        )
        if info.installed_version == target and not activation_only:
            return fail(f"portmap {target} is already installed and running")
        brew = None
        release = None
        asset = None
        if not activation_only:
            try:
                release = fetch_release_manifest(tag_for_version(target), expected_version=target)
            except UpdateError as exc:
                return fail(f"official release check failed: {exc}")
            if info.method == "standalone":
                # Only the standalone path installs the frozen archive; the
                # Brew formula is Python-source based (depends_on :linux,
                # any CPU) and must not require a per-CPU frozen asset.
                asset = platform_asset(release)
                if asset is None:
                    return fail(f"release {target} publishes no archive for this platform")
                log(f"official manifest for {target} verified ({asset.filename}, sha256 {asset.sha256[:12]}…)")
            else:
                log(f"official release {target} verified against the published manifest")
        if info.method == "brew":
            # Bound to the detected prefix and preceded by one fixed tap
            # refresh, so a stale target machine cannot force a manual
            # `brew update` and PATH cannot supply an unrelated brew.
            try:
                brew = bound_brew_command(info)
            except UpdateError as exc:
                return fail(str(exc))
            try:
                if not activation_only:
                    log("refreshing Brew tap metadata (brew update)")
                    refresh_brew_metadata(state_dir, brew)
                available = brew_available_version(state_dir, brew)
            except UpdateError as exc:
                return fail(str(exc))
            if available != target:
                return fail(
                    f"the Brew formula offers {available}, not the requested {target}; "
                    "refresh the tap (`brew update`) or pick the published version"
                )
        requirements = _preflight_privilege(state_dir)
        expect_agent = bool(requirements["expect_agent"])
        use_sudo = bool(requirements["sudo_ok"])
        connections = _connection_summary(state_dir)
        snapshot_path = updates_dir(state_dir) / f"{job_id}.conn.json"
        _atomic_write_json(snapshot_path, connections if connections is not None else [])

        # ------------------------------------------------ installing --------
        if activation_only:
            # The target is already installed (e.g. an external brew upgrade
            # while this agent kept running): no reinstall, only recovery.
            new_binary = info.installed_executable
            if new_binary is None or not new_binary.is_file():
                return fail(
                    f"portmap {target} is installed but its binary could not be located "
                    f"under {info.brew_prefix or info.install_root}"
                )
            save(
                state="installing",
                message=f"{target} is already installed; activating it without reinstalling",
            )
            try:
                observed = observed_version(new_binary, state_dir)
            except UpdateError as exc:
                return fail(str(exc))
            if observed != target:
                return fail(f"the installed binary reports {observed}, not the requested {target}")
            log(f"activation only: using installed binary {new_binary}")
        else:
            save(state="installing", message=f"installing portmap {target} ({info.method})")
            if info.method == "brew":
                new_binary = install_brew(target, state_dir, log=log, installed_exe=installed_exe, brew=brew)
            else:
                new_binary = install_standalone(release, asset, state_dir, log=log, installed_exe=installed_exe)
            installed_probe = probe_installation(new_binary)
            if installed_probe.installed_version != target:
                return fail(
                    f"after installation the package reports {installed_probe.installed_version!r}, "
                    f"not {target}"
                )

        # --------------------------------------------- restarting ----------
        save(state="restarting", message="running the fixed recovery entry from the new binary")
        result_path = updates_dir(state_dir) / f"{job_id}.recover.json"
        argv = [
            str(new_binary), "_update-recover",
            "--state-dir", str(state_dir),
            "--target-version", target,
            "--result-file", str(result_path),
            "--connections-snapshot", str(snapshot_path),
        ]
        if expect_agent:
            argv.append("--expect-agent")
        if use_sudo:
            argv.append("--use-sudo")
        env = clean_binary_environment(state_dir)
        log(f"running: {' '.join(argv[:3])} …")
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(updates_dir(state_dir)),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            return fail(f"could not execute the new binary's recovery entry: {exc}")
        started = time.monotonic()
        while process.poll() is None:
            if time.monotonic() - started >= RECOVERY_TIMEOUT:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                return fail(f"the recovery entry did not finish within {RECOVERY_TIMEOUT:g}s")
            if time.monotonic() - started >= HEARTBEAT_INTERVAL and int(time.monotonic() - started) % int(HEARTBEAT_INTERVAL) == 0:
                save(message=f"recovery in progress ({int(time.monotonic() - started)}s)")
            time.sleep(POLL_INTERVAL)
        if process.returncode != 0:
            return fail(
                f"the new binary's recovery entry failed with exit {process.returncode}: "
                f"{_recovery_error(result_path)}"
            )

        # --------------------------------------------- verifying -----------
        result = _recovery_result(result_path)
        observed = []
        steps = result.get("steps") if isinstance(result, dict) else None
        if isinstance(steps, dict):
            for name in ("agent", "catalog"):
                step = steps.get(name)
                if isinstance(step, dict) and isinstance(step.get("version"), str):
                    observed.append(f"{name} {step['version']}")
        save(
            state="verifying",
            message=f"recovery reported {', '.join(observed) or 'its own running version'}",
        )
        if not isinstance(result, dict) or not result.get("ok"):
            return fail(f"recovery did not confirm the update: {_recovery_error(result_path)}")
        save(
            state="succeeded",
            version_after=target,
            error=None,
            message=(
                f"portmap {target} installed and observed running "
                f"({', '.join(observed) or 'new binary verified'}); roles, connections and DNS preserved"
            ),
        )
        log(f"SUCCEEDED: portmap {target} active ({', '.join(observed)})")
        return 0
    except WorkerStopped as exc:
        save(state="interrupted", error=str(exc), message="update worker stopped before finishing")
        log(f"INTERRUPTED: {exc}")
        return 2
    except (UpdateError, PortmapError) as exc:
        return fail(str(exc))
    except Exception as exc:  # a durable worker never dies silently
        return fail(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            lock.__exit__(None, None, None)
        finally:
            log_stream.close()


def fail_after_lock_timeout(job_id: str, state_dir: Path, log: Callable[[str], None]) -> int:
    try:
        job = _load_job_file(_jobs_dir(state_dir) / f"{job_id}.json")
    except UpdateError:
        return 1
    job["state"] = "failed"
    job["error"] = "another update worker holds the worker lock; this job was not executed"
    job["message"] = "update failed"
    _save_job(state_dir, job)
    log(f"FAILED: worker lock stayed busy for {WORKER_LOCK_WAIT_SECONDS:g}s")
    return 1

def _recovery_result(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _recovery_error(path: Path) -> str:
    result = _recovery_result(path)
    if isinstance(result, dict) and isinstance(result.get("error"), str):
        return result["error"][:1000]
    return "no recovery result was recorded"


if __name__ == "__main__":
    raise SystemExit(main())
