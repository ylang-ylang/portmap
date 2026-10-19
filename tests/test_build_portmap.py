"""Build identity and output behavior without invoking a freezer or Git."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import tarfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("build_portmap", TOOLS / "build_portmap.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
COMMIT = "a" * 40


@pytest.mark.parametrize("version", ["0.19.1", "0.20.0", "0.20.3", "1.0.1"])
def test_release_identity_matches_the_full_version_and_head(monkeypatch, version):
    monkeypatch.setattr(builder, "resolve_head", lambda: COMMIT)
    tag, source = builder.resolve_release_identity(version, f"v{version}", COMMIT)
    assert tag == f"v{version}"
    assert source == {"repository": "ylang-ylang/portmap", "tag": tag, "commit": COMMIT}


@pytest.mark.parametrize(
    "version, tag",
    [
        ("0.19.0", "V0.19"),
        ("0.19.0", "v0.19.0"),
        ("0.18.1", "v0.18.1"),
        ("0.0.0", "v0.0.0"),
        ("0.20.0", "V0.20"),
        ("0.20.3", "v0.20.0"),
        ("0.20.3", "v0.21.3"),
        ("0.20.3", "v0.20"),
        ("0.20.3", "v0.20.03"),
        ("0.20.03", "v0.20.03"),
    ],
)
def test_release_identity_rejects_legacy_mismatched_and_noncanonical_tags(monkeypatch, version, tag):
    def no_head_lookup():
        pytest.fail("invalid tag/version must be rejected before resolving HEAD")

    monkeypatch.setattr(builder, "resolve_head", no_head_lookup)
    with pytest.raises(ValueError):
        builder.resolve_release_identity(version, tag, COMMIT)


@pytest.mark.parametrize("tag, commit", [("v0.20.3", None), (None, COMMIT)])
def test_release_identity_requires_paired_flags(tag, commit):
    with pytest.raises(ValueError, match="paired"):
        builder.resolve_release_identity("0.20.3", tag, commit)


def test_release_identity_rejects_short_commit_and_wrong_head(monkeypatch):
    monkeypatch.setattr(builder, "resolve_head", lambda: "b" * 40)
    with pytest.raises(ValueError, match="40-hex"):
        builder.resolve_release_identity("0.20.3", "v0.20.3", "abc")
    with pytest.raises(ValueError, match="not the requested release source"):
        builder.resolve_release_identity("0.20.3", "v0.20.3", COMMIT)


@pytest.mark.parametrize("version", ["0.19.0", "0.20.0", "0.20.3"])
def test_local_candidate_uses_full_tag_without_claiming_source_identity(monkeypatch, version):
    def no_head_lookup():
        pytest.fail("local candidate does not claim a release source commit")

    monkeypatch.setattr(builder, "resolve_head", no_head_lookup)
    assert builder.resolve_release_identity(version, None, None) == (f"v{version}", None)


@pytest.mark.parametrize("release_mode", [False, True])
def test_patch_build_outputs_exact_urls_and_identity_without_mutating_source(tmp_path, monkeypatch, release_mode):
    root = tmp_path / "source"
    static = root / "src/portmap/catalog_static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("catalog\n")
    skill = root / "skills/project-integration/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("fixture skill\n")
    (root / "pyproject.toml").write_text('[project]\nversion = "0.20.3"\n')
    (root / "src/portmap/__init__.py").write_text('__version__ = "0.20.3"\n')
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    out = tmp_path / "output"
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder, "platform_key", lambda: "linux-amd64")
    monkeypatch.setattr(builder, "resolve_head", lambda: COMMIT)

    def freeze(command, *, cwd, env, check):
        assert command[1:3] == ["-m", "PyInstaller"]
        assert cwd == root and check is True
        staged_static = Path(env["PORTMAP_BUILD_STATIC_ROOT"])
        assert (staged_static / "skills/project-integration/SKILL.md").read_text() == "fixture skill\n"
        package = out / "frozen/portmap"
        package.mkdir(parents=True)
        executable = package / "portmap"
        executable.write_bytes(b"fixture executable\n")
        executable.chmod(0o755)
        (package / "release.json").write_text("stale manifest must not enter archive\n")

    monkeypatch.setattr(builder.subprocess, "run", freeze)
    argv = ["build_portmap.py", "--out-dir", str(out)]
    if release_mode:
        argv += ["--release-tag", "v0.20.3", "--source-commit", COMMIT]
    monkeypatch.setattr(builder.sys, "argv", argv)
    assert builder.main() == 0

    manifest_bytes = (out / "release.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["version"] == "0.20.3"
    if release_mode:
        assert manifest["source"] == {
            "repository": "ylang-ylang/portmap", "tag": "v0.20.3", "commit": COMMIT,
        }
    else:
        assert "source" not in manifest
    asset = manifest["assets"][0]
    filename = "portmap-0.20.3-linux-amd64.tar.gz"
    assert asset["filename"] == filename
    assert asset["source_url"] == f"https://github.com/ylang-ylang/portmap/releases/download/v0.20.3/{filename}"
    archive = out / filename
    assert asset["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert asset["size"] == archive.stat().st_size
    assert (out / "frozen/portmap/release.json").read_bytes() == manifest_bytes
    with tarfile.open(archive) as contents:
        assert contents.getnames() == ["portmap"]
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before
