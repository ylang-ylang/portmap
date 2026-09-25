"""Standalone client download publishing for the catalog web server.

Serves the versioned client release manifest, streams cached client archives
and renders the maintained installer shell template. Every archive served
through this module is pinned by ``client_release.json``: filenames are only
ever resolved through the manifest allowlist, cache entries and freshly
downloaded artifacts are checksum-verified before they are exposed, and
network fetches are restricted to the pinned trusted GitHub release hosts.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import __version__

CLIENT_NAME = "portmap-client"
RELEASE_MANIFEST_PATH = Path(__file__).with_name("client_release.json")
INSTALL_SCRIPT_PATH = Path(__file__).with_name("download_assets") / "install-client.sh"
INSTALL_SCRIPT_CONTENT_TYPE = "text/x-shellscript; charset=utf-8"
CLIENT_DOWNLOADS_PREFIX = "/downloads/client/"
ARCHIVE_CONTENT_TYPE = "application/gzip"
CACHE_DIR_NAME = "client-downloads"

# Manifest source URLs must point at the project's GitHub release downloads;
# redirects are additionally allowed onto GitHub's asset CDN hosts.
TRUSTED_SOURCE_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
MANIFEST_SOURCE_HOST = "github.com"
TRUSTED_SOURCE_PATH_PREFIX = "/ylang-ylang/portmap/releases/download/"

SUPPORTED_PLATFORM_OS = frozenset({"linux", "darwin"})
SUPPORTED_PLATFORM_ARCH = frozenset({"amd64", "arm64"})
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ARCHIVE_FILENAME_RE = re.compile(
    r"portmap-client-(?P<version>\d+\.\d+\.\d+)-(?P<os>[a-z0-9]+)-(?P<arch>[a-z0-9]+)\.tar\.gz"
)

MIN_ARCHIVE_SIZE = 1
MAX_ARCHIVE_SIZE = 512 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 120
# Build baseline of the current Linux artifacts (Debian 11 / glibc 2.31);
# surfaced informationally per platform and enforced by the installer.
GLIBC_MINIMUM = "2.31"
REQUIREMENTS_BY_OS: dict[str, str | None] = {
    "linux": f"glibc {GLIBC_MINIMUM}+ (musl/Alpine unsupported)",
    "darwin": None,
}


class ClientDownloadUnavailable(Exception):
    """A client download request cannot be served; carries an HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class ClientAsset:
    os: str
    arch: str
    filename: str
    sha256: str
    size: int
    source_url: str

    @property
    def platform(self) -> tuple[str, str]:
        return (self.os, self.arch)

    def payload(self) -> dict[str, Any]:
        return {
            "os": self.os,
            "arch": self.arch,
            "filename": self.filename,
            "sha256": self.sha256,
            "size": self.size,
            "url": f"{CLIENT_DOWNLOADS_PREFIX}{self.filename}",
            "requirements": REQUIREMENTS_BY_OS.get(self.os),
        }


@dataclass(frozen=True)
class ClientRelease:
    version: str
    assets: tuple[ClientAsset, ...]

    def asset_for(self, filename: str) -> ClientAsset | None:
        for asset in self.assets:
            if asset.filename == filename:
                return asset
        return None

    def payload(self) -> dict[str, Any]:
        return {
            "name": CLIENT_NAME,
            "version": self.version,
            "available": True,
            "message": None,
            "platforms": [asset.payload() for asset in self.assets],
            "install_script": "/install-client.sh",
        }


def unavailable_payload(message: str) -> dict[str, Any]:
    return {
        "name": CLIENT_NAME,
        "version": None,
        "available": False,
        "message": message,
        "platforms": [],
        "install_script": None,
    }


def validate_trusted_source_url(url: str) -> None:
    """Allow only clean https URLs on the trusted GitHub release hosts."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError(f"source URL must use https: {url!r}")
    if "@" in parsed.netloc or ":" in parsed.netloc:
        raise ValueError(f"source URL must not carry credentials or a port: {url!r}")
    host = (parsed.hostname or "").lower()
    if host not in TRUSTED_SOURCE_HOSTS:
        raise ValueError(f"source URL host is not trusted: {host!r}")
    if not parsed.path or parsed.path == "/":
        raise ValueError(f"source URL has no asset path: {url!r}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"source URL must not include query or fragment: {url!r}")


def validate_manifest_source_url(url: str, *, filename: str) -> None:
    """Manifest entries pin the canonical GitHub release download URL."""
    validate_trusted_source_url(url)
    parsed = urllib.parse.urlsplit(url)
    if (parsed.hostname or "").lower() != MANIFEST_SOURCE_HOST:
        raise ValueError(f"source URL must be hosted on {MANIFEST_SOURCE_HOST}: {url!r}")
    if not parsed.path.startswith(TRUSTED_SOURCE_PATH_PREFIX):
        raise ValueError(f"source URL is outside the project release downloads: {url!r}")
    if parsed.path.rsplit("/", 1)[-1] != filename:
        raise ValueError(f"source URL does not reference {filename}: {url!r}")


def _parse_asset(entry: Any, *, version: str, index: int) -> ClientAsset:
    if not isinstance(entry, dict):
        raise ValueError(f"asset #{index} must be a JSON object")

    def text(key: str) -> str:
        value = entry.get(key)
        if not isinstance(value, str):
            raise ValueError(f"asset #{index} field {key!r} must be a string")
        return value

    os_name = text("os")
    arch = text("arch")
    filename = text("filename")
    sha256 = text("sha256")
    source_url = text("source_url")
    size = entry.get("size")
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError(f"asset #{index} field 'size' must be an integer")
    if os_name not in SUPPORTED_PLATFORM_OS:
        raise ValueError(f"asset #{index} has unsupported os: {os_name!r}")
    if arch not in SUPPORTED_PLATFORM_ARCH:
        raise ValueError(f"asset #{index} has unsupported arch: {arch!r}")
    match = ARCHIVE_FILENAME_RE.fullmatch(filename)
    if match is None:
        raise ValueError(f"asset #{index} filename is malformed: {filename!r}")
    if match.group("version") != version:
        raise ValueError(f"asset #{index} filename does not match manifest version: {filename!r}")
    if match.group("os") != os_name or match.group("arch") != arch:
        raise ValueError(f"asset #{index} filename does not match its platform: {filename!r}")
    if SHA256_RE.fullmatch(sha256) is None:
        raise ValueError(f"asset #{index} sha256 must be 64 lowercase hex characters")
    if not MIN_ARCHIVE_SIZE <= size <= MAX_ARCHIVE_SIZE:
        raise ValueError(f"asset #{index} size is out of bounds: {size}")
    try:
        validate_manifest_source_url(source_url, filename=filename)
    except ValueError as exc:
        raise ValueError(f"asset #{index} {exc}") from exc
    return ClientAsset(
        os=os_name,
        arch=arch,
        filename=filename,
        sha256=sha256,
        size=size,
        source_url=source_url,
    )


def parse_client_release(payload: Any) -> ClientRelease:
    """Validate a raw manifest document; raise ValueError on anything malformed."""
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    version = payload.get("version")
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise ValueError(f"manifest version is malformed: {version!r}")
    if version != __version__:
        raise ValueError(f"manifest version {version} does not match portmap {__version__}")
    assets_payload = payload.get("assets")
    if not isinstance(assets_payload, list) or not assets_payload:
        raise ValueError("manifest assets must be a non-empty list")
    assets: list[ClientAsset] = []
    seen_platforms: set[tuple[str, str]] = set()
    seen_filenames: set[str] = set()
    for index, entry in enumerate(assets_payload):
        asset = _parse_asset(entry, version=version, index=index)
        if asset.platform in seen_platforms:
            raise ValueError(f"duplicate platform in manifest: {asset.os}/{asset.arch}")
        if asset.filename in seen_filenames:
            raise ValueError(f"duplicate filename in manifest: {asset.filename}")
        seen_platforms.add(asset.platform)
        seen_filenames.add(asset.filename)
        assets.append(asset)
    return ClientRelease(version=version, assets=tuple(assets))


def client_release_state() -> tuple[str, ClientRelease | None]:
    """Load the shipped manifest. Returns (reason, release); release is None when
    the server cannot honestly publish client downloads."""
    if not RELEASE_MANIFEST_PATH.is_file():
        return (
            "client downloads are not published for this build (no release manifest)",
            None,
        )
    try:
        raw = RELEASE_MANIFEST_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return (f"client release manifest is unreadable: {exc}", None)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return (f"client release manifest is not valid JSON: {exc}", None)
    try:
        return ("", parse_client_release(payload))
    except ValueError as exc:
        return (f"client release manifest is invalid: {exc}", None)


def client_download_info() -> dict[str, Any]:
    message, release = client_release_state()
    if release is None:
        return unavailable_payload(message)
    return release.payload()


def client_download_dir() -> Path:
    explicit = os.environ.get("PORTMAP_CLIENT_DOWNLOAD_DIR")
    if explicit:
        return Path(explicit).expanduser()
    from .settings import load_portmap_settings

    return load_portmap_settings().state_dir / CACHE_DIR_NAME


def archive_matches(path: Path, asset: ClientAsset) -> bool:
    """Streaming size+sha256 check; never loads the archive into memory."""
    try:
        if path.stat().st_size != asset.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(READ_CHUNK_BYTES):
                digest.update(chunk)
        return digest.hexdigest() == asset.sha256
    except OSError:
        return False


@contextmanager
def archive_lock(directory: Path, filename: str) -> Iterator[None]:
    lock_path = directory / f"{filename}.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class TrustedAssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow release redirects only when every hop stays on trusted hosts."""

    def redirect_request(  # type: ignore[override]
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        try:
            validate_trusted_source_url(newurl)
        except ValueError as exc:
            raise urllib.error.URLError(f"untrusted redirect target: {exc}") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def trusted_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(TrustedAssetRedirectHandler())


def fetch_client_archive(asset: ClientAsset, directory: Path) -> Path:
    """Download the pinned asset into the cache, verifying size and sha256.

    The archive streams through a bounded buffer into a temp file that is
    atomically renamed into place only after both checks pass.
    """
    request = urllib.request.Request(
        asset.source_url,
        headers={"user-agent": f"portmap-catalog/{__version__}"},
    )
    fd, temp_name = tempfile.mkstemp(dir=directory, prefix=f".{asset.filename}.", suffix=".part")
    target = directory / asset.filename
    try:
        with os.fdopen(fd, "wb") as temp_file:
            try:
                with trusted_opener().open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := response.read(READ_CHUNK_BYTES):
                        size += len(chunk)
                        if size > asset.size:
                            raise ClientDownloadUnavailable(
                                503,
                                f"client archive exceeded manifest size: {asset.filename}",
                            )
                        digest.update(chunk)
                        temp_file.write(chunk)
                    if size != asset.size:
                        raise ClientDownloadUnavailable(
                            503,
                            f"client archive size mismatch for {asset.filename}: "
                            f"expected {asset.size}, got {size}",
                        )
                    if digest.hexdigest() != asset.sha256:
                        raise ClientDownloadUnavailable(
                            503,
                            f"client archive checksum mismatch for {asset.filename}",
                        )
            except ClientDownloadUnavailable:
                raise
            except (OSError, ValueError) as exc:
                raise ClientDownloadUnavailable(
                    503,
                    f"failed to download client archive {asset.filename}: {exc}",
                ) from exc
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return target


def resolve_client_archive(filename: str, *, cache_dir: Path | None = None) -> Path:
    """Return a verified local archive path for a manifest-pinned filename.

    Unknown filenames, traversal attempts and unpublished manifests never touch
    the network. Cached (including preseeded) copies are served offline; misses
    lazily fetch the pinned trusted GitHub asset under a per-archive lock.
    """
    message, release = client_release_state()
    if release is None:
        raise ClientDownloadUnavailable(404, message)
    asset = release.asset_for(filename)
    if asset is None:
        raise ClientDownloadUnavailable(404, f"unknown client archive: {filename}")
    directory = cache_dir or client_download_dir()
    target = directory / asset.filename
    if archive_matches(target, asset):
        return target
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ClientDownloadUnavailable(500, f"cannot prepare client download cache: {exc}") from exc
    with archive_lock(directory, asset.filename):
        if archive_matches(target, asset):
            return target
        return fetch_client_archive(asset, directory)


def install_script_body() -> bytes:
    """The installer is a maintained shell asset; it is only published when the
    manifest validates, so the curl|sh pipe never installs fiction."""
    message, release = client_release_state()
    if release is None:
        raise ClientDownloadUnavailable(404, message)
    try:
        return INSTALL_SCRIPT_PATH.read_bytes()
    except OSError as exc:
        raise ClientDownloadUnavailable(500, f"installer template is unavailable: {exc}") from exc
