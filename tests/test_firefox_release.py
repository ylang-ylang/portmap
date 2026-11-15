"""Consumer-facing packaging and immutable Firefox release boundaries."""
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("firefox_packaging", ROOT / "tools/build_firefox_extension.py")
packaging = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = packaging
_spec.loader.exec_module(packaging)
_pipeline_spec = importlib.util.spec_from_file_location("firefox_release_pipeline", ROOT / "tools/release_pipeline.py")
pipeline = importlib.util.module_from_spec(_pipeline_spec)
sys.modules[_pipeline_spec.name] = pipeline
_pipeline_spec.loader.exec_module(pipeline)


def test_development_package_is_reproducible_and_cannot_be_published(tmp_path):
    first, second = tmp_path / "first.xpi", tmp_path / "second.xpi"
    packaging.build(ROOT, first)
    packaging.build(ROOT, second)
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(ValueError, match="signature envelope"):
        packaging.check_signed_payload(ROOT, first)
    before = first.read_bytes()
    with pytest.raises(FileExistsError):
        packaging.build(ROOT, first)
    assert first.read_bytes() == before


def test_changed_runtime_bytes_are_rejected_even_with_signature_envelope(tmp_path):
    _, files = packaging.runtime_files(ROOT)
    path = tmp_path / "changed.xpi"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content + b"\n" if name == "core.js" else content)
        # This only exercises payload comparison, never signature verification.
        archive.writestr("META-INF/cose.sig", b"test-envelope-not-a-valid-signature")
    with pytest.raises(ValueError, match="differs from source: core.js"):
        packaging.check_signed_payload(ROOT, path)


def test_symlinked_runtime_file_is_not_packaged(tmp_path):
    source = tmp_path / "extension/firefox"
    source.mkdir(parents=True)
    (source / "manifest.json").symlink_to(ROOT / "extension/firefox/manifest.json")
    with pytest.raises(ValueError, match="symlinked"):
        packaging.build(tmp_path, tmp_path / "output.xpi")
    assert not (tmp_path / "output.xpi").exists()


@pytest.mark.parametrize("change", [
    {"signature_required": False}, {"platform": "desktop-firefox"},
    {"extension_sha256": "b" * 64}, {"source_commit": "c" * 40},
    {"status": "skipped"}, {"cases": []},
    {"cases": [{"name": "two nodes", "status": "skipped"}]},
])
def test_android_release_proof_cannot_be_replaced_by_unsigned_or_other_build(change, tmp_path):
    evidence = {
        "status": "passed", "platform": "android-firefox", "signature_required": True,
        "extension_sha256": "a" * 64, "extension_version": "0.21.0",
        "source_commit": "d" * 40, "browser_version": "128.0",
        "cases": [{"name": "two nodes", "status": "passed"}],
        **change,
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError):
        packaging.check_evidence(path, platform="android-firefox", digest="a" * 64,
                                 version="0.21.0", commit="d" * 40)


def release_fixture(tmp_path):
    version, tag = "0.21.0", "v0.21.0"
    records = []
    for name, body in (
        ("portmap-0.21.0-linux-amd64.tar.gz", b"runtime archive fixture"),
        ("portmap-firefox-0.21.0.xpi", b"signed XPI bytes fixture"),
        ("firefox-updates.json", b'{"addons":{}}\n'),
    ):
        path = tmp_path / name
        path.write_bytes(body)
        digest = packaging.sha256(path)
        (tmp_path / (name + ".sha256")).write_text(f"{digest}  {name}\n")
        records.append({"filename": name, "size": len(body), "sha256": digest,
                        "source_url": pipeline.canonical_source_url(tag, name)})
    records[0].update(os="linux", arch="amd64")
    manifest = {"version": version, "assets": [records[0]],
                "firefox": {"xpi": records[1], "updates": records[2]}}
    (tmp_path / "release.json").write_text(json.dumps(manifest))
    return manifest, records


def test_firefox_assets_participate_in_immutable_digest_validation(tmp_path):
    manifest, records = release_fixture(tmp_path)
    assert pipeline.validate_manifest(manifest, "0.21.0", tmp_path, tag="v0.21.0") == []
    names = pipeline.release_file_names(manifest)
    published = {record["filename"]: {"digest": "sha256:" + record["sha256"]}
                 for record in records}
    missing = pipeline._verify_published(published, manifest, json.dumps(manifest), names)
    assert "portmap-firefox-0.21.0.xpi" not in missing
    assert "firefox-updates.json.sha256" in missing
    published["portmap-firefox-0.21.0.xpi"]["digest"] = "sha256:" + "f" * 64
    with pytest.raises(pipeline.ReleaseError, match="immutable"):
        pipeline._verify_published(published, manifest, json.dumps(manifest), names)
    (tmp_path / "firefox-updates.json").write_text("tampered")
    errors = pipeline.validate_manifest(manifest, "0.21.0", tmp_path, tag="v0.21.0")
    assert "firefox asset content mismatch: firefox-updates.json" in errors


@pytest.mark.parametrize("filename", ["../outside.xpi", "other.xpi", "portmap-firefox-0.20.10.xpi"])
def test_release_manifest_cannot_redirect_firefox_asset_path(filename, tmp_path):
    manifest, _ = release_fixture(tmp_path)
    manifest["firefox"]["xpi"]["filename"] = filename
    assert pipeline.validate_manifest(manifest, "0.21.0", tmp_path, tag="v0.21.0")
