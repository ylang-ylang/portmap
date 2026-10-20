import errno
import hashlib
import http.client
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
import urllib.response
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import __version__
from portmap.catalog import CatalogHandler
from portmap.downloads import (
    INSTALL_SCRIPT_PATH,
    DownloadUnavailable,
    ReleaseAsset,
    TrustedAssetRedirectHandler,
    archive_matches,
    download_cache_dir,
    download_info,
    fetch_archive,
    parse_release,
    published_manifest_url,
    release_state,
    resolve_archive,
    tag_for_version,
    validate_trusted_source_url,
    version_for_tag,
)
VERSION = __version__
LINUX_ASSET = f"portmap-{VERSION}-linux-amd64.tar.gz"
DARWIN_ASSET = f"portmap-{VERSION}-darwin-arm64.tar.gz"


@pytest.fixture(autouse=True)
def isolated_client_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTMAP_ROOT", str(tmp_path / "config-root"))
    monkeypatch.setenv("PORTMAP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PORTMAP_DOWNLOAD_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(tmp_path / "missing-agent.sock"))


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any lazy GitHub fetch in a unit test is a bug: manifests must reject
    before network and preseeded caches must serve offline."""

    def boom():
        raise AssertionError("unexpected network fetch during test")

    monkeypatch.setattr("portmap.downloads.trusted_opener", boom)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name == "portmap" else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def fake_portmap_archive() -> bytes:
    return build_archive(
        {
            "portmap": f"#!/bin/sh\necho fake-portmap {VERSION}\n".encode(),
            "_internal/portmap/note.txt": b"payload\n",
        }
    )


def asset_payload(
    *,
    os: str = "linux",
    arch: str = "amd64",
    filename: str | None = None,
    sha256: str | None = None,
    size: int | None = None,
    source_url: str | None = None,
) -> dict:
    resolved_filename = filename or f"portmap-{VERSION}-{os}-{arch}.tar.gz"
    return {
        "os": os,
        "arch": arch,
        "filename": resolved_filename,
        "sha256": sha256 or "a" * 64,
        "size": size if size is not None else 1024,
        "source_url": source_url
        or f"https://github.com/ylang-ylang/portmap/releases/download/{tag_for_version(VERSION)}/{resolved_filename}",
    }


def valid_manifest_payload() -> dict:
    return {"version": VERSION, "assets": [asset_payload(), asset_payload(os="darwin", arch="arm64")]}


def publish_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload) -> Path:
    """Install a local manifest as the bundle sidecar: the only local
    manifest candidate (source installs bootstrap the official manifest)."""
    sidecar = tmp_path / "bundle" / "release.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("portmap.downloads.frozen_manifest_sidecar_path", lambda: sidecar)
    return sidecar


def unpublish_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "portmap.downloads.frozen_manifest_sidecar_path",
        lambda: tmp_path / "absent-release.json",
    )

def make_asset(**overrides) -> ReleaseAsset:
    return parse_release({"version": VERSION, "assets": [asset_payload(**overrides)]}).assets[0]


def publish_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, archive: bytes, filename: str = LINUX_ASSET
) -> dict:
    """Publish a checksum-consistent release with a preseeded offline cache."""
    asset = asset_payload(filename=filename, sha256=sha256_hex(archive), size=len(archive))
    publish_manifest(monkeypatch, tmp_path, {"version": VERSION, "assets": [asset]})
    cache = Path(os.environ["PORTMAP_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    (cache / filename).write_bytes(archive)
    return asset


class CatalogServer:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path: str, *, method: str = "GET") -> tuple[int, http.client.HTTPMessage, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=10)
        self.server.server_close()


@pytest.fixture
def catalog() -> CatalogServer:
    server = CatalogServer()
    try:
        yield server
    finally:
        server.close()


# --- manifest validation ----------------------------------------------------


def test_parse_release_accepts_valid_manifest() -> None:
    release = parse_release(valid_manifest_payload())
    assert release.version == VERSION
    assert [asset.platform for asset in release.assets] == [("linux", "amd64"), ("darwin", "arm64")]
    assert release.assets[0].filename == LINUX_ASSET


@pytest.mark.parametrize(
    "mutate, match",
    [
        pytest.param(lambda p: p.update(version="9.9.9"), "does not match portmap", id="version-mismatch"),
        pytest.param(lambda p: p.update(version="dev"), "version is malformed", id="version-malformed"),
        pytest.param(lambda p: p.update(assets=[]), "non-empty list", id="assets-empty"),
        pytest.param(lambda p: p.update(assets={}), "non-empty list", id="assets-not-list"),
        pytest.param(lambda p: p["assets"][0].update(sha256="A" * 64), "sha256", id="sha-uppercase"),
        pytest.param(lambda p: p["assets"][0].update(sha256="abc"), "sha256", id="sha-short"),
        pytest.param(
            lambda p: p["assets"][0].update(filename="../escape.tar.gz"),
            "filename is malformed",
            id="filename-traversal",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(filename="portmap.zip"),
            "filename is malformed",
            id="filename-pattern",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(filename=f"portmap-client-{VERSION}-linux-amd64.tar.gz"),
            "filename is malformed",
            id="filename-legacy-program-name",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(filename="portmap-0.0.1-linux-amd64.tar.gz"),
            "does not match manifest version",
            id="filename-version",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(filename=f"portmap-{VERSION}-darwin-amd64.tar.gz"),
            "does not match its platform",
            id="filename-platform",
        ),
        pytest.param(lambda p: p["assets"][0].update(size=0), "out of bounds", id="size-zero"),
        pytest.param(lambda p: p["assets"][0].update(size=-5), "out of bounds", id="size-negative"),
        pytest.param(lambda p: p["assets"][0].update(size=10**12), "out of bounds", id="size-huge"),
        pytest.param(lambda p: p["assets"][0].update(size="1024"), "must be an integer", id="size-string"),
        pytest.param(lambda p: p["assets"][0].update(size=True), "must be an integer", id="size-bool"),
        pytest.param(lambda p: p["assets"][0].pop("size"), "must be an integer", id="size-missing"),
        pytest.param(
            lambda p: p["assets"][0].update(os="windows"), "unsupported os", id="os-unsupported"
        ),
        pytest.param(
            lambda p: p["assets"][0].update(arch="riscv64"), "unsupported arch", id="arch-unsupported"
        ),
        pytest.param(
            lambda p: p["assets"][0].update(
                source_url="http://github.com/ylang-ylang/portmap/releases/download/V0.7/x.tar.gz"
            ),
            "must use https",
            id="url-http",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(source_url="https://evil.example/download/x.tar.gz"),
            "host is not trusted",
            id="url-foreign-host",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(
                source_url="https://objects.githubusercontent.com/ylang-ylang/portmap/x.tar.gz"
            ),
            "must be hosted on github.com",
            id="url-cdn-host",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(
                source_url=f"https://github.com:443/ylang-ylang/portmap/releases/download/V0.7/{LINUX_ASSET}"
            ),
            "port",
            id="url-port",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(
                source_url="https://github.com/other/portmap/releases/download/V0.7/x.tar.gz"
            ),
            "outside the project release downloads",
            id="url-wrong-repo",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(
                source_url=f"https://github.com/ylang-ylang/portmap/releases/download/V0.7/{LINUX_ASSET}?t=1"
            ),
            "query or fragment",
            id="url-query",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(
                source_url="https://github.com/ylang-ylang/portmap/releases/download/V0.7/other.tar.gz"
            ),
            "does not reference",
            id="url-basename",
        ),
        pytest.param(
            lambda p: p["assets"].append(asset_payload(sha256="b" * 64)),
            "duplicate platform",
            id="duplicate-platform",
        ),
        pytest.param(lambda p: p["assets"].__setitem__(0, "nope"), "must be a JSON object", id="asset-not-object"),
        pytest.param(lambda p: p.pop("version"), "version is malformed", id="version-missing"),
        pytest.param(lambda p: p.pop("assets"), "non-empty list", id="assets-missing"),
    ],
)
def test_parse_release_rejects_malformed_manifests(mutate, match: str) -> None:
    payload = valid_manifest_payload()
    mutate(payload)
    with pytest.raises(ValueError, match=match):
        parse_release(payload)


def test_parse_release_accepts_generated_source_block() -> None:
    payload = valid_manifest_payload()
    payload["source"] = {
        "repository": "ylang-ylang/portmap",
        "tag": tag_for_version(VERSION),
        "commit": "b" * 40,
    }
    release = parse_release(payload)
    assert release.version == VERSION


@pytest.mark.parametrize(
    "mutate, match",
    [
        pytest.param(lambda p: p.update(source=None), "source must be", id="explicit-null"),
        pytest.param(lambda p: p.update(source=[]), "source must be", id="source-not-object"),
        pytest.param(
            lambda p: p.update(source={"repository": "evil/portmap", "tag": "V0.19", "commit": "b" * 40}),
            "repository",
            id="foreign-repository",
        ),
        pytest.param(
            lambda p: p.update(source={"repository": "ylang-ylang/portmap", "tag": "V0.18", "commit": "b" * 40}),
            "does not match version",
            id="tag-version-mismatch",
        ),
        pytest.param(
            lambda p: p["source"].update(commit="abc"),
            "commit is malformed",
            id="short-commit",
        ),
    ],
)
def test_parse_release_rejects_malformed_source_blocks(mutate, match: str) -> None:
    payload = valid_manifest_payload()
    payload["source"] = {
        "repository": "ylang-ylang/portmap",
        "tag": tag_for_version(VERSION),
        "commit": "b" * 40,
    }
    mutate(payload)
    with pytest.raises(ValueError, match=match):
        parse_release(payload)



@pytest.mark.parametrize(
    "version, tag",
    [
        ("0.0.0", "V0.0"),
        ("0.19.0", "V0.19"),
        ("0.19.1", "v0.19.1"),
        ("0.20.0", "v0.20.0"),
        ("0.20.3", "v0.20.3"),
        ("1.0.0", "v1.0.0"),
    ],
)
def test_published_manifest_url_pins_exact_or_historical_version(monkeypatch, version, tag):
    monkeypatch.setattr("portmap.downloads.__version__", version)
    assert tag_for_version(version) == tag
    assert version_for_tag(tag) == version
    assert published_manifest_url() == (
        f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/release.json"
    )


@pytest.mark.parametrize("tag", ["v0.0.0", "v0.19.0", "v0.18.1", "V0.20", "V1.0"])
def test_nonhistorical_pretransition_tags_are_not_release_aliases(tag):
    with pytest.raises(ValueError):
        version_for_tag(tag)


def test_pretransition_patch_has_no_invented_release_tag():
    with pytest.raises(ValueError, match="no historical tag"):
        tag_for_version("0.18.1")


@pytest.mark.parametrize("version", ["0.19.0", "0.20.0", "0.20.3"])
def test_parse_release_reads_legacy_and_exact_patch_identity(monkeypatch, version):
    monkeypatch.setattr("portmap.downloads.__version__", version)
    filename = f"portmap-{version}-linux-amd64.tar.gz"
    tag = tag_for_version(version)
    payload = {
        "version": version,
        "source": {"repository": "ylang-ylang/portmap", "tag": tag, "commit": "a" * 40},
        "assets": [asset_payload(
            filename=filename,
            source_url=f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/{filename}",
        )],
    }
    assert parse_release(payload).version == version
    payload.pop("source")
    assert parse_release(payload).version == version
    payload["source"] = {"repository": "ylang-ylang/portmap", "tag": "v0.20.2", "commit": "a" * 40}
    with pytest.raises(ValueError, match="does not match version"):
        parse_release(payload)

def test_validate_trusted_source_url_hosts() -> None:
    validate_trusted_source_url(
        f"https://github.com/ylang-ylang/portmap/releases/download/V0.7/{LINUX_ASSET}"
    )
    validate_trusted_source_url("https://objects.githubusercontent.com/containers/release-assets/asset.tgz")
    validate_trusted_source_url("https://release-assets.githubusercontent.com/ylang/x.tgz")
    for bad in (
        "http://github.com/ylang-ylang/portmap/releases/download/V0.7/a.tgz",
        "https://cdn.example.com/a.tgz",
        "https://github.com:8443/ylang-ylang/portmap/releases/download/V0.7/a.tgz",
        "https://user@github.com/ylang-ylang/portmap/releases/download/V0.7/a.tgz",
        "https://github.com/ylang-ylang/portmap/releases/download/V0.7/a.tgz?x=1",
        "https://github.com/ylang-ylang/portmap/releases/download/V0.7/a.tgz#frag",
        "https://github.com/",
        "https://github.com",
    ):
        with pytest.raises(ValueError):
            validate_trusted_source_url(bad)


def test_trusted_redirect_handler_blocks_untrusted_hops() -> None:
    handler = TrustedAssetRedirectHandler()
    with pytest.raises(urllib.error.URLError, match="untrusted redirect target"):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/asset.tgz")
    with pytest.raises(urllib.error.URLError, match="untrusted redirect target"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://github.com/asset.tgz")


def test_downloads_follow_signed_cdn_redirect_into_verified_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = fake_portmap_archive()
    asset = asset_payload(sha256=sha256_hex(archive), size=len(archive))
    publish_manifest(monkeypatch, tmp_path, {"version": VERSION, "assets": [asset]})
    signed_url = (
        "https://release-assets.githubusercontent.com/github-production-release-asset/asset"
        "?sp=r&sig=encoded%2Bsignature%3D&jwt=signed-token"
    )
    responses = {
        asset["source_url"]: (302, {"location": signed_url}, b""),
        signed_url: (200, {}, archive),
    }

    class AssetHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            # Only the transport is replaced: urllib follows the real redirect
            # policy, and losing the signed query makes the asset unavailable.
            if request.full_url not in responses:
                raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
            code, headers, body = responses[request.full_url]
            response = urllib.response.addinfourl(io.BytesIO(body), headers, request.full_url, code)
            response.msg = "Found" if code == 302 else "OK"
            return response

    monkeypatch.setattr(
        "portmap.downloads.trusted_opener",
        lambda: urllib.request.build_opener(AssetHTTPSHandler(), TrustedAssetRedirectHandler()),
    )
    assert resolve_archive(LINUX_ASSET).read_bytes() == archive


def unreachable_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the trusted opener at an unreachable upstream."""

    def factory():
        class StubOpener:
            def open(self, request, timeout=None):
                raise OSError("connection refused")

        return StubOpener()

    monkeypatch.setattr("portmap.downloads.trusted_opener", factory)


def test_release_state_reports_missing_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unpublish_manifest(monkeypatch, tmp_path)
    unreachable_opener(monkeypatch)
    message, release = release_state()
    assert release is None
    assert "not published" in message
    assert published_manifest_url() in message
    info = download_info()
    assert info["available"] is False
    assert info["platforms"] == []
    assert info["install_script"] is None
    assert info["version"] is None
    assert info["message"] == message


def test_release_state_reports_malformed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    publish_manifest(monkeypatch, tmp_path, {"version": "not-semver", "assets": [asset_payload()]})
    message, release = release_state()
    assert release is None
    assert "invalid" in message
    assert download_info()["available"] is False


# --- manifest lifecycle: sidecar, remote bootstrap ---------------------------


def use_frozen_sidecar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    sidecar = tmp_path / "bundle" / "release.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("portmap.downloads.frozen_manifest_sidecar_path", lambda: sidecar)
    return sidecar


def test_frozen_sidecar_wins_over_cached_bootstrap_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = use_frozen_sidecar(monkeypatch, tmp_path)
    archive = fake_portmap_archive()
    asset = asset_payload(sha256=sha256_hex(archive), size=len(archive))
    sidecar.write_text(json.dumps({"version": VERSION, "assets": [asset]}), encoding="utf-8")
    # A previously bootstrapped official manifest sits in the shared cache and
    # disagrees; the local sidecar must win without touching the network.
    cache = Path(os.environ["PORTMAP_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    cached = {"version": VERSION, "assets": [asset_payload(os="darwin", arch="arm64")]}
    (cache / "release.json").write_text(json.dumps(cached), encoding="utf-8")
    message, release = release_state()
    assert release is not None
    assert release.assets[0].filename == asset["filename"]
    assert (cache / "release.json").read_text(encoding="utf-8") == json.dumps(cached)

def test_corrupt_sidecar_is_refused_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = use_frozen_sidecar(monkeypatch, tmp_path)
    sidecar.write_text("{ not json", encoding="utf-8")
    # forbid_network stays armed: a corrupt local manifest must be an honest
    # failure, never silently replaced by a network fetch.
    message, release = release_state()
    assert release is None
    assert "not valid JSON" in message


def test_version_mismatched_sidecar_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = use_frozen_sidecar(monkeypatch, tmp_path)
    sidecar.write_text(
        json.dumps({"version": "9.9.9", "assets": [asset_payload()]}), encoding="utf-8"
    )
    message, release = release_state()
    assert release is None
    assert "does not match portmap" in message


def test_raw_archive_bootstraps_pinned_published_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unpublish_manifest(monkeypatch, tmp_path)
    cache = Path(os.environ["PORTMAP_DOWNLOAD_DIR"])
    archive = fake_portmap_archive()
    asset = asset_payload(sha256=sha256_hex(archive), size=len(archive))
    document = (json.dumps({"version": VERSION, "assets": [asset]}, indent=2) + "\n").encode()
    opens: list[str] = []
    monkeypatch.setattr("portmap.downloads.trusted_opener", stub_opener_factory(document, opens))

    message, release = release_state()
    assert release is not None
    assert message == ""
    # The bootstrap is pinned to the running version's published manifest.
    assert opens == [published_manifest_url()]
    assert (cache / "release.json").read_bytes() == document

    # The cached document serves later requests without further network.
    unreachable_opener(monkeypatch)
    message, cached = release_state()
    assert cached == release


@pytest.mark.parametrize("version, tag", [("0.19.0", "V0.19"), ("0.20.3", "v0.20.3")])
def test_manifest_bootstrap_replaces_other_versions_without_losing_exact_tag(monkeypatch, tmp_path, version, tag):
    monkeypatch.setattr("portmap.downloads.__version__", version)
    unpublish_manifest(monkeypatch, tmp_path)
    cache = Path(os.environ["PORTMAP_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    stale_filename = "portmap-0.18.0-linux-amd64.tar.gz"
    stale_asset = asset_payload(
        filename=stale_filename,
        source_url=f"https://github.com/ylang-ylang/portmap/releases/download/V0.18/{stale_filename}",
    )
    (cache / "release.json").write_text(json.dumps({"version": "0.18.0", "assets": [stale_asset]}))
    filename = f"portmap-{version}-linux-amd64.tar.gz"
    asset = asset_payload(
        filename=filename,
        source_url=f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/{filename}",
    )
    payload = {"version": version, "assets": [asset]}
    document = json.dumps(payload).encode()
    opens = []
    monkeypatch.setattr("portmap.downloads.trusted_opener", stub_opener_factory(document, opens))
    message, release = release_state()
    assert message == ""
    assert release.version == version
    assert opens == [f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/release.json"]
    assert (cache / "release.json").read_bytes() == document
    unreachable_opener(monkeypatch)
    assert release_state() == ("", release)


def test_remote_manifest_corrupt_or_mismatched_documents_are_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unpublish_manifest(monkeypatch, tmp_path)
    cache = Path(os.environ["PORTMAP_DOWNLOAD_DIR"])
    for document in (
        b"garbage",
        json.dumps({"version": "9.9.9", "assets": [asset_payload()]}).encode(),
    ):
        monkeypatch.setattr("portmap.downloads.trusted_opener", stub_opener_factory(document, []))
        _, release = release_state()
        assert release is None
        assert not (cache / "release.json").exists()


# --- HTTP routes ------------------------------------------------------------


def test_api_downloads_returns_manifest_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    archive = fake_portmap_archive()
    linux = asset_payload(sha256=sha256_hex(archive), size=len(archive))
    darwin = asset_payload(os="darwin", arch="arm64")
    publish_manifest(monkeypatch, tmp_path, {"version": VERSION, "assets": [linux, darwin]})

    status, headers, body = catalog.request("/api/downloads")
    assert status == 200
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {
        "name": "portmap",
        "version": VERSION,
        "available": True,
        "message": None,
        "platforms": [
            {
                "os": "linux",
                "arch": "amd64",
                "filename": LINUX_ASSET,
                "sha256": sha256_hex(archive),
                "size": len(archive),
                "url": f"/downloads/portmap/{LINUX_ASSET}",
                "source_url": linux["source_url"],
                "requirements": "glibc 2.31+ (musl/Alpine unsupported)",
            },
            {
                "os": "darwin",
                "arch": "arm64",
                "filename": DARWIN_ASSET,
                "sha256": darwin["sha256"],
                "size": darwin["size"],
                "url": f"/downloads/portmap/{DARWIN_ASSET}",
                "source_url": darwin["source_url"],
                "requirements": None,
            },
        ],
        "install_script": "/install.sh",
    }


def test_api_downloads_honest_when_manifest_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    unpublish_manifest(monkeypatch, tmp_path)
    unreachable_opener(monkeypatch)
    status, _, body = catalog.request("/api/downloads")
    payload = json.loads(body)
    assert status == 200
    assert payload["available"] is False
    assert payload["platforms"] == []
    assert payload["install_script"] is None

    status, _, body = catalog.request("/install.sh")
    assert status == 404

    status, _, _ = catalog.request(f"/downloads/portmap/{LINUX_ASSET}")
    assert status == 404

    # The retired client-only endpoints are gone without aliases.
    assert catalog.request("/api/client")[0] == 404
    assert catalog.request("/install-client.sh")[0] == 404
    assert catalog.request(f"/downloads/client/{LINUX_ASSET}")[0] == 404


def test_api_downloads_honest_when_manifest_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    payload = valid_manifest_payload()
    payload["assets"][0]["sha256"] = "ZZZ"
    publish_manifest(monkeypatch, tmp_path, payload)
    status, _, body = catalog.request("/api/downloads")
    assert status == 200
    assert json.loads(body)["available"] is False
    assert json.loads(body)["platforms"] == []
    status, _, _ = catalog.request(f"/downloads/portmap/{LINUX_ASSET}")
    assert status == 404


def test_downloads_stream_preseeded_cache_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    archive = fake_portmap_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)

    status, headers, body = catalog.request(f"/downloads/portmap/{LINUX_ASSET}")
    assert status == 200
    assert headers["content-type"] == "application/gzip"
    assert int(headers["content-length"]) == len(archive)
    assert body == archive

    head_status, head_headers, head_body = catalog.request(f"/downloads/portmap/{LINUX_ASSET}", method="HEAD")
    assert head_status == 200
    assert int(head_headers["content-length"]) == len(archive)
    assert head_headers["content-type"] == "application/gzip"
    assert head_body == b""


def test_install_script_served_only_for_validated_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    publish_release(monkeypatch, tmp_path, archive=fake_portmap_archive())
    status, headers, body = catalog.request("/install.sh")
    assert status == 200
    assert headers["content-type"] == "text/x-shellscript; charset=utf-8"
    assert int(headers["content-length"]) == len(body)
    assert body == INSTALL_SCRIPT_PATH.read_bytes()
    assert b"/downloads/portmap/" in body


def test_downloads_reject_unknown_and_traversal_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    archive = fake_portmap_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)
    for path in (
        "/downloads/portmap/unknown.tar.gz",
        f"/downloads/portmap/{DARWIN_ASSET}",
        "/downloads/portmap/../release.json",
        "/downloads/portmap/%2e%2e%2frelease.json",
        "/downloads/portmap/",
        "/downloads/portmap",
    ):
        status, _, _ = catalog.request(path)
        assert status == 404, path
    # Preseeded serving never fetches, so no lock or temp artifacts appear.
    cache = Path(os.environ["PORTMAP_DOWNLOAD_DIR"])
    assert sorted(item.name for item in cache.iterdir()) == [LINUX_ASSET]


class StubResponse(io.BytesIO):
    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def stub_opener_factory(body: bytes, opens: list, *, delay: float = 0.0):
    def factory():
        class StubOpener:
            def open(self, request, timeout=None):
                opens.append(request.full_url)
                if delay:
                    time.sleep(delay)
                return StubResponse(body)

        return StubOpener()

    return factory


def test_downloads_refetch_when_cache_corrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archive = fake_portmap_archive()
    asset = asset_payload(sha256=sha256_hex(archive), size=len(archive))
    publish_manifest(monkeypatch, tmp_path, {"version": VERSION, "assets": [asset]})
    cache = Path(os.environ["PORTMAP_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    (cache / LINUX_ASSET).write_bytes(b"corrupted bytes")

    opens: list[str] = []
    monkeypatch.setattr("portmap.downloads.trusted_opener", stub_opener_factory(archive, opens))

    resolved = resolve_archive(LINUX_ASSET)
    assert resolved.read_bytes() == archive
    assert opens == [asset["source_url"]]
    # The verified download replaced the corrupt cache entry.
    assert archive_matches(cache / LINUX_ASSET, make_asset(**asset))
    # A second resolve is served from the verified cache without refetching.
    assert resolve_archive(LINUX_ASSET).read_bytes() == archive
    assert len(opens) == 1


def test_downloads_lazy_fetch_verifies_size_and_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = fake_portmap_archive()
    cache = Path(os.environ["PORTMAP_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    opens: list[str] = []

    def publish(**overrides) -> None:
        payload = {"version": VERSION, "assets": [asset_payload(**overrides)]}
        publish_manifest(monkeypatch, tmp_path, payload)

    monkeypatch.setattr("portmap.downloads.trusted_opener", stub_opener_factory(archive, opens))

    # Shorter than the manifest size: reject and keep the cache clean.
    publish(sha256=sha256_hex(archive), size=len(archive) + 1)
    with pytest.raises(DownloadUnavailable, match="size mismatch"):
        resolve_archive(LINUX_ASSET)
    assert not (cache / LINUX_ASSET).exists()

    # Checksum mismatch: reject and keep the cache clean.
    publish(sha256="b" * 64, size=len(archive))
    with pytest.raises(DownloadUnavailable, match="checksum mismatch"):
        resolve_archive(LINUX_ASSET)
    assert not (cache / LINUX_ASSET).exists()

    # No stray temp files survive the failures.
    assert [item.name for item in cache.iterdir() if item.name.endswith(".part")] == []

    # A healthy pinned fetch lands atomically in the cache.
    publish(sha256=sha256_hex(archive), size=len(archive))
    assert resolve_archive(LINUX_ASSET).read_bytes() == archive


def test_fetch_unreachable_upstream_is_service_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    publish_manifest(monkeypatch, tmp_path, {"version": VERSION, "assets": [asset_payload()]})
    cache = Path(os.environ["PORTMAP_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)

    def failing_factory():
        class StubOpener:
            def open(self, request, timeout=None):
                raise OSError("connection refused")

        return StubOpener()

    monkeypatch.setattr("portmap.downloads.trusted_opener", failing_factory)
    with pytest.raises(DownloadUnavailable, match="failed to download"):
        fetch_archive(make_asset(), cache)
    assert [item.name for item in cache.iterdir() if item.name.endswith(".part")] == []


def test_download_cache_dir_defaults_to_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORTMAP_DOWNLOAD_DIR")
    assert download_cache_dir() == Path(os.environ["PORTMAP_STATE_DIR"]) / "downloads"


@pytest.mark.parametrize("override", [None, ""])
def test_download_cache_dir_explicit_state_precedes_ambient_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: str | None
) -> None:
    if override is None:
        monkeypatch.delenv("PORTMAP_DOWNLOAD_DIR")
    else:
        monkeypatch.setenv("PORTMAP_DOWNLOAD_DIR", override)
    state_dir = tmp_path / "publishing-state"
    assert download_cache_dir(state_dir) == state_dir / "downloads"


@pytest.mark.parametrize("state_dir", [None, Path("publishing-state")])
def test_download_cache_dir_environment_precedes_explicit_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state_dir: Path | None
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PORTMAP_DOWNLOAD_DIR", "~/artifacts")
    assert download_cache_dir(state_dir) == tmp_path / "artifacts"


def test_concurrent_resolves_share_one_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archive = fake_portmap_archive()
    publish_manifest(
        monkeypatch,
        tmp_path,
        {"version": VERSION, "assets": [asset_payload(sha256=sha256_hex(archive), size=len(archive))]},
    )
    cache = Path(os.environ["PORTMAP_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    # No preseeded archive: both threads must lazily fetch under the lock.
    opens: list[str] = []
    monkeypatch.setattr(
        "portmap.downloads.trusted_opener",
        stub_opener_factory(archive, opens, delay=0.05),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda _: resolve_archive(LINUX_ASSET), range(2)))
    assert len(opens) == 1
    assert all(path.read_bytes() == archive for path in paths)


# --- installer (real shell, real HTTP, sandboxed HOME) ----------------------


class StaticOrigin(http.server.SimpleHTTPRequestHandler):
    """Dumb origin for installer failure modes the catalog itself never produces."""

    def log_message(self, fmt, *args) -> None:
        pass


@pytest.fixture
def installer_home(tmp_path: Path) -> SimpleNamespace:
    home = tmp_path / "home"
    data = tmp_path / "data"
    downloads = tmp_path / "installer-tmp"
    for path in (home, data, downloads):
        path.mkdir(parents=True)
    return SimpleNamespace(home=home, data=data, tmp=downloads)


def run_installer(
    origin: str, *, installer_home: SimpleNamespace, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(installer_home.home),
        "XDG_DATA_HOME": str(installer_home.data),
        "TMPDIR": str(installer_home.tmp),
        "PATH": os.environ["PATH"],
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["sh", str(INSTALL_SCRIPT_PATH), origin],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_installer_preserves_unrelated_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_portmap_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)
    unrelated = installer_home.data / "portmap" / VERSION
    unrelated.mkdir(parents=True)
    (unrelated / "precious.txt").write_text("keep me")

    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode == 0, result.stderr
    assert assert_installed(installer_home) != unrelated
    assert (unrelated / "precious.txt").read_text() == "keep me"
    assert [item.name for item in unrelated.iterdir()] == ["precious.txt"]
    assert list(installer_home.tmp.iterdir()) == []


def test_installer_refuses_unrelated_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_portmap_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)
    bin_dir = installer_home.home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "portmap"
    launcher.write_text("#!/bin/sh\nexit 42\n")
    launcher.chmod(0o755)

    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode != 0
    assert "refusing to replace unrelated" in result.stderr
    assert launcher.read_text().startswith("#!/bin/sh")
    assert not (installer_home.data / "portmap").exists()

    # A foreign symlink is refused too.
    launcher.unlink()
    launcher.symlink_to("/tmp/definitely-not-portmap")
    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode != 0
    assert "refusing to replace unrelated" in result.stderr
    assert os.readlink(launcher) == "/tmp/definitely-not-portmap"


def test_installer_refuses_unsafe_archive_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    evil = build_archive({"../evil.txt": b"escaped\n"})
    publish_release(monkeypatch, tmp_path, archive=evil)

    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode != 0
    assert "unsafe paths" in result.stderr
    assert not (installer_home.data / "evil.txt").exists()
    assert not (installer_home.data / "portmap" / "evil.txt").exists()
    # Nothing was installed and no staging artifacts survive the failure.
    stage_root = installer_home.data / "portmap"
    assert not stage_root.exists() or list(stage_root.iterdir()) == []
    assert list(installer_home.tmp.iterdir()) == []


def archive_with_link_member(member_type: str, linkname: str) -> bytes:
    buffer = io.BytesIO()
    executable = f"#!/bin/sh\necho fake-portmap {VERSION}\n".encode()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("portmap")
        info.size = len(executable)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(executable))
        link = tarfile.TarInfo("danger")
        link.type = member_type
        link.linkname = linkname
        tar.addfile(link)
    return buffer.getvalue()


def non_executable_archive() -> bytes:
    buffer = io.BytesIO()
    executable = f"#!/bin/sh\necho fake-portmap {VERSION}\n".encode()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("portmap")
        info.size = len(executable)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(executable))
    return buffer.getvalue()


def test_installer_preserves_install_when_archive_layout_is_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    good = fake_portmap_archive()
    publish_release(monkeypatch, tmp_path, archive=good)
    assert run_installer(catalog.origin, installer_home=installer_home).returncode == 0
    original_bundle = assert_installed(installer_home)

    # A builder regression wraps everything in an enclosing directory.
    wrapped = build_archive(
        {
            "portmap/portmap": f"#!/bin/sh\necho wrong-layout {VERSION}\n".encode(),
            "portmap/_internal/note.txt": b"x\n",
        }
    )
    publish_release(monkeypatch, tmp_path, archive=wrapped)
    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode != 0
    assert "unexpected layout" in result.stderr
    # The previous install and launcher survive untouched and still run.
    link = installer_home.home / ".local" / "bin" / "portmap"
    ran = subprocess.run([str(link), "--version"], capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0
    assert ran.stdout.strip() == f"fake-portmap {VERSION}"
    assert link.resolve() == original_bundle / "portmap"
    assert list(installer_home.tmp.iterdir()) == []


def test_installer_rejects_non_executable_root_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    publish_release(monkeypatch, tmp_path, archive=non_executable_archive())
    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode != 0
    assert "does not ship an executable" in result.stderr
    assert not (installer_home.home / ".local" / "bin" / "portmap").exists()
    assert list(installer_home.tmp.iterdir()) == []


@pytest.mark.parametrize(
    "member_type, linkname",
    [
        (tarfile.SYMTYPE, "/etc/passwd"),
        (tarfile.LNKTYPE, "portmap"),
    ],
)
def test_installer_rejects_link_members(
    member_type,
    linkname: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    catalog: CatalogServer,
    installer_home: SimpleNamespace,
) -> None:
    publish_release(monkeypatch, tmp_path, archive=archive_with_link_member(member_type, linkname))
    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode != 0
    assert "non-regular members" in result.stderr
    assert not (installer_home.home / ".local" / "bin" / "portmap").exists()
    assert list(installer_home.tmp.iterdir()) == []

def assert_installed(installer_home: SimpleNamespace) -> Path:
    link = installer_home.home / ".local" / "bin" / "portmap"
    assert link.is_symlink()
    executable = link.resolve()
    bundle = executable.parent
    assert bundle.parent == installer_home.data / "portmap"
    assert (bundle / ".portmap-install").is_file()
    assert executable.is_file()
    ran = subprocess.run(
        [str(link), "--version"], capture_output=True, text=True, timeout=30
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == f"fake-portmap {VERSION}"
    # The installer writes a strictly valid canonical manifest sidecar.
    sidecar = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
    assert sidecar["version"] == VERSION
    (asset,) = sidecar["assets"]
    assert asset["filename"] == LINUX_ASSET
    assert asset["os"] == "linux" and asset["arch"] == "amd64"
    assert re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])
    assert asset["size"] > 0
    assert asset["source_url"].startswith(
        "https://github.com/ylang-ylang/portmap/releases/download/"
    )
    parse_release(sidecar)
    # No staging or download temp files survive a successful install.
    assert list(installer_home.tmp.iterdir()) == []
    return bundle


def test_installer_writes_canonical_manifest_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_portmap_archive()
    asset = publish_release(monkeypatch, tmp_path, archive=archive)

    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode == 0, result.stderr
    bundle = (installer_home.home / ".local" / "bin" / "portmap").resolve().parent
    assert json.loads((bundle / "release.json").read_text(encoding="utf-8")) == {
        "version": VERSION,
        "assets": [
            {
                "os": asset["os"],
                "arch": asset["arch"],
                "filename": asset["filename"],
                "sha256": asset["sha256"],
                "size": asset["size"],
                "source_url": asset["source_url"],
            }
        ],
    }


def test_installer_end_to_end_from_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_portmap_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)

    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode == 0, result.stderr
    assert_installed(installer_home)

    # Reinstalling also activates a complete bundle without overwriting the old one.
    again = run_installer(catalog.origin, installer_home=installer_home)
    assert again.returncode == 0, again.stderr
    assert_installed(installer_home)


def test_same_version_upgrade_preserves_running_bundle_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    old_archive = build_archive({
        "portmap": b'#!/bin/sh\nread request\ncat "$(dirname "$0")/_internal/worker-assets"\n',
        "_internal/worker-assets": b"assets required by the running worker\n",
    })
    publish_release(monkeypatch, tmp_path, archive=old_archive)
    installed = run_installer(catalog.origin, installer_home=installer_home)
    assert installed.returncode == 0, installed.stderr
    launcher = installer_home.home / ".local" / "bin" / "portmap"
    old_executable = launcher.resolve()
    worker = subprocess.Popen(
        [str(old_executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        publish_release(monkeypatch, tmp_path, archive=fake_portmap_archive())
        upgraded = run_installer(catalog.origin, installer_home=installer_home)
        assert upgraded.returncode == 0, upgraded.stderr
        output, error = worker.communicate("read\n", timeout=10)
        assert worker.returncode == 0, error
        assert output == "assets required by the running worker\n"
        assert launcher.resolve() != old_executable
        version = subprocess.run([str(launcher), "--version"], capture_output=True, text=True, timeout=10)
        assert version.returncode == 0, version.stderr
        assert version.stdout.strip() == f"fake-portmap {VERSION}"
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=10)


def test_installer_retires_only_owned_legacy_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    """A legacy portmap-client launcher positively owned by the old installer
    is removed after the unified install; its old bundles stay for the runtime
    state migration."""
    archive = fake_portmap_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)

    legacy_bundle = installer_home.data / "portmap-client" / "0.8.0"
    legacy_bundle.mkdir(parents=True)
    (legacy_bundle / "portmap-client").write_text("#!/bin/sh\necho old client\n")
    (legacy_bundle / ".portmap-client-install").write_text("installed by install-client.sh\n")
    legacy_link = installer_home.home / ".local" / "bin" / "portmap-client"
    legacy_link.parent.mkdir(parents=True)
    legacy_link.symlink_to(legacy_bundle / "portmap-client")

    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode == 0, result.stderr
    assert_installed(installer_home)
    assert not legacy_link.exists()
    # Old bundle files are retained for worker migration.
    assert (legacy_bundle / "portmap-client").read_text() == "#!/bin/sh\necho old client\n"
    assert (legacy_bundle / ".portmap-client-install").is_file()

    # A legacy launcher pointing somewhere unrelated is left untouched.
    foreign = installer_home.home / ".local" / "bin" / "other"
    foreign.write_text("#!/bin/sh\nexit 7\n")
    legacy_link.symlink_to(foreign)
    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode == 0, result.stderr
    assert_installed(installer_home)
    assert os.readlink(legacy_link) == str(foreign)
    assert foreign.read_text() == "#!/bin/sh\nexit 7\n"


def test_installer_leaves_unrelated_legacy_launcher_file_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_portmap_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)
    legacy_link = installer_home.home / ".local" / "bin" / "portmap-client"
    legacy_link.parent.mkdir(parents=True)
    legacy_link.write_text("#!/bin/sh\nexit 5\n")
    legacy_link.chmod(0o755)

    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode == 0, result.stderr
    assert_installed(installer_home)
    assert legacy_link.read_text() == "#!/bin/sh\nexit 5\n"


def test_installer_preserves_unmarked_bundle_under_legacy_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    publish_release(monkeypatch, tmp_path, archive=fake_portmap_archive())
    unmarked = installer_home.data / "portmap-client" / "0.7.0"
    unmarked.mkdir(parents=True)
    executable = unmarked / "portmap-client"
    executable.write_text("#!/bin/sh\nexit 7\n")
    legacy_link = installer_home.home / ".local" / "bin" / "portmap-client"
    legacy_link.parent.mkdir(parents=True)
    legacy_link.symlink_to(executable)

    result = run_installer(catalog.origin, installer_home=installer_home)

    assert result.returncode == 0, result.stderr
    assert_installed(installer_home)
    assert legacy_link.is_symlink()
    assert os.readlink(legacy_link) == str(executable)
    assert executable.read_text() == "#!/bin/sh\nexit 7\n"


@pytest.mark.parametrize("stderr_is_terminal", [True, False], ids=["stderr-tty", "stdout-tty"])
def test_piped_installer_progress_follows_stderr(
    stderr_is_terminal: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    catalog: CatalogServer,
    installer_home: SimpleNamespace,
) -> None:
    publish_release(monkeypatch, tmp_path, archive=fake_portmap_archive())
    master, slave = os.openpty()
    try:
        result = subprocess.run(
            ["sh", "-s", "--", catalog.origin],
            input=INSTALL_SCRIPT_PATH.read_bytes(),
            stdout=subprocess.PIPE if stderr_is_terminal else slave,
            stderr=slave if stderr_is_terminal else subprocess.PIPE,
            env={
                "HOME": str(installer_home.home),
                "XDG_DATA_HOME": str(installer_home.data),
                "TMPDIR": str(installer_home.tmp),
                "PATH": os.environ["PATH"],
                "LC_ALL": "C",
            },
            timeout=30,
        )
        os.close(slave)
        slave = None
        chunks = []
        while True:
            try:
                chunk = os.read(master, 8192)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            chunks.append(chunk)
        terminal_output = b"".join(chunks)
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)

    assert result.returncode == 0, result.stderr or terminal_output
    if stderr_is_terminal:
        assert re.search(rb"(?:^|\r)\s*100\s", terminal_output)
        assert b"\r" not in result.stdout
    else:
        assert result.stderr == b""
    assert_installed(installer_home)


def test_installer_reports_missing_readlink_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    publish_release(monkeypatch, tmp_path, archive=fake_portmap_archive())
    initial = run_installer(catalog.origin, installer_home=installer_home)
    assert initial.returncode == 0, initial.stderr
    bundle = assert_installed(installer_home)
    original = (bundle / "portmap").read_bytes()
    tool_bin = tmp_path / "tools"
    tool_bin.mkdir()
    for name in (
        "sh", "curl", "tar", "awk", "grep", "sha256sum", "uname", "getconf",
        "tr", "wc", "sed", "rm", "mv", "mkdir", "mktemp", "chmod", "ln", "cat",
    ):
        (tool_bin / name).symlink_to(shutil.which(name))

    result = run_installer(
        catalog.origin, installer_home=installer_home, extra_env={"PATH": str(tool_bin)}
    )
    assert result.returncode != 0
    assert "missing required tool: readlink" in result.stderr
    assert_installed(installer_home)
    assert (bundle / "portmap").read_bytes() == original


def test_installer_rejects_tampered_archive(tmp_path: Path, installer_home: SimpleNamespace) -> None:
    origin_root = tmp_path / "static-origin"
    (origin_root / "api").mkdir(parents=True)
    (origin_root / "downloads" / "portmap").mkdir(parents=True)
    archive = fake_portmap_archive()
    (origin_root / "downloads" / "portmap" / LINUX_ASSET).write_bytes(archive)
    (origin_root / "api" / "downloads").write_text(
        json.dumps(
            {
                "name": "portmap",
                "version": VERSION,
                "available": True,
                "platforms": [
                    {
                        "os": "linux",
                        "arch": "amd64",
                        "filename": LINUX_ASSET,
                        "sha256": "c" * 64,
                        "size": len(archive),
                        "url": f"/downloads/portmap/{LINUX_ASSET}",
                        "source_url": f"https://github.com/ylang-ylang/portmap/releases/download/V0.7/{LINUX_ASSET}",
                    }
                ],
            }
        )
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *args: StaticOrigin(*args, directory=str(origin_root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = run_installer(f"http://127.0.0.1:{server.server_port}", installer_home=installer_home)
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (installer_home.data / "portmap" / VERSION).exists()
    assert list(installer_home.tmp.iterdir()) == []


def test_installer_rejects_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_portmap_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)

    shim = tmp_path / "uname-shim"
    shim.mkdir()
    for system, machine, expected in (
        ("Linux", "riscv64", "unsupported architecture"),
        ("SunOS", "x86_64", "unsupported operating system"),
    ):
        (shim / "uname").write_text(f"#!/bin/sh\n[ \"$1\" = -m ] && echo {machine} || echo {system}\n")
        (shim / "uname").chmod(0o755)
        result = run_installer(
            catalog.origin,
            installer_home=installer_home,
            extra_env={"PATH": f"{shim}:{os.environ['PATH']}"},
        )
        assert result.returncode != 0
        assert expected in result.stderr


def test_installer_enforces_linux_glibc_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_portmap_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)
    shim = tmp_path / "getconf-shim"
    shim.mkdir()

    def shim_getconf(script_body: str) -> None:
        (shim / "getconf").write_text(script_body)
        (shim / "getconf").chmod(0o755)

    def run_with_shim() -> subprocess.CompletedProcess:
        return run_installer(
            catalog.origin,
            installer_home=installer_home,
            extra_env={"PATH": f"{shim}:{os.environ['PATH']}"},
        )

    # Older than the Debian 11 baseline: refuse before downloading anything.
    shim_getconf('#!/bin/sh\n[ "$1" = GNU_LIBC_VERSION ] && echo "glibc 2.17" || exit 1\n')
    result = run_with_shim()
    assert result.returncode != 0
    assert "glibc 2.31" in result.stderr
    assert not (installer_home.data / "portmap" / VERSION).exists()
    assert list(installer_home.tmp.iterdir()) == []

    # musl-style systems do not answer GNU_LIBC_VERSION at all.
    shim_getconf('#!/bin/sh\nexit 1\n')
    result = run_with_shim()
    assert result.returncode != 0
    assert "musl/Alpine" in result.stderr

    # Exactly the baseline version installs fine.
    shim_getconf('#!/bin/sh\n[ "$1" = GNU_LIBC_VERSION ] && echo "glibc 2.31" || exit 1\n')
    result = run_with_shim()
    assert result.returncode == 0, result.stderr
    assert_installed(installer_home)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["https://catalog.example.com", "extra"],
        ["catalog.example.com"],
        ["ftp://catalog.example.com"],
        ["http://user@host"],
        ["http://"],
    ],
)
def test_installer_validates_origin_argument(args: list[str], installer_home: SimpleNamespace) -> None:
    env = {
        "HOME": str(installer_home.home),
        "XDG_DATA_HOME": str(installer_home.data),
        "TMPDIR": str(installer_home.tmp),
        "PATH": os.environ["PATH"],
    }
    result = subprocess.run(
        ["sh", str(INSTALL_SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode != 0
    assert "install:" in result.stderr
    assert list(installer_home.tmp.iterdir()) == []
