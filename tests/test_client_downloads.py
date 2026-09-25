import hashlib
import http.client
import http.server
import io
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import __version__
from portmap.catalog import CatalogHandler
from portmap.client_downloads import (
    INSTALL_SCRIPT_PATH,
    ClientAsset,
    ClientDownloadUnavailable,
    TrustedAssetRedirectHandler,
    archive_matches,
    client_download_dir,
    client_download_info,
    client_release_state,
    fetch_client_archive,
    parse_client_release,
    resolve_client_archive,
    validate_trusted_source_url,
)

VERSION = __version__
LINUX_ASSET = "portmap-client-0.7.0-linux-amd64.tar.gz"
DARWIN_ASSET = "portmap-client-0.7.0-darwin-arm64.tar.gz"


@pytest.fixture(autouse=True)
def isolated_client_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTMAP_ROOT", str(tmp_path / "config-root"))
    monkeypatch.setenv("PORTMAP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PORTMAP_CLIENT_DOWNLOAD_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(tmp_path / "missing-agent.sock"))


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any lazy GitHub fetch in a unit test is a bug: manifests must reject
    before network and preseeded caches must serve offline."""

    def boom():
        raise AssertionError("unexpected network fetch during test")

    monkeypatch.setattr("portmap.client_downloads.trusted_opener", boom)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name == "portmap-client" else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def fake_client_archive() -> bytes:
    return build_archive(
        {
            "portmap-client": f"#!/bin/sh\necho fake-client {VERSION}\n".encode(),
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
    resolved_filename = filename or f"portmap-client-{VERSION}-{os}-{arch}.tar.gz"
    return {
        "os": os,
        "arch": arch,
        "filename": resolved_filename,
        "sha256": sha256 or "a" * 64,
        "size": size if size is not None else 1024,
        "source_url": source_url
        or f"https://github.com/ylang-ylang/portmap/releases/download/V0.7/{resolved_filename}",
    }


def valid_manifest_payload() -> dict:
    return {"version": VERSION, "assets": [asset_payload(), asset_payload(os="darwin", arch="arm64")]}


def publish_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload) -> Path:
    manifest_path = tmp_path / "client_release.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("portmap.client_downloads.RELEASE_MANIFEST_PATH", manifest_path)
    return manifest_path


def unpublish_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "portmap.client_downloads.RELEASE_MANIFEST_PATH",
        tmp_path / "absent-client_release.json",
    )


def make_asset(**overrides) -> ClientAsset:
    return parse_client_release({"version": VERSION, "assets": [asset_payload(**overrides)]}).assets[0]


def publish_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, archive: bytes, filename: str = LINUX_ASSET
) -> dict:
    """Publish a checksum-consistent release with a preseeded offline cache."""
    asset = asset_payload(filename=filename, sha256=sha256_hex(archive), size=len(archive))
    publish_manifest(monkeypatch, tmp_path, {"version": VERSION, "assets": [asset]})
    cache = Path(os.environ["PORTMAP_CLIENT_DOWNLOAD_DIR"])
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


def test_parse_client_release_accepts_valid_manifest() -> None:
    release = parse_client_release(valid_manifest_payload())
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
            lambda p: p["assets"][0].update(filename="portmap-client.zip"),
            "filename is malformed",
            id="filename-pattern",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(filename="portmap-client-0.0.1-linux-amd64.tar.gz"),
            "does not match manifest version",
            id="filename-version",
        ),
        pytest.param(
            lambda p: p["assets"][0].update(filename="portmap-client-0.7.0-darwin-amd64.tar.gz"),
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
def test_parse_client_release_rejects_malformed_manifests(mutate, match: str) -> None:
    payload = valid_manifest_payload()
    mutate(payload)
    with pytest.raises(ValueError, match=match):
        parse_client_release(payload)


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


def test_client_release_state_reports_missing_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unpublish_manifest(monkeypatch, tmp_path)
    message, release = client_release_state()
    assert release is None
    assert "not published" in message
    info = client_download_info()
    assert info["available"] is False
    assert info["platforms"] == []
    assert info["install_script"] is None
    assert info["version"] is None
    assert info["message"] == message


def test_client_release_state_reports_malformed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    publish_manifest(monkeypatch, tmp_path, {"version": "not-semver", "assets": [asset_payload()]})
    message, release = client_release_state()
    assert release is None
    assert "invalid" in message
    assert client_download_info()["available"] is False


# --- HTTP routes ------------------------------------------------------------


def test_api_client_returns_manifest_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    archive = fake_client_archive()
    linux = asset_payload(sha256=sha256_hex(archive), size=len(archive))
    darwin = asset_payload(os="darwin", arch="arm64")
    publish_manifest(monkeypatch, tmp_path, {"version": VERSION, "assets": [linux, darwin]})

    status, headers, body = catalog.request("/api/client")
    assert status == 200
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {
        "name": "portmap-client",
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
                "url": f"/downloads/client/{LINUX_ASSET}",
                "requirements": "glibc 2.31+ (musl/Alpine unsupported)",
            },
            {
                "os": "darwin",
                "arch": "arm64",
                "filename": DARWIN_ASSET,
                "sha256": darwin["sha256"],
                "size": darwin["size"],
                "url": f"/downloads/client/{DARWIN_ASSET}",
                "requirements": None,
            },
        ],
        "install_script": "/install-client.sh",
    }


def test_api_client_honest_when_manifest_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    unpublish_manifest(monkeypatch, tmp_path)
    status, _, body = catalog.request("/api/client")
    payload = json.loads(body)
    assert status == 200
    assert payload["available"] is False
    assert payload["platforms"] == []
    assert payload["install_script"] is None
    assert "not published" in payload["message"]

    status, _, body = catalog.request("/install-client.sh")
    assert status == 404
    assert b"not published" in body

    status, _, _ = catalog.request(f"/downloads/client/{LINUX_ASSET}")
    assert status == 404


def test_api_client_honest_when_manifest_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    payload = valid_manifest_payload()
    payload["assets"][0]["sha256"] = "ZZZ"
    publish_manifest(monkeypatch, tmp_path, payload)
    status, _, body = catalog.request("/api/client")
    assert status == 200
    assert json.loads(body)["available"] is False
    assert json.loads(body)["platforms"] == []
    status, _, _ = catalog.request(f"/downloads/client/{LINUX_ASSET}")
    assert status == 404


def test_downloads_stream_preseeded_cache_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    archive = fake_client_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)

    status, headers, body = catalog.request(f"/downloads/client/{LINUX_ASSET}")
    assert status == 200
    assert headers["content-type"] == "application/gzip"
    assert int(headers["content-length"]) == len(archive)
    assert body == archive

    head_status, head_headers, head_body = catalog.request(f"/downloads/client/{LINUX_ASSET}", method="HEAD")
    assert head_status == 200
    assert int(head_headers["content-length"]) == len(archive)
    assert head_headers["content-type"] == "application/gzip"
    assert head_body == b""


def test_install_script_served_only_for_validated_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    publish_release(monkeypatch, tmp_path, archive=fake_client_archive())
    status, headers, body = catalog.request("/install-client.sh")
    assert status == 200
    assert headers["content-type"] == "text/x-shellscript; charset=utf-8"
    assert int(headers["content-length"]) == len(body)
    assert body == INSTALL_SCRIPT_PATH.read_bytes()
    assert b"portmap-client" in body


def test_downloads_reject_unknown_and_traversal_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer
) -> None:
    archive = fake_client_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)
    for path in (
        "/downloads/client/unknown.tar.gz",
        f"/downloads/client/{DARWIN_ASSET}",
        "/downloads/client/../client_release.json",
        "/downloads/client/%2e%2e%2fclient_release.json",
        "/downloads/client/",
        "/downloads/client",
    ):
        status, _, _ = catalog.request(path)
        assert status == 404, path
    # Preseeded serving never fetches, so no lock or temp artifacts appear.
    cache = Path(os.environ["PORTMAP_CLIENT_DOWNLOAD_DIR"])
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
    archive = fake_client_archive()
    asset = asset_payload(sha256=sha256_hex(archive), size=len(archive))
    publish_manifest(monkeypatch, tmp_path, {"version": VERSION, "assets": [asset]})
    cache = Path(os.environ["PORTMAP_CLIENT_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    (cache / LINUX_ASSET).write_bytes(b"corrupted bytes")

    opens: list[str] = []
    monkeypatch.setattr("portmap.client_downloads.trusted_opener", stub_opener_factory(archive, opens))

    resolved = resolve_client_archive(LINUX_ASSET)
    assert resolved.read_bytes() == archive
    assert opens == [asset["source_url"]]
    # The verified download replaced the corrupt cache entry.
    assert archive_matches(cache / LINUX_ASSET, make_asset(**asset))
    # A second resolve is served from the verified cache without refetching.
    assert resolve_client_archive(LINUX_ASSET).read_bytes() == archive
    assert len(opens) == 1


def test_downloads_lazy_fetch_verifies_size_and_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = fake_client_archive()
    cache = Path(os.environ["PORTMAP_CLIENT_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    opens: list[str] = []

    def publish(**overrides) -> None:
        payload = {"version": VERSION, "assets": [asset_payload(**overrides)]}
        publish_manifest(monkeypatch, tmp_path, payload)

    monkeypatch.setattr("portmap.client_downloads.trusted_opener", stub_opener_factory(archive, opens))

    # Shorter than the manifest size: reject and keep the cache clean.
    publish(sha256=sha256_hex(archive), size=len(archive) + 1)
    with pytest.raises(ClientDownloadUnavailable, match="size mismatch"):
        resolve_client_archive(LINUX_ASSET)
    assert not (cache / LINUX_ASSET).exists()

    # Checksum mismatch: reject and keep the cache clean.
    publish(sha256="b" * 64, size=len(archive))
    with pytest.raises(ClientDownloadUnavailable, match="checksum mismatch"):
        resolve_client_archive(LINUX_ASSET)
    assert not (cache / LINUX_ASSET).exists()

    # No stray temp files survive the failures.
    assert [item.name for item in cache.iterdir() if item.name.endswith(".part")] == []

    # A healthy pinned fetch lands atomically in the cache.
    publish(sha256=sha256_hex(archive), size=len(archive))
    assert resolve_client_archive(LINUX_ASSET).read_bytes() == archive


def test_fetch_unreachable_upstream_is_service_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    publish_manifest(monkeypatch, tmp_path, {"version": VERSION, "assets": [asset_payload()]})
    cache = Path(os.environ["PORTMAP_CLIENT_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)

    def failing_factory():
        class StubOpener:
            def open(self, request, timeout=None):
                raise OSError("connection refused")

        return StubOpener()

    monkeypatch.setattr("portmap.client_downloads.trusted_opener", failing_factory)
    with pytest.raises(ClientDownloadUnavailable, match="failed to download"):
        fetch_client_archive(make_asset(), cache)
    assert [item.name for item in cache.iterdir() if item.name.endswith(".part")] == []


def test_download_cache_dir_defaults_to_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORTMAP_CLIENT_DOWNLOAD_DIR")
    assert client_download_dir() == Path(os.environ["PORTMAP_STATE_DIR"]) / "client-downloads"


def test_concurrent_resolves_share_one_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archive = fake_client_archive()
    publish_manifest(
        monkeypatch,
        tmp_path,
        {"version": VERSION, "assets": [asset_payload(sha256=sha256_hex(archive), size=len(archive))]},
    )
    cache = Path(os.environ["PORTMAP_CLIENT_DOWNLOAD_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    # No preseeded archive: both threads must lazily fetch under the lock.
    opens: list[str] = []
    monkeypatch.setattr(
        "portmap.client_downloads.trusted_opener",
        stub_opener_factory(archive, opens, delay=0.05),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda _: resolve_client_archive(LINUX_ASSET), range(2)))
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



def test_installer_refuses_unrelated_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_client_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)
    unrelated = installer_home.data / "portmap-client" / VERSION
    unrelated.mkdir(parents=True)
    (unrelated / "precious.txt").write_text("keep me")

    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode != 0
    assert "refusing to overwrite unrelated directory" in result.stderr
    assert (unrelated / "precious.txt").read_text() == "keep me"
    assert list((installer_home.data / "portmap-client").iterdir()) == [unrelated]
    assert list(installer_home.tmp.iterdir()) == []


def test_installer_refuses_unrelated_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_client_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)
    bin_dir = installer_home.home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "portmap-client"
    launcher.write_text("#!/bin/sh\nexit 42\n")
    launcher.chmod(0o755)

    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode != 0
    assert "refusing to replace unrelated" in result.stderr
    assert launcher.read_text().startswith("#!/bin/sh")
    assert not (installer_home.data / "portmap-client" / VERSION).exists()

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
    assert not (installer_home.data / "portmap-client" / "evil.txt").exists()
    # Nothing was installed and no staging artifacts survive the failure.
    assert not (installer_home.data / "portmap-client" / VERSION).exists()
    stage_root = installer_home.data / "portmap-client"
    assert not stage_root.exists() or list(stage_root.iterdir()) == []
    assert list(installer_home.tmp.iterdir()) == []


def archive_with_link_member(member_type: str, linkname: str) -> bytes:
    buffer = io.BytesIO()
    executable = f"#!/bin/sh\necho fake-client {VERSION}\n".encode()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("portmap-client")
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
    executable = f"#!/bin/sh\necho fake-client {VERSION}\n".encode()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("portmap-client")
        info.size = len(executable)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(executable))
    return buffer.getvalue()


def test_installer_preserves_install_when_archive_layout_is_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    good = fake_client_archive()
    publish_release(monkeypatch, tmp_path, archive=good)
    assert run_installer(catalog.origin, installer_home=installer_home).returncode == 0
    assert_installed(installer_home)

    # A builder regression wraps everything in an enclosing directory.
    wrapped = build_archive(
        {
            "portmap-client/portmap-client": f"#!/bin/sh\necho wrong-layout {VERSION}\n".encode(),
            "portmap-client/_internal/note.txt": b"x\n",
        }
    )
    publish_release(monkeypatch, tmp_path, archive=wrapped)
    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode != 0
    assert "unexpected layout" in result.stderr
    # The previous install and launcher survive untouched and still run.
    link = installer_home.home / ".local" / "bin" / "portmap-client"
    ran = subprocess.run([str(link), "--version"], capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0
    assert ran.stdout.strip() == f"fake-client {VERSION}"
    assert [item.name for item in (installer_home.data / "portmap-client").iterdir()] == [VERSION]
    assert list(installer_home.tmp.iterdir()) == []


def test_installer_rejects_non_executable_root_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    publish_release(monkeypatch, tmp_path, archive=non_executable_archive())
    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode != 0
    assert "does not ship an executable" in result.stderr
    assert not (installer_home.data / "portmap-client" / VERSION).exists()
    assert list(installer_home.tmp.iterdir()) == []


@pytest.mark.parametrize(
    "member_type, linkname",
    [
        (tarfile.SYMTYPE, "/etc/passwd"),
        (tarfile.LNKTYPE, "portmap-client"),
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
    assert not (installer_home.data / "portmap-client" / VERSION).exists()
    assert list(installer_home.tmp.iterdir()) == []

def assert_installed(installer_home: SimpleNamespace) -> Path:
    bundle = installer_home.data / "portmap-client" / VERSION
    assert (bundle / ".portmap-client-install").is_file()
    assert (bundle / "portmap-client").is_file()
    link = installer_home.home / ".local" / "bin" / "portmap-client"
    assert link.is_symlink()
    # The launcher must point at the executable inside the bundle — a symlink
    # to the bundle directory is not runnable (execve(2) of a dir is EACCES).
    assert os.readlink(link) == str(bundle / "portmap-client")
    ran = subprocess.run(
        [str(link), "--version"], capture_output=True, text=True, timeout=30
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == f"fake-client {VERSION}"
    # No staging or download temp files survive a successful install.
    assert [item.name for item in (installer_home.data / "portmap-client").iterdir()] == [VERSION]
    assert list(installer_home.tmp.iterdir()) == []
    return bundle


def test_installer_end_to_end_from_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_client_archive()
    publish_release(monkeypatch, tmp_path, archive=archive)

    result = run_installer(catalog.origin, installer_home=installer_home)
    assert result.returncode == 0, result.stderr
    assert_installed(installer_home)

    # A managed reinstall of the same version upgrades in place.
    again = run_installer(catalog.origin, installer_home=installer_home)
    assert again.returncode == 0, again.stderr
    assert_installed(installer_home)


def test_installer_reports_missing_readlink_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    publish_release(monkeypatch, tmp_path, archive=fake_client_archive())
    initial = run_installer(catalog.origin, installer_home=installer_home)
    assert initial.returncode == 0, initial.stderr
    bundle = assert_installed(installer_home)
    original = (bundle / "portmap-client").read_bytes()
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
    assert (bundle / "portmap-client").read_bytes() == original


def test_installer_rejects_tampered_archive(tmp_path: Path, installer_home: SimpleNamespace) -> None:
    origin_root = tmp_path / "static-origin"
    (origin_root / "api").mkdir(parents=True)
    (origin_root / "downloads" / "client").mkdir(parents=True)
    archive = fake_client_archive()
    (origin_root / "downloads" / "client" / LINUX_ASSET).write_bytes(archive)
    (origin_root / "api" / "client").write_text(
        json.dumps(
            {
                "name": "portmap-client",
                "version": VERSION,
                "available": True,
                "platforms": [
                    {
                        "os": "linux",
                        "arch": "amd64",
                        "filename": LINUX_ASSET,
                        "sha256": "c" * 64,
                        "size": len(archive),
                        "url": f"/downloads/client/{LINUX_ASSET}",
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
    assert not (installer_home.data / "portmap-client" / VERSION).exists()
    assert list(installer_home.tmp.iterdir()) == []


def test_installer_rejects_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: CatalogServer, installer_home: SimpleNamespace
) -> None:
    archive = fake_client_archive()
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
    archive = fake_client_archive()
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
    assert not (installer_home.data / "portmap-client" / VERSION).exists()
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
    assert "install-client:" in result.stderr
    assert list(installer_home.tmp.iterdir()) == []
