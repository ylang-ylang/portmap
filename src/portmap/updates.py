"""Engine-owned local portmap update machinery.

The functions here run on the machine being updated (inside its host agent
or a detached worker) and perform only fixed, verified operations against
the official release sources already trusted by :mod:`portmap.downloads`:

* ``local_status`` reports the actually executing version separately from
  the installed package version, a real build fingerprint, the detected
  installation method and the latest official release (cached, never a
  hard-coded guess);
* ``start_update`` launches one durable detached worker per update under a
  single-flight lock with durable ``request_id`` idempotency;
* ``get_job`` reads the atomically persisted job record and distinguishes a
  dead worker (``interrupted``) from success;
* ``launch_durable_worker`` re-executes this code from a private snapshot so
  replacing the installed package can never kill or corrupt a running
  update (or batch) worker.

Installation itself is limited to the two supported methods - the official
Brew formula and the official standalone bundle layout managed by
``install.sh`` - and refuses everything else explicitly. Recovery after an
install never re-runs ``portmap setup``: the fixed internal recovery entry
(``portmap _update-recover``, executed from the *new* binary) re-syncs the
existing infrastructure through the standard publication path so server and
client roles, saved connections, host DNS state and the HTTP entry survive,
then proves the observed agent and catalog versions before success is
recorded. No command is ever run through SSH and no step may wait for an
interactive privilege prompt: every child runs with ``stdin`` at
``/dev/null`` and passwordless elevation is verified with ``sudo -n``
during preflight, before anything is installed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform as _platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import __version__
from .client_runtime import _disposable_user_scope, frozen, process_identity, worker_environment
from .downloads import (
    MANIFEST_TIMEOUT_SECONDS,
    RELEASE_MANIFEST_CACHE_NAME,
    ReleaseAsset,
    Release,
    _parse_asset,
    fetch_archive,
    trusted_opener,
    validate_manifest_source_url,
)
from .errors import PortmapError
from .permissions import ensure_private_directory, ensure_private_file
from .settings import PortmapSettings, load_portmap_settings

# Private state layout (sibling names under <state_dir>/updates/ belong to
# update_control.py and are deliberately not used here).
UPDATES_DIR_NAME = "updates"
JOBS_DIR_NAME = "jobs"
LOGS_DIR_NAME = "logs"
ARCHIVES_DIR_NAME = "archives"
SNAPSHOTS_DIR_NAME = "snapshots"
WORKER_SNAPSHOT_LINK = "worker-snapshot"
SNAPSHOT_MARKER = ".snapshot-build-id"
CHECK_CACHE_NAME = "check.json"
WORKER_LOCK_NAME = "worker.lock"
START_LOCK_NAME = "start.lock"
JOB_LOG_SUFFIX = ".log"

STANDALONE_MARKER = ".portmap-install"
BUNDLE_DIR_RE = re.compile(r"^(?P<version>\d+\.\d+\.\d+)\.[0-9A-Za-z]{6}$")
BREW_PATH_RE = re.compile(
    r"^(?P<prefix>.+)/(?:Cellar|opt)/portmap/(?P<version>\d+\.\d+\.\d+)(?:/|$)"
)
BREW_FORMULA = "ylang-ylang/tap/portmap"
JOB_ID_RE = re.compile(r"job-\d{8}T\d{6}Z-[0-9a-f]{8}\Z")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+\Z")
REQUEST_ID_MAX = 128
LOG_TAIL_BYTES = 16 * 1024
CHECK_TTL_SECONDS = 300.0
# A queued job whose worker never registered as alive is declared orphaned
# after this grace period (clock skew and process startup both stay well below).
QUEUED_GRACE_SECONDS = 90.0

TERMINAL_STATES = frozenset({"succeeded", "failed", "interrupted"})
TRANSIENT_STATES = frozenset({"queued", "preflight", "installing", "restarting", "verifying"})

WORKER_KINDS = frozenset({"local", "batch"})

# Fixed trusted release sources. The manifest itself is pinned to the
# project's GitHub release downloads and validated by the same rules the
# catalog uses for its own published downloads.
RELEASES_LATEST_URL = "https://github.com/ylang-ylang/portmap/releases/latest"
RELEASE_TAG_RE = re.compile(r"^/ylang-ylang/portmap/releases/tag/(V\d+\.\d+)$")
RELEASES_PATH_PREFIX = "/ylang-ylang/portmap/releases"

SUDO_PROBE_TIMEOUT = 10.0
BREW_INFO_TIMEOUT = 60.0
BREW_UPDATE_TIMEOUT = 600.0
BREW_UPGRADE_TIMEOUT = 1800.0
RECOVERY_TIMEOUT = 900.0
VERSION_PROBE_TIMEOUT = 30.0
AGENT_VERIFY_TIMEOUT = 90.0
CATALOG_VERIFY_TIMEOUT = 120.0
CHILD_POLL_INTERVAL = 1.0

ARCHIVE_MEMBER_MODE_MASK = 0o777
NO_PYTHON_OVERRIDE_VARS = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PORTMAP_PYTHONPATH_DIR")


class UpdateError(PortmapError):
    """An update operation failed with an actionable explanation."""


class InvalidUpdateRequest(UpdateError):
    """The caller's request payload is malformed (vs a legitimate conflict)."""


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


def updates_dir(state_dir: Path) -> Path:
    return state_dir / UPDATES_DIR_NAME


def _jobs_dir(state_dir: Path) -> Path:
    return updates_dir(state_dir) / JOBS_DIR_NAME


def _logs_dir(state_dir: Path) -> Path:
    return updates_dir(state_dir) / LOGS_DIR_NAME


def _archives_dir(state_dir: Path) -> Path:
    return updates_dir(state_dir) / ARCHIVES_DIR_NAME


def _snapshots_dir(state_dir: Path) -> Path:
    return updates_dir(state_dir) / SNAPSHOTS_DIR_NAME


def _check_cache_path(state_dir: Path) -> Path:
    return updates_dir(state_dir) / CHECK_CACHE_NAME


def _job_path(state_dir: Path, job_id: str) -> Path:
    return _jobs_dir(state_dir) / f"{job_id}.json"


def job_log_path(state_dir: Path, job_id: str) -> Path:
    return _logs_dir(state_dir) / f"{job_id}{JOB_LOG_SUFFIX}"


def batch_log_path(state_dir: Path, identifier: str) -> Path:
    return _logs_dir(state_dir) / f"batch-{identifier}{JOB_LOG_SUFFIX}"


def prepare_updates_state(state_dir: Path) -> None:
    """Create the private engine state layout under ``state_dir``."""
    state_dir = state_dir.expanduser().resolve()
    ensure_private_directory(
        state_dir,
        message=f"client state must be an owned private directory with mode 0700: {state_dir}",
    )
    for directory in (updates_dir(state_dir), _jobs_dir(state_dir), _logs_dir(state_dir)):
        ensure_private_directory(
            directory,
            message=f"update state must be an owned private directory with mode 0700: {directory}",
        )


def _atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write_bytes(path, data, mode=mode)
    if path.exists():
        try:
            ensure_private_file(path, message=f"update record must stay owner-private: {path}")
        except PortmapError:
            # Ownership problems surface through the state gates that matter;
            # a race with a parallel reader is not a write failure.
            pass


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_job_id() -> str:
    # Must satisfy JOB_ID_RE exactly: list_jobs/get_job otherwise reject the
    # ids this engine itself generates.
    return f"job-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"


def version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise UpdateError(f"malformed portmap version: {value!r}") from exc


# ---------------------------------------------------------------------------
# Installation identity
# ---------------------------------------------------------------------------


class InstallInfo:
    """What this machine actually runs and how it was installed."""

    def __init__(
        self,
        *,
        method: str,
        installed_version: str | None,
        executable: Path | None,
        bundle_dir: Path | None = None,
        install_root: Path | None = None,
        launcher: Path | None = None,
        brew_prefix: Path | None = None,
        installed_executable: Path | None = None,
        reason: str | None = None,
    ) -> None:
        self.method = method
        self.installed_version = installed_version
        self.executable = executable
        self.bundle_dir = bundle_dir
        self.install_root = install_root
        self.launcher = launcher
        self.brew_prefix = brew_prefix
        # The binary the installed/linked package provides right now (may be
        # newer than this running process after an external upgrade).
        self.installed_executable = installed_executable
        self.reason = reason

    @property
    def supported(self) -> bool:
        return self.method in {"brew", "standalone"}


def current_platform() -> tuple[str, str] | None:
    system = _platform.system().lower()
    machine = _platform.machine().lower()
    os_name = {"linux": "linux", "darwin": "darwin"}.get(system)
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if os_name is None or arch is None:
        return None
    return os_name, arch


def _read_sidecar_version(bundle_dir: Path) -> str | None:
    """The canonical installed version: the release manifest sidecar."""
    sidecar = bundle_dir / RELEASE_MANIFEST_CACHE_NAME
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    if isinstance(version, str) and VERSION_RE.fullmatch(version):
        return version
    return None


def _standalone_launcher(executable: Path) -> Path | None:
    """The ``install.sh`` managed launcher symlink for this install root.

    The launcher may legitimately point at a *newer* bundle than this
    running process (an external update already flipped it), so ownership is
    proven by the target living in the same install.sh-managed install root.
    """
    install_root = executable.resolve().parent.parent
    candidates = []
    which = shutil.which("portmap")
    if which:
        candidates.append(Path(which))
    candidates.append(Path.home() / ".local" / "bin" / "portmap")
    for candidate in candidates:
        try:
            if not candidate.is_symlink():
                continue
            target = candidate.resolve()
            if target == executable.resolve() or target.parent.parent == install_root:
                return candidate
        except OSError:
            continue
    return None


def probe_installation(executable: Path | str | None = None) -> InstallInfo:
    """Detect how portmap is installed on this machine.

    With ``executable`` omitted the *running* program is inspected. The
    official Brew formula installs a virtualenv under
    ``<prefix>/Cellar/portmap/<version>/libexec``, so a non-frozen portmap
    whose package (this very module) lives on such a path is Brew-managed -
    only a genuine checkout elsewhere reports ``unsupported``. For Brew the
    *installed* version is read from the currently linked keg
    (``opt/portmap``), never from this process's possibly outdated directory.
    """
    if executable is None:
        executable = Path(sys.executable) if frozen() else Path(__file__)
    exe = Path(executable).expanduser()
    try:
        exe = exe.resolve()
    except OSError as exc:
        return InstallInfo(
            method="unsupported", installed_version=None, executable=exe,
            reason=f"cannot resolve the portmap executable: {exc}",
        )
    if not exe.is_file():
        return InstallInfo(
            method="unsupported", installed_version=None, executable=exe,
            reason=f"portmap executable does not exist: {exe}",
        )
    bundle_dir = exe.parent

    if (bundle_dir / STANDALONE_MARKER).is_file():
        launcher = _standalone_launcher(exe)
        # The linked bundle - not this process's directory - defines what is
        # installed right now (an external update already flipped the symlink).
        linked = launcher.resolve() if launcher else exe
        version = _read_sidecar_version(linked.parent)
        if version is None:
            match = BUNDLE_DIR_RE.fullmatch(linked.parent.name)
            version = match.group("version") if match else None
        reason = None
        if launcher is None:
            reason = (
                "no install.sh managed ~/.local/bin/portmap launcher points at the running "
                f"bundle {bundle_dir}; managed updates require the managed symlink"
            )
        return InstallInfo(
            method="standalone",
            installed_version=version,
            executable=exe,
            bundle_dir=bundle_dir,
            install_root=bundle_dir.parent,
            launcher=launcher,
            installed_executable=linked,
            reason=reason,
        )

    brew_match = BREW_PATH_RE.search(str(exe))
    if brew_match:
        prefix = Path(brew_match.group("prefix"))
        # The linked keg - not this process's directory - defines what is
        # installed right now (an external `brew upgrade` already moved it).
        installed_version = brew_match.group("version")
        installed_exe: Path | None = None
        for linked in (prefix / "opt" / "portmap", prefix / "bin" / "portmap"):
            try:
                resolved = linked.resolve()
            except OSError:
                continue
            linked_match = BREW_PATH_RE.search(str(resolved))
            if linked_match and linked_match.group("prefix") == str(prefix):
                installed_version = linked_match.group("version")
                installed_exe = _linked_brew_binary(prefix, resolved)
                break
        brew_command = prefix / "bin" / "brew"
        reason = None
        if not brew_command.is_file():
            reason = f"the Brew portmap formula was found under {prefix}, but {brew_command} is missing"
        return InstallInfo(
            method="brew",
            installed_version=installed_version,
            executable=exe,
            bundle_dir=bundle_dir,
            brew_prefix=prefix,
            launcher=None,
            installed_executable=installed_exe,
            reason=reason,
        )

    return InstallInfo(
        method="unsupported",
        installed_version=None,
        executable=exe,
        bundle_dir=bundle_dir,
        reason=(
            f"portmap {__version__} runs from a source checkout ({exe.parent}); managed "
            "updates need the official Brew formula or install.sh standalone layout"
        ),
    )


def _linked_brew_binary(prefix: Path, resolved_keg: Path) -> Path | None:
    """The portmap launcher inside a resolved keg, whichever layout it uses."""
    for candidate in (resolved_keg / "bin" / "portmap", resolved_keg / "portmap"):
        if candidate.is_file():
            return candidate
    return None


def build_fingerprint(executable: Path | None = None, *, package_dir: Path | None = None) -> str:
    """A stable build identity for the actually running code.

    Frozen bundles hash the real executable. Source/virtualenv runs hash the
    whole running package - Python sources *and* runtime data such as
    ``catalog_static`` UI assets, the installer template and the packaged
    release manifest - so a hotfixed working tree is distinguishable from a
    clean tag. Bytecode caches are excluded.
    """
    target = Path(executable) if executable is not None else None
    if target is None and frozen():
        target = Path(sys.executable)
    if target is not None:
        digest = hashlib.sha256()
        try:
            with target.open("rb") as handle:
                while chunk := handle.read(1 << 16):
                    digest.update(chunk)
        except OSError as exc:
            raise UpdateError(f"cannot fingerprint {target}: {exc}") from exc
        return digest.hexdigest()[:16]
    package = (package_dir or Path(__file__).resolve().parent).resolve()
    digest = hashlib.sha256()
    for source in sorted(package.rglob("*")):
        if source.is_dir():
            continue
        relative = str(source.relative_to(package))
        if "__pycache__" in source.parts or source.suffix in {".pyc", ".pyo"}:
            continue
        digest.update(relative.encode("utf-8"))
        try:
            digest.update(hashlib.sha256(source.read_bytes()).digest())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Trusted official release lookup
# ---------------------------------------------------------------------------


RELEASE_TAG_VALUE_RE = re.compile(r"V\d+\.\d+\Z")


def _release_manifest_url(tag: str) -> str:
    if RELEASE_TAG_VALUE_RE.fullmatch(tag) is None:
        raise UpdateError(f"refusing malformed release tag: {tag!r}")
    url = f"https://github.com{RELEASES_PATH_PREFIX}/download/{tag}/{RELEASE_MANIFEST_CACHE_NAME}"
    try:
        validate_manifest_source_url(url, filename=RELEASE_MANIFEST_CACHE_NAME)
    except ValueError as exc:
        raise UpdateError(f"refusing untrusted release manifest URL: {exc}") from exc
    return url


def parse_update_manifest(payload: Any, *, expected_version: str | None = None) -> Release:
    """Validate a release manifest document for an update target.

    Identical rules to :func:`portmap.downloads.parse_release` except the
    version is checked against ``expected_version`` (the concrete requested
    version) instead of the running version - an updater must accept
    manifests for versions newer than itself.
    """
    if not isinstance(payload, dict):
        raise UpdateError("release manifest must be a JSON object")
    version = payload.get("version")
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise UpdateError(f"release manifest version is malformed: {version!r}")
    if expected_version is not None and version != expected_version:
        raise UpdateError(f"release manifest describes {version}, not the requested {expected_version}")
    assets_payload = payload.get("assets")
    if not isinstance(assets_payload, list) or not assets_payload:
        raise UpdateError("release manifest assets must be a non-empty list")
    assets: list[ReleaseAsset] = []
    seen_platforms: set[tuple[str, str]] = set()
    seen_filenames: set[str] = set()
    for index, entry in enumerate(assets_payload):
        try:
            asset = _parse_asset(entry, version=version, index=index)
        except ValueError as exc:
            raise UpdateError(f"release manifest is invalid: {exc}") from exc
        if asset.platform in seen_platforms:
            raise UpdateError(f"duplicate platform in release manifest: {asset.os}/{asset.arch}")
        if asset.filename in seen_filenames:
            raise UpdateError(f"duplicate filename in release manifest: {asset.filename}")
        seen_platforms.add(asset.platform)
        seen_filenames.add(asset.filename)
        assets.append(asset)
    return Release(version=version, assets=tuple(assets))


def fetch_release_manifest(tag: str, *, expected_version: str | None = None) -> Release:
    """Fetch and strictly validate the official manifest for ``tag``."""
    url = _release_manifest_url(tag)
    import urllib.request

    request = urllib.request.Request(url, headers={"user-agent": f"portmap-updates/{__version__}"})
    try:
        with trusted_opener().open(request, timeout=MANIFEST_TIMEOUT_SECONDS) as response:
            data = response.read(256 * 1024 + 1)
    except OSError as exc:
        raise UpdateError(f"could not fetch the official release manifest {url}: {exc}") from exc
    if len(data) > 256 * 1024:
        raise UpdateError(f"official release manifest exceeds the size bound: {url}")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError(f"official release manifest is not valid JSON: {exc}") from exc
    return parse_update_manifest(payload, expected_version=expected_version)


def fetch_latest_tag() -> str:
    """Resolve the latest official release tag through the pinned project URL."""
    import urllib.parse
    import urllib.request

    request = urllib.request.Request(
        RELEASES_LATEST_URL, method="HEAD", headers={"user-agent": f"portmap-updates/{__version__}"}
    )
    try:
        with trusted_opener().open(request, timeout=MANIFEST_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
    except OSError as exc:
        raise UpdateError(f"could not resolve the latest official release: {exc}") from exc
    parsed = urllib.parse.urlsplit(final_url)
    match = RELEASE_TAG_RE.fullmatch(parsed.path) if parsed.path else None
    if (parsed.hostname or "").lower() != "github.com" or match is None:
        raise UpdateError(f"the latest-release pointer resolved outside the project tags: {final_url}")
    return match.group(1)


def tag_for_version(version: str) -> str:
    return "V" + ".".join(version.split(".")[:2])


def platform_asset(release: Release) -> ReleaseAsset | None:
    plat = current_platform()
    if plat is None:
        return None
    for asset in release.assets:
        if (asset.os, asset.arch) == plat:
            return asset
    return None


def _read_check_cache(state_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(_check_cache_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _cache_fresh(cache: Mapping[str, Any]) -> bool:
    checked_at = cache.get("checked_at")
    if not isinstance(checked_at, str):
        return False
    try:
        age = time.time() - datetime_from_iso(checked_at)
    except ValueError:
        return False
    return 0 <= age < CHECK_TTL_SECONDS


def _write_check_cache(state_dir: Path, *, latest_version: str | None, reason: str | None, tag: str | None) -> None:
    try:
        prepare_updates_state(state_dir)
        _atomic_write_json(
            _check_cache_path(state_dir),
            {
                "checked_at": _utc_now(),
                "checked_with": __version__,
                "latest_version": latest_version,
                "reason": reason,
                "tag": tag,
            },
        )
    except (OSError, PortmapError):
        # The cache only saves network round trips.
        pass


def _check_latest_release(state_dir: Path) -> tuple[str | None, str | None]:
    """Cached latest-official-release lookup. Returns (version, reason)."""
    cached = _read_check_cache(state_dir)
    if isinstance(cached, dict) and _cache_fresh(cached):
        latest = cached.get("latest_version")
        if latest is None or isinstance(latest, str):
            reason = cached.get("reason")
            return latest, reason if isinstance(reason, str) else None
    try:
        tag = fetch_latest_tag()
        release = fetch_release_manifest(tag)
    except UpdateError as exc:
        _write_check_cache(state_dir, latest_version=None, reason=str(exc), tag=None)
        return None, str(exc)
    _write_check_cache(state_dir, latest_version=release.version, reason=None, tag=tag)
    return release.version, None


def datetime_from_iso(value: str) -> float:
    import calendar

    parsed = time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return calendar.timegm(parsed)


# ---------------------------------------------------------------------------
# Job records
# ---------------------------------------------------------------------------

JOB_FIELDS = (
    "id",
    "request_id",
    "state",
    "version_before",
    "target_version",
    "version_after",
    "created_at",
    "updated_at",
    "message",
    "log_tail",
    "error",
)


def _public_job(job: Mapping[str, Any]) -> dict[str, Any]:
    public = {key: job.get(key) for key in JOB_FIELDS}
    public["id"] = job.get("id")
    public["request_id"] = job.get("request_id")
    public["state"] = job.get("state", "interrupted")
    return public


def _load_job_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UpdateError(f"unknown update job: {path.stem}") from None
    except (OSError, ValueError) as exc:
        raise UpdateError(f"update job record is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("id") != path.stem:
        raise UpdateError(f"update job record is invalid: {path}")
    return payload


def list_jobs(state_dir: Path) -> list[dict[str, Any]]:
    directory = _jobs_dir(state_dir)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    jobs = []
    for name in sorted(names):
        if not JOB_ID_RE.fullmatch(name[: -len(".json")] if name.endswith(".json") else ""):
            continue
        try:
            jobs.append(_load_job_file(directory / name))
        except UpdateError:
            continue
    return jobs


def _save_job(state_dir: Path, job: dict[str, Any]) -> None:
    job["updated_at"] = _utc_now()
    _atomic_write_json(_job_path(state_dir, job["id"]), job)


def _worker_alive(job: Mapping[str, Any]) -> bool:
    pid = job.get("worker_pid")
    start = job.get("worker_start")
    if not isinstance(pid, int) or not isinstance(start, str):
        return False
    identity = process_identity(pid)
    if identity is None:
        return False
    # identity is (uid, starttime, argv): both uid and starttime must match,
    # so a recycled pid owned by anyone can never impersonate the worker.
    return identity[0] == os.getuid() and identity[1] == start


def read_log_tail(path: Path, *, limit: int = LOG_TAIL_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > limit and text and not text.startswith("\n"):
        # The window may begin mid-line; drop that partial line only when a
        # complete line actually follows it (otherwise the whole window is
        # the tail of one very long line and is kept).
        cut = text.find("\n")
        if cut != -1 and cut + 1 < len(text):
            text = text[cut + 1 :]
    return text[-limit:]


def _job_log_tail(state_dir: Path, job_id: str) -> str:
    return read_log_tail(job_log_path(state_dir, job_id))


def _finalize_interrupted(state_dir: Path, job: dict[str, Any], message: str) -> dict[str, Any]:
    job["state"] = "interrupted"
    job["error"] = message
    job["message"] = "update worker stopped before finishing"
    _save_job(state_dir, job)
    return job


def _sweep_interrupted(state_dir: Path) -> None:
    """Mark transient jobs whose worker died as interrupted."""
    for job in list_jobs(state_dir):
        if job.get("state") not in TRANSIENT_STATES:
            continue
        if _worker_alive(job):
            continue
        if _job_orphaned_after_grace(job):
            _finalize_interrupted(
                state_dir, job, f"update worker {job.get('worker_pid')} is no longer running; the job stopped before completion"
            )


def _job_orphaned_after_grace(job: Mapping[str, Any]) -> bool:
    """A queued job without a worker is only orphaned after the grace period."""
    if job.get("state") == "queued" and job.get("worker_pid") is None:
        try:
            age = time.time() - datetime_from_iso(job.get("created_at", ""))
        except (ValueError, TypeError):
            age = QUEUED_GRACE_SECONDS
        return age >= QUEUED_GRACE_SECONDS
    return True


def _latest_by_state(jobs: list[dict[str, Any]], states: frozenset[str]) -> dict[str, Any] | None:
    for job in reversed(jobs):
        if job.get("state") in states:
            return job
    return None


def _active_job(state_dir: Path) -> dict[str, Any] | None:
    for job in reversed(list_jobs(state_dir)):
        if job.get("state") in TRANSIENT_STATES and _worker_alive(job):
            return job
    return None


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


@contextmanager
def _file_lock(path: Path, *, blocking: bool = True, message: str = "update lock") -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise UpdateError(f"another process holds the {message}: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _start_lock(state_dir: Path) -> "contextmanager":
    return _file_lock(updates_dir(state_dir) / START_LOCK_NAME, message="update start lock")


def foreground_runner_active(state_dir: Path) -> bool:
    """True while a foreground ``portmap run`` session owns this state."""
    from .lifecycle import runner_lock_path

    path = runner_lock_path(state_dir)
    try:
        handle = path.open("a+b")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def local_status(*, state_dir: Path, check: bool = False) -> dict[str, Any]:
    """Actual running vs installed version, install method and job state."""
    state_dir = state_dir.expanduser().resolve()
    prepare_updates_state(state_dir)
    _sweep_interrupted(state_dir)

    info = probe_installation()
    installed = info.installed_version
    latest: str | None = None
    check_reason: str | None = None
    if check:
        latest, check_reason = _check_latest_release(state_dir)
    else:
        cached = _read_check_cache(state_dir)
        if isinstance(cached, dict) and _cache_fresh(cached):
            value = cached.get("latest_version")
            if isinstance(value, str) and VERSION_RE.fullmatch(value):
                latest = value

    active = _active_job(state_dir)
    jobs = list_jobs(state_dir)
    last = _latest_by_state(jobs, TERMINAL_STATES)

    platform_ok = current_platform() is not None
    foreground = foreground_runner_active(state_dir)

    reason_parts: list[str] = []
    if check_reason:
        # An explicit trusted-source failure is the most actionable fact and
        # must never be masked by the static capability notes below.
        reason_parts.append(check_reason)
    if info.reason:
        reason_parts.append(info.reason)
    if not platform_ok:
        system, machine = _platform.system().lower(), _platform.machine().lower()
        reason_parts.append(f"no official portmap release is published for {system}/{machine}")
    if foreground:
        reason_parts.append(
            "a foreground `portmap run` session owns this machine; stop it (Ctrl-C) and "
            "update from a background agent session - managed updates never kill your foreground session"
        )
    if active is not None:
        reason_parts.append(f"update job {active['id']} is {active.get('state')}")

    installed_value = installed

    update_available = bool(latest and installed_value and version_tuple(latest) > version_tuple(installed_value))
    restart_pending = bool(installed_value and version_tuple(installed_value) > version_tuple(__version__))
    if restart_pending:
        reason_parts.append(
            f"installed {installed_value} is newer than the running {__version__}; "
            "restart portmap to activate it"
        )
    reason = "; ".join(reason_parts) if reason_parts else None

    can_update = (
        info.supported
        and info.reason is None
        and platform_ok
        and active is None
        and not foreground
    )

    return {
        "current_version": __version__,
        "installed_version": installed_value,
        "build_id": build_fingerprint(),
        "latest_version": latest,
        "install_method": info.method,
        "update_available": update_available,
        "can_update": can_update,
        "reason": reason,
        "active_job": _public_job(active) if active else None,
        "last_job": _public_job(last) if last else None,
    }


def _validate_version(version: str) -> str:
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise InvalidUpdateRequest(
            "a concrete official version (e.g. 0.16.0) is required; resolve 'latest' "
            "through local_status(check=True) first"
        )
    return version


def _validate_request_id(request_id: str) -> str:
    if (
        not isinstance(request_id, str)
        or not 1 <= len(request_id) <= REQUEST_ID_MAX
        or not request_id.isprintable()
    ):
        raise InvalidUpdateRequest("request_id must be 1-128 printable characters")
    return request_id


def start_update(version: str, request_id: str, *, state_dir: Path) -> dict[str, Any]:
    """Launch one durable update worker for a concrete official version."""
    state_dir = state_dir.expanduser().resolve()
    target = _validate_version(version)
    _validate_request_id(request_id)
    prepare_updates_state(state_dir)
    _sweep_interrupted(state_dir)

    with _start_lock(state_dir):
        for job in reversed(list_jobs(state_dir)):
            if job.get("request_id") == request_id:
                if job.get("target_version") != target:
                    raise UpdateError(
                        f"request_id {request_id!r} was already used for version "
                        f"{job.get('target_version')}, not {target}; use a new request_id"
                    )
                # Durable idempotency: the same request never installs twice.
                return _public_job(job)
        active = _active_job(state_dir)
        if active is not None:
            raise UpdateError(
                f"an update is already in progress: job {active['id']} is {active.get('state')}"
            )

        info = probe_installation()
        if not info.supported:
            raise UpdateError(info.reason or f"unsupported installation method: {info.method}")
        if info.reason:
            raise UpdateError(info.reason)
        if current_platform() is None:
            raise UpdateError("no official portmap release is published for this platform")
        if foreground_runner_active(state_dir):
            raise UpdateError(
                "a foreground `portmap run` session owns this machine; managed updates never "
                "kill it. Stop the session (Ctrl-C) and retry from a background agent session"
            )

        installed = info.installed_version
        if installed is None:
            raise UpdateError(
                f"cannot determine the installed portmap version under {info.bundle_dir}; "
                "managed updates refuse to proceed without a trustworthy baseline"
            )
        if version_tuple(target) < version_tuple(installed):
            raise UpdateError(
                f"refusing downgrade: installed portmap is {installed}, requested {target}"
            )
        if target == installed:
            if version_tuple(installed) > version_tuple(__version__):
                # Installed but not yet active: completing the activation is
                # a legitimate managed job (recovery + verification, no
                # reinstall), so the web UI can finish it without a CLI
                # restart.
                pass
            else:
                raise UpdateError(f"portmap {target} is already installed and running")

        _preflight_privilege(state_dir)

        job = {
            "id": _new_job_id(),
            "request_id": request_id,
            "state": "queued",
            "version_before": __version__,
            "target_version": target,
            "version_after": None,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "message": "queued for the durable update worker",
            "log_tail": "",
            "error": None,
            "worker_pid": None,
            "worker_start": None,
            "install_method": info.method,
            "install_executable": str(info.executable) if info.executable else None,
            "activation_only": target == installed,
        }
        _save_job(state_dir, job)
        try:
            identity = _spawn_worker("local", job["id"], state_dir)
        except OSError as exc:
            job["state"] = "failed"
            job["error"] = f"could not launch the durable update worker: {exc}"
            _save_job(state_dir, job)
            raise UpdateError(job["error"]) from exc
        job["worker_pid"] = identity["pid"]
        job["worker_start"] = identity["start"]
        job["message"] = f"update worker launched (pid {identity['pid']})"
        _save_job(state_dir, job)
        return _public_job(job)


def get_job(job_id: str, *, state_dir: Path) -> dict[str, Any]:
    """Read one job record; unknown or malformed ids are rejected."""
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise UpdateError(f"unknown update job: {job_id!r}")
    state_dir = state_dir.expanduser().resolve()
    path = _job_path(state_dir, job_id)
    job = _load_job_file(path)
    if job.get("state") in TRANSIENT_STATES and not _worker_alive(job) and _job_orphaned_after_grace(job):
        job = _finalize_interrupted(
            state_dir, job,
            f"update worker {job.get('worker_pid')} is no longer running; the job stopped before completion",
        )
    job["log_tail"] = _job_log_tail(state_dir, job_id)
    return _public_job(job)


def launch_durable_worker(kind: str, identifier: str, *, state_dir: Path) -> None:
    """Start a detached durable worker from the private code snapshot."""
    if kind not in WORKER_KINDS:
        raise InvalidUpdateRequest(f"worker kind must be one of {sorted(WORKER_KINDS)}, not {kind!r}")
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= REQUEST_ID_MAX or not identifier.isprintable():
        raise InvalidUpdateRequest("worker identifier must be 1-128 printable characters")
    state_dir = state_dir.expanduser().resolve()
    prepare_updates_state(state_dir)
    if kind == "local":
        if JOB_ID_RE.fullmatch(identifier) is None:
            raise UpdateError(f"unknown update job: {identifier!r}")
        job = _load_job_file(_job_path(state_dir, identifier))
        if job.get("state") not in TRANSIENT_STATES:
            raise UpdateError(f"job {identifier} already finished ({job.get('state')})")
        if _worker_alive(job):
            raise UpdateError(f"job {identifier} already has a live worker (pid {job.get('worker_pid')})")
        identity = _spawn_worker(kind, identifier, state_dir)
        job["worker_pid"] = identity["pid"]
        job["worker_start"] = identity["start"]
        _save_job(state_dir, job)
        return
    _spawn_worker(kind, identifier, state_dir)


# ---------------------------------------------------------------------------
# Durable worker snapshot and spawn
# ---------------------------------------------------------------------------


def _snapshot_source() -> tuple[Path, str]:
    """(source directory to copy, entry name inside the snapshot)."""
    if frozen():
        return Path(sys.executable).resolve().parent, Path(sys.executable).name
    return Path(__file__).resolve().parent, ""


def _ensure_worker_snapshot(state_dir: Path) -> tuple[list[str], dict[str, str]]:
    """Snapshot this worker's code; returns (argv prefix, env overrides).

    Frozen bundles copy the entire one-directory bundle (executable plus
    ``_internal``) so later package replacement - including Brew removing
    the old keg - cannot break the running worker. Source checkouts copy
    the package tree and run it through ``PYTHONPATH``. A snapshot matching
    the current build fingerprint is reused.
    """
    prepare_updates_state(state_dir)
    directory = _snapshots_dir(state_dir)
    ensure_private_directory(
        directory,
        message=f"worker snapshots must be an owned private directory with mode 0700: {directory}",
    )
    fingerprint = build_fingerprint()
    link = updates_dir(state_dir) / WORKER_SNAPSHOT_LINK
    marker_value = f"{fingerprint}\n"

    def _snapshot_ready(candidate: Path) -> bool:
        try:
            return (candidate / SNAPSHOT_MARKER).read_text(encoding="utf-8") == marker_value
        except OSError:
            return False

    current: Path | None = None
    if link.is_symlink():
        resolved = link.resolve()
        if resolved.is_dir() and _snapshot_ready(resolved):
            current = resolved
    if current is None:
        source, entry_name = _snapshot_source()
        candidate = directory / f"{fingerprint}-{secrets.token_hex(4)}"
        ensure_private_directory(
            candidate,
            message=f"worker snapshot must be an owned private directory with mode 0700: {candidate}",
        )
        if entry_name:
            shutil.copytree(source, candidate / "bundle", symlinks=True)
        else:
            shutil.copytree(
                source,
                candidate / "portmap",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        (candidate / SNAPSHOT_MARKER).write_text(marker_value, encoding="utf-8")
        current = candidate
        temp_link = updates_dir(state_dir) / f".{WORKER_SNAPSHOT_LINK}.{secrets.token_hex(4)}"
        # Relative to the link's directory (updates/): snapshots/<name>.
        os.symlink(f"{SNAPSHOTS_DIR_NAME}/{candidate.name}", temp_link)
        os.replace(temp_link, link)
        # Old snapshots are deliberately retained: detached batch workers may
        # still be executing from them.

    if frozen():
        entry = current / "bundle" / Path(sys.executable).name
        # The frozen snapshot is driven through the hidden CLI subcommand.
        return [str(entry), "_update-worker"], {}
    python_dir = current
    return [sys.executable, "-m", "portmap.update_worker"], {"PYTHONPATH": str(python_dir)}


def _engine_pins(state_dir: Path, settings: PortmapSettings | None = None) -> dict[str, str]:
    """Portmap state pins shared by every engine-spawned process."""
    settings = settings or load_portmap_settings()
    return {
        "PORTMAP_ROOT": str(settings.root),
        "PORTMAP_STATE_DIR": str(settings.state_dir),
        "PORTMAP_AGENT_RUNTIME_HOST_DIR": str(settings.agent_runtime_dir),
        "PORTMAP_CLIENT_STATE_DIR": str(state_dir),
        "PORTMAP_AGENT_SOCKET": str(settings.agent_socket),
    }


def clean_binary_environment(state_dir: Path, settings: PortmapSettings | None = None) -> dict[str, str]:
    """Environment for executing an installed portmap binary.

    Loader overrides of *this* (possibly snapshot-run) worker must not leak
    into the new program, or it would import old snapshot code and report
    wrong versions. Real user/session variables (XDG, DOCKER_HOST,
    CONTAINERS_CONF, SSH) and the portmap state pins are preserved.
    """
    env = dict(os.environ)
    env.update(_engine_pins(state_dir, settings))
    for name in NO_PYTHON_OVERRIDE_VARS:
        env.pop(name, None)
    return env


def _spawn_worker(kind: str, identifier: str, state_dir: Path) -> dict[str, str]:
    """Spawn the detached durable worker; returns its process identity."""
    prefix, overrides = _ensure_worker_snapshot(state_dir)
    argv = [*prefix, "--kind", kind, "--identifier", identifier, "--state-dir", str(state_dir)]
    log = job_log_path(state_dir, identifier) if kind == "local" else batch_log_path(state_dir, identifier)
    env = worker_environment()
    env.update(_engine_pins(state_dir))
    env.update(overrides)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        process = subprocess.Popen(
            argv,
            cwd=str(updates_dir(state_dir)),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    identity = process_identity(process.pid)
    start = identity[1] if identity else ""
    return {"pid": process.pid, "start": start}


# ---------------------------------------------------------------------------
# Privilege preflight
# ---------------------------------------------------------------------------


def _sudo_noninteractive(*, timeout: float = SUDO_PROBE_TIMEOUT) -> bool:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    if shutil.which("sudo") is None:
        return False
    try:
        result = subprocess.run(
            # sudo -v can demand a password under verifypw=all even when
            # executing commands is NOPASSWD. Probe execution, not validation.
            ["sudo", "-n", "true"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _load_infrastructure_state(state_dir: Path) -> dict[str, Any] | None:
    from .infrastructure import load_infrastructure

    try:
        return load_infrastructure(state_dir)
    except FileNotFoundError:
        return None


def privilege_requirements(state_dir: Path) -> dict[str, Any]:
    """What the post-install recovery will need, honestly assessed."""
    infra = _load_infrastructure_state(state_dir)
    from .agent import agent_status

    settings = load_portmap_settings()
    expect_agent = agent_status(settings).running
    host_dns = bool(infra and infra.get("host_dns_enabled"))
    resolver_backend = (infra.get("resolver") or {}).get("backend") if infra else None
    if host_dns and resolver_backend == "forward":
        from .client_dns import resolver_status

        # A healthy owned redirect is reused without writing resolv.conf.
        # Updating code must not introduce a new passwordless-sudo dependency.
        if not resolver_status(state_dir).get("installed"):
            raise UpdateError("restore the existing host DNS with portmap setup before updating")
    dns_needs_sudo = host_dns and resolver_backend not in {"forward", "external"}
    needs_sudo = bool(infra and infra.get("ingress_backend") == "systemd") or dns_needs_sudo
    in_disposable_scope = _disposable_user_scope() is not None and sys.platform.startswith("linux")
    agent_scope_needs_sudo = (
        expect_agent
        and in_disposable_scope
        and not (hasattr(os, "geteuid") and os.geteuid() == 0)
        and _platform.system().lower() != "darwin"
    )
    return {
        "infrastructure": infra is not None,
        "host_dns_enabled": host_dns,
        "dns_needs_sudo": dns_needs_sudo,
        "expect_agent": expect_agent,
        "needs_sudo": needs_sudo or agent_scope_needs_sudo,
        "agent_scope_needs_sudo": agent_scope_needs_sudo,
        "sudo_ok": _sudo_noninteractive() if needs_sudo or agent_scope_needs_sudo else False,
    }


def _preflight_privilege(state_dir: Path) -> dict[str, Any]:
    requirements = privilege_requirements(state_dir)
    if requirements["needs_sudo"] and not requirements["sudo_ok"]:
        if requirements["dns_needs_sudo"]:
            detail = "restoring host DNS"
        elif requirements["agent_scope_needs_sudo"]:
            detail = "restarting the agent outside this login session"
        else:
            detail = "restoring the systemd HTTP ingress"
        raise UpdateError(
            f"this update needs passwordless administrator rights ({detail} after the "
            "install), but `sudo -n` did not succeed. Grant NOPASSWD rights or run the "
            "update from a session that already owns the portmap scope; nothing was installed"
        )
    return requirements


# ---------------------------------------------------------------------------
# Fixed install operations (worker side)
# ---------------------------------------------------------------------------


def _run_child(
    argv: list[str], *, env: dict[str, str], timeout: float, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one fixed child command; never interactive, never unbounded."""
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"command timed out after {timeout:g}s: {' '.join(argv[:4])}…") from exc
    except OSError as exc:
        raise UpdateError(f"could not execute {' '.join(argv[:4])}…: {exc}") from exc


def observed_version(executable: Path, state_dir: Path) -> str:
    """The version a portmap binary actually reports when executed."""
    result = _run_child(
        [str(executable), "--version"],
        env=clean_binary_environment(state_dir),
        timeout=VERSION_PROBE_TIMEOUT,
    )
    if result.returncode != 0:
        raise UpdateError(
            f"{executable} --version failed with exit {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    match = re.search(r"portmap\s+(\d+\.\d+\.\d+)", result.stdout or "")
    if match is None:
        raise UpdateError(f"{executable} --version did not report a portmap version: {result.stdout!r}")
    return match.group(1)


def _brew_environment(state_dir: Path) -> dict[str, str]:
    """Non-interactive Brew environment for the fixed updater commands.

    Cleanup is disabled so the previous keg - which this worker and any
    running portmap processes still execute from - is retained, upgrades
    must not be suppressed, and Brew must fail instead of prompting. Tap
    metadata is refreshed by one explicit fixed `brew update` instead.
    """
    env = clean_binary_environment(state_dir)
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    env["HOMEBREW_NO_INSTALL_CLEANUP"] = "1"
    env["HOMEBREW_NONINTERACTIVE"] = "1"
    env.pop("HOMEBREW_NO_INSTALL_UPGRADE", None)
    return env


def bound_brew_command(info: InstallInfo) -> str:
    """The brew binary of the DETECTED portmap prefix, never one from PATH."""
    if info.brew_prefix is None:
        raise UpdateError("no Brew prefix was detected for this portmap installation")
    brew = info.brew_prefix / "bin" / "brew"
    if not brew.is_file():
        raise UpdateError(
            f"the detected Brew prefix {info.brew_prefix} has no bin/brew; refusing to use "
            "an unrelated brew from PATH"
        )
    return str(brew)


def refresh_brew_metadata(state_dir: Path, brew: str) -> None:
    """One fixed `brew update` so formula metadata can reach the target.

    A target machine with a stale tap would otherwise force the user to run
    `brew update` manually, breaking web-driven completion. The user's tap
    checkout is never reset; local modifications surface as an explicit
    error from brew itself.
    """
    result = _run_child([brew, "update"], env=_brew_environment(state_dir), timeout=BREW_UPDATE_TIMEOUT)
    if result.returncode != 0:
        raise UpdateError(
            f"`brew update` failed with exit {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[-1000:]}"
        )


def brew_available_version(state_dir: Path, brew: str) -> str:
    """The formula version Brew would install, without changing anything."""
    env = _brew_environment(state_dir)
    result = _run_child([brew, "info", "--json=v2", BREW_FORMULA], env=env, timeout=BREW_INFO_TIMEOUT)
    if result.returncode != 0:
        raise UpdateError(
            f"`brew info --json=v2 {BREW_FORMULA}` failed with exit {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    try:
        payload = json.loads(result.stdout)
        formulae = payload["formulae"]
        stable = formulae[0]["versions"]["stable"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise UpdateError(f"`brew info` returned an unexpected document: {exc}") from exc
    if not isinstance(stable, str) or VERSION_RE.fullmatch(stable) is None:
        raise UpdateError(f"Brew reports a malformed portmap version: {stable!r}")
    return stable


# `brew upgrade` cannot skip the formula's postinstall hook (which would run
# portmap setup behind the updater's back); `brew install --skip-post-install`
# on the official tap formula performs the same upgrade without any hook, so
# the durable updater stays the only recovery path.
BREW_INSTALL_ARGV = ["install", "--skip-post-install", "--formula", BREW_FORMULA]


def install_brew(
    target: str, state_dir: Path, *, log, installed_exe: Path | None = None, brew: str | None = None
) -> Path:
    """Fixed-argv official Brew install/upgrade with honest verification."""
    # The worker runs from a code snapshot; probe the real installed executable.
    info = probe_installation(installed_exe)
    if info.method != "brew":
        raise UpdateError(f"this portmap is not Brew-managed (detected {info.method})")
    brew = brew or bound_brew_command(info)
    available = brew_available_version(state_dir, brew)
    if available != target:
        raise UpdateError(
            f"the Brew formula offers {available}, not the requested {target}; "
            "refresh the tap (`brew update`) or pick the published version"
        )
    env = _brew_environment(state_dir)
    log(f"running: brew {' '.join(BREW_INSTALL_ARGV)} (to {target})")
    result = _run_child([brew, *BREW_INSTALL_ARGV], env=env, timeout=BREW_UPGRADE_TIMEOUT)
    if result.returncode != 0:
        raise UpdateError(
            f"`brew {' '.join(BREW_INSTALL_ARGV)}` failed with exit {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[-1000:]}"
        )
    # rc0 alone proves nothing: `brew install` is a no-op on an unchanged
    # version. The located keg and the executed binary must both be the target.
    prefix_result = _run_child([brew, "--prefix", BREW_FORMULA], env=env, timeout=BREW_INFO_TIMEOUT)
    if prefix_result.returncode != 0:
        raise UpdateError(f"`brew --prefix {BREW_FORMULA}` failed after the install")
    opt = Path(prefix_result.stdout.strip())
    candidates = [opt / "bin" / "portmap", opt / "portmap"]
    binary = next((candidate for candidate in candidates if candidate.is_file()), None)
    if binary is None:
        raise UpdateError(f"no portmap binary found under the Brew prefix {opt}")
    binary = binary.resolve()
    match = BREW_PATH_RE.search(str(binary))
    if match is None or match.group("version") != target:
        located = match.group("version") if match else "unknown"
        raise UpdateError(f"Brew left {located} installed, not the requested {target}")
    observed = observed_version(binary, state_dir)
    if observed != target:
        raise UpdateError(f"the installed binary reports {observed}, not the requested {target}")
    log(f"brew installed portmap {target} ({binary})")
    return binary


def _safe_extract_archive(archive: Path, destination: Path, *, max_bytes: int) -> None:
    """Extract only regular files and directories from a verified archive."""
    try:
        bundle = tarfile.open(archive, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise UpdateError(f"release archive cannot be read: {exc}") from exc
    with bundle:
        try:
            members = bundle.getmembers()
        except (tarfile.TarError, OSError) as exc:
            raise UpdateError(f"release archive cannot be read: {exc}") from exc
        total = 0
        for member in members:
            if member.name.startswith("/") or ".." in Path(member.name).parts or member.name == "":
                raise UpdateError(f"release archive contains an unsafe path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise UpdateError(
                    f"release archive contains a non-regular member: {member.name} "
                    f"({stat.filemode(member.mode)})"
                )
            total += max(member.size, 0)
        if total > max_bytes:
            raise UpdateError(
                f"release archive expands to {total} bytes, beyond the {max_bytes}-byte bound"
            )
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = member.mode & ARCHIVE_MEMBER_MODE_MASK
            mode = (mode | 0o400) & ~0o022
            with bundle.extractfile(member) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            os.chmod(target, mode)


def install_standalone(
    release: Release, asset: ReleaseAsset, state_dir: Path, *, log, installed_exe: Path | None = None
) -> Path:
    """Install the official standalone bundle exactly like ``install.sh``.

    The archive is downloaded and sha256-verified through the same trusted
    machinery the catalog uses, extracted into a fresh versioned staging
    directory, proven to execute the target version, and only then does the
    managed launcher symlink flip atomically. Existing bundles are never
    replaced or deleted: running workers keep them alive.
    """
    # The worker runs from a code snapshot; probe the real installed executable.
    info = probe_installation(installed_exe)
    if info.method != "standalone":
        raise UpdateError(f"this portmap is not a standalone install.sh bundle (detected {info.method})")
    if info.launcher is None or info.install_root is None:
        raise UpdateError(info.reason or "the standalone launcher is not managed by install.sh")
    archives = _archives_dir(state_dir)
    ensure_private_directory(
        archives,
        message=f"update archives must be an owned private directory with mode 0700: {archives}",
    )
    free = shutil.disk_usage(str(info.install_root)).free
    if free < asset.size * 3:
        raise UpdateError(
            f"not enough disk space under {info.install_root}: {free} bytes free, "
            f"the update needs roughly {asset.size * 3}"
        )
    log(f"downloading verified archive {asset.filename}")
    try:
        archive = fetch_archive(asset, archives)
    except Exception as exc:  # DownloadUnavailable carries an HTTP-flavored message
        raise UpdateError(f"could not fetch the official release archive: {exc}") from exc

    stage = Path(tempfile.mkdtemp(prefix=f"{release.version}.", dir=str(info.install_root)))
    try:
        _safe_extract_archive(archive, stage, max_bytes=asset.size * 3)
        binary = stage / "portmap"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise UpdateError(f"the release archive has no executable portmap launcher at its root: {asset.filename}")
        observed = observed_version(binary, state_dir)
        if observed != release.version:
            raise UpdateError(
                f"the staged bundle reports {observed}, not the manifest version {release.version}"
            )
        manifest_bytes = json.dumps(
            {"version": release.version, "assets": [asset_payload(entry) for entry in release.assets]},
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        (stage / RELEASE_MANIFEST_CACHE_NAME).write_bytes(manifest_bytes)
        (stage / STANDALONE_MARKER).write_text("installed by portmap update worker\n", encoding="utf-8")
        launcher = info.launcher
        launcher.parent.mkdir(parents=True, exist_ok=True)
        temp_link = launcher.parent / f".portmap.link.{secrets.token_hex(4)}"
        os.symlink(str(binary), temp_link)
        os.replace(temp_link, launcher)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    log(f"installed standalone bundle {release.version} at {stage}")
    return binary


def asset_payload(asset: ReleaseAsset) -> dict[str, Any]:
    return {
        "os": asset.os,
        "arch": asset.arch,
        "filename": asset.filename,
        "sha256": asset.sha256,
        "size": asset.size,
        "source_url": asset.source_url,
    }


# Stable identity of one saved connection. Dynamic metadata (endpoints,
# last-checked stamps, live tunnel handles) is deliberately excluded: batch
# status queries and normal external operations refresh those constantly.
CONNECTION_IDENTITY_FIELDS = ("via", "address", "ssh_target", "target", "remote_http_port", "http_port")


def _connection_summary(state_dir: Path) -> list[dict[str, Any]] | None:
    """Light identity read of saved connections; None when nothing comparable."""
    try:
        payload = json.loads((state_dir / "connections.json").read_text(encoding="utf-8"))
        connections = payload["connections"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    summary = []
    for domain, profile in sorted(connections.items()):
        if not isinstance(profile, dict):
            continue
        entry: dict[str, Any] = {"domain": domain}
        for field in CONNECTION_IDENTITY_FIELDS:
            if field in profile:
                entry[field] = profile[field]
        summary.append(entry)
    return summary or None


def connections_preserved(before: list[dict[str, Any]] | None, after: list[dict[str, Any]] | None) -> tuple[bool, str | None]:
    """Every pre-update connection must survive with an unchanged target.

    New connections added while the update ran are fine, and so is normal
    dynamic metadata refresh; a vanished connection or a changed transport
    target is a real regression and fails the update honestly. The snapshot
    is never written back, so external changes are never overwritten.
    """
    if before is None or after is None:
        return True, None
    current = {entry["domain"]: entry for entry in after if isinstance(entry, dict)}
    # Existence first: a vanished connection is the regression to report even
    # when surviving entries also carry field diffs.
    for entry in before:
        if entry.get("domain") not in current:
            return False, f"connection {entry.get('domain')} disappeared during the update"
    for entry in before:
        domain = entry.get("domain")
        present = current[domain]
        for field, value in entry.items():
            if field != "domain" and present.get(field) != value:
                return False, (
                    f"connection {domain} changed {field} during the update: "
                    f"{value!r} -> {present.get(field)!r}"
                )
    return True, None


# ---------------------------------------------------------------------------
# Fixed internal recovery entry (runs from the NEW binary)
# ---------------------------------------------------------------------------


def run_recovery(
    *,
    state_dir: Path,
    target_version: str,
    result_file: Path,
    connections_snapshot: Path | None = None,
    expect_agent: bool = False,
    use_sudo: bool = False,
) -> int:
    """Re-mount the new code into the existing roles and prove it runs.

    Executed as ``portmap _update-recover`` by the *newly installed* binary
    (never the old worker snapshot), this is the fixed recovery entry:

    * existing infrastructure is re-synced through the standard publication
      path (``client._publish`` → ``infrastructure.sync_infrastructure``),
      which re-binds the catalog container to the new code directory while
      preserving server/client roles, host DNS state, saved connections and
      the HTTP entry - it is NOT an unconditional ``portmap setup``;
    * a previously running background agent is restarted through the
      standard agent ownership path (version mismatch drives replacement);
    * the observed catalog ``/api/version``, agent health version and this
      very process's version must all equal the target before success.

    The outcome is written atomically to ``result_file`` for the worker.
    """
    state_dir = state_dir.expanduser().resolve()
    result: dict[str, Any] = {
        "ok": False,
        "target_version": target_version,
        "running_version": __version__,
        "steps": {},
        "error": None,
    }
    try:
        if __version__ != target_version:
            raise UpdateError(
                f"recovery process runs {__version__}, not the updated {target_version}"
            )
        settings = load_portmap_settings()
        infra = _load_infrastructure_state(state_dir)
        if infra is not None:
            from .client import _load as load_profiles, _publish, client_lock

            with client_lock(state_dir):
                shared = _publish(
                    state_dir, load_profiles(state_dir), settings=settings,
                    use_sudo=use_sudo, host_dns=None,
                )
            result["steps"]["infrastructure"] = {
                "phase": shared.get("phase"),
                "http_port": shared.get("http_port"),
            }
        else:
            result["steps"]["infrastructure"] = None

        if expect_agent:
            from .agent import ensure_agent_started
            from .agent_client import agent_health

            status = ensure_agent_started(settings, use_sudo=use_sudo, access_state_dir=state_dir)
            if not status.running:
                raise UpdateError(f"the agent did not restart: {status.message}")
            deadline = time.monotonic() + AGENT_VERIFY_TIMEOUT
            agent_version = None
            while True:
                try:
                    health = agent_health(socket_path=settings.agent_socket, timeout=2.0)
                except (RuntimeError, OSError):
                    health = {}
                agent_version = health.get("version")
                if agent_version == target_version:
                    break
                if time.monotonic() >= deadline:
                    raise UpdateError(
                        f"the restarted agent reports {agent_version!r}, not {target_version}"
                    )
                time.sleep(CHILD_POLL_INTERVAL)
            result["steps"]["agent"] = {"version": agent_version}

        if infra is not None:
            from .infrastructure import _json_get, load_infrastructure

            port = load_infrastructure(state_dir)["http_port"]
            deadline = time.monotonic() + CATALOG_VERIFY_TIMEOUT
            catalog_version = None
            while True:
                try:
                    body = _json_get(port, "/api/version", host="localhost", timeout=2.0)
                except (PortmapError, OSError, ValueError):
                    body = None
                catalog_version = body.get("catalog_version") if isinstance(body, dict) else None
                if catalog_version == target_version:
                    break
                if time.monotonic() >= deadline:
                    raise UpdateError(
                        f"the catalog reports version {catalog_version!r} on port {port}, "
                        f"not {target_version}"
                    )
                time.sleep(CHILD_POLL_INTERVAL)
            result["steps"]["catalog"] = {"version": catalog_version, "http_port": port}

        if connections_snapshot is not None:
            before = _read_connections_file(connections_snapshot)
            after = _connection_summary(state_dir)
            ok, regression = connections_preserved(before, after)
            if not ok:
                raise UpdateError(regression or "saved connections changed during the update")
            result["steps"]["connections_preserved"] = ok

        result["ok"] = True
        return 0
    except PortmapError as exc:
        result["error"] = str(exc)
        return 1
    except Exception as exc:  # never leave the worker without a verdict
        result["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        try:
            result_file.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(result_file, result)
        except OSError:
            pass


def _read_connections_file(path: Path) -> list[dict[str, str]] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, list) else None
