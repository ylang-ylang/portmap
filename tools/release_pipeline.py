#!/usr/bin/env python3
"""Tag-driven immutable release pipeline for portmap.

Driven by ``.github/workflows/release.yml`` from exactly two entrances:

* **push of a ``v<major>.<minor>.<patch>`` tag** — the full first publication:
  validate the tag, rebuild and test the exact tagged commit, build the
  frozen archive with paired provenance flags, runtime-smoke the real
  binary and publish the GitHub release (draft, complete, publish).
* **``workflow_dispatch`` with an existing tag** — verify/complete/retry of
  an already tagged release. Publication is idempotent: complete published
  releases verify to a no-op and the run finishes the Brew distribution.
  Historical ``V0.0`` through ``V0.19`` tags are accepted only here, and
  only when the release already exists; they are never built or newly published.
  New lowercase versions must be above 0.19.0; older immutable package
  versions cannot be republished under alternate lowercase tags.

The pipeline NEVER allocates versions, bumps files, writes branches,
commits, merges or tags. Tags are created and pushed by maintainers from
history already merged into ``main``; this automation only publishes what
an immutable tag pins.

Security model
--------------
* Orchestration code (this script) always runs from the protected ``dev``
  revision resolved by the workflow through the GitHub API — never from
  the event tag or any caller ref — in a full trusted clone that carries
  ``origin/main`` and all tags.
* ``validate-tag`` peels the event tag and refuses it unless the peeled
  commit is part of ``origin/main`` history and the ``pyproject.toml``
  committed at that commit is exactly ``<major>.<minor>.<patch>``. This runs
  BEFORE any tag-sourced code (npm/pytest/docker) is checked out or
  executed.
* Source code executes only in the read-only build job (``contents: read``,
  no persisted credentials); it never sees the publication token.
* New releases publish a BUILD-GENERATED manifest that is never committed
  to the source: it must carry the top-level provenance block
  ``source = {repository, tag, commit}`` matching the tag identity, and it
  is validated against the actual built files, not against any
  tag-committed generated checksum.
* Historical releases (e.g. the immutable V0.19) committed their manifest
  at the tag; their retries anchor to those exact tag-committed manifest
  bytes plus the actual published asset digests and are never rewritten.
* Published assets are verified by sha256 digest (never by size alone) and
  are never replaced. New releases are created as drafts, completed with
  the full asset set and only then published. Retries of published
  releases never depend on CI artifacts or rebuilds; only completing an
  unfinished draft recovers the original run's retained artifact bytes.

Exit codes: 0 success, 1 general failure, 2 safety/conflict refusal.
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
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.request import urlopen

TRAILER_SOURCE = "Portmap-Source"
TRAILER_BUILD_RUN = "Portmap-Build-Run"
TRAILER_BUILD_ATTEMPT = "Portmap-Build-Attempt"
TRAILER_MANIFEST_SHA = "Portmap-Manifest-Sha256"
TRAILER_VERSION = "Portmap-Version"

GITHUB_REPO = "ylang-ylang/portmap"
ALLOWED_PLATFORMS = {("linux", "amd64"), ("linux", "arm64")}
MANIFEST_NAME = "release.json"
COMMITTED_MANIFEST_PATH = "src/portmap/release.json"
# Basenames are restricted to the canonical archive/manifest/companion forms;
# no separators, dot segments, whitespace, glob or gh label metacharacters.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ARCHIVE_NAME = re.compile(r"portmap-[0-9]+\.[0-9]+\.[0-9]+-[a-z0-9]+-[a-z0-9]+\.tar\.gz")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
COMMIT_HEX = re.compile(r"[0-9a-f]{40}")

EXIT_GENERAL = 1
EXIT_CONFLICT = 2


class ReleaseError(RuntimeError):
    """Release pipeline failure with a user-facing explanation."""


class TagConflictError(ReleaseError):
    """A published artifact conflicts with the requested release."""


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


def is_legacy_tag(tag: str) -> bool:
    """Only the immutable historical V0.0 through V0.19 namespace."""
    return re.fullmatch(r"V0\.(?:0|[1-9]|1[0-9])", tag) is not None


def parse_tag(tag: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag)
    if match:
        version = tuple(int(part) for part in match.groups())
        return version if version > (0, 19, 0) else None
    if is_legacy_tag(tag):
        return 0, int(tag[3:]), 0
    return None


def version_for_tag(tag: str) -> str:
    version = parse_tag(tag)
    if version is None:
        raise ReleaseError(
            f"tag {tag!r} must be v<major>.<minor>.<patch> above 0.19.0 or historical V0.0..V0.19"
        )
    return ".".join(str(part) for part in version)


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


def validate_manifest(
    manifest: dict, version: str, assets_dir: Path, *, tag: str, require_files: bool = True
) -> list[str]:
    """Validate a manifest against the release version and asset directory.

    With ``require_files`` the referenced archive and its checksum companion
    must exist as safe regular files whose bytes match the manifest exactly.
    """
    errors: list[str] = []
    if version_for_tag(tag) != version:
        errors.append(f"tag {tag!r} does not match expected version {version!r}")
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


def validate_source_identity(manifest: dict, tag: str, commit: str) -> None:
    """Require the build-generated provenance block to match the tag identity."""
    source = manifest.get("source")
    expected = {"repository": GITHUB_REPO, "tag": tag, "commit": commit}
    if not isinstance(source, dict) or source != expected:
        raise ReleaseError(
            f"manifest source identity {source!r} does not match the release identity {expected!r}; "
            "new manifests are generated by the build from the exact tagged commit and never committed to source"
        )


# --- tag validation (trusted bootstrap) -------------------------------------


def tag_exists(repo: Path, tag: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}", check=False).returncode == 0


def resolve_tag_commit(repo: Path, tag: str) -> str:
    """Peel a tag to its exact 40-hex commit id."""
    peeled = git_text(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    if not COMMIT_HEX.fullmatch(peeled):
        raise ReleaseError(f"tag {tag} does not peel to a full 40-hex commit id")
    return peeled


def committed_manifest_text(repo: Path, tag: str) -> str | None:
    """Bytes (as text) of the historical manifest committed at the tag, if any."""
    result = git(repo, "show", f"{tag}:{COMMITTED_MANIFEST_PATH}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def committed_file(repo: Path, commit: str, path: str) -> str | None:
    """File content committed at a commit, or None when the path is absent."""
    result = git(repo, "show", f"{commit}:{path}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout


INIT_VERSION_RE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
LOCK_VERSION_RE = re.compile(r'^name = "portmap"\nversion = "([^"]+)"$', re.MULTILINE)


def source_version_problems(repo: Path, commit: str, version: str) -> list[str]:
    """Every committed source-version marker that disagrees with the tag.

    pyproject.toml and src/portmap/__init__.py must both carry exactly the
    release version; a committed uv.lock, when present, must agree too (a
    repository without uv.lock at that commit is accepted).
    """
    problems: list[str] = []
    pyproject = committed_file(repo, commit, "pyproject.toml")
    if pyproject is None:
        problems.append("pyproject.toml is missing")
    else:
        try:
            committed = str(tomllib.loads(pyproject)["project"]["version"])
        except (tomllib.TOMLDecodeError, KeyError):
            problems.append("pyproject.toml has no readable project.version")
        else:
            if committed != version:
                problems.append(f"pyproject.toml version {committed!r}")
    init = committed_file(repo, commit, "src/portmap/__init__.py")
    if init is None:
        problems.append("src/portmap/__init__.py is missing")
    else:
        match = INIT_VERSION_RE.search(init)
        if match is None:
            problems.append('src/portmap/__init__.py has no __version__ = "..." line')
        elif match.group(1) != version:
            problems.append(f"src/portmap/__init__.py __version__ {match.group(1)!r}")
    lock = committed_file(repo, commit, "uv.lock")
    if lock is not None:
        match = LOCK_VERSION_RE.search(lock)
        if match is None:
            problems.append("uv.lock carries no portmap package version")
        elif match.group(1) != version:
            problems.append(f"uv.lock portmap version {match.group(1)!r}")
    return problems


def validate_tag(
    repo: Path, *, event_name: str, event_tag: str, event_sha: str | None = None
) -> dict[str, object]:
    """Validate the event tag against remote main before any tag code runs.

    Runs on the trusted automation checkout (protected dev revision, full
    clone with tags). The tag must peel to a commit that is already part of
    ``origin/main`` history and the pyproject version committed at that
    commit must be exactly the full ``major.minor.patch`` tag version.
    Historical uppercase tags are retry-only through explicit dispatch.
    Push events additionally require the event sha to be the peeled commit.
    """
    tag = event_tag or ""
    if parse_tag(tag) is None:
        raise ReleaseError(
            f"event tag {tag!r} must be v<major>.<minor>.<patch> or historical V0.0..V0.19; refusing to release"
        )
    if is_legacy_tag(tag) and event_name != "workflow_dispatch":
        raise ReleaseError(f"historical tag {tag} is retry-only through workflow_dispatch")
    if not tag_exists(repo, tag):
        raise ReleaseError(
            f"tag {tag} does not exist at the trusted automation revision; there is nothing to release"
        )
    commit = resolve_tag_commit(repo, tag)
    if event_name == "push":
        expected = (event_sha or "").strip().lower()
        if expected != commit:
            raise ReleaseError(
                f"push event sha {expected!r} is not the peeled commit of tag {tag} ({commit})"
            )
    main_ref = "refs/remotes/origin/main"
    if git(repo, "rev-parse", "--verify", "--quiet", main_ref, check=False).returncode != 0:
        raise ReleaseError(
            "origin/main is unavailable from the trusted checkout; cannot validate tag ancestry"
        )
    if git(repo, "merge-base", "--is-ancestor", commit, main_ref, check=False).returncode != 0:
        raise ReleaseError(
            f"tag {tag} peels to {commit}, which is not part of origin/main history; "
            "refusing to release foreign history"
        )
    version = version_for_tag(tag)
    problems = source_version_problems(repo, commit, version)
    if problems:
        raise ReleaseError(
            f"source version metadata at {commit[:12]} disagrees with tag {tag}, which "
            f"requires exactly {version!r}: " + "; ".join(problems)
        )
    historical = is_legacy_tag(tag) and committed_manifest_text(repo, tag) is not None
    return {
        "tag": tag,
        "version": version,
        "commit": commit,
        "historical": historical,
        "event": event_name,
    }

def plan_run(
    repo: Path,
    *,
    event_name: str,
    event_tag: str,
    event_sha: str | None = None,
    runner=None,
) -> dict[str, object]:
    """Validate the tag and decide the run mode from the RELEASE STATE.

    The mode is decided from what actually exists on GitHub — never from the
    event type — so a re-run of the original push after publication, and a
    dispatch of an already released tag, never rebuild published bytes:

    * ``build``  — no release exists yet (genuine HTTP 404 only): the exact
      tagged commit is built, smoked and published for the first time. This
      covers both the tag push and an initial dispatch of a valid existing
      lowercase main tag whose first publication never completed. An absent
      historical uppercase release is always refused, never rebuilt.
    * ``verify`` — a release (draft or published) already exists: no source
      checkout or build; the publish job verifies/completes it from the
      anchor manifest and the original attempt's retained artifact.

    A non-404 API failure (403, 5xx, network) is a hard error and is never
    interpreted as an absent release.
    """
    runner = gh_runner if runner is None else runner
    validated = validate_tag(
        repo, event_name=event_name, event_tag=event_tag, event_sha=event_sha
    )
    document = gh_release_document(str(validated["tag"]), repo, runner)
    release_present = document is not None
    if not release_present and is_legacy_tag(str(validated["tag"])):
        raise ReleaseError(
            f"historical tag {validated['tag']} has no existing release; "
            "refusing new publication or rebuild of legacy tags"
        )
    return {
        **validated,
        "mode": "verify" if release_present else "build",
        "release_present": release_present,
        "is_draft": bool(document.get("draft")) if release_present else False,
    }


def check_catalog(repo: Path) -> dict[str, object]:
    """Require a fresh catalog rebuild to equal the tagged committed tree.

    ``npm ci && npm run build`` runs in the tagged checkout first; Brew
    installs the GitHub tag SOURCE archive, so a stale committed
    ``catalog_static`` tree would ship an old UI to source/Brew users while
    the frozen bundle ships the new one. Any rebuilt difference fails the
    release before publication. Maintainers must run
    ``npm ci && npm run build`` before committing and tagging.
    """
    static = repo / "src" / "portmap" / "catalog_static"
    required = ("skills/project-integration/SKILL.md", "assets/catalog.js")
    missing = [name for name in required if not (static / name).is_file()]
    if missing:
        raise ReleaseError(
            "compiled catalog assets missing from the tagged tree: "
            + ", ".join(missing)
            + "; maintainers must run npm ci && npm run build before committing and tagging"
        )
    rows = git(repo, "status", "--porcelain", "-uall", "--", "src/portmap/catalog_static").stdout
    changes = [line for line in rows.splitlines() if line.strip()]
    if changes:
        raise ReleaseError(
            "rebuilding the catalog changed the tagged committed tree ("
            + "; ".join(change.strip() for change in changes)
            + "); maintainers must run npm ci && npm run build before committing and tagging "
            "so Brew/source installs ship the same UI as the frozen bundle"
        )
    return {"catalog_static": "identical", "checked": list(required)}


def check_source_clean(repo: Path) -> dict[str, object]:
    """Reject tracked source drift before the frozen build (exact tag bytes).

    ``git diff --quiet HEAD --`` semantics: any modification to a TRACKED
    file (staged or not) fails the run — tests or the catalog rebuild must
    not have changed the tagged tree the archive is built from. Untracked
    build outputs (node_modules, dist) stay usable and are not considered.
    """
    if git(repo, "diff", "--quiet", "HEAD", "--", check=False).returncode != 0:
        details = git_text(repo, "diff", "--name-status", "HEAD", "--")
        raise ReleaseError(
            "tracked source drifted from the tagged commit during the build job ("
            + "; ".join(line.strip() for line in details.splitlines() if line.strip())
            + "); the frozen archive must be built from the exact tagged tree"
        )
    return {"tracked_source": "identical-to-tag"}

# --- gh boundary -------------------------------------------------------------


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


def gh_download_manifest_asset(tag: str, repo: Path, runner=gh_runner) -> str:
    """Download the published manifest asset bytes (drafts included)."""
    with tempfile.TemporaryDirectory(prefix="portmap-anchor-") as temporary:
        dest = Path(temporary)
        code, _, err = runner(
            ["release", "download", tag, "--pattern", MANIFEST_NAME, "--dir", str(dest), "--clobber"],
            repo,
        )
        if code != 0:
            raise ReleaseError(
                f"downloading the {MANIFEST_NAME} asset of release {tag} failed: {(err or '').strip()}"
            )
        try:
            return (dest / MANIFEST_NAME).read_text(encoding="utf-8")
        except OSError as error:
            raise ReleaseError(f"release {tag} manifest download produced no file: {error}") from error


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


def _expected_content(anchor: dict, name: str, anchor_text: str) -> bytes | None:
    """Bytes a published/recovered file must have, derived from the anchor.

    Archives cannot be derived (their bytes exist only in the original
    build's retained artifact and are digest-bound instead); the manifest
    and the checksum companions are byte-exact derivatives of the anchor.
    """
    if name == MANIFEST_NAME:
        return anchor_text.encode("utf-8")
    for asset in anchor.get("assets", []):
        filename = asset.get("filename", "")
        if name == filename:
            return None
        if name == checksum_companion_name(filename):
            return f"{asset['sha256']}  {filename}\n".encode("utf-8")
    raise ReleaseError(f"required file {name} is not described by the release manifest anchor")


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


def marker_from_message(message: str) -> dict[str, str]:
    marker: dict[str, str] = {}
    for line in message.splitlines():
        match = re.fullmatch(
            r"Portmap-(Source|Build-Run|Build-Attempt|Manifest-Sha256|Version): (\S+)",
            line.strip(),
        )
        if match:
            key = match.group(1).lower().replace("-", "_")
            marker[key] = match.group(2)
    return marker


def release_file_names(manifest: dict) -> list[str]:
    names: list[str] = []
    for asset in manifest.get("assets", []):
        filename = asset.get("filename", "")
        if filename:
            names.extend([filename, checksum_companion_name(filename)])
    names.append(MANIFEST_NAME)
    return list(dict.fromkeys(names))


# --- publication -------------------------------------------------------------


def _published_assets(document: dict | None) -> dict[str, dict]:
    if document is None:
        return {}
    return {
        asset["name"]: asset
        for asset in document.get("assets", [])
        if isinstance(asset, dict) and isinstance(asset.get("name"), str)
    }


def _verify_published(
    published: dict[str, dict], anchor: dict, anchor_text: str, required: list[str]
) -> list[str]:
    """Verify published assets by actual digest; return the missing names."""
    for name, asset in published.items():
        if name not in required:
            continue
        expected = _expected_content(anchor, name, anchor_text)
        if expected is None:
            expected_digest = next(
                entry["sha256"] for entry in anchor["assets"] if entry["filename"] == name
            )
        else:
            expected_digest = digest_bytes(expected)
        actual = _published_digest(asset, name)
        if actual != expected_digest:
            raise ReleaseError(
                f"published asset {name} digest {actual} differs from the release anchor "
                f"({expected_digest}); published assets are immutable and are never replaced"
            )
    return [name for name in required if name not in published]


def _anchor_state(
    repo: Path, tag: str, *, assets_dir: Path | None, runner=gh_runner
) -> dict[str, object]:
    """Resolve the authoritative manifest anchor and the release state.

    The tag is FULLY revalidated first (existence, peel, origin/main
    ancestry and committed source versions) so every mutation decision —
    including retries from a fresh publish checkout — re-checks the tag
    against main history before anything is written.

    Anchor precedence:
      * historical tags (a manifest committed at the tag, e.g. V0.19):
        the exact tag-committed manifest bytes — immutable, never rewritten;
      * an existing release whose manifest asset is present: those exact
        published manifest bytes;
      * no release at all: the local build-generated manifest (first
        publication, identity-validated against the tag);
      * a draft whose manifest asset upload never completed: the original
        attempt's retained artifact manifest, authenticated against the
        manifest digest bound into the draft's own release notes at
        creation time (never a rebuild, never a deletion).
    """
    validated = validate_tag(repo, event_name="workflow_dispatch", event_tag=tag)
    commit = str(validated["commit"])
    version = str(validated["version"])
    historical_text = committed_manifest_text(repo, tag) if is_legacy_tag(tag) else None
    document = gh_release_document(tag, repo, runner)
    if document is None and is_legacy_tag(tag):
        raise ReleaseError(
            f"historical tag {tag} has no existing release; "
            "refusing new publication or rebuild of legacy tags"
        )
    published = _published_assets(document)
    is_draft = bool(document is not None and document.get("draft"))
    marker = (
        marker_from_message(str(document.get("body") or "")) if document is not None else {}
    )

    anchor_text: str | None = None
    anchor_kind: str | None = None
    if historical_text is not None:
        anchor_text, anchor_kind = historical_text, "historical"
    elif document is not None and MANIFEST_NAME in published:
        anchor_text, anchor_kind = gh_download_manifest_asset(tag, repo, runner), "release"
    elif document is None and assets_dir is not None:
        local = assets_dir / MANIFEST_NAME
        if local.is_file() and not local.is_symlink():
            anchor_text, anchor_kind = local.read_text(encoding="utf-8"), "build"
    elif (
        document is not None
        and is_draft
        and marker.get("build_run")
        and marker.get("build_attempt")
        and marker.get("manifest_sha256")
    ):
        # The draft was created but its manifest asset upload never finished;
        # the original attempt's retained manifest is authenticated by the
        # digest bound into the draft notes at creation time. Never deleted,
        # never rebuilt. Without a local asset directory (inspect mode) this
        # is reported as a recoverable state instead of an error so the
        # workflow can fetch the retained artifact first.
        if assets_dir is None:
            return {
                "tag": tag,
                "version": version,
                "commit": commit,
                "historical": False,
                "anchor": None,
                "anchor_json": None,
                "anchor_text": None,
                "document": document,
                "is_draft": True,
                "missing": [],
                "manifest_asset_present": False,
                "needs_manifest_recovery": True,
                "origin_run_id": marker.get("build_run"),
                "origin_run_attempt": marker.get("build_attempt"),
                "manifest_sha256": marker.get("manifest_sha256"),
            }
        local = assets_dir / MANIFEST_NAME
        if not local.is_file() or local.is_symlink():
            raise ReleaseError(
                f"draft {tag} lacks its manifest asset and the original attempt's retained "
                "artifact manifest is not available; retry while the "
                f"release-assets-{marker['build_run']}-{marker['build_attempt']} artifact "
                "is retained — published bytes are never rebuilt or deleted"
            )
        recovered = local.read_text(encoding="utf-8")
        if digest_bytes(recovered.encode("utf-8")) != marker["manifest_sha256"]:
            raise ReleaseError(
                f"recovered manifest bytes for draft {tag} do not match the digest recorded "
                "in the draft notes; a different build can never replace release bytes"
            )
        anchor_text, anchor_kind = recovered, "recovered"

    if anchor_text is None:
        if document is not None:
            raise TagConflictError(
                f"release {tag} carries no manifest asset and no authenticatable manifest is "
                "available for it; refusing to publish metadata this pipeline cannot bind"
            )
        return {
            "tag": tag,
            "version": version,
            "commit": commit,
            "historical": False,
            "anchor": None,
            "anchor_json": None,
            "anchor_text": None,
            "document": None,
            "is_draft": False,
            "missing": [],
            "manifest_asset_present": False,
            "needs_manifest_recovery": False,
            "origin_run_id": None,
            "origin_run_attempt": None,
            "manifest_sha256": None,
        }

    try:
        anchor = json.loads(anchor_text)
    except json.JSONDecodeError as error:
        raise TagConflictError(f"manifest anchor for {tag} is not valid JSON: {error}") from error
    if not isinstance(anchor, dict):
        raise TagConflictError(f"manifest anchor for {tag} is not a JSON object")
    errors = validate_manifest(anchor, version, assets_dir or Path("."), tag=tag, require_files=False)
    if errors:
        raise ReleaseError(
            f"the manifest anchor for {tag} fails release validation: " + "; ".join(errors)
        )
    if anchor_kind != "historical":
        validate_source_identity(anchor, tag, commit)
    required = release_file_names(anchor)
    missing = _verify_published(published, anchor, anchor_text, required)
    if document is not None and not is_draft and missing:
        raise ReleaseError(
            f"published release {tag} is incomplete (missing {', '.join(missing)}); published "
            "releases are immutable — assets are never attached, rebuilt or replaced"
        )
    return {
        "tag": tag,
        "version": version,
        "commit": commit,
        "historical": anchor_kind == "historical",
        "anchor": anchor_kind,
        "anchor_json": anchor,
        "anchor_text": anchor_text,
        "document": document,
        "is_draft": is_draft,
        "missing": missing,
        "manifest_asset_present": MANIFEST_NAME in published,
        "needs_manifest_recovery": False,
        "origin_run_id": marker.get("build_run"),
        "origin_run_attempt": marker.get("build_attempt"),
        "manifest_sha256": marker.get("manifest_sha256"),
    }


def inspect_release(repo: Path, *, tag: str, runner=gh_runner) -> dict[str, object]:
    """Read-only release state for retry orchestration."""
    state = _anchor_state(repo, tag, assets_dir=None, runner=runner)
    recover_artifact = None
    if (
        state["document"] is not None
        and state["is_draft"]
        and (state["missing"] or not state["manifest_asset_present"])
        and state["origin_run_id"]
        and state["origin_run_attempt"]
    ):
        recover_artifact = (
            f"release-assets-{state['origin_run_id']}-{state['origin_run_attempt']}"
        )
    return {
        "tag": state["tag"],
        "version": state["version"],
        "commit": state["commit"],
        "historical": state["historical"],
        "anchor": state["anchor"],
        "release_present": state["document"] is not None,
        "is_draft": state["is_draft"],
        "missing": list(state["missing"]),
        "manifest_asset_present": state["manifest_asset_present"],
        "origin_run_id": state["origin_run_id"],
        "origin_run_attempt": state["origin_run_attempt"],
        "recover_artifact": recover_artifact,
    }


def _release_notes(
    assets_dir: Path,
    tag: str,
    version: str,
    commit: str,
    run_id: str,
    run_attempt: str | None,
    manifest_sha256: str,
) -> Path:
    attempt_note = f" (attempt {run_attempt})" if run_attempt else ""
    attempt_trailer = f"{TRAILER_BUILD_ATTEMPT}: {run_attempt}\n" if run_attempt else ""
    body = (
        f"Automated release {tag} (portmap {version}).\n\n"
        f"- Source commit: {commit}\n"
        f"- Workflow run: {run_id}{attempt_note}\n"
        "- Assets: frozen linux-amd64 archive, matching sha256 checksum and the release manifest.\n\n"
        f"{TRAILER_VERSION}: {version}\n"
        f"{TRAILER_SOURCE}: {commit}\n"
        f"{TRAILER_BUILD_RUN}: {run_id}\n"
        f"{attempt_trailer}"
        f"{TRAILER_MANIFEST_SHA}: {manifest_sha256}\n"
        "\nPublished by .github/workflows/release.yml. The Homebrew tap distribution is updated\n"
        "by the reusable release-distribution workflow in the same run.\n"
    )
    notes = assets_dir / "release-notes.md"
    _write_exclusive_no_follow(notes, body.encode("utf-8"))
    return notes


def publish_release(
    repo: Path,
    *,
    tag: str,
    assets_dir: Path,
    run_id: str,
    run_attempt: str | None = None,
    runner=gh_runner,
) -> dict[str, object]:
    """Create or complete the GitHub release for an immutable tag.

    Every decision is bound to the manifest anchor resolved by
    ``_anchor_state``: recovered archives must match its digests, the
    manifest and checksum companions are byte-exact derivatives of it, and
    published assets are verified by actual sha256 digest (never by size
    alone) and are never replaced. New releases are created as drafts with
    the full asset set and published only once complete. Completing an
    unfinished draft recovers the original run's retained artifact bytes; a
    published release with missing assets is a hard failure (immutable
    releases are never rebuilt).
    """
    assets_dir.mkdir(parents=True, exist_ok=True)
    state = _anchor_state(repo, tag, assets_dir=assets_dir, runner=runner)
    if state["anchor"] is None:
        raise ReleaseError(
            f"no release exists for {tag} and this run carries no build manifest; "
            "first publication must run from the tag push (workflow_dispatch only "
            "verifies and completes existing releases) — re-run the original run"
        )
    anchor_json = state["anchor_json"]
    anchor_text = state["anchor_text"]
    document = state["document"]
    is_draft = state["is_draft"]
    missing = list(state["missing"])
    version = str(state["version"])
    commit = str(state["commit"])

    uploads: list[Path] = []
    for name in missing:
        expected = _expected_content(anchor_json, name, anchor_text)
        path = assets_dir / name
        if expected is None:
            safe_path, safe_error = _safe_member(assets_dir, name)
            if safe_error:
                raise ReleaseError(
                    f"archive {name} is unavailable ({safe_error}); published releases are "
                    "immutable and never rebuilt, and drafts recover only from the original "
                    "run's retained artifact"
                )
            recorded = next(
                entry["sha256"] for entry in anchor_json["assets"] if entry["filename"] == name
            )
            if digest(safe_path) != recorded:
                raise ReleaseError(
                    f"local archive {name} does not match the anchor sha256 {recorded}; a fresh "
                    "rebuild can never replace release bytes"
                )
            uploads.append(safe_path)
        else:
            if path.is_symlink():
                raise ReleaseError(f"refusing to write metadata {name} through a symlink")
            if path.exists():
                if path.read_bytes() != expected:
                    raise TagConflictError(
                        f"local file {name} differs from the anchor content; refusing mismatched metadata"
                    )
            else:
                _write_exclusive_no_follow(path, expected)
            uploads.append(path)

    if document is None:
        notes = _release_notes(
            assets_dir,
            tag,
            version,
            commit,
            run_id,
            run_attempt,
            digest_bytes(str(anchor_text).encode("utf-8")),
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
    elif uploads:
        if not is_draft:
            # Unreachable for complete-by-construction pipelines (published
            # releases with missing assets fail in _anchor_state), kept as a
            # hard guard: published releases are immutable, nothing is ever
            # attached to them.
            raise ReleaseError(
                f"refusing to attach files to the already published release {tag}; "
                "published releases are immutable"
            )
        code, _, err = runner(["release", "upload", tag, *[str(path) for path in uploads]], repo)
        if code != 0:
            raise ReleaseError(
                f"gh release upload failed for {tag}: {missing}; {(err or '').strip()}"
            )

    # Final completeness gate with actual digests before anything is (or was)
    # flipped out of draft.
    final = gh_release_document(tag, repo, runner)
    if final is None:
        raise ReleaseError(f"release {tag} disappeared during publication")
    missing_after = _verify_published(
        _published_assets(final), anchor_json, anchor_text, release_file_names(anchor_json)
    )
    if missing_after:
        raise ReleaseError(
            f"release {tag} is still missing required assets after publication: "
            + ", ".join(missing_after)
        )
    if bool(final.get("draft")):
        code, _, err = runner(["release", "edit", tag, "--draft=false"], repo)
        if code != 0:
            raise ReleaseError(
                f"gh release edit failed to publish the completed draft for {tag}: {(err or '').strip()}"
            )
    return {
        "tag": tag,
        "version": version,
        "commit": commit,
        "anchor": state["anchor"],
        "created": document is None,
        "uploaded": [path.name for path in uploads],
    }


# --- build smoke gate --------------------------------------------------------


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def smoke_build(repo: Path, *, version: str, dist_dir: Path) -> dict[str, object]:
    """Runtime-gate the actual frozen artifact on an owned loopback listener.

    Executes the built binary (never in a write-token job): its reported
    ``--version`` must equal the release version, and its catalog server
    must serve the canonical rendered project skill over HTTP 200 as
    Markdown on owned loopback ports. Only this spawned process is ever
    terminated.
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


# --- CLI ---------------------------------------------------------------------


def _repo_root(value: str | None) -> Path:
    return Path(value).resolve() if value else Path.cwd()


def cmd_validate_tag(args: argparse.Namespace) -> int:
    result = validate_tag(
        _repo_root(args.repo_root),
        event_name=args.event_name,
        event_tag=args.event_tag,
        event_sha=args.event_sha,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_plan_run(args: argparse.Namespace) -> int:
    result = plan_run(
        _repo_root(args.repo_root),
        event_name=args.event_name,
        event_tag=args.event_tag,
        event_sha=args.event_sha,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_check_source(args: argparse.Namespace) -> int:
    print(json.dumps(check_source_clean(_repo_root(args.repo_root)), indent=2))
    return 0


def cmd_check_catalog(args: argparse.Namespace) -> int:
    print(json.dumps(check_catalog(_repo_root(args.repo_root)), indent=2))
    return 0


def cmd_check_build(args: argparse.Namespace) -> int:
    result = check_build(
        Path(args.assets_dir).resolve(), tag=args.tag, commit=args.commit
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_smoke_build(args: argparse.Namespace) -> int:
    result = smoke_build(
        _repo_root(args.repo_root), version=args.version, dist_dir=Path(args.dist_dir).resolve()
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_inspect_release(args: argparse.Namespace) -> int:
    result = inspect_release(_repo_root(args.repo_root), tag=args.tag)
    print(json.dumps(result, indent=2))
    return 0


def cmd_publish_release(args: argparse.Namespace) -> int:
    result = publish_release(
        _repo_root(args.repo_root),
        tag=args.tag,
        assets_dir=Path(args.assets_dir).resolve(),
        run_id=args.run_id,
        run_attempt=args.run_attempt,
    )
    print(json.dumps(result, indent=2))
    return 0


def check_build(assets_dir: Path, *, tag: str, commit: str) -> dict[str, object]:
    """Validate the build-generated manifest against the tag identity.

    New manifests are generated by the builder from the exact tagged commit
    (paired ``--release-tag``/``--source-commit`` flags) and are validated
    here against the tag identity and the actual built files — never
    against a tag-committed generated checksum.
    """
    if parse_tag(tag) is None or is_legacy_tag(tag):
        raise ReleaseError(f"new builds require v<major>.<minor>.<patch>, got {tag!r}")
    if not COMMIT_HEX.fullmatch(commit or ""):
        raise ReleaseError(f"source commit {commit!r} is not a full 40-hex commit id")
    version = version_for_tag(tag)
    manifest = load_manifest(assets_dir / MANIFEST_NAME)
    validate_source_identity(manifest, tag, commit)
    errors = validate_manifest(manifest, version, assets_dir, tag=tag)
    if errors:
        raise ReleaseError("built release failed validation: " + "; ".join(errors))
    return {"tag": tag, "version": version, "commit": commit, "assets": manifest["assets"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=None, help="trusted checkout with tags and origin/main (defaults to the current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-tag", help="validate the event tag against remote main before any tag code runs")
    validate.add_argument("--event-name", required=True, choices=["push", "workflow_dispatch"])
    validate.add_argument("--event-tag", required=True, help="vX.Y.Z above 0.19.0; historical V0.0..V0.19 only with explicit dispatch")
    validate.add_argument("--event-sha", default=None, help="push events pin the exact event commit")
    validate.set_defaults(func=cmd_validate_tag)

    plan = sub.add_parser(
        "plan-run",
        help="validate the tag and select build vs verify from the actual release state (404-only absence)",
    )
    plan.add_argument("--event-name", required=True, choices=["push", "workflow_dispatch"])
    plan.add_argument("--event-tag", required=True, help="vX.Y.Z above 0.19.0; historical V0.0..V0.19 requires an existing release")
    plan.add_argument("--event-sha", default=None, help="push events pin the exact event commit")
    plan.set_defaults(func=cmd_plan_run)

    source = sub.add_parser(
        "check-source",
        help="reject tracked source drift from the tagged commit before the frozen build",
    )
    source.set_defaults(func=cmd_check_source)

    catalog = sub.add_parser("check-catalog", help="require a fresh catalog rebuild to equal the tagged committed tree")
    catalog.set_defaults(func=cmd_check_catalog)

    check = sub.add_parser("check-build", help="validate the build-generated manifest and assets against the tag identity")
    check.add_argument("--tag", required=True, help="new lowercase vX.Y.Z tag above 0.19.0 (legacy builds forbidden)")
    check.add_argument("--commit", required=True)
    check.add_argument("--assets-dir", default="dist")
    check.set_defaults(func=cmd_check_build)

    smoke = sub.add_parser(
        "smoke-build",
        help="runtime-gate the actual frozen binary on an owned loopback listener (build job only)",
    )
    smoke.add_argument("--version", required=True)
    smoke.add_argument("--dist-dir", default="dist")
    smoke.set_defaults(func=cmd_smoke_build)

    inspect = sub.add_parser("inspect-release", help="read-only state of a tagged release for retry orchestration")
    inspect.add_argument("--tag", required=True)
    inspect.set_defaults(func=cmd_inspect_release)

    publish = sub.add_parser("publish-release", help="create or complete the GitHub release without clobbering assets")
    publish.add_argument("--tag", required=True)
    publish.add_argument("--assets-dir", required=True)
    publish.add_argument("--run-id", required=True)
    publish.add_argument("--run-attempt", default=None)
    publish.set_defaults(func=cmd_publish_release)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except TagConflictError as error:
        print(f"release-pipeline: safety refusal: {error}", file=sys.stderr)
        return EXIT_CONFLICT
    except ReleaseError as error:
        print(f"release-pipeline: {error}", file=sys.stderr)
        return EXIT_GENERAL


if __name__ == "__main__":
    raise SystemExit(main())
