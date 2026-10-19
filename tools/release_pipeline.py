#!/usr/bin/env python3
"""Automated immutable release pipeline for portmap.

Driven by ``.github/workflows/release.yml`` on every push to ``dev``
(feature-branch pushes never publish) and on manual ``workflow_dispatch``
retries with optional source selection.

The pipeline allocates exactly one ``V<major>.<minor>`` release per dev
source commit. All generated changes (version bump in pyproject.toml,
``src/portmap/__init__.py`` and uv.lock, the compiled catalog UI/skill tree,
the actual built ``src/portmap/release.json`` manifest and branch logs) are
committed on ``feat/release-Vx.y`` branched from the exact source commit,
merged ``--no-ff`` into ``dev``, then ``dev`` is merged ``--no-ff`` into
``main`` and the ``Vx.y`` tag is created. The release merge commit on main
and the tag carry ``Portmap-Source`` / ``Portmap-Build-Run`` /
``Portmap-Version`` trailers so rerunning the same source reuses the already
allocated version instead of publishing a second one, and never rewrites a
published tag or clobbers published release assets.

Security model
--------------
This script is orchestration code and must always be executed from a trusted
checkout (the workflow's own revision), pointed at the validated source via
``--repo-root``; the workflow never executes source or artifact code while
holding publication credentials. Publication binds every uploaded or
recovered artifact to the manifest committed at the exact tag: manifest
bytes, archive sha256/size, canonical checksum companions and the exact
GitHub release URL are all enforced, and published assets are verified by
digest (never by size alone). Releases are created as drafts, completed with
their full asset set and only then published, so partial asset sets are never
visible to consumers. Immutability is enforced by this automation itself and
does not depend on any GitHub repository setting.

dev/main/tag are pushed in a single guarded ``--atomic`` push that first
verifies the remote refs still sit at the planned baseline commits;
concurrent remote movement aborts the release with no partial publication.
This module never deploys to hosts, never force-pushes and never bypasses
branch protection.

Exit codes: 0 success, 1 general failure, 2 safety/conflict refusal,
3 remote advanced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.request import urlopen

TRAILER_SOURCE = "Portmap-Source"
TRAILER_BUILD_RUN = "Portmap-Build-Run"
TRAILER_VERSION = "Portmap-Version"

RELEASE_BRANCH_PREFIX = "feat/release-"
GITHUB_REPO = "ylang-ylang/portmap"
ALLOWED_PLATFORMS = {("linux", "amd64"), ("linux", "arm64")}
MANIFEST_NAME = "release.json"
# Basenames are restricted to the canonical archive/manifest/companion forms;
# no separators, dot segments, whitespace, glob or gh label metacharacters.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ARCHIVE_NAME = re.compile(r"portmap-[0-9]+\.[0-9]+\.[0-9]+-[a-z0-9]+-[a-z0-9]+\.tar\.gz")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")

ALLOWED_STATUS_PATHS = {
    "pyproject.toml",
    "uv.lock",
    "src/portmap/__init__.py",
    "src/portmap/release.json",
    ".branch_logs/dev.md",
    ".branch_logs/main.md",
}
ALLOWED_STATUS_PREFIXES = ("src/portmap/catalog_static/",)

EXIT_GENERAL = 1
EXIT_CONFLICT = 2
EXIT_REMOTE_ADVANCED = 3


class ReleaseError(RuntimeError):
    """Release pipeline failure with a user-facing explanation."""


class TagConflictError(ReleaseError):
    """A published artifact conflicts with the requested release."""


class RemoteAdvancedError(ReleaseError):
    """The remote moved between planning and the atomic push."""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def git_text(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def parse_version(text: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text.strip())
    if not match:
        raise ReleaseError(f"version {text!r} must be numeric major.minor.patch")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def parse_tag(tag: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"V(\d+)\.(\d+)", tag)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def tag_for_version(version: str) -> str:
    major, minor, _ = parse_version(version)
    return f"V{major}.{minor}"


def read_pyproject_version(repo: Path) -> str:
    data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    try:
        return str(data["project"]["version"])
    except KeyError as error:
        raise ReleaseError("pyproject.toml is missing project.version") from error


def list_release_tags(repo: Path) -> list[str]:
    output = git_text(repo, "tag", "-l")
    tags = [line.strip() for line in output.splitlines() if line.strip()]
    return sorted((tag for tag in tags if parse_tag(tag) is not None), key=parse_tag)


def derive_pending_version(pyproject_version: str, tags: list[str]) -> str:
    """Pick the next version: pyproject when ahead of the latest tag.

    The repository releases one ``V<major>.<minor>`` tag per release, so the
    pending version is the pyproject version whenever its major.minor is
    greater than the latest tag; otherwise the next minor is allocated with a
    ``.0`` patch.
    """
    pending = parse_version(pyproject_version)
    numbered = [parse_tag(tag) for tag in tags]
    base = max((pair for pair in numbered if pair), default=None)
    if base is None or (pending[0], pending[1]) > base:
        return pyproject_version.strip()
    major, minor = base
    return f"{major}.{minor + 1}.0"


def marker_from_message(message: str) -> dict[str, str]:
    marker: dict[str, str] = {}
    for line in message.splitlines():
        match = re.fullmatch(r"Portmap-(Source|Build-Run|Version): (\S+)", line.strip())
        if match:
            key = match.group(1).lower().replace("-", "_")
            marker[key] = match.group(2)
    return marker


def trailer_lines(version: str, source_sha: str, run_id: str) -> str:
    return (
        f"{TRAILER_VERSION}: {version}\n"
        f"{TRAILER_SOURCE}: {source_sha}\n"
        f"{TRAILER_BUILD_RUN}: {run_id}\n"
    )


def tag_marker(repo: Path, tag: str) -> dict[str, str]:
    obj_type = git_text(repo, "cat-file", "-t", tag)
    if obj_type == "tag":
        raw = git_text(repo, "cat-file", "tag", tag)
        message = raw.split("\n\n", 1)[1] if "\n\n" in raw else ""
    else:
        message = git_text(repo, "log", "-1", "--format=%B", tag)
    return marker_from_message(message)


def tag_exists(repo: Path, tag: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}", check=False).returncode == 0


def find_release_for_source(repo: Path, source_sha: str) -> dict[str, str] | None:
    for tag in list_release_tags(repo):
        marker = tag_marker(repo, tag)
        if marker.get("source") == source_sha:
            return {"tag": tag, **marker}
    return None


def resolve_source(repo: Path, source_sha: str | None) -> str:
    if source_sha:
        if git(repo, "cat-file", "-e", f"{source_sha}^{{commit}}", check=False).returncode != 0:
            raise ReleaseError(f"source commit {source_sha} does not exist in this checkout")
    else:
        source_sha = git_text(repo, "rev-parse", "HEAD")
    if git(repo, "merge-base", "--is-ancestor", source_sha, "refs/remotes/origin/dev", check=False).returncode != 0:
        raise ReleaseError(
            f"source commit {source_sha} is not part of origin/dev; refusing to release foreign history"
        )
    return source_sha


def select_source(repo: Path, raw_input: str | None, default_sha: str | None = None) -> dict[str, str]:
    """Validate a requested source against protected dev (trusted orchestration).

    Runs on the trusted checkout before any selected code is executed. Only
    full 40-hex commit ids are accepted, and the commit must already be part
    of origin/dev. An explicit ``default_sha`` (push events pin the exact
    event commit) wins over the fallback; an omitted selection otherwise
    defaults to the CURRENT origin/dev head from the trusted fetch, never to
    the trusted checkout's own revision.
    """
    raw = (raw_input or "").strip()
    if raw:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", raw):
            raise ReleaseError(
                f"source {raw!r} is not a full 40-hex commit id; refusing ambiguous history selection"
            )
        sha = raw.lower()
    else:
        sha = (default_sha or "").strip().lower() or git_text(
            repo, "rev-parse", "--verify", "refs/remotes/origin/dev"
        )
    return {"source_sha": resolve_source(repo, sha)}


def plan_release(repo: Path, source_sha: str | None = None) -> dict[str, object]:
    source = resolve_source(repo, source_sha)
    base_main = git_text(repo, "rev-parse", "--verify", "refs/remotes/origin/main")
    existing = find_release_for_source(repo, source)
    if existing:
        tag = existing["tag"]
        version = existing.get("version", "")
        if not version or tag_for_version(version) != tag:
            raise TagConflictError(
                f"tag {tag} records source {source} but its Portmap-Version trailer "
                f"({version!r}) does not match the tag name; refusing to touch the published tag"
            )
        return {
            "mode": "existing",
            "version": version,
            "tag": tag,
            "source_sha": source,
            "base_main": base_main,
            "build_run_id": existing.get("build_run", ""),
        }
    # Policy (explicit): the pending version is the pyproject version when
    # its major.minor is greater than the latest tag, otherwise the next
    # minor with a .0 patch. A preexisting tag at the PENDING version that
    # does not record this source was published from different history and
    # is never reallocated, moved or rewritten; planning refuses instead.
    # (A pyproject version that merely equals the latest released tag is the
    # normal post-release state and simply allocates the next minor.)
    version = derive_pending_version(read_pyproject_version(repo), list_release_tags(repo))
    tag = tag_for_version(version)
    if tag_exists(repo, tag):
        marker = tag_marker(repo, tag)
        raise TagConflictError(
            f"pending tag {tag} already exists (marker source {marker.get('source', '<none>')}) "
            f"and does not record source {source}; refusing to reallocate or rewrite it"
        )
    return {
        "mode": "new",
        "version": version,
        "tag": tag,
        "source_sha": source,
        "base_main": base_main,
        "build_run_id": "",
    }


def _replace_once(text: str, pattern: str, replacement: str, label: str) -> str:
    matches = list(re.finditer(pattern, text, flags=re.MULTILINE))
    if len(matches) != 1:
        raise ReleaseError(f"expected exactly one {label} entry, found {len(matches)}")
    return text[: matches[0].start()] + re.sub(pattern, replacement, matches[0].group(0), count=1) + text[matches[0].end():]


def bump_version_files(repo: Path, version: str) -> None:
    parse_version(version)
    pyproject = repo / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    head, separator, tail = text.partition("\n[tool.")
    # The project table must carry exactly one version line; [tool.*] tables
    # below it are never touched.
    bumped = _replace_once(head, r'^version = "[^"]*"$', f'version = "{version}"', "pyproject project.version")
    pyproject.write_text(bumped + (separator + tail if separator else ""), encoding="utf-8")

    init = repo / "src" / "portmap" / "__init__.py"
    init.write_text(
        _replace_once(init.read_text(encoding="utf-8"), r'^__version__ = "[^"]*"$', f'__version__ = "{version}"', "__version__"),
        encoding="utf-8",
    )

    lock = repo / "uv.lock"
    lock.write_text(
        _replace_once(
            lock.read_text(encoding="utf-8"),
            r'^name = "portmap"\nversion = "[^"]*"$',
            f'name = "portmap"\nversion = "{version}"',
            "uv.lock portmap package version",
        ),
        encoding="utf-8",
    )
    check_version_consistency(repo, version)


def check_version_consistency(repo: Path, version: str) -> None:
    problems = []
    if read_pyproject_version(repo) != version:
        problems.append("pyproject.toml")
    init_text = (repo / "src" / "portmap" / "__init__.py").read_text(encoding="utf-8")
    if f'__version__ = "{version}"' not in init_text:
        problems.append("src/portmap/__init__.py")
    lock_text = (repo / "uv.lock").read_text(encoding="utf-8")
    if f'name = "portmap"\nversion = "{version}"' not in lock_text:
        problems.append("uv.lock")
    if problems:
        raise ReleaseError(f"version metadata inconsistent for {version}: {', '.join(problems)}")


def load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(f"release manifest {path} is unreadable: {error}") from error


def _safe_member(assets_dir: Path, name: str) -> tuple[Path, str | None]:
    """Return (path, error) for a manifest-controlled member name.

    Only plain basenames inside assets_dir are accepted: no separators,
    absolute paths, dot segments, whitespace or gh label metacharacters, and
    never a symlink.
    """
    if not SAFE_NAME.fullmatch(name) or name in {".", ".."} or name != Path(name).name:
        return assets_dir / name, f"unsafe release file name {name!r}"
    path = assets_dir / name
    if path.is_symlink():
        return path, f"release file {name} is a symlink"
    try:
        contained = path.resolve().parent == assets_dir.resolve()
    except OSError:
        return path, f"release file {name} cannot be resolved"
    if not contained:
        return path, f"release file {name} escapes the asset directory"
    if not path.is_file():
        return path, f"asset file missing: {name}"
    return path, None


def canonical_source_url(tag: str, filename: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{filename}"


def checksum_companion_name(filename: str) -> str:
    return f"{filename}.sha256"


def validate_manifest(manifest: dict, version: str, assets_dir: Path, *, require_files: bool = True) -> list[str]:
    """Validate a manifest against the planned version and asset directory.

    With ``require_files`` the referenced archive and its checksum companion
    must exist as safe regular files whose bytes match the manifest exactly.
    """
    errors: list[str] = []
    tag = tag_for_version(version)
    if manifest.get("version") != version:
        errors.append(f"manifest version {manifest.get('version')!r} != expected {version!r}")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        return errors + ["manifest lists no assets"]
    seen: set[str] = set()
    for asset in assets:
        filename = str(asset.get("filename", ""))
        os_name = str(asset.get("os", ""))
        arch = str(asset.get("arch", ""))
        if filename in seen:
            errors.append(f"duplicate asset filename {filename!r}")
        seen.add(filename)
        if not ARCHIVE_NAME.fullmatch(filename):
            errors.append(f"asset filename {filename!r} is not a canonical release archive basename")
            continue
        if filename != f"portmap-{version}-{os_name}-{arch}.tar.gz":
            errors.append(f"asset filename {filename!r} does not match its os/arch metadata")
        if (os_name, arch) not in ALLOWED_PLATFORMS:
            errors.append(f"asset platform {os_name}-{arch} is not an allowed release platform")
        sha = str(asset.get("sha256", ""))
        if not SHA256_HEX.fullmatch(sha):
            errors.append(f"asset sha256 for {filename} is not a lowercase 64-hex digest")
        if not isinstance(asset.get("size"), int) or asset["size"] < 0:
            errors.append(f"asset size for {filename} is not a non-negative integer")
        if asset.get("source_url") != canonical_source_url(tag, filename):
            errors.append(
                f"asset source_url must be exactly {canonical_source_url(tag, filename)}"
            )
        if not require_files:
            continue
        archive, archive_error = _safe_member(assets_dir, filename)
        if archive_error:
            errors.append(archive_error)
            continue
        if digest(archive) != sha:
            errors.append(f"asset sha256 mismatch: {filename}")
        if archive.stat().st_size != asset["size"]:
            errors.append(f"asset size mismatch: {filename} ({asset['size']!r} recorded)")
        companion, companion_error = _safe_member(assets_dir, checksum_companion_name(filename))
        if companion_error:
            errors.append(companion_error)
        elif companion.read_text(encoding="utf-8") != f"{sha}  {filename}\n":
            errors.append(f"checksum companion {checksum_companion_name(filename)} is malformed")
    if require_files:
        manifest_path, manifest_error = _safe_member(assets_dir, MANIFEST_NAME)
        if manifest_error:
            errors.append(manifest_error)
    return errors


def copy_release_manifest(repo: Path, manifest_path: Path, version: str) -> None:
    data = manifest_path.read_bytes()
    manifest = json.loads(data.decode("utf-8"))
    if manifest.get("version") != version:
        raise ReleaseError(
            f"built manifest version {manifest.get('version')!r} != planned {version!r}; refusing to commit stale manifest"
        )
    if not manifest.get("assets"):
        raise ReleaseError("built manifest lists no assets")
    target = repo / "src" / "portmap" / "release.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _log_entry(text: str, entry: str) -> str:
    if text and not text.endswith("\n"):
        text += "\n"
    return text + entry + "\n"


def append_branch_logs(repo: Path, version: str, tag: str, source_sha: str) -> None:
    short = source_sha[:10]
    dev_entry = (
        f"- Release {tag} (portmap {version}) from dev {short} via the automated pipeline: "
        "compiled catalog UI/skill rebuilt, frozen linux-amd64 archive, checksum and matching "
        "manifest published to the immutable GitHub release; non-integration suite green."
    )
    main_entry = (
        f"- Release `{tag}` (`portmap {version}`): automated immutable distribution of the "
        "linux-amd64 archive, sha256 checksum and matching release manifest; the Brew tap is "
        "updated by the reusable release-distribution workflow in the same run."
    )
    for name, entry in (("dev.md", dev_entry), ("main.md", main_entry)):
        path = repo / ".branch_logs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else f"# {name[:-3]}\n\n"
        path.write_text(_log_entry(text, entry), encoding="utf-8")


def unexpected_status_paths(repo: Path) -> list[str]:
    output = git(repo, "status", "--porcelain", "-uall").stdout
    unexpected = []
    for line in output.splitlines():
        # Porcelain v1 rows are "XY PATH"; never strip the row itself or the
        # leading status column shifts the path.
        entry = line[3:].strip().strip('"')
        if " -> " in entry:
            unexpected.extend(part.strip() for part in entry.split(" -> "))
            continue
        if entry in ALLOWED_STATUS_PATHS or entry.startswith(ALLOWED_STATUS_PREFIXES):
            continue
        unexpected.append(entry)
    return unexpected


def _release_branch(tag: str) -> str:
    return f"{RELEASE_BRANCH_PREFIX}{tag}"


def prepare_release_commit(
    repo: Path, *, version: str, tag: str, source_sha: str, run_id: str, assets_dir: Path
) -> dict[str, object]:
    if tag_for_version(version) != tag:
        raise ReleaseError(f"tag {tag} does not match version {version}")
    head = git_text(repo, "rev-parse", "HEAD")
    if head != source_sha:
        raise ReleaseError(f"HEAD {head} is not the release source {source_sha}; checkout drifted")
    branch = _release_branch(tag)
    if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise TagConflictError(f"local release branch {branch} already exists; refusing to reuse it")
    remote = ls_remote(repo, f"refs/heads/{branch}")
    if remote:
        raise TagConflictError(f"remote release branch {branch} already exists; refusing to reuse it")

    preexisting = unexpected_status_paths(repo)
    if preexisting:
        raise ReleaseError(
            "worktree already carries changes outside the release scope before preparing: "
            + ", ".join(sorted(preexisting))
        )
    bump_version_files(repo, version)
    copy_release_manifest(repo, assets_dir / MANIFEST_NAME, version)
    manifest = load_manifest(assets_dir / MANIFEST_NAME)
    errors = validate_manifest(manifest, version, assets_dir)
    if errors:
        raise ReleaseError("release assets failed validation: " + "; ".join(errors))
    append_branch_logs(repo, version, tag, source_sha)

    unexpected = unexpected_status_paths(repo)
    if unexpected:
        raise ReleaseError(
            "worktree carries changes outside the release scope: " + ", ".join(sorted(unexpected))
        )
    # Only mutate refs once every refusal check has passed; the dirty tree
    # (restored catalog assets plus the generated files above) carries over
    # because the branch starts at the exact source commit HEAD already is.
    git(repo, "checkout", "-b", branch)
    for path in sorted(ALLOWED_STATUS_PATHS) + ["src/portmap/catalog_static"]:
        if (repo / path).exists():
            git(repo, "add", "--", path)
    message = (
        f"release: {tag} automated pipeline distribution\n\n"
        f"Version {version}, built catalog assets and the matching immutable release\n"
        f"manifest built from dev {source_sha} (workflow run {run_id}).\n\n"
        + trailer_lines(version, source_sha, run_id)
    )
    try:
        git(repo, "commit", "-m", message)
    except subprocess.CalledProcessError as error:
        raise ReleaseError(
            "release commit failed (empty change set or missing git identity): "
            f"{error.stderr.strip()}"
        ) from error
    commit = git_text(repo, "rev-parse", "HEAD")
    if git_text(repo, "rev-parse", f"{commit}^") != source_sha:
        raise ReleaseError("release commit is not parented on the source commit")
    return {"branch": branch, "commit": commit}


def ls_remote(repo: Path, *refs: str) -> dict[str, str]:
    result = git(repo, "ls-remote", "origin", *refs)
    mapping: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "\t" in line:
            sha, ref = line.split("\t", 1)
            mapping[ref.strip()] = sha.strip()
    return mapping


def guarded_atomic_push(repo: Path, tag: str, base_dev: str, base_main: str) -> None:
    remote = ls_remote(repo, "refs/heads/dev", "refs/heads/main", f"refs/tags/{tag}")
    remote_dev = remote.get("refs/heads/dev")
    remote_main = remote.get("refs/heads/main")
    if remote_tag := remote.get(f"refs/tags/{tag}"):
        raise TagConflictError(f"tag {tag} already exists on the remote at {remote_tag}; refusing to rewrite")
    if remote_dev != base_dev:
        raise RemoteAdvancedError(
            f"origin/dev moved to {remote_dev} while the release was being built "
            f"(expected {base_dev}); aborting with no partial push"
        )
    if remote_main != base_main:
        raise RemoteAdvancedError(
            f"origin/main moved to {remote_main} while the release was being built "
            f"(expected {base_main}); aborting with no partial push"
        )
    try:
        git(
            repo,
            "push",
            "--atomic",
            "origin",
            "refs/heads/dev:refs/heads/dev",
            "refs/heads/main:refs/heads/main",
            f"refs/tags/{tag}:refs/tags/{tag}",
        )
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip() if error.stderr else ""
        raise ReleaseError(
            f"atomic push rejected (remote likely moved during the release): {stderr}"
        ) from error


def finalize_release(
    repo: Path,
    *,
    version: str,
    tag: str,
    source_sha: str,
    run_id: str,
    base_main: str | None = None,
) -> dict[str, object]:
    if tag_for_version(version) != tag:
        raise ReleaseError(f"tag {tag} does not match version {version}")
    branch = _release_branch(tag)
    if git_text(repo, "rev-parse", "--abbrev-ref", "HEAD") != branch:
        raise ReleaseError(f"HEAD must be the prepared release branch {branch}")
    if git_text(repo, "status", "--porcelain", "-uall"):
        raise ReleaseError("worktree must be clean before merging the release")
    if tag_exists(repo, tag):
        raise TagConflictError(f"tag {tag} already exists locally; refusing to rewrite it")
    if base_main is None:
        base_main = git_text(repo, "rev-parse", "--verify", "refs/remotes/origin/main")
    tracking_main = git_text(repo, "rev-parse", "--verify", "refs/remotes/origin/main")
    if tracking_main != base_main:
        raise RemoteAdvancedError(
            f"origin/main is now {tracking_main}, but this release was planned against "
            f"{base_main}; aborting before any merge so main is never advanced implicitly"
        )

    if git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/dev", check=False).returncode == 0:
        local_dev = git_text(repo, "rev-parse", "refs/heads/dev")
        if local_dev != source_sha:
            raise TagConflictError(
                f"local dev branch sits at {local_dev}, not the release source {source_sha}"
            )
    else:
        git(repo, "branch", "dev", source_sha)
    git(repo, "checkout", "dev")
    git(repo, "merge", "--no-ff", branch, "-m", f"merge: automated release {tag} into dev")

    if git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/main", check=False).returncode == 0:
        local_main = git_text(repo, "rev-parse", "refs/heads/main")
        if local_main != base_main:
            raise TagConflictError(
                f"local main branch sits at {local_main}, not the expected base {base_main}"
            )
    else:
        git(repo, "branch", "main", base_main)
    git(repo, "checkout", "main")
    main_message = (
        f"release: {tag} portmap {version}\n\n"
        f"Merge dev into main for the automated {tag} distribution built from dev\n"
        f"{source_sha} (workflow run {run_id}).\n\n"
        + trailer_lines(version, source_sha, run_id)
    )
    git(repo, "merge", "--no-ff", "dev", "-m", main_message)

    tag_message = (
        f"release {tag} portmap {version}\n\n"
        f"Immutable linux-amd64 distribution built from dev {source_sha}\n"
        f"(workflow run {run_id}).\n\n"
        + trailer_lines(version, source_sha, run_id)
    )
    git(repo, "tag", "-a", tag, "-m", tag_message)

    guarded_atomic_push(repo, tag, source_sha, base_main)
    return {
        "tag": tag,
        "version": version,
        "dev": git_text(repo, "rev-parse", "refs/heads/dev"),
        "main": git_text(repo, "rev-parse", "refs/heads/main"),
        "tag_object": git_text(repo, "rev-parse", f"refs/tags/{tag}"),
    }


def committed_manifest_text(repo: Path, tag: str) -> str:
    """Raw bytes (as text) of src/portmap/release.json committed at the tag."""
    result = git(repo, "show", f"{tag}:src/portmap/release.json", check=False)
    if result.returncode != 0:
        raise TagConflictError(f"tag {tag} carries no src/portmap/release.json manifest")
    return result.stdout


def committed_manifest(repo: Path, tag: str) -> dict:
    try:
        return json.loads(committed_manifest_text(repo, tag))
    except json.JSONDecodeError as error:
        raise TagConflictError(f"tag {tag} manifest is not valid JSON: {error}") from error


def gh_runner(argv: list[str], repo: Path) -> tuple[int, str, str]:
    proc = subprocess.run(["gh", *argv], cwd=str(repo), capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _gh_repo_slug() -> str:
    env_slug = os.environ.get("GH_REPO", "").strip()
    if env_slug and env_slug != GITHUB_REPO:
        raise ReleaseError(
            f"GH_REPO {env_slug!r} does not match the release repository "
            f"{GITHUB_REPO!r}; refusing to operate on a foreign repository"
        )
    return GITHUB_REPO


def gh_release_document(tag: str, repo: Path, runner=gh_runner) -> dict | None:
    """Release metadata from the REST API; ``None`` only on a genuine 404.

    ``gh release view --json assets`` reports ``digest: null`` on real
    installations, so asset digests MUST come from the REST endpoint, which
    does return them. Any non-404 failure (bad credentials, forbidden,
    5xx, network) is a hard error and is never interpreted as an absent
    release.
    """
    slug = _gh_repo_slug()
    code, out, err = runner(["api", f"repos/{slug}/releases/tags/{tag}"], repo)
    if code == 0:
        try:
            document = json.loads(out)
        except json.JSONDecodeError as error:
            raise ReleaseError(f"cannot parse REST release metadata for {tag}: {error}") from error
        if not isinstance(document, dict):
            raise ReleaseError(f"REST release metadata for {tag} is not an object")
        return document
    diagnostic = (err or out or "").strip()
    if "http 404" in diagnostic.lower():
        return None
    raise ReleaseError(
        f"reading release metadata for {tag} failed without a 404 (authentication, "
        f"permission, server and network failures never count as an absent release): {diagnostic}"
    )


def verify_existing_release(
    repo: Path, *, tag: str, source_sha: str, run_id: str, runner=gh_runner
) -> dict[str, object]:
    if not tag_exists(repo, tag):
        raise TagConflictError(f"tag {tag} does not exist; there is nothing to recover")
    marker = tag_marker(repo, tag)
    if marker.get("source") != source_sha:
        raise TagConflictError(
            f"tag {tag} records source {marker.get('source', '<none>')}, not {source_sha}; "
            "refusing to recover a release belonging to different history"
        )
    if marker.get("build_run") and run_id and marker["build_run"] != run_id:
        raise TagConflictError(
            f"tag {tag} records build run {marker['build_run']}, not the requested {run_id}"
        )
    version = marker.get("version")
    if not version:
        raise TagConflictError(f"tag {tag} carries no Portmap-Version trailer; cannot verify assets")
    manifest = committed_manifest(repo, tag)
    if manifest.get("version") != version:
        raise TagConflictError(
            f"tag {tag} committed manifest version {manifest.get('version')!r} "
            f"disagrees with its Portmap-Version trailer {version!r}"
        )
    required = release_file_names(manifest)
    document = gh_release_document(tag, repo, runner)
    if document is None:
        return {
            "tag": tag,
            "version": version,
            "build_run_id": marker.get("build_run", ""),
            "release_present": False,
            "is_draft": False,
            "missing_files": required,
        }
    published = {
        asset["name"]
        for asset in document.get("assets", [])
        if isinstance(asset, dict) and isinstance(asset.get("name"), str)
    }
    missing = [name for name in required if name not in published]
    return {
        "tag": tag,
        "version": version,
        "build_run_id": marker.get("build_run", ""),
        "release_present": True,
        "is_draft": bool(document.get("draft")),
        "missing_files": missing,
    }


def release_file_names(manifest: dict) -> list[str]:
    names: list[str] = []
    for asset in manifest.get("assets", []):
        filename = asset.get("filename", "")
        if filename:
            names.extend([filename, checksum_companion_name(filename)])
    names.append(MANIFEST_NAME)
    return list(dict.fromkeys(names))


def _marker_consistent(label: str, explicit: str | None, recorded: str | None) -> str | None:
    """Resolve an explicit argument against the durable tag marker."""
    if explicit is not None and recorded and explicit != recorded:
        raise TagConflictError(
            f"requested {label} {explicit!r} conflicts with the {label} recorded on the tag "
            f"({recorded!r}); published metadata is immutable"
        )
    return explicit if explicit is not None else recorded


def _published_digest(asset: dict, name: str) -> str:
    raw = asset.get("digest")
    if not isinstance(raw, str):
        raise ReleaseError(
            f"GitHub returned no digest for published asset {name}; refusing to verify by name/size alone"
        )
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", raw)
    if not match:
        raise ReleaseError(f"published asset {name} carries an unparseable digest {raw!r}")
    return match.group(1)


def _expected_content(committed: dict, name: str, committed_text: str) -> bytes | None:
    """Bytes a published/recovered file must have, derived from the tag alone.

    Archives cannot be derived (their bytes must come from the retained CI
    artifact and are digest-bound instead); the manifest and the checksum
    companions are byte-exact derivatives of the committed manifest.
    """
    if name == MANIFEST_NAME:
        return committed_text.encode("utf-8")
    for asset in committed.get("assets", []):
        filename = asset.get("filename", "")
        if name == filename:
            return None
        if name == checksum_companion_name(filename):
            return f"{asset['sha256']}  {filename}\n".encode("utf-8")
    raise ReleaseError(f"required file {name} is not described by the tag's committed manifest")

def _write_exclusive_no_follow(path: Path, data: bytes) -> None:
    """Create path exclusively; never follow or replace an existing symlink."""
    if path.is_symlink():
        raise ReleaseError(f"refusing to write {path.name} through a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(path, flags, 0o644)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)



def publish_release(
    repo: Path,
    *,
    tag: str,
    assets_dir: Path,
    version: str | None = None,
    source_sha: str | None = None,
    run_id: str | None = None,
    runner=gh_runner,
) -> dict[str, object]:
    """Create or complete the GitHub release for an immutable tag.

    Every decision is bound to the manifest committed at the exact tag: the
    retained artifact's manifest must match it byte for byte, recovered
    archives must match its digests, published assets are verified by
    sha256 digest (never by size alone), and only genuinely missing files are
    attached. New releases are created as drafts and published only once the
    full asset set is attached.
    """
    if not tag_exists(repo, tag):
        raise ReleaseError(f"tag {tag} does not exist; publish-release only completes tagged releases")
    marker = tag_marker(repo, tag)
    if not marker.get("source") or not marker.get("version"):
        raise TagConflictError(
            f"tag {tag} carries no pipeline markers; refusing to publish a release this pipeline does not own"
        )
    version = _marker_consistent("version", version, marker.get("version"))
    _marker_consistent("source", source_sha, marker.get("source"))
    _marker_consistent("build run", run_id, marker.get("build_run"))
    if not version or tag_for_version(version) != tag:
        raise ReleaseError(f"cannot determine a version consistent with tag {tag}")

    committed_text = committed_manifest_text(repo, tag)
    committed = json.loads(committed_text)
    if committed.get("version") != version:
        raise TagConflictError(
            f"tag {tag} committed manifest version {committed.get('version')!r} disagrees with "
            f"its Portmap-Version trailer {version!r}"
        )
    schema_errors = validate_manifest(committed, version, assets_dir, require_files=False)
    if schema_errors:
        raise ReleaseError(
            "the manifest committed at the tag fails release validation: " + "; ".join(schema_errors)
        )
    required = release_file_names(committed)

    if assets_dir.exists():
        local_manifest = assets_dir / MANIFEST_NAME
        if local_manifest.is_symlink():
            raise TagConflictError(
                f"retained artifact manifest {MANIFEST_NAME} is a symlink; refusing to "
                "read or publish through it"
            )
        if local_manifest.is_file():
            if local_manifest.read_text(encoding="utf-8") != committed_text:
                raise TagConflictError(
                    "retained artifact manifest does not match the manifest committed at "
                    f"{tag} byte for byte; refusing to publish mismatched metadata"
                )

    document = gh_release_document(tag, repo, runner)
    if document is None:
        is_draft = False
        missing = list(required)
    else:
        published = {}
        for asset in document.get("assets", []):
            if isinstance(asset, dict) and isinstance(asset.get("name"), str):
                published[asset["name"]] = asset
        is_draft = bool(document.get("draft"))
        for name, asset in published.items():
            if name not in required:
                continue
            expected = _expected_content(committed, name, committed_text)
            local = assets_dir / name
            if expected is None:
                expected_digest = next(
                    entry["sha256"] for entry in committed["assets"] if entry["filename"] == name
                )
            else:
                expected_digest = digest_bytes(expected)
            if _published_digest(asset, name) != expected_digest:
                detail = f"expected sha256 {expected_digest}"
                if expected is None and local.is_file() and not local.is_symlink():
                    detail = f"retained artifact sha256 {digest(local)}"
                raise ReleaseError(
                    f"published asset {name} content differs from the tag's committed manifest "
                    f"({_published_digest(asset, name)}; {detail}); published assets are immutable "
                    "and are never replaced"
                )
        missing = [name for name in required if name not in published]

    uploads: list[Path] = []
    assets_dir.mkdir(parents=True, exist_ok=True)
    for name in missing:
        expected = _expected_content(committed, name, committed_text)
        path = assets_dir / name
        if expected is None:
            # The archive bytes exist only in the retained CI artifact of the
            # recorded run; they must digest-match the committed manifest.
            safe_path, safe_error = _safe_member(assets_dir, name)
            if safe_error:
                raise ReleaseError(
                    f"archive {name} is missing from the retained artifact ({safe_error}); a "
                    "fresh rebuild would produce different bytes, so recovery fails here"
                )
            recorded = next(
                entry["sha256"] for entry in committed["assets"] if entry["filename"] == name
            )
            if digest(safe_path) != recorded:
                raise ReleaseError(
                    f"retained artifact archive {name} does not match the sha256 committed at "
                    f"{tag}; refusing to publish mismatched binaries"
                )
            uploads.append(safe_path)
        else:
            # Manifest and companions are byte-exact derivatives of the tag;
            # they are written authoritatively (never from the artifact) and
            # never through a symlink.
            if path.is_symlink():
                raise ReleaseError(f"refusing to write metadata {name} through a symlink")
            if path.exists():
                if path.read_bytes() != expected:
                    raise TagConflictError(
                        f"retained artifact file {name} differs from the tag-committed content; "
                        "refusing to publish mismatched metadata"
                    )
            else:
                _write_exclusive_no_follow(path, expected)
            uploads.append(path)

    if document is None:
        notes = assets_dir / "release-notes.md"
        _write_exclusive_no_follow(
            notes,
            (
                f"Automated release {tag} (portmap {version}).\n\n"
                f"- Source commit: {marker.get('source', '')}\n"
                f"- Workflow run: {run_id or marker.get('build_run', '')}\n"
                "- Assets: frozen linux-amd64 archive, matching sha256 checksum and the release manifest.\n\n"
                "Published by .github/workflows/release.yml. The Homebrew tap distribution is updated\n"
                "by the reusable release-distribution workflow in the same run.\n"
            ).encode("utf-8"),
        )
        code, _, err = runner(
            [
                "release",
                "create",
                tag,
                "--draft",
                "--title",
                f"portmap {version}",
                "--notes-file",
                str(notes),
                "--verify-tag",
                *[str(path) for path in uploads],
            ],
            repo,
        )
        if code != 0:
            raise ReleaseError(f"gh release create failed for {tag}: {(err or '').strip()}")
        code, _, err = runner(["release", "edit", tag, "--draft=false"], repo)
        if code != 0:
            raise ReleaseError(
                f"gh release edit failed to publish the completed draft for {tag}: {(err or '').strip()}"
            )
        return {"tag": tag, "version": version, "created": True, "uploaded": [p.name for p in uploads]}

    if missing:
        if not is_draft:
            # A published (non-draft) release is already immutable-visible;
            # attaching is attempted exactly once, clearly and without any
            # overwrite. A rejection surfaces as a hard failure.
            print(
                f"release-pipeline: attaching missing files to the already published release "
                f"{tag}: {', '.join(missing)} (no existing asset is ever replaced)"
            )
        code, _, err = runner(["release", "upload", tag, *[str(path) for path in uploads]], repo)
        if code != 0:
            raise ReleaseError(
                f"gh release upload failed for {tag}: {missing}; if the release rejects new "
                "assets, complete recovery from a release owned by this pipeline is impossible "
                f"without rewriting it, so this fails loudly instead: {(err or '').strip()}"
            )
    if is_draft:
        # A verified draft is published regardless of whether this run had to
        # attach anything; an interrupted earlier publish must not leave the
        # complete release invisible forever.
        code, _, err = runner(["release", "edit", tag, "--draft=false"], repo)
        if code != 0:
            raise ReleaseError(
                f"gh release edit failed to publish the completed draft for {tag}: {(err or '').strip()}"
            )
    return {"tag": tag, "version": version, "created": False, "uploaded": [p.name for p in uploads]}


def _repo_root(value: str | None) -> Path:
    return Path(value).resolve() if value else Path.cwd()


def cmd_plan(args: argparse.Namespace) -> int:
    print(json.dumps(plan_release(_repo_root(args.repo_root), args.source_sha), indent=2))
    return 0


def cmd_select_source(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            select_source(_repo_root(args.repo_root), args.input, args.default_sha), indent=2
        )
    )
    return 0


def cmd_bump(args: argparse.Namespace) -> int:
    bump_version_files(_repo_root(args.repo_root), args.version)
    print(json.dumps({"version": args.version, "files": ["pyproject.toml", "src/portmap/__init__.py", "uv.lock"]}, indent=2))
    return 0


def cmd_check_build(args: argparse.Namespace) -> int:
    assets = Path(args.assets_dir).resolve()
    manifest = load_manifest(assets / MANIFEST_NAME)
    errors = validate_manifest(manifest, args.version, assets)
    if errors:
        raise ReleaseError("built release failed validation: " + "; ".join(errors))
    print(json.dumps({"version": args.version, "assets": manifest["assets"]}, indent=2))
    return 0

def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def smoke_build(repo: Path, *, version: str, dist_dir: Path) -> dict[str, object]:
    """Runtime-gate the actual frozen artifact on an owned loopback listener.

    Executes the built binary (never in a write-token job): its reported
    ``--version`` must equal the planned version, and its catalog server must
    serve the canonical rendered project skill over HTTP 200 as Markdown on
    owned loopback ports. Only this spawned process is ever terminated.
    """
    parse_version(version)
    executable = dist_dir / "frozen" / "portmap" / "portmap"
    if not executable.is_file():
        raise ReleaseError(f"frozen executable missing: {executable}")
    template_path = repo / "src" / "portmap" / "catalog_static" / "skills" / "project-integration" / "SKILL.md"
    if not template_path.is_file():
        raise ReleaseError(f"compiled skill asset missing from the checkout: {template_path}")
    expected_body = template_path.read_text(encoding="utf-8").replace("{{PORTMAP_VERSION}}", version)

    reported = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, timeout=120
    )
    if reported.returncode != 0:
        raise ReleaseError(
            f"frozen portmap --version failed ({reported.returncode}): {reported.stderr.strip()}"
        )
    if reported.stdout.strip() != f"portmap {version}":
        raise ReleaseError(
            f"frozen portmap --version reported {reported.stdout.strip()!r}, expected 'portmap {version}'"
        )

    private_port = _free_loopback_port()
    public_port = _free_loopback_port()
    environment = dict(os.environ)
    environment.update(
        {
            "PORTMAP_CATALOG_LISTEN_HOST": "127.0.0.1",
            "PORTMAP_CATALOG_LISTEN_PORT": str(private_port),
            "PORTMAP_CATALOG_PUBLIC_LISTEN_PORT": str(public_port),
        }
    )
    server = subprocess.Popen(
        [str(executable), "_serve-catalog"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if server.poll() is not None:
                _, stderr = server.communicate(timeout=5)
                raise ReleaseError(
                    f"frozen catalog server exited early with {server.returncode}: {stderr.strip()}"
                )
            try:
                with socket.create_connection(("127.0.0.1", private_port), timeout=1):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise ReleaseError("frozen catalog server never started listening on the owned loopback port")

        checked = []
        for port in (private_port, public_port):
            with urlopen(
                f"http://127.0.0.1:{port}/skills/project-integration/SKILL.md", timeout=30
            ) as response:
                status = response.status
                content_type = response.headers.get("content-type", "")
                body = response.read().decode("utf-8")
            if status != 200:
                raise ReleaseError(f"skill route on port {port} returned HTTP {status}, expected 200")
            if not content_type.startswith("text/markdown"):
                raise ReleaseError(f"skill route on port {port} served {content_type!r}, expected Markdown")
            if body != expected_body:
                raise ReleaseError(
                    f"skill route on port {port} did not serve the canonical render of version {version}"
                )
            checked.append(port)
        return {"version": version, "skill_ports": checked}
    finally:
        # Terminate only the process this command spawned.
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=15)


def cmd_smoke_build(args: argparse.Namespace) -> int:
    result = smoke_build(_repo_root(args.repo_root), version=args.version, dist_dir=Path(args.dist_dir).resolve())
    print(json.dumps(result, indent=2))
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    result = prepare_release_commit(
        _repo_root(args.repo_root),
        version=args.version,
        tag=args.tag,
        source_sha=args.source_sha,
        run_id=args.run_id,
        assets_dir=Path(args.assets_dir).resolve(),
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    result = finalize_release(
        _repo_root(args.repo_root),
        version=args.version,
        tag=args.tag,
        source_sha=args.source_sha,
        run_id=args.run_id,
        base_main=args.base_main,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_verify_existing(args: argparse.Namespace) -> int:
    result = verify_existing_release(
        _repo_root(args.repo_root), tag=args.tag, source_sha=args.source_sha, run_id=args.run_id
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_publish_release(args: argparse.Namespace) -> int:
    result = publish_release(
        _repo_root(args.repo_root),
        tag=args.tag,
        assets_dir=Path(args.assets_dir).resolve(),
        version=args.version,
        source_sha=args.source_sha,
        run_id=args.run_id,
    )
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=None, help="validated source repository (defaults to the current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    select = sub.add_parser("select-source", help="validate a requested source commit against protected dev")
    select.add_argument("--input", default=None, help="raw requested sha; empty falls back to --default-sha or the current origin/dev head")
    select.add_argument("--default-sha", default=None, help="explicit fallback commit (push events pin the exact event sha)")
    select.set_defaults(func=cmd_select_source)

    plan = sub.add_parser("plan", help="decide version/tag and idempotency mode")
    plan.add_argument("--source-sha", default=None)
    plan.set_defaults(func=cmd_plan)

    bump = sub.add_parser("bump", help="rewrite version metadata files (no commit)")
    bump.add_argument("--version", required=True)
    bump.set_defaults(func=cmd_bump)

    check = sub.add_parser("check-build", help="validate built dist assets against the planned version")
    check.add_argument("--version", required=True)
    check.add_argument("--assets-dir", default="dist")
    check.set_defaults(func=cmd_check_build)

    smoke = sub.add_parser(
        "smoke-build",
        help="runtime-gate the actual frozen binary on an owned loopback listener (build job only)",
    )
    smoke.add_argument("--version", required=True)
    smoke.add_argument("--dist-dir", default="dist")
    smoke.set_defaults(func=cmd_smoke_build)

    prepare = sub.add_parser("prepare", help="commit generated release state on feat/release-Vx.y")
    prepare.add_argument("--version", required=True)
    prepare.add_argument("--tag", required=True)
    prepare.add_argument("--source-sha", required=True)
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--assets-dir", required=True)
    prepare.set_defaults(func=cmd_prepare)

    finalize = sub.add_parser("finalize", help="merge, tag and atomically push dev/main/tag")
    finalize.add_argument("--version", required=True)
    finalize.add_argument("--tag", required=True)
    finalize.add_argument("--source-sha", required=True)
    finalize.add_argument("--run-id", required=True)
    finalize.add_argument("--base-main", default=None, help="planned origin/main baseline from the plan step")
    finalize.set_defaults(func=cmd_finalize)

    verify = sub.add_parser("verify-existing", help="validate an already tagged release for recovery")
    verify.add_argument("--tag", required=True)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--run-id", required=True)
    verify.set_defaults(func=cmd_verify_existing)

    publish = sub.add_parser("publish-release", help="create or complete the GitHub release without clobbering assets")
    publish.add_argument("--tag", required=True)
    publish.add_argument("--assets-dir", required=True)
    publish.add_argument("--version", default=None)
    publish.add_argument("--source-sha", default=None)
    publish.add_argument("--run-id", default=None)
    publish.set_defaults(func=cmd_publish_release)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except TagConflictError as error:
        print(f"release-pipeline: safety refusal: {error}", file=sys.stderr)
        return EXIT_CONFLICT
    except RemoteAdvancedError as error:
        print(f"release-pipeline: remote advanced: {error}", file=sys.stderr)
        return EXIT_REMOTE_ADVANCED
    except ReleaseError as error:
        print(f"release-pipeline: {error}", file=sys.stderr)
        return EXIT_GENERAL


if __name__ == "__main__":
    raise SystemExit(main())
