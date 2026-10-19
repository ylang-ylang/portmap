#!/usr/bin/env python3
"""Homebrew tap distribution automation for published portmap releases.

Two operations, both driven by the release-distribution workflow:

  verify-release   Confirm an immutable ``V<major>.<minor>`` tag exists, a
                   non-draft/non-prerelease Release is published for it, and
                   the expected ``release.json`` plus linux-amd64 archive
                   assets are present, uploaded, and consistent with the
                   manifest (bounded wait for upload races). The manifest's
                   generated ``source`` block (repository, tag, commit) is
                   bound to the peeled tag commit and that commit must
                   belong to the repository's main history; legacy manifests
                   without ``source`` are only accepted when their bytes
                   exactly match the historical ``src/portmap/release.json``
                   committed at the tag. Emits the verified identity (tag
                   object SHA, peeled commit SHA, release id, archive
                   digest/size) for downstream binding.
  update-tap       Download the EXACT formula source archive, hash its bytes,
                   update only the class-level ``url``/``version``/``sha256``
                   of the tap formula, open (or reuse) a deterministic
                   per-tag PR whose complete diff is exactly that formula
                   change, wait for the required tap check, and merge
                   through the normal GitHub merge API. Never downgrades,
                   never rewrites tags, never adopts foreign branches or PRs,
                   and fails loudly on any error or identity drift.

Standard library only. Tokens are read from the environment and are never
written to logs or exception messages.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

API_BASE_DEFAULT = "https://api.github.com"
GITHUB_HOST = "github.com"
TAP_TOKEN_ENV = "HOMEBREW_TAP_TOKEN"
SOURCE_TOKEN_ENV = "GITHUB_TOKEN"
REQUIRED_CHECK_DEFAULT = "tap-checks"
GZIP_MAGIC = b"\x1f\x8b"
MAX_PAGES = 20

TAG_RE = re.compile(r"V(?P<major>[0-9]+)\.(?P<minor>[0-9]+)")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SHA1_RE = re.compile(r"[0-9a-f]{40}")
FORMULA_PATH_RE = re.compile(r"Formula/[A-Za-z0-9_.-]+\.rb")
FORMULA_KEY_RE = re.compile(
    r'^(?P<indent>[ ]{2})(?P<key>url|version|sha256)(?P<space>[ \t]+)"(?P<value>[^"\n]*)"',
    re.MULTILINE,
)
PASSING_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class ReleaseError(RuntimeError):
    """Any failure that must fail the workflow run with a nonzero exit."""


class InputError(ReleaseError):
    """Bad tag/repo/path/token input (exit code 2)."""


class FormulaError(ReleaseError):
    """The tap formula does not have the expected class-level shape."""


class CheckFailure(ReleaseError):
    """A reported check or status is failing; merging is not allowed."""


class CheckTimeout(ReleaseError):
    """The bounded wait for green checks expired."""


class ApiError(ReleaseError):
    """A GitHub REST call failed (network, auth, merge, rate limit)."""


def log(message: str) -> None:
    print(f"[release-brew] {message}", flush=True)


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------


def parse_tag(tag: str) -> tuple[int, int]:
    match = TAG_RE.fullmatch(tag or "")
    if not match:
        raise InputError(
            f"tag must be V<major>.<minor> (e.g. V0.19), got {tag!r}"
        )
    return int(match.group("major")), int(match.group("minor"))


def validate_repo(repo: str, label: str = "repo") -> str:
    if not REPO_RE.fullmatch(repo or ""):
        raise InputError(f"{label} must be <owner>/<name>, got {repo!r}")
    owner, name = repo.split("/")
    if owner in {".", ".."} or name in {".", ".."}:
        raise InputError(f"{label} must not contain path components, got {repo!r}")
    return repo


def validate_formula_path(path: str) -> str:
    if not FORMULA_PATH_RE.fullmatch(path or "") or ".." in path:
        raise InputError(
            f"formula path must look like Formula/<name>.rb, got {path!r}"
        )
    return path


def validate_sha(value: str, label: str) -> str:
    if not SHA1_RE.fullmatch(value or ""):
        raise InputError(f"{label} must be a 40-hex git SHA, got {value!r}")
    return value


def source_archive_url(source_repo: str, tag: str) -> str:
    return f"https://{GITHUB_HOST}/{source_repo}/archive/refs/tags/{tag}.tar.gz"


def validate_archive_url(url: str, source_repo: str, tag: str) -> str:
    """Strict check of the exact Brew source archive URL we will download."""
    expected = source_archive_url(source_repo, tag)
    parts = urllib.parse.urlsplit(url)
    problems = []
    if parts.scheme != "https":
        problems.append("must use https")
    if parts.hostname != GITHUB_HOST:
        problems.append(f"host must be {GITHUB_HOST}")
    if parts.port is not None:
        problems.append("port must not be present")
    if parts.username or parts.password:
        problems.append("credentials must not be present")
    if parts.query or parts.fragment:
        problems.append("query/fragment must not be present")
    if parts.path != f"/{source_repo}/archive/refs/tags/{tag}.tar.gz":
        problems.append(f"path must be /{source_repo}/archive/refs/tags/{tag}.tar.gz")
    if problems:
        raise InputError(f"invalid source archive url {url!r}: {'; '.join(problems)}")
    return expected


# --------------------------------------------------------------------------
# Release manifest (release.json asset)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseSpec:
    tag: str
    version: str
    archive_filename: str
    archive_sha256: str
    archive_size: int
    source_url: str
    tag_object_sha: str
    commit_sha: str
    release_id: int
    manifest_sha256: str
    manifest_asset_id: int
    archive_asset_id: int
    manifest_source: dict[str, str] | None = None


def expected_archive_filename(version: str) -> str:
    return f"portmap-{version}-linux-amd64.tar.gz"


def parse_manifest_source(
    payload: dict[str, Any], *, source_repo: str, tag: str, version: str
) -> dict[str, str] | None:
    """Validate the optional generated ``source`` identity block.

    New manifests record ``{repository, tag, commit}`` from the paired
    builder flags. Historical manifests (through V0.19) simply omit the
    key — an explicit ``source: null`` is malformed, not legacy; the
    caller must anchor key-absent manifests to the exact tag-committed
    manifest bytes (see :func:`verify_manifest_identity`).
    """
    if "source" not in payload:
        return None
    source = payload["source"]
    if not isinstance(source, dict):
        raise InputError("release.json source must be a JSON object")
    repository = source.get("repository")
    if repository != source_repo:
        raise InputError(
            f"release.json source repository must be {source_repo}, got {repository!r}"
        )
    if source.get("tag") != tag:
        raise InputError(
            f"release.json source tag must be {tag}, got {source.get('tag')!r}"
        )
    commit = source.get("commit")
    if not isinstance(commit, str) or not SHA1_RE.fullmatch(commit):
        raise InputError(
            "release.json source commit must be a 40-hex git SHA, "
            f"got {commit!r}"
        )
    if version != f"{parse_tag(tag)[0]}.{parse_tag(tag)[1]}.0":
        raise InputError(
            f"release.json version {version} does not match tag {tag}"
        )
    return {"repository": repository, "tag": tag, "commit": commit}


def parse_release_manifest(document: str, *, source_repo: str, tag: str) -> dict[str, Any]:
    major, minor = parse_tag(tag)
    try:
        manifest = json.loads(document)
    except json.JSONDecodeError as exc:
        raise InputError(f"release.json is not valid JSON: {exc}") from None
    if not isinstance(manifest, dict):
        raise InputError("release.json must be a JSON object")

    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise InputError(f"release.json version must be X.Y.Z, got {version!r}")
    if version != f"{major}.{minor}.0":
        raise InputError(
            f"release.json version {version} does not match tag {tag} "
            "(tagged releases are exactly major.minor.0)"
        )
    source = parse_manifest_source(manifest, source_repo=source_repo, tag=tag, version=version)

    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise InputError("release.json has no assets")
    matches = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and asset.get("os") == "linux"
        and asset.get("arch") == "amd64"
    ]
    if len(matches) != 1:
        raise InputError(
            f"release.json must have exactly one linux/amd64 asset, found {len(matches)}"
        )
    asset = matches[0]

    filename = asset.get("filename")
    if filename != expected_archive_filename(version):
        raise InputError(
            f"release.json filename must be {expected_archive_filename(version)!r}, got {filename!r}"
        )
    sha256 = asset.get("sha256")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise InputError("release.json asset sha256 must be 64 lowercase hex digits")
    size = asset.get("size")
    if not isinstance(size, int) or size <= 0:
        raise InputError("release.json asset size must be a positive integer")

    source_url = asset.get("source_url")
    if not isinstance(source_url, str):
        raise InputError("release.json asset source_url must be a string")
    parts = urllib.parse.urlsplit(source_url)
    if (
        parts.scheme != "https"
        or parts.hostname != GITHUB_HOST
        or parts.port is not None
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path != f"/{source_repo}/releases/download/{tag}/{filename}"
    ):
        raise InputError(
            f"release.json source_url must be https://github.com/{source_repo}"
            f"/releases/download/{tag}/{filename}, got {source_url!r}"
        )

    return {
        "version": version,
        "filename": filename,
        "sha256": sha256,
        "size": size,
        "source_url": source_url,
        "source": source,
    }


# --------------------------------------------------------------------------
# Formula mutation
# --------------------------------------------------------------------------


def mutate_formula(text: str, *, url: str, version: str, sha256: str) -> str:
    """Replace ONLY the class-level (two-space indented) url/version/sha256.

    The pattern is anchored to the formula body indent, so the deeper
    ``resource`` stanzas (helper URLs and checksums) and every other line
    are preserved byte-for-byte. Replacing with identical values is a
    no-op, which makes retries idempotent.
    """
    replacements = {"url": url, "version": version, "sha256": sha256}
    seen: set[str] = set()

    def substitute(match: re.Match[str]) -> str:
        key = match.group("key")
        if key in seen:
            raise FormulaError(f"class-level {key} is declared more than once")
        seen.add(key)
        return f'{match.group("indent")}{key}{match.group("space")}"{replacements[key]}"'

    mutated = FORMULA_KEY_RE.sub(substitute, text)
    missing = sorted(set(replacements) - seen)
    if missing:
        raise FormulaError(f"formula is missing class-level {', '.join(missing)}")
    return mutated


def parse_formula_version(text: str) -> tuple[int, int, int]:
    match = re.search(r'^[ ]{2}version[ \t]+"([^"\n]+)"', text, re.MULTILINE)
    if not match:
        raise FormulaError("formula has no class-level version")
    version = match.group(1)
    if not VERSION_RE.fullmatch(version):
        raise FormulaError(f"formula version must be X.Y.Z, got {version!r}")
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def version_key(version: str | tuple[int, ...]) -> tuple[int, ...]:
    if isinstance(version, str):
        if not VERSION_RE.fullmatch(version):
            raise InputError(f"version must be X.Y.Z, got {version!r}")
        return tuple(int(part) for part in version.split("."))
    return tuple(version)


def formula_is_current(text: str, *, url: str, version: str, sha256: str) -> bool:
    return mutate_formula(text, url=url, version=version, sha256=sha256) == text


def classify_tap_update(
    current: tuple[int, ...], target: tuple[int, ...]
) -> str:
    if current > target:
        return "superseded"
    return "update"


# --------------------------------------------------------------------------
# Pull request identity and selection
# --------------------------------------------------------------------------


def pull_matches(
    pull: dict[str, Any],
    *,
    tap_repo: str,
    branch: str,
    default_branch: str,
) -> bool:
    """The PR must come from our branch in the tap repo onto the tap default."""
    base = pull.get("base") or {}
    head = pull.get("head") or {}
    base_repo = (base.get("repo") or {}).get("full_name")
    head_repo = (head.get("repo") or {}).get("full_name")
    return (
        base.get("ref") == default_branch
        and base_repo == tap_repo
        and head.get("ref") == branch
        and head_repo == tap_repo
    )


def require_distribution_pr(
    pull: dict[str, Any],
    *,
    tap_repo: str,
    branch: str,
    default_branch: str,
    context: str,
) -> None:
    number = pull.get("number")
    if not pull_matches(
        pull, tap_repo=tap_repo, branch=branch, default_branch=default_branch
    ):
        base = (pull.get("base") or {}).get("ref")
        head = (pull.get("head") or {}).get("ref")
        raise ReleaseError(
            f"{context}: PR #{number} targets {base!r} from {head!r}; "
            f"expected {tap_repo}:{default_branch} from {tap_repo}:{branch}"
        )


def choose_pull_action(pulls: list[dict[str, Any]]) -> tuple[str, int | None]:
    """Decide how to obtain the distribution PR from eligible ones.

    Returns ("reuse", number) for an open PR on our deterministic branch,
    ("reopen", number) for a closed-but-unmerged PR, or ("create", None).
    Merged PRs are never reopened; the default-branch comparison above
    already decided whether anything still needs to happen.
    """
    open_pulls = [pull for pull in pulls if pull.get("state") == "open"]
    if open_pulls:
        return "reuse", int(open_pulls[0]["number"])
    closed_unmerged = [
        pull
        for pull in pulls
        if pull.get("state") == "closed" and not pull.get("merged_at")
    ]
    if closed_unmerged:
        return "reopen", int(closed_unmerged[0]["number"])
    return "create", None


# --------------------------------------------------------------------------
# Check gating
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckVerdict:
    state: str  # "ready" | "waiting" | "failing"
    problems: tuple[str, ...]


def latest_statuses_by_context(
    statuses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reduce a commit's statuses to the latest one per reporting context."""
    latest: dict[str, dict[str, Any]] = {}
    for status in sorted(statuses, key=lambda item: item.get("id", 0)):
        context = status.get("context")
        if context is not None:
            latest[context] = status
    return list(latest.values())


def evaluate_checks(
    check_runs: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    required_name: str,
) -> CheckVerdict:
    """Every reported check/status must pass AND every required-name run
    must have completed with success. Check-run names are not unique
    (several workflows/apps may report the same name), so a second
    failing or pending run named like the required check still blocks.
    """
    problems: list[str] = []
    pending: list[str] = []
    required_seen = False

    for run in check_runs:
        name = run.get("name")
        status = run.get("status")
        conclusion = run.get("conclusion")
        if name == required_name:
            required_seen = True
            if status != "completed":
                pending.append(f"required check {name!r} is {status}")
            elif conclusion != "success":
                problems.append(
                    f"required check {name!r} concluded {conclusion}"
                )
        elif status != "completed":
            pending.append(f"check {name!r} is {status}")
        elif conclusion not in PASSING_CONCLUSIONS:
            problems.append(f"check {name!r} concluded {conclusion}")

    for status_entry in latest_statuses_by_context(statuses):
        context = status_entry.get("context")
        state = status_entry.get("state")
        if state == "pending":
            pending.append(f"status {context!r} is pending")
        elif state != "success":
            problems.append(f"status {context!r} is {state}")

    if not required_seen:
        pending.append(f"required check {required_name!r} has not been reported yet")

    if problems:
        return CheckVerdict("failing", tuple(problems))
    if pending:
        return CheckVerdict("waiting", tuple(pending))
    return CheckVerdict("ready", ())


def wait_for_checks(
    fetch: Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]]]],
    *,
    required_name: str,
    timeout_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> CheckVerdict:
    """Poll check-runs and statuses until green, failing, or timed out."""
    started = now()
    last: CheckVerdict | None = None
    while True:
        check_runs, statuses = fetch()
        last = evaluate_checks(check_runs, statuses, required_name)
        if last.problems:
            log(f"checks failing: {'; '.join(last.problems)}")
        elif last.state == "waiting":
            log(f"checks pending: {'; '.join(last.problems)}")
        if last.state == "failing":
            raise CheckFailure("; ".join(last.problems))
        if last.state == "ready":
            return last
        elapsed = now() - started
        if elapsed >= timeout_seconds:
            raise CheckTimeout(
                f"checks not green after {int(elapsed)}s: {'; '.join(last.problems)}"
            )
        sleep(poll_seconds)


# --------------------------------------------------------------------------
# GitHub REST client (stdlib)
# --------------------------------------------------------------------------


class GitHub:
    def __init__(
        self,
        token: str | None = None,
        api_base: str = API_BASE_DEFAULT,
        timeout: float = 30.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.sleep = sleep

    def request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        headers = {
            "Accept": accept,
            "User-Agent": "portmap-release-brew",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(
                url, data=data, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                return json.loads(body) if body.strip() else {}
            except urllib.error.HTTPError as exc:
                message = _http_error_message(exc)
                if exc.code in RETRYABLE_STATUS and attempt < self.retries:
                    self.sleep(2**attempt)
                    continue
                raise ApiError(
                    f"GitHub API {method} {path} failed with HTTP {exc.code}: {message}"
                ) from None
            except urllib.error.URLError as exc:
                if attempt < self.retries:
                    self.sleep(2**attempt)
                    continue
                raise ReleaseError(
                    f"network error calling {method} {path}: {exc.reason}"
                ) from None
            except TimeoutError:
                if attempt < self.retries:
                    self.sleep(2**attempt)
                    continue
                raise ReleaseError(f"timeout calling {method} {path}") from None
        raise ApiError(f"GitHub API {method} {path} failed after {self.retries} attempts")

    def request_all(
        self, path: str, items_key: str | None = None
    ) -> list[dict[str, Any]]:
        """Collect every page of a paginated list endpoint (bounded)."""
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            separator = "&" if "?" in path else "?"
            payload = self.request(
                "GET", f"{path}{separator}per_page=100&page={page}"
            )
            batch = payload.get(items_key) if items_key else payload
            if not isinstance(batch, list):
                raise ReleaseError(f"unexpected paginated payload for {path}")
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise ReleaseError(f"pagination for {path} exceeded {MAX_PAGES} pages")


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read() or b"{}")
    except (ValueError, OSError):
        return exc.reason
    if isinstance(payload, dict):
        message = payload.get("message")
        if message:
            return str(message)
    return exc.reason


def download_bytes(url: str, *, timeout: float = 120.0) -> bytes:
    """Unauthenticated download (public release assets / source archives)."""
    request = urllib.request.Request(url, headers={"User-Agent": "portmap-release-brew"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise ReleaseError(f"download {url} failed with HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise ReleaseError(f"download {url} failed: {exc.reason}") from None


def download_sha256(url: str) -> str:
    """Hash the actual bytes at url (rejects non-gzip payloads)."""
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "portmap-release-brew"})
    try:
        with urllib.request.urlopen(request, timeout=180.0) as response:
            first = response.read(2)
            if first != GZIP_MAGIC:
                raise ReleaseError(
                    f"downloaded archive {url} is not gzip data; refusing to hash it"
                )
            digest.update(first)
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
    except urllib.error.HTTPError as exc:
        raise ReleaseError(f"download {url} failed with HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise ReleaseError(f"download {url} failed: {exc.reason}") from None
    return digest.hexdigest()


def fetch_file_content(
    github: GitHub, repo: str, path: str, ref: str
) -> tuple[str, str | None]:
    """Return (decoded text, blob sha) for a file at ref, sha None if absent."""
    try:
        payload = github.request(
            "GET", f"/repos/{repo}/contents/{path}?ref={urllib.parse.quote(ref)}"
        )
    except ApiError as exc:
        if "HTTP 404" in str(exc):
            return "", None
        raise
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        raise ReleaseError(f"unexpected contents payload for {repo}/{path}")
    content = payload.get("content") or ""
    text = base64.b64decode("".join(content.splitlines())).decode("utf-8")
    return text, payload.get("sha")


# --------------------------------------------------------------------------
# verify-release
# --------------------------------------------------------------------------


def _asset_by_name(release: dict[str, Any], name: str) -> dict[str, Any] | None:
    for asset in release.get("assets") or []:
        if asset.get("name") == name:
            return asset
    return None


def get_tag_object_sha(github: GitHub, repo: str, tag: str) -> str:
    try:
        ref = github.request("GET", f"/repos/{repo}/git/ref/tags/{tag}")
    except ApiError as exc:
        if "HTTP 404" in str(exc):
            raise ReleaseError(
                f"tag {tag} does not exist in {repo}; this tool never creates "
                "or rewrites tags"
            ) from None
        raise
    obj = ref.get("object") or {}
    sha = obj.get("sha")
    if not isinstance(sha, str) or not SHA1_RE.fullmatch(sha):
        raise ReleaseError(f"tag {tag} has no resolvable object SHA")
    return sha


def peel_tag_commit(github: GitHub, repo: str, tag: str) -> tuple[str, str]:
    """Return (tag object SHA, peeled commit SHA) for an immutable tag."""
    tag_sha = get_tag_object_sha(github, repo, tag)
    ref = github.request("GET", f"/repos/{repo}/git/ref/tags/{tag}")
    obj_type = (ref.get("object") or {}).get("type")
    if obj_type == "commit":
        return tag_sha, tag_sha
    if obj_type == "tag":
        annotated = github.request("GET", f"/repos/{repo}/git/tags/{tag_sha}")
        inner = annotated.get("object") or {}
        commit = inner.get("sha")
        if inner.get("type") != "commit" or not isinstance(commit, str):
            raise ReleaseError(f"annotated tag {tag} does not point at a commit")
        return tag_sha, commit
    raise ReleaseError(f"tag {tag} has unsupported object type {obj_type!r}")


MAIN_BRANCH = "main"


def require_commit_in_main_history(
    github: GitHub, repo: str, *, commit_sha: str
) -> None:
    """The peeled tag commit must belong to the repository's main history.

    ``compare/{commit}...main`` reports ``ahead``/``identical`` exactly when
    the commit is the main head or one of its ancestors: the tag was cut
    from mainline history. The commit need not be the CURRENT head; main may
    have advanced past it. (The repository's default branch is dev; release
    tags are cut from main merges, so the anchor is the literal main ref.)
    """
    try:
        compare = github.request(
            "GET", f"/repos/{repo}/compare/{commit_sha}...{MAIN_BRANCH}"
        )
    except ApiError as exc:
        if "HTTP 404" in str(exc):
            raise ReleaseError(
                f"repository {repo} has no {MAIN_BRANCH} branch to validate "
                f"tag commit {commit_sha} against"
            ) from None
        raise
    status = compare.get("status")
    if status not in {"ahead", "identical"}:
        raise ReleaseError(
            f"tag commit {commit_sha} is not part of {repo}'s {MAIN_BRANCH} "
            f"history (compare status {status!r}); refusing to distribute "
            "history that never merged to main"
        )


def committed_manifest_bytes_at(
    github: GitHub, repo: str, *, tag: str, commit_sha: str
) -> bytes:
    """Exact bytes of the historical src/portmap/release.json at the tagged
    commit. The immutable peeled commit SHA (not the moving tag name) is the
    anchor: this tool never rewrites tags, but only the commit address is
    trusted for content."""
    path = "src/portmap/release.json"
    try:
        payload = github.request(
            "GET",
            f"/repos/{repo}/contents/{path}?ref={urllib.parse.quote(commit_sha)}",
        )
    except ApiError as exc:
        if "HTTP 404" in str(exc):
            raise ReleaseError(
                f"tag {tag} commit {commit_sha[:12]} carries no historical "
                f"{path}; a legacy manifest without source identity cannot be "
                "anchored to it"
            ) from None
        raise
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        raise ReleaseError(f"unexpected contents payload for {repo}/{path}")
    try:
        return base64.b64decode("".join((payload.get("content") or "").splitlines()))
    except (ValueError, TypeError) as exc:
        raise ReleaseError(
            f"historical {path} at {tag} commit {commit_sha[:12]} is not "
            f"decodable: {exc}"
        ) from None


INIT_VERSION_RE = re.compile(
    r'^__version__[ \t]*=[ \t]*["\']([0-9]+\.[0-9]+\.[0-9]+)["\']', re.MULTILINE
)


def tagged_file_text(github: GitHub, repo: str, path: str, *, commit_sha: str) -> str:
    text, _ = fetch_file_content(github, repo, path, commit_sha)
    if not text:
        raise ReleaseError(
            f"tag commit {commit_sha[:12]} carries no readable {path}"
        )
    return text


def verify_tagged_source_version(
    github: GitHub, repo: str, *, tag: str, commit_sha: str, version: str
) -> None:
    """The exact tagged source must declare this release version.

    The standalone distribution dispatch (workflow_dispatch on this workflow)
    bypasses the release pipeline's validator, so both tagged version
    declarations — pyproject.toml ``project.version`` and
    ``src/portmap/__init__.py`` ``__version__`` — are independently read at
    the immutable peeled commit and must equal the manifest version
    (exactly ``major.minor.0`` for the tag).
    """
    pyproject = tagged_file_text(github, repo, "pyproject.toml", commit_sha=commit_sha)
    try:
        declared = tomllib.loads(pyproject)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ReleaseError(
            f"tag {tag} commit {commit_sha[:12]} pyproject.toml has no "
            f"readable project.version: {exc}"
        ) from None
    if declared != version:
        raise ReleaseError(
            f"tag {tag} pyproject.toml declares version {declared!r}, "
            f"not the release version {version!r}; refusing to distribute"
        )
    init_text = tagged_file_text(
        github, repo, "src/portmap/__init__.py", commit_sha=commit_sha
    )
    match = INIT_VERSION_RE.search(init_text)
    if match is None:
        raise ReleaseError(
            f"tag {tag} src/portmap/__init__.py declares no __version__"
        )
    if match.group(1) != version:
        raise ReleaseError(
            f"tag {tag} src/portmap/__init__.py declares __version__ "
            f"{match.group(1)!r}, not the release version {version!r}; "
            "refusing to distribute"
        )


def verify_manifest_identity(
    github: GitHub,
    repo: str,
    tag: str,
    *,
    commit_sha: str,
    manifest: dict[str, Any],
    document: bytes,
) -> None:
    """Bind the manifest's identity to the immutable tag and main history.

    Both manifest generations are held to the same standard: the peeled tag
    commit must belong to the repository's main history, and the exact
    tagged source must declare the release version (checked at the immutable
    commit, never at the moving tag name). On top of that, a generated
    ``source`` block must name this repository, this tag and exactly the
    peeled tag commit, while legacy manifests without ``source`` (published
    through V0.19) are accepted only when the published document is
    byte-for-byte the historical manifest committed at that commit.
    """
    require_commit_in_main_history(github, repo, commit_sha=commit_sha)
    verify_tagged_source_version(
        github, repo, tag=tag, commit_sha=commit_sha, version=manifest["version"]
    )
    source = manifest.get("source")
    if source is not None:
        if source["commit"] != commit_sha:
            raise ReleaseError(
                f"release.json source commit {source['commit']} is not the "
                f"peeled tag commit {commit_sha}; refusing to distribute"
            )
        return
    committed = committed_manifest_bytes_at(github, repo, tag=tag, commit_sha=commit_sha)
    if committed != document:
        raise ReleaseError(
            f"legacy release.json for {tag} has no source identity and does "
            "not match the exact historical tag-committed manifest bytes; "
            "refusing to distribute"
        )



def verify_published_release(
    github: GitHub,
    repo: str,
    tag: str,
    *,
    asset_wait_seconds: float,
    poll_seconds: float,
    digest_of: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> ReleaseSpec:
    """Verify an immutable, published, complete release; return its identity.

    The tag object SHA is captured before anything else and re-checked
    before returning, so the returned identity describes a tag that did
    not move during verification. Uploaded asset state, size, and (when
    ``digest_of`` is given) the binary asset digest are checked against
    the manifest.
    """
    parse_tag(tag)
    tag_object_sha, commit_sha = peel_tag_commit(github, repo, tag)

    started = now()
    last_problem = "release lookup has not run yet"
    while True:
        try:
            release = github.request("GET", f"/repos/{repo}/releases/tags/{tag}")
        except ApiError as exc:
            if "HTTP 404" not in str(exc):
                raise
            release = None

        if release is not None:
            if release.get("draft"):
                raise ReleaseError(
                    f"release {tag} is a draft; publish it before distributing"
                )
            if release.get("prerelease"):
                raise ReleaseError(
                    f"release {tag} is a prerelease; only stable releases are distributed"
                )
            if release.get("tag_name") != tag:
                raise ReleaseError(
                    f"release tag_name {release.get('tag_name')!r} != requested {tag!r}"
                )
            release_id = release.get("id")
            if not isinstance(release_id, int):
                raise ReleaseError(f"release {tag} has no id")

            problems: list[str] = []
            manifest_asset_id: int | None = None
            archive_asset_id: int | None = None
            manifest_sha256: str | None = None
            manifest_asset = _asset_by_name(release, "release.json")
            if manifest_asset is None:
                problems.append("release.json asset is missing")
            else:
                manifest_asset_id = manifest_asset.get("id")
                if not isinstance(manifest_asset_id, int):
                    problems.append("release.json asset has no id")
                    manifest_asset_id = None
                document = download_bytes(
                    str(manifest_asset["browser_download_url"])
                )
                manifest_sha256 = hashlib.sha256(document).hexdigest()
                manifest = parse_release_manifest(
                    document.decode("utf-8"), source_repo=repo, tag=tag
                )
                # Identity binding is permanent, never a retryable race:
                # the manifest's source identity (or, for legacy manifests,
                # the exact tag-committed bytes) must match the peeled tag.
                verify_manifest_identity(
                    github,
                    repo,
                    tag,
                    commit_sha=commit_sha,
                    manifest=manifest,
                    document=document,
                )
                archive_asset = _asset_by_name(release, manifest["filename"])
                if archive_asset is None:
                    problems.append(f"{manifest['filename']} asset is missing")
                else:
                    archive_asset_id = archive_asset.get("id")
                    if not isinstance(archive_asset_id, int):
                        problems.append(f"{manifest['filename']} asset has no id")
                        archive_asset_id = None
                    state = archive_asset.get("state")
                    if state != "uploaded":
                        problems.append(
                            f"{manifest['filename']} upload state is {state!r}"
                        )
                    size = archive_asset.get("size")
                    if isinstance(size, int) and size != manifest["size"]:
                        problems.append(
                            f"{manifest['filename']} uploaded size {size} != manifest {manifest['size']}"
                        )
                    elif not isinstance(size, int):
                        problems.append(f"{manifest['filename']} has no size")
                    if digest_of is not None and state == "uploaded":
                        digest = digest_of(
                            str(archive_asset["browser_download_url"])
                        )
                        if digest != manifest["sha256"]:
                            problems.append(
                                f"{manifest['filename']} digest {digest} != manifest {manifest['sha256']}"
                            )

            if manifest_sha256 is None or manifest_asset_id is None:
                problems.append("release.json identity is incomplete")
            if archive_asset_id is None and not any(
                "asset is missing" in problem for problem in problems
            ):
                problems.append("archive asset identity is incomplete")
            if not problems:
                if get_tag_object_sha(github, repo, tag) != tag_object_sha:
                    raise ReleaseError(
                        f"tag {tag} moved during verification; tags are immutable"
                    )
                fresh_tag_sha, fresh_commit_sha = peel_tag_commit(github, repo, tag)
                if (
                    fresh_tag_sha != tag_object_sha
                    or fresh_commit_sha != commit_sha
                ):
                    raise ReleaseError(
                        f"tag {tag} identity changed during verification; "
                        "tags are immutable"
                    )
                origin = (
                    f"source {manifest['source']['tag']}@{manifest['source']['commit'][:12]}"
                    if manifest.get("source")
                    else "legacy tag-committed manifest"
                )
                log(
                    f"release {tag} verified: version {manifest['version']}, "
                    f"assets release.json + {manifest['filename']} "
                    f"(tag object {tag_object_sha[:12]}, commit {commit_sha[:12]}, "
                    f"{origin})"
                )
                return ReleaseSpec(
                    tag=tag,
                    version=manifest["version"],
                    archive_filename=manifest["filename"],
                    archive_sha256=manifest["sha256"],
                    archive_size=manifest["size"],
                    source_url=manifest["source_url"],
                    tag_object_sha=tag_object_sha,
                    commit_sha=commit_sha,
                    release_id=release_id,
                    manifest_sha256=manifest_sha256,
                    manifest_asset_id=manifest_asset_id,
                    archive_asset_id=archive_asset_id,
                    manifest_source=manifest.get("source"),
                )
            last_problem = "; ".join(problems)
        else:
            last_problem = f"no published release found for tag {tag}"

        elapsed = now() - started
        if elapsed >= asset_wait_seconds:
            raise ReleaseError(
                f"release {tag} incomplete after {int(elapsed)}s: {last_problem}"
            )
        log(f"waiting for release assets ({last_problem})")
        sleep(poll_seconds)


# --------------------------------------------------------------------------
# update-tap
# --------------------------------------------------------------------------


def load_tap_token() -> str:
    token = os.environ.get(TAP_TOKEN_ENV, "").strip()
    if not token:
        raise InputError(
            f"{TAP_TOKEN_ENV} is not set; add the dedicated least-privilege "
            "tap token as a secret on the source repository"
        )
    if any(character.isspace() for character in token):
        raise InputError(f"{TAP_TOKEN_ENV} contains whitespace")
    return token


def branch_name_for(tag: str) -> str:
    return f"brew/portmap-{tag}"


def pull_request_body(
    *, source_repo: str, tag: str, url: str, version: str, sha256: str, required_check: str
) -> str:
    return (
        f"Automated Homebrew tap update for portmap {version}.\n\n"
        f"- Source: {url}\n"
        f"- Version: {version}\n"
        f"- SHA256: `{sha256}` (computed from the downloaded source archive)\n"
        f"- Release: https://github.com/{source_repo}/releases/tag/{tag}\n\n"
        f"Opened by the release-distribution workflow; merges only after the "
        f"required `{required_check}` check is green."
    )


def validate_branch_diff(
    tap: GitHub,
    tap_repo: str,
    *,
    base_sha: str,
    branch: str,
    branch_sha: str,
    formula_path: str,
) -> None:
    """An adopted deterministic branch may differ from base ONLY by the
    formula file; any other change or divergence is rejected, never reset."""
    compare = tap.request(
        "GET", f"/repos/{tap_repo}/compare/{base_sha}...{branch_sha}"
    )
    status = compare.get("status")
    if status == "diverged":
        raise ReleaseError(
            f"branch {branch} diverged from the tap base; refusing to adopt it"
        )
    files = compare.get("files") or []
    if compare.get("files_truncated"):
        raise ReleaseError(
            f"branch {branch} diff is truncated/too large; refusing to adopt it"
        )
    unexpected = sorted({file.get("filename") for file in files} - {formula_path})
    if unexpected:
        raise ReleaseError(
            f"branch {branch} carries unrelated changes ({', '.join(unexpected)}); "
            "refusing to adopt it"
        )


def verify_default_formula(
    tap: GitHub,
    tap_repo: str,
    formula_path: str,
    default_branch: str,
    *,
    url: str,
    version: str,
    sha256: str,
) -> None:
    text, _ = fetch_file_content(tap, tap_repo, formula_path, default_branch)
    if not formula_is_current(text, url=url, version=version, sha256=sha256):
        raise ReleaseError(
            f"{default_branch}:{formula_path} does not serve {version} "
            f"with sha256 {sha256}"
        )


def check_identity_binding(
    spec: ReleaseSpec,
    *,
    expected_tag_sha: str | None,
    expected_commit_sha: str | None,
    expected_archive_sha256: str | None,
    expected_archive_size: int | None,
    expected_release_id: int | None = None,
    expected_manifest_sha256: str | None = None,
    expected_manifest_asset_id: int | None = None,
    expected_archive_asset_id: int | None = None,
) -> None:
    """The identities verified by the verify-release job must still hold."""
    mismatches = []
    if expected_tag_sha is not None and spec.tag_object_sha != expected_tag_sha:
        mismatches.append(
            f"tag object sha {spec.tag_object_sha} != verified {expected_tag_sha}"
        )
    if expected_commit_sha is not None and spec.commit_sha != expected_commit_sha:
        mismatches.append(
            f"tag commit sha {spec.commit_sha} != verified {expected_commit_sha}"
        )
    if expected_release_id is not None and spec.release_id != expected_release_id:
        mismatches.append(
            f"release id {spec.release_id} != verified {expected_release_id}"
        )
    if (
        expected_manifest_sha256 is not None
        and spec.manifest_sha256 != expected_manifest_sha256
    ):
        mismatches.append(
            f"release.json body sha256 {spec.manifest_sha256} != verified {expected_manifest_sha256}"
        )
    if (
        expected_manifest_asset_id is not None
        and spec.manifest_asset_id != expected_manifest_asset_id
    ):
        mismatches.append(
            f"release.json asset id {spec.manifest_asset_id} != verified {expected_manifest_asset_id}"
        )
    if (
        expected_archive_asset_id is not None
        and spec.archive_asset_id != expected_archive_asset_id
    ):
        mismatches.append(
            f"{spec.archive_filename} asset id {spec.archive_asset_id} != verified {expected_archive_asset_id}"
        )
    if (
        expected_archive_sha256 is not None
        and spec.archive_sha256 != expected_archive_sha256
    ):
        mismatches.append(
            f"manifest archive sha256 {spec.archive_sha256} != verified {expected_archive_sha256}"
        )
    if (
        expected_archive_size is not None
        and spec.archive_size != expected_archive_size
    ):
        mismatches.append(
            f"manifest archive size {spec.archive_size} != verified {expected_archive_size}"
        )
    if mismatches:
        raise ReleaseError(
            "release identity changed since verification: " + "; ".join(mismatches)
        )


def reverify_release_identity(
    source: GitHub, source_repo: str, spec: ReleaseSpec
) -> None:
    """Re-fetch the published release and its manifest and compare the
    WHOLE verified identity immediately before merge.

    A replaced asset (new id) or release.json body is rejected even when
    the archive hash is unchanged, as are draft/prerelease flips. This
    binds the merge to the exact release the verify job confirmed.
    """
    try:
        release = source.request(
            "GET", f"/repos/{source_repo}/releases/tags/{spec.tag}"
        )
    except ApiError as exc:
        if "HTTP 404" in str(exc):
            raise ReleaseError(
                f"release {spec.tag} disappeared before merge"
            ) from None
        raise
    if release.get("draft"):
        raise ReleaseError(f"release {spec.tag} became a draft before merge")
    if release.get("prerelease"):
        raise ReleaseError(f"release {spec.tag} became a prerelease before merge")
    if release.get("tag_name") != spec.tag or release.get("id") != spec.release_id:
        raise ReleaseError(f"release {spec.tag} identity changed before merge")

    manifest_asset = _asset_by_name(release, "release.json")
    if manifest_asset is None or manifest_asset.get("id") != spec.manifest_asset_id:
        raise ReleaseError(
            "release.json asset was replaced before merge; refusing to distribute"
        )
    archive_asset = _asset_by_name(release, spec.archive_filename)
    if archive_asset is None or archive_asset.get("id") != spec.archive_asset_id:
        raise ReleaseError(
            f"{spec.archive_filename} asset was replaced before merge; "
            "refusing to distribute"
        )
    if archive_asset.get("state") != "uploaded":
        raise ReleaseError(
            f"{spec.archive_filename} is no longer uploaded before merge"
        )
    if archive_asset.get("size") != spec.archive_size:
        raise ReleaseError(
            f"{spec.archive_filename} size changed before merge"
        )

    body = download_bytes(str(manifest_asset["browser_download_url"]))
    if hashlib.sha256(body).hexdigest() != spec.manifest_sha256:
        raise ReleaseError(
            "release.json content changed before merge; refusing to distribute"
        )
    manifest = parse_release_manifest(
        body.decode("utf-8"), source_repo=source_repo, tag=spec.tag
    )
    # Re-run the full source-identity binding against the freshly downloaded
    # document: source commit/peeled-tag binding and main history for new
    # manifests, exact tag-committed bytes for legacy ones.
    verify_manifest_identity(
        source,
        source_repo,
        spec.tag,
        commit_sha=spec.commit_sha,
        manifest=manifest,
        document=body,
    )
    if manifest != {
        "version": spec.version,
        "filename": spec.archive_filename,
        "sha256": spec.archive_sha256,
        "size": spec.archive_size,
        "source_url": spec.source_url,
        "source": spec.manifest_source,
    }:
        raise ReleaseError(
            "release manifest no longer matches the verified release"
        )


def update_tap(
    *,
    source_repo: str,
    tap_repo: str,
    tag: str,
    formula_path: str,
    required_check: str,
    check_timeout_seconds: float,
    poll_seconds: float,
    tap: GitHub,
    source: GitHub,
    sha256_of: Callable[[str], str],
    digest_of: Callable[[str], str] | None = None,
    expected_tag_sha: str | None = None,
    expected_commit_sha: str | None = None,
    expected_archive_sha256: str | None = None,
    expected_archive_size: int | None = None,
    expected_release_id: int | None = None,
    expected_manifest_sha256: str | None = None,
    expected_manifest_asset_id: int | None = None,
    expected_archive_asset_id: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> int:
    parse_tag(tag)
    archive_url = validate_archive_url(source_archive_url(source_repo, tag), source_repo, tag)

    spec = verify_published_release(
        source,
        source_repo,
        tag,
        asset_wait_seconds=0,
        poll_seconds=poll_seconds,
        digest_of=digest_of,
        sleep=sleep,
        now=now,
    )
    check_identity_binding(
        spec,
        expected_tag_sha=expected_tag_sha,
        expected_commit_sha=expected_commit_sha,
        expected_archive_sha256=expected_archive_sha256,
        expected_archive_size=expected_archive_size,
        expected_release_id=expected_release_id,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_manifest_asset_id=expected_manifest_asset_id,
        expected_archive_asset_id=expected_archive_asset_id,
    )

    sha256 = sha256_of(archive_url)
    log(f"hashed {archive_url} -> sha256 {sha256}")
    if get_tag_object_sha(source, source_repo, tag) != spec.tag_object_sha:
        raise ReleaseError(
            f"tag {tag} moved while the source archive was downloaded"
        )

    repo_info = tap.request("GET", f"/repos/{tap_repo}")
    default_branch = str(repo_info["default_branch"])

    # Capture the base commit first, then read the formula AT that commit so
    # the update decision and the branch base describe the same snapshot.
    base_ref = tap.request(
        "GET", f"/repos/{tap_repo}/git/ref/heads/{default_branch}"
    )
    base_sha = str(base_ref["object"]["sha"])

    current_text, _ = fetch_file_content(tap, tap_repo, formula_path, base_sha)
    current_version = parse_formula_version(current_text)
    target_version = version_key(spec.version)
    if classify_tap_update(current_version, target_version) == "superseded":
        log(
            f"tap already at {current_version} > {spec.version}; refusing to downgrade"
        )
        return 0
    if current_version == target_version:
        if formula_is_current(
            current_text, url=archive_url, version=spec.version, sha256=sha256
        ):
            log(f"tap formula already distributes {spec.version}; nothing to do")
            return 0
        raise ReleaseError(
            f"tap already declares version {spec.version} with a different "
            "url/sha256; the identity of a shipped version is immutable "
            "(tags never move), refusing to replace it"
        )

    target_text = mutate_formula(
        current_text, url=archive_url, version=spec.version, sha256=sha256
    )

    branch = branch_name_for(tag)
    branch_ref_path = f"/repos/{tap_repo}/git/ref/heads/{branch}"
    try:
        head_sha = str(tap.request("GET", branch_ref_path)["object"]["sha"])
        log(f"validating existing deterministic branch {branch}")
        validate_branch_diff(
            tap,
            tap_repo,
            base_sha=base_sha,
            branch=branch,
            branch_sha=head_sha,
            formula_path=formula_path,
        )
    except ApiError as exc:
        if "HTTP 404" not in str(exc):
            raise
        tap.request(
            "POST",
            f"/repos/{tap_repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        head_sha = base_sha
        log(f"created deterministic branch {branch} at {base_sha[:12]}")

    branch_text, branch_blob_sha = fetch_file_content(tap, tap_repo, formula_path, branch)
    if branch_text != target_text:
        put = tap.request(
            "PUT",
            f"/repos/{tap_repo}/contents/{formula_path}",
            {
                "message": f"portmap {spec.version} ({tag})",
                "content": base64.b64encode(target_text.encode("utf-8")).decode("ascii"),
                "branch": branch,
                "sha": branch_blob_sha,
            },
        )
        commit_sha = (put.get("commit") or {}).get("sha")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise ReleaseError(
                f"contents update on {branch} did not return a commit SHA; "
                "refusing to guess the branch head"
            )
        # Gate on exactly the commit we created; a concurrent push to the
        # branch is never adopted.
        head_sha = commit_sha
        log(f"updated {formula_path} on {branch} at {head_sha[:12]}")

    pulls = tap.request_all(
        "/repos/{}/pulls?head={}&state=all&base={}".format(
            tap_repo,
            urllib.parse.quote(f"{tap_repo.split('/')[0]}:{branch}", safe=""),
            urllib.parse.quote(default_branch, safe=""),
        )
    )
    eligible = [
        pull
        for pull in pulls
        if pull_matches(
            pull, tap_repo=tap_repo, branch=branch, default_branch=default_branch
        )
    ]
    action, existing_number = choose_pull_action(eligible)
    if action == "reuse":
        number = existing_number
        log(f"reusing open PR #{number}")
    elif action == "reopen":
        number = existing_number
        tap.request("PATCH", f"/repos/{tap_repo}/pulls/{number}", {"state": "open"})
        log(f"reopened PR #{number}")
    else:
        created = tap.request(
            "POST",
            f"/repos/{tap_repo}/pulls",
            {
                "title": f"portmap {spec.version} ({tag})",
                "head": branch,
                "base": default_branch,
                "body": pull_request_body(
                    source_repo=source_repo,
                    tag=tag,
                    url=archive_url,
                    version=spec.version,
                    sha256=sha256,
                    required_check=required_check,
                ),
            },
        )
        number = int(created["number"])
        log(f"opened PR #{number}")

    pull = tap.request("GET", f"/repos/{tap_repo}/pulls/{number}")
    require_distribution_pr(
        pull,
        tap_repo=tap_repo,
        branch=branch,
        default_branch=default_branch,
        context="discovered distribution PR",
    )
    if pull.get("merged_at"):
        log(f"PR #{number} was merged concurrently")
        verify_default_formula(
            tap, tap_repo, formula_path, default_branch,
            url=archive_url, version=spec.version, sha256=sha256,
        )
        return 0
    if pull.get("mergeable") is False:
        raise CheckFailure(
            f"PR #{number} has conflicts with {default_branch}; resolve manually"
        )
    if str(pull["head"]["sha"]) != head_sha:
        raise ReleaseError(
            f"PR #{number} head {pull['head']['sha']} is not the commit we pushed "
            f"({head_sha}); refusing to gate on a different SHA"
        )

    head_sha = str(pull["head"]["sha"])
    log(f"waiting for required check {required_check!r} on {head_sha[:12]}")
    wait_for_checks(
        lambda: (
            tap.request_all(
                f"/repos/{tap_repo}/commits/{head_sha}/check-runs", "check_runs"
            ),
            tap.request_all(f"/repos/{tap_repo}/commits/{head_sha}/statuses"),
        ),
        required_name=required_check,
        timeout_seconds=check_timeout_seconds,
        poll_seconds=poll_seconds,
        sleep=sleep,
        now=now,
    )

    # --- pre-merge revalidation: nothing may have shifted while waiting ---
    pull = tap.request("GET", f"/repos/{tap_repo}/pulls/{number}")
    require_distribution_pr(
        pull,
        tap_repo=tap_repo,
        branch=branch,
        default_branch=default_branch,
        context="pre-merge PR revalidation",
    )
    if pull.get("merged_at"):
        verify_default_formula(
            tap, tap_repo, formula_path, default_branch,
            url=archive_url, version=spec.version, sha256=sha256,
        )
        return 0
    if str(pull["head"]["sha"]) != head_sha:
        raise ReleaseError(
            f"PR #{number} head moved from {head_sha} to {pull['head']['sha']} "
            "while checks were evaluated; refusing to merge"
        )

    current_default_sha = str(
        tap.request("GET", f"/repos/{tap_repo}/git/ref/heads/{default_branch}")[
            "object"
        ]["sha"]
    )
    if current_default_sha != base_sha:
        text_now, _ = fetch_file_content(
            tap, tap_repo, formula_path, current_default_sha
        )
        version_now = parse_formula_version(text_now)
        if version_now > target_version:
            raise ReleaseError(
                f"{default_branch} advanced to {version_now} while merging "
                f"{spec.version}; refusing a downgrade merge"
            )
        if version_now == target_version and formula_is_current(
            text_now, url=archive_url, version=spec.version, sha256=sha256
        ):
            log(
                f"{default_branch} already serves {spec.version}; nothing to merge"
            )
            return 0

    if get_tag_object_sha(source, source_repo, tag) != spec.tag_object_sha:
        raise ReleaseError(
            f"tag {tag} moved before merge; refusing to distribute"
        )
    reverify_release_identity(source, source_repo, spec)

    files = tap.request_all(f"/repos/{tap_repo}/pulls/{number}/files")
    unexpected = sorted({file.get("filename") for file in files} - {formula_path})
    if unexpected:
        raise ReleaseError(
            f"PR #{number} carries unrelated changes ({', '.join(unexpected)}); "
            "refusing to merge"
        )
    head_text, _ = fetch_file_content(tap, tap_repo, formula_path, head_sha)
    if head_text != target_text:
        raise ReleaseError(
            f"PR #{number} head formula content drifted from the expected update"
        )

    merge = tap.request(
        "PUT",
        f"/repos/{tap_repo}/pulls/{number}/merge",
        {"sha": head_sha, "merge_method": "merge"},
    )
    if not merge.get("merged"):
        raise ReleaseError(f"merge of PR #{number} did not report success: {merge}")
    merged_pull = tap.request("GET", f"/repos/{tap_repo}/pulls/{number}")
    if not merged_pull.get("merged_at"):
        raise ReleaseError(f"PR #{number} is not merged after merge call")

    verify_default_formula(
        tap, tap_repo, formula_path, default_branch,
        url=archive_url, version=spec.version, sha256=sha256,
    )
    log(
        f"merged PR #{number}: {formula_path} now serves {spec.version} "
        f"from {archive_url} (sha256 {sha256})"
    )
    return 0


def required_name_check_safe(required_check: str) -> str:
    if not required_check or any(character.isspace() for character in required_check):
        raise InputError(f"required check name is invalid: {required_check!r}")
    return required_check


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_brew.py",
        description="Homebrew tap distribution automation for portmap releases",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser(
        "verify-release", help="verify a published release and its assets"
    )
    verify.add_argument("--repo", required=True, help="source repository owner/name")
    verify.add_argument("--tag", required=True, help="release tag, e.g. V0.19")
    verify.add_argument(
        "--asset-wait-minutes",
        type=float,
        default=10.0,
        help="bounded wait for release assets to finish uploading (default: 10)",
    )
    verify.add_argument(
        "--poll-seconds", type=float, default=15.0, help="poll interval (default: 15)"
    )
    verify.add_argument(
        "--skip-asset-digest",
        action="store_true",
        help="skip downloading the binary asset to verify its digest against release.json",
    )

    update = subparsers.add_parser(
        "update-tap", help="update the tap formula via PR and merge after checks"
    )
    update.add_argument("--source-repo", required=True)
    update.add_argument("--tag", required=True, help="release tag, e.g. V0.19")
    update.add_argument("--tap-repo", required=True)
    update.add_argument(
        "--formula-path", default="Formula/portmap.rb", help="tap formula path"
    )
    update.add_argument(
        "--required-check",
        default=REQUIRED_CHECK_DEFAULT,
        help="check-run name that must complete successfully (default: tap-checks)",
    )
    update.add_argument(
        "--check-timeout-minutes",
        type=float,
        default=20.0,
        help="bounded wait for green checks before failing (default: 20)",
    )
    update.add_argument(
        "--poll-seconds", type=float, default=20.0, help="poll interval (default: 20)"
    )
    update.add_argument(
        "--expected-tag-sha",
        default=None,
        help="tag object SHA verified by verify-release; mismatch fails the run",
    )
    update.add_argument(
        "--expected-commit-sha",
        default=None,
        help="peeled tag commit SHA verified by verify-release; mismatch fails the run",
    )
    update.add_argument(
        "--expected-archive-sha256",
        default=None,
        help="manifest archive sha256 verified by verify-release; mismatch fails the run",
    )
    update.add_argument(
        "--expected-archive-size",
        type=int,
        default=None,
        help="manifest archive size verified by verify-release; mismatch fails the run",
    )
    update.add_argument(
        "--expected-release-id",
        type=int,
        default=None,
        help="release id verified by verify-release; mismatch fails the run",
    )
    update.add_argument(
        "--expected-manifest-sha256",
        default=None,
        help="release.json body sha256 verified by verify-release; mismatch fails the run",
    )
    update.add_argument(
        "--expected-manifest-asset-id",
        type=int,
        default=None,
        help="release.json asset id verified by verify-release; mismatch fails the run",
    )
    update.add_argument(
        "--expected-archive-asset-id",
        type=int,
        default=None,
        help="archive asset id verified by verify-release; mismatch fails the run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify-release":
            repo = validate_repo(args.repo)
            github = GitHub(token=os.environ.get(SOURCE_TOKEN_ENV) or None)
            spec = verify_published_release(
                github,
                repo,
                args.tag,
                asset_wait_seconds=max(0.0, args.asset_wait_minutes * 60),
                poll_seconds=max(1.0, args.poll_seconds),
                digest_of=None if args.skip_asset_digest else download_sha256,
            )
            print(
                json.dumps(
                    {
                        "tag": spec.tag,
                        "version": spec.version,
                        "archive": spec.archive_filename,
                        "archive_sha256": spec.archive_sha256,
                        "archive_size": spec.archive_size,
                        "tag_object_sha": spec.tag_object_sha,
                        "commit_sha": spec.commit_sha,
                        "release_id": spec.release_id,
                        "manifest_sha256": spec.manifest_sha256,
                        "manifest_asset_id": spec.manifest_asset_id,
                        "archive_asset_id": spec.archive_asset_id,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return 0
        if args.command == "update-tap":
            required_name_check_safe(args.required_check)
            source_repo = validate_repo(args.source_repo, "source repo")
            tap_repo = validate_repo(args.tap_repo, "tap repo")
            validate_formula_path(args.formula_path)
            parse_tag(args.tag)
            if args.expected_tag_sha is not None:
                validate_sha(args.expected_tag_sha, "--expected-tag-sha")
            if args.expected_commit_sha is not None:
                validate_sha(args.expected_commit_sha, "--expected-commit-sha")
            if args.expected_manifest_sha256 is not None and not SHA256_RE.fullmatch(
                args.expected_manifest_sha256
            ):
                raise InputError(
                    "--expected-manifest-sha256 must be 64 lowercase hex digits"
                )
            tap = GitHub(token=load_tap_token())
            source = GitHub(token=os.environ.get(SOURCE_TOKEN_ENV) or None)
            return update_tap(
                source_repo=source_repo,
                tap_repo=tap_repo,
                tag=args.tag,
                formula_path=args.formula_path,
                required_check=args.required_check,
                check_timeout_seconds=max(0.0, args.check_timeout_minutes * 60),
                poll_seconds=max(1.0, args.poll_seconds),
                tap=tap,
                source=source,
                sha256_of=download_sha256,
                digest_of=download_sha256,
                expected_tag_sha=args.expected_tag_sha,
                expected_commit_sha=args.expected_commit_sha,
                expected_archive_sha256=args.expected_archive_sha256,
                expected_archive_size=args.expected_archive_size,
                expected_release_id=args.expected_release_id,
                expected_manifest_sha256=args.expected_manifest_sha256,
                expected_manifest_asset_id=args.expected_manifest_asset_id,
                expected_archive_asset_id=args.expected_archive_asset_id,
            )
    except InputError as exc:
        log(f"input error: {exc}")
        return 2
    except ReleaseError as exc:
        log(f"error: {exc}")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
