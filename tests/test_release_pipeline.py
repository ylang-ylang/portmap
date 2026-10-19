"""Behavior tests for the tag-driven release pipeline (tools/release_pipeline.py).

These exercise real git operations against temporary repositories with a bare
``Origin`` and a scripted gh boundary, so tag validation, catalog-tree
pinning, manifest identity gating, immutable-asset completeness and
draft/published retry behavior are verified as behavior, not as text
matching.
"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "tools" / "release_pipeline.py"

_spec = importlib.util.spec_from_file_location("release_pipeline", SCRIPT_PATH)
release_pipeline = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("release_pipeline", release_pipeline)
_spec.loader.exec_module(release_pipeline)


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Release Tester",
            "GIT_AUTHOR_EMAIL": "tester@example.com",
            "GIT_COMMITTER_NAME": "Release Tester",
            "GIT_COMMITTER_EMAIL": "tester@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
    )
    return env


GIT_ENV = _git_env()


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        env=GIT_ENV,
    )


def git_text(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def write_repo_files(
    repo: Path,
    version: str,
    *,
    committed_manifest: dict | None = None,
    init_version: str | None = None,
    lock_version: str | None = None,
    write_lock: bool = True,
) -> None:
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "portmap"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    init = repo / "src" / "portmap" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f'__version__ = "{init_version or version}"\n', encoding="utf-8")
    lock = repo / "uv.lock"
    if write_lock:
        lock.write_text(
            "version = 1\n\n[[package]]\n"
            f'name = "portmap"\nversion = "{lock_version or version}"\n',
            encoding="utf-8",
        )
    elif lock.exists():
        lock.unlink()
    if committed_manifest is not None:
        (repo / "src" / "portmap" / "release.json").write_text(
            json.dumps(committed_manifest, indent=2) + "\n", encoding="utf-8"
        )
    static = repo / "src" / "portmap" / "catalog_static"
    (static / "assets").mkdir(parents=True, exist_ok=True)
    (static / "skills" / "project-integration").mkdir(parents=True, exist_ok=True)
    (static / "__init__.py").write_text("", encoding="utf-8")
    (static / "assets" / "catalog.js").write_text("// catalog ui\n", encoding="utf-8")
    (static / "skills" / "project-integration" / "SKILL.md").write_text(
        "# portmap skill {{PORTMAP_VERSION}}\n", encoding="utf-8"
    )


HISTORICAL_MANIFEST = {
    "version": "0.19.0",
    "assets": [
        {
            "os": "linux",
            "arch": "amd64",
            "filename": "portmap-0.19.0-linux-amd64.tar.gz",
            "sha256": "a" * 64,
            "size": 1234,
            "source_url": (
                "https://github.com/ylang-ylang/portmap/releases/download/"
                "V0.19/portmap-0.19.0-linux-amd64.tar.gz"
            ),
        }
    ],
}


def make_repo(tmp_path: Path) -> Path:
    """Main history with historical V0.19, normal v0.20.0 and hotfix v0.20.1.

    Only V0.19 carries a committed src/portmap/release.json manifest.
    v0.21.0 never reached main; v0.22.0 has source-version drift.
    v0.20.2 deliberately points at the v0.20.1 source version.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.name", "Release Tester")
    git(work, "config", "user.email", "tester@example.com")
    git(work, "config", "commit.gpgsign", "false")

    write_repo_files(work, "0.19.0", committed_manifest=HISTORICAL_MANIFEST)
    git(work, "add", "-A")
    git(work, "commit", "-m", "release V0.19 historical state")
    git(work, "tag", "-a", "V0.19", "-m", "release V0.19 portmap 0.19.0")

    write_repo_files(work, "0.20.0")
    # New-style tags commit no manifest: release.json is build-generated.
    (work / "src" / "portmap" / "release.json").unlink()
    git(work, "add", "-A")
    git(work, "commit", "-m", "prepare v0.20.0")
    git(work, "tag", "v0.20.0")

    write_repo_files(work, "0.20.1")
    git(work, "add", "-A")
    git(work, "commit", "-m", "prepare hotfix v0.20.1")
    git(work, "tag", "v0.20.1")
    git(work, "tag", "v0.20.2")

    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-u", "origin", "main", "refs/tags/V0.19", "refs/tags/v0.20.0", "refs/tags/v0.20.1", "refs/tags/v0.20.2")

    # Future work that never merged into main.
    git(work, "checkout", "--quiet", "-b", "dev")
    write_repo_files(work, "0.21.0")
    git(work, "add", "-A")
    git(work, "commit", "-m", "dev-only work")
    git(work, "tag", "v0.21.0")
    write_repo_files(work, "0.21.1")
    git(work, "add", "-A")
    git(work, "commit", "-m", "unmerged patch")
    git(work, "tag", "v0.21.1")
    git(work, "checkout", "--quiet", "main")

    write_repo_files(work, "0.22.3")
    git(work, "add", "-A")
    git(work, "commit", "-m", "patch drift")
    git(work, "tag", "v0.22.0")

    write_repo_files(work, "0.23.0", init_version="0.22.9")
    git(work, "add", "-A")
    git(work, "commit", "-m", "init drift")
    git(work, "tag", "v0.23.0")

    write_repo_files(work, "0.24.0", lock_version="0.24.9")
    git(work, "add", "-A")
    git(work, "commit", "-m", "lock drift")
    git(work, "tag", "v0.24.0")

    write_repo_files(work, "0.25.0", write_lock=False)
    git(work, "add", "-A")
    git(work, "commit", "-m", "no lockfile")
    git(work, "tag", "v0.25.0")
    git(
        work,
        "push",
        "origin",
        "main",
        "refs/tags/v0.22.0",
        "refs/tags/v0.23.0",
        "refs/tags/v0.24.0",
        "refs/tags/v0.25.0",
    )
    git(work, "checkout", "--quiet", "main")
    return work


def make_assets(
    base_dir: Path,
    version: str,
    tag: str,
    commit: str | None,
    payload: bytes = b"portmap-frozen-bundle",
) -> tuple[Path, str]:
    """Build a dist-style asset directory; commit embeds the source block."""
    assets = base_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    filename = f"portmap-{version}-linux-amd64.tar.gz"
    archive = assets / filename
    archive.write_bytes(payload)
    sha = release_pipeline.digest(archive)
    (assets / f"{filename}.sha256").write_text(f"{sha}  {filename}\n", encoding="utf-8")
    manifest: dict = {
        "version": version,
        "assets": [
            {
                "os": "linux",
                "arch": "amd64",
                "filename": filename,
                "sha256": sha,
                "size": len(payload),
                "source_url": (
                    f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/{filename}"
                ),
            }
        ],
    }
    if commit is not None:
        manifest["source"] = {
            "repository": "ylang-ylang/portmap",
            "tag": tag,
            "commit": commit,
        }
    text = json.dumps(manifest, indent=2) + "\n"
    (assets / "release.json").write_text(text, encoding="utf-8")
    return assets, text


class FakeGh:
    """Records gh argv and answers from a scripted queue.

    Responses are (returncode, stdout, stderr) triples matching the real
    gh_runner contract. ``download`` is an optional callable receiving the
    parsed --dir that materializes the manifest asset for
    ``gh release download`` calls.
    """

    def __init__(self, responses: list[tuple[int, str, str]], download=None):
        self.responses = list(responses)
        self.calls: list[list[str]] = []
        self.download = download

    def __call__(self, argv: list[str], repo: Path) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if argv[:2] == ["release", "download"]:
            if self.download is None:
                raise AssertionError(f"unexpected gh release download: {argv}")
            dest = Path(argv[argv.index("--dir") + 1])
            self.download(dest)
            return 0, "", ""
        if self.responses:
            return self.responses.pop(0)
        raise AssertionError(f"unexpected gh call: {argv}")


NOT_FOUND: tuple[int, str, str] = (1, "", "gh: Not Found (HTTP 404)")


def rest_response(
    anchor_text: str,
    *,
    skip: set[str] = set(),
    draft: bool = False,
    body: str = "",
    tamper: dict[str, str] = {},
) -> tuple[int, str, str]:
    """A REST release payload derived from the anchor manifest bytes.

    ``tamper`` overrides the digest of named assets (for refusal tests).
    """
    manifest = json.loads(anchor_text)
    entries = []
    for name in release_pipeline.release_file_names(manifest):
        if name in skip:
            continue
        if name == "release.json":
            content_digest = release_pipeline.digest_bytes(anchor_text.encode("utf-8"))
            size = len(anchor_text.encode("utf-8"))
        elif name.endswith(".sha256"):
            filename = name[: -len(".sha256")]
            entry = next(a for a in manifest["assets"] if a["filename"] == filename)
            content = f"{entry['sha256']}  {filename}\n".encode("utf-8")
            content_digest = release_pipeline.digest_bytes(content)
            size = len(content)
        else:
            entry = next(a for a in manifest["assets"] if a["filename"] == name)
            content_digest = entry["sha256"]
            size = entry["size"]
        if name in tamper:
            content_digest = tamper[name]
        entries.append({"name": name, "size": size, "digest": "sha256:" + content_digest})
    document = {"draft": draft, "assets": entries}
    if body:
        document["body"] = body
    return 0, json.dumps(document), ""


def call_of(gh: FakeGh, *prefix: str) -> list[str]:
    return next(call for call in gh.calls if call[: len(prefix)] == list(prefix))


# --- validate-tag (trusted bootstrap) ---------------------------------------


@pytest.mark.parametrize("tag,version", [("v0.20.0", "0.20.0"), ("v0.20.1", "0.20.1")])
def test_validate_tag_push_new_style(tmp_path: Path, tag: str, version: str) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", f"{tag}^{{commit}}")
    result = release_pipeline.validate_tag(
        work, event_name="push", event_tag=tag, event_sha=peeled
    )
    assert result == {
        "tag": tag,
        "version": version,
        "commit": peeled,
        "historical": False,
        "event": "push",
    }


def test_validate_tag_historical_annotated_tag(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "V0.19^{commit}")
    result = release_pipeline.validate_tag(
        work, event_name="workflow_dispatch", event_tag="V0.19"
    )
    assert result["historical"] is True
    assert result["version"] == "0.19.0"
    assert result["commit"] == peeled


def test_validate_tag_dispatch_accepts_no_event_sha(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    result = release_pipeline.validate_tag(
        work, event_name="workflow_dispatch", event_tag="v0.20.0", event_sha=None
    )
    assert result["version"] == "0.20.0"


@pytest.mark.parametrize(
    "tag",
    ["v0.20", "V0.20", "V0.20.0", "V1.2", "V0.019", "v00.20.0", "v0.020.1", "v0.20.01",
     "v0.19.0", "v0.18.1", "v0.0.0",
     "v0.20.1.2", "v0.20.1-rc1", "v0.20.1+build", " v0.20.0", "v0.20.0\n", "V20", "V0.20-rc1", ""],
)
def test_validate_tag_rejects_non_release_tags(tmp_path: Path, tag: str) -> None:
    work = make_repo(tmp_path)
    with pytest.raises(release_pipeline.ReleaseError, match="v<major>.<minor>.<patch>"):
        release_pipeline.validate_tag(work, event_name="push", event_tag=tag, event_sha="0" * 40)


def test_validate_tag_rejects_unknown_tag(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    with pytest.raises(release_pipeline.ReleaseError, match="does not exist"):
        release_pipeline.validate_tag(work, event_name="workflow_dispatch", event_tag="v0.99.0")


@pytest.mark.parametrize("tag", ["v0.20.0", "v0.20.1"])
def test_validate_tag_rejects_push_sha_mismatch(tmp_path: Path, tag: str) -> None:
    work = make_repo(tmp_path)
    with pytest.raises(release_pipeline.ReleaseError, match="peeled commit"):
        release_pipeline.validate_tag(
            work, event_name="push", event_tag=tag, event_sha="0" * 40
        )


@pytest.mark.parametrize("tag", ["v0.21.0", "v0.21.1"])
def test_validate_tag_rejects_foreign_history(tmp_path: Path, tag: str) -> None:
    work = make_repo(tmp_path)
    with pytest.raises(release_pipeline.ReleaseError, match="origin/main history"):
        release_pipeline.validate_tag(work, event_name="workflow_dispatch", event_tag=tag)


@pytest.mark.parametrize(
    "tag,version", [("v0.22.0", "0.22.0"), ("v0.20.2", "0.20.2")]
)
def test_validate_tag_rejects_patch_version_mismatch(tmp_path: Path, tag: str, version: str) -> None:
    work = make_repo(tmp_path)
    with pytest.raises(release_pipeline.ReleaseError, match=f"requires exactly '{version}'"):
        release_pipeline.validate_tag(work, event_name="workflow_dispatch", event_tag=tag)


# --- check-catalog (Brew ships the tagged source tree) -----------------------


def test_check_catalog_accepts_identical_rebuild(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "--quiet", "v0.20.0")
    result = release_pipeline.check_catalog(work)
    assert result["catalog_static"] == "identical"
    assert "skills/project-integration/SKILL.md" in result["checked"]


def test_check_catalog_rejects_modified_asset(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "--quiet", "v0.20.0")
    (work / "src/portmap/catalog_static/assets/catalog.js").write_text(
        "// rebuilt differently\n", encoding="utf-8"
    )
    with pytest.raises(release_pipeline.ReleaseError, match="catalog.js"):
        release_pipeline.check_catalog(work)


def test_check_catalog_rejects_untracked_rebuild_output(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "--quiet", "v0.20.0")
    (work / "src/portmap/catalog_static/assets/extra.js").write_text("x", encoding="utf-8")
    with pytest.raises(release_pipeline.ReleaseError, match="extra.js"):
        release_pipeline.check_catalog(work)


def test_check_catalog_requires_generated_skill(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "--quiet", "v0.20.0")
    (work / "src/portmap/catalog_static/skills/project-integration/SKILL.md").unlink()
    with pytest.raises(release_pipeline.ReleaseError, match="SKILL.md"):
        release_pipeline.check_catalog(work)


# --- check-build (build-generated manifest identity) -------------------------


@pytest.mark.parametrize("tag,version", [("v0.20.0", "0.20.0"), ("v0.20.1", "0.20.1")])
def test_check_build_accepts_tag_identity(tmp_path: Path, tag: str, version: str) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", f"{tag}^{{commit}}")
    assets, _ = make_assets(tmp_path, version, tag, peeled)
    result = release_pipeline.check_build(assets, tag=tag, commit=peeled)
    assert result["version"] == version
    assert result["assets"][0]["filename"] == f"portmap-{version}-linux-amd64.tar.gz"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda source: source.update(commit="0" * 40),
        lambda source: source.update(tag="v0.21.0"),
        lambda source: source.update(repository="evil/example"),
        lambda source: source.clear(),
    ],
)
def test_check_build_rejects_wrong_source_identity(tmp_path: Path, mutation) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    assets, text = make_assets(tmp_path, "0.20.0", "v0.20.0", peeled)
    manifest = json.loads(text)
    mutation(manifest["source"])
    (assets / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(release_pipeline.ReleaseError, match="source identity"):
        release_pipeline.check_build(assets, tag="v0.20.0", commit=peeled)


def test_check_build_rejects_archive_digest_drift(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    assets, _ = make_assets(tmp_path, "0.20.0", "v0.20.0", peeled)
    (assets / "portmap-0.20.0-linux-amd64.tar.gz").write_bytes(b"tampered")
    with pytest.raises(release_pipeline.ReleaseError, match="sha256 mismatch"):
        release_pipeline.check_build(assets, tag="v0.20.0", commit=peeled)


def test_check_build_rejects_historical_shape_without_identity(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    assets, _ = make_assets(tmp_path, "0.20.0", "v0.20.0", None)
    with pytest.raises(release_pipeline.ReleaseError, match="source identity"):
        release_pipeline.check_build(assets, tag="v0.20.0", commit=peeled)


# --- inspect-release ----------------------------------------------------------


def test_inspect_release_absent(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    gh = FakeGh([NOT_FOUND])
    result = release_pipeline.inspect_release(work, tag="v0.20.0", runner=gh)
    assert result["release_present"] is False
    assert result["is_draft"] is False
    assert result["anchor"] is None
    assert result["recover_artifact"] is None


def test_inspect_release_incomplete_draft_names_recovery_artifact(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    _, anchor_text = make_assets(tmp_path / "build", "0.20.0", "v0.20.0", peeled)
    archive = "portmap-0.20.0-linux-amd64.tar.gz"
    body = (
        "Automated release v0.20.0 (portmap 0.20.0).\n\n"
        "Portmap-Version: 0.20.0\nPortmap-Source: deadbeef\n"
        "Portmap-Build-Run: 4242\nPortmap-Build-Attempt: 3\n"
    )
    doc = rest_response(
        anchor_text,
        skip={archive},
        draft=True,
        body=body,
    )
    gh = FakeGh([doc], download=lambda dest: (dest / "release.json").write_text(anchor_text, "utf-8"))
    result = release_pipeline.inspect_release(work, tag="v0.20.0", runner=gh)
    assert result["release_present"] is True
    assert result["is_draft"] is True
    assert result["anchor"] == "release"
    assert result["missing"] == [archive]
    assert result["origin_run_id"] == "4242"
    assert result["origin_run_attempt"] == "3"
    assert result["recover_artifact"] == "release-assets-4242-3"


def test_inspect_release_published_complete_needs_no_recovery(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    _, anchor_text = make_assets(tmp_path / "build", "0.20.0", "v0.20.0", peeled)
    doc = rest_response(anchor_text, draft=False, body="Portmap-Build-Run: 11\n")
    gh = FakeGh([doc], download=lambda dest: (dest / "release.json").write_text(anchor_text, "utf-8"))
    result = release_pipeline.inspect_release(work, tag="v0.20.0", runner=gh)
    assert result["missing"] == []
    assert result["recover_artifact"] is None
    assert result["anchor"] == "release"


# --- publish-release ----------------------------------------------------------


@pytest.mark.parametrize("tag,version", [("v0.20.0", "0.20.0"), ("v0.20.1", "0.20.1")])
def test_publish_creates_complete_draft_then_publishes(tmp_path: Path, tag: str, version: str) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", f"{tag}^{{commit}}")
    assets, anchor_text = make_assets(tmp_path, version, tag, peeled)
    final = rest_response(anchor_text, draft=True)
    gh = FakeGh([NOT_FOUND, (0, "", ""), final, (0, "", "")])
    result = release_pipeline.publish_release(
        work, tag=tag, assets_dir=assets, run_id="77", run_attempt="1", runner=gh
    )
    assert result["created"] is True
    assert sorted(result["uploaded"]) == [
        f"portmap-{version}-linux-amd64.tar.gz",
        f"portmap-{version}-linux-amd64.tar.gz.sha256",
        "release.json",
    ]
    create = call_of(gh, "release", "create")
    assert create[2] == tag
    assert "--draft" in create and "--verify-tag" in create
    assert any(argument.endswith(f"portmap-{version}-linux-amd64.tar.gz") for argument in create)
    assert any(argument.endswith("release.json") for argument in create)
    notes = (assets / "release-notes.md").read_text(encoding="utf-8")
    assert f"Portmap-Build-Run: 77" in notes
    assert "Portmap-Build-Attempt: 1" in notes
    edit = call_of(gh, "release", "edit")
    assert "--draft=false" in edit


def test_publish_refuses_manifest_with_foreign_identity(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    assets, _ = make_assets(tmp_path, "0.20.0", "v0.20.0", "f" * 40)
    gh = FakeGh([NOT_FOUND])
    with pytest.raises(release_pipeline.ReleaseError, match="source identity"):
        release_pipeline.publish_release(work, tag="v0.20.0", assets_dir=assets, run_id="7", runner=gh)
    assert not any(call[:2] == ["release", "create"] for call in gh.calls)


def test_publish_without_release_or_build_refuses(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    empty = tmp_path / "empty-assets"
    empty.mkdir()
    gh = FakeGh([NOT_FOUND])
    with pytest.raises(release_pipeline.ReleaseError, match="re-run the original"):
        release_pipeline.publish_release(
            work, tag="v0.20.0", assets_dir=empty, run_id="7", runner=gh
        )
    assert not any(call[:2] == ["release", "create"] for call in gh.calls)


def test_publish_completes_draft_from_original_attempt_bytes(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    original_payload = b"original-run-bytes"
    original_assets, anchor_text = make_assets(
        tmp_path / "original", "0.20.0", "v0.20.0", peeled, payload=original_payload
    )
    archive = "portmap-0.20.0-linux-amd64.tar.gz"
    partial = rest_response(
        anchor_text,
        skip={archive},
        draft=True,
        body="Portmap-Build-Run: 901\nPortmap-Build-Attempt: 1\n",
    )
    final = rest_response(anchor_text, draft=True)
    gh = FakeGh(
        [partial, (0, "", ""), final, (0, "", "")],
        download=lambda dest: (dest / "release.json").write_text(anchor_text, "utf-8"),
    )
    result = release_pipeline.publish_release(
        work, tag="v0.20.0", assets_dir=original_assets, run_id="999", run_attempt="2", runner=gh
    )
    assert result["created"] is False
    assert result["uploaded"] == [archive]
    upload = call_of(gh, "release", "upload")
    assert upload[2] == "v0.20.0"
    assert upload[3].endswith(archive)
    assert "--draft=false" in call_of(gh, "release", "edit")


def test_publish_refuses_rebuilt_archive_for_existing_anchor(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    original_assets, anchor_text = make_assets(
        tmp_path / "original", "0.20.0", "v0.20.0", peeled, payload=b"original-run-bytes"
    )
    # The re-run rebuild produced different archive bytes than the anchor.
    rebuilt = original_assets / "portmap-0.20.0-linux-amd64.tar.gz"
    rebuilt.write_bytes(b"rebuilt-different-bytes")
    archive = "portmap-0.20.0-linux-amd64.tar.gz"
    partial = rest_response(anchor_text, skip={archive}, draft=True)
    gh = FakeGh(
        [partial],
        download=lambda dest: (dest / "release.json").write_text(anchor_text, "utf-8"),
    )
    with pytest.raises(release_pipeline.ReleaseError, match="anchor sha256"):
        release_pipeline.publish_release(
            work, tag="v0.20.0", assets_dir=original_assets, run_id="999", runner=gh
        )
    assert not any(call[:2] == ["release", "upload"] for call in gh.calls)


def test_publish_published_complete_is_idempotent_noop(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    assets, anchor_text = make_assets(tmp_path, "0.20.0", "v0.20.0", peeled)
    doc = rest_response(anchor_text, draft=False)
    gh = FakeGh([doc, doc], download=lambda dest: (dest / "release.json").write_text(anchor_text, "utf-8"))
    result = release_pipeline.publish_release(
        work, tag="v0.20.0", assets_dir=assets, run_id="5", runner=gh
    )
    assert result["created"] is False
    assert result["uploaded"] == []
    assert not any(call[:2] in (["release", "upload"], ["release", "edit"]) for call in gh.calls)
    assert not any(call[:2] == ["release", "create"] for call in gh.calls)


def test_publish_published_incomplete_is_hard_failure(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    # Local bytes matching the anchor exist, yet a published release must
    # never be completed or rebuilt — the refusal happens before any write.
    assets, anchor_text = make_assets(tmp_path, "0.20.0", "v0.20.0", peeled)
    archive = "portmap-0.20.0-linux-amd64.tar.gz"
    doc = rest_response(anchor_text, skip={archive}, draft=False)
    gh = FakeGh(
        [doc],
        download=lambda dest: (dest / "release.json").write_text(anchor_text, "utf-8"),
    )
    with pytest.raises(release_pipeline.ReleaseError, match="published release v0.20.0 is incomplete"):
        release_pipeline.publish_release(work, tag="v0.20.0", assets_dir=assets, run_id="5", runner=gh)
    assert not any(call[:2] in (["release", "upload"], ["release", "create"], ["release", "edit"]) for call in gh.calls)

def test_publish_historical_incomplete_published_is_hard_failure(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    anchor_text = historical_anchor(work)
    doc = rest_response(anchor_text, skip={"release.json"}, draft=False)
    gh = FakeGh([doc])
    empty = tmp_path / "recover"
    empty.mkdir()
    with pytest.raises(release_pipeline.ReleaseError, match="published release V0.19 is incomplete"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=empty, run_id="5", runner=gh)
    assert not any(call[:2] in (["release", "upload"], ["release", "create"], ["release", "edit"]) for call in gh.calls)
# --- historical (immutable V0.19) retry ---------------------------------------


def historical_anchor(work: Path) -> str:
    text = release_pipeline.committed_manifest_text(work, "V0.19")
    assert text is not None
    return text


def test_publish_historical_complete_verifies_without_writes(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    anchor_text = historical_anchor(work)
    doc = rest_response(anchor_text, draft=False)
    gh = FakeGh([doc, doc])
    empty = tmp_path / "no-assets"
    empty.mkdir()
    result = release_pipeline.publish_release(
        work, tag="V0.19", assets_dir=empty, run_id="1234", runner=gh
    )
    assert result["anchor"] == "historical"
    assert result["created"] is False
    assert result["uploaded"] == []
    # The anchor is the tag-committed manifest, never a download.
    assert not any(call[:2] == ["release", "download"] for call in gh.calls)
    assert not any(call[:2] in (["release", "upload"], ["release", "edit"], ["release", "create"]) for call in gh.calls)



def test_publish_historical_refuses_rewritten_manifest_asset(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    anchor_text = historical_anchor(work)
    forged = anchor_text.replace("0.19.0", "0.19.1")
    doc = rest_response(forged, draft=False)
    gh = FakeGh([doc])
    empty = tmp_path / "recover"
    empty.mkdir()
    with pytest.raises(release_pipeline.ReleaseError, match="never replaced"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=empty, run_id="5", runner=gh)


# --- shared helpers ------------------------------------------------------------


def test_marker_from_message_parses_attempt_trailer() -> None:
    marker = release_pipeline.marker_from_message(
        "notes\n\nPortmap-Build-Run: 42\nPortmap-Build-Attempt: 7\nPortmap-Source: abc\n"
    )
    assert marker == {"build_run": "42", "build_attempt": "7", "source": "abc"}


def test_version_for_tag_enforces_release_shape() -> None:
    assert release_pipeline.version_for_tag("V0.19") == "0.19.0"
    assert release_pipeline.version_for_tag("V0.0") == "0.0.0"
    assert release_pipeline.version_for_tag("v0.19.1") == "0.19.1"
    assert release_pipeline.version_for_tag("v0.20.0") == "0.20.0"
    assert release_pipeline.version_for_tag("v0.20.1") == "0.20.1"
    assert release_pipeline.version_for_tag("v1.2.3") == "1.2.3"
    with pytest.raises(release_pipeline.ReleaseError):
        release_pipeline.version_for_tag("0.19.0")


# --- smoke gate (stubbed frozen binary; the real one runs in CI) ---------------

STUB_SERVER = """#!/usr/bin/env python3
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "{version}"
SKILL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill_body.md")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with open(SKILL, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if len(sys.argv) > 1 and sys.argv[1] == "--version":
    print(f"portmap {{VERSION}}")
    raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] == "_serve-catalog":
    servers = [
        ThreadingHTTPServer(("127.0.0.1", int(os.environ[name])), Handler)
        for name in ("PORTMAP_CATALOG_LISTEN_PORT", "PORTMAP_CATALOG_PUBLIC_LISTEN_PORT")
    ]
    for server in servers[1:]:
        import threading

        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()
raise SystemExit(2)
"""


def make_stub_binary(dist: Path, version: str, skill_body: str) -> None:
    bundle = dist / "frozen" / "portmap"
    bundle.mkdir(parents=True, exist_ok=True)
    executable = bundle / "portmap"
    executable.write_text(STUB_SERVER.format(version=version), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (bundle / "skill_body.md").write_text(skill_body, encoding="utf-8")


def test_smoke_build_accepts_matching_binary(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "--quiet", "v0.20.0")
    dist = tmp_path / "dist"
    make_stub_binary(dist, "0.20.0", "# portmap skill 0.20.0\n")
    result = release_pipeline.smoke_build(work, version="0.20.0", dist_dir=dist)
    assert result["version"] == "0.20.0"
    assert len(result["skill_ports"]) == 2


def test_smoke_build_rejects_version_drift(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "--quiet", "v0.20.0")
    dist = tmp_path / "dist"
    make_stub_binary(dist, "0.21.0", "# portmap skill 0.20.0\n")
    with pytest.raises(release_pipeline.ReleaseError, match="expected 'portmap 0.20.0'"):
        release_pipeline.smoke_build(work, version="0.20.0", dist_dir=dist)


def test_smoke_build_rejects_stale_skill_render(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "--quiet", "v0.20.0")
    dist = tmp_path / "dist"
    make_stub_binary(dist, "0.20.0", "# portmap skill 0.19.0\n")
    with pytest.raises(release_pipeline.ReleaseError, match="canonical render"):
        release_pipeline.smoke_build(work, version="0.20.0", dist_dir=dist)


# --- plan-run (release-state mode selection) -----------------------------------


@pytest.mark.parametrize("tag", ["v0.20.0", "v0.20.1"])
@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_plan_run_absent_release_selects_build(tmp_path: Path, event: str, tag: str) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", f"{tag}^{{commit}}")
    gh = FakeGh([NOT_FOUND])
    result = release_pipeline.plan_run(
        work,
        event_name=event,
        event_tag=tag,
        event_sha=peeled if event == "push" else None,
        runner=gh,
    )
    assert result["mode"] == "build"
    assert result["release_present"] is False
    assert result["commit"] == peeled


def test_plan_run_existing_draft_selects_verify(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    _, anchor_text = make_assets(tmp_path / "build", "0.20.0", "v0.20.0", peeled)
    doc = rest_response(anchor_text, draft=True, body="Portmap-Build-Run: 9\n")
    gh = FakeGh([doc], download=lambda dest: (dest / "release.json").write_text(anchor_text, "utf-8"))
    result = release_pipeline.plan_run(work, event_name="workflow_dispatch", event_tag="v0.20.0", runner=gh)
    assert result["mode"] == "verify"
    assert result["is_draft"] is True


def test_plan_run_published_release_selects_verify(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    _, anchor_text = make_assets(tmp_path / "build", "0.20.0", "v0.20.0", peeled)
    doc = rest_response(anchor_text, draft=False)
    gh = FakeGh([doc], download=lambda dest: (dest / "release.json").write_text(anchor_text, "utf-8"))
    result = release_pipeline.plan_run(work, event_name="push", event_tag="v0.20.0", event_sha=peeled, runner=gh)
    assert result["mode"] == "verify"


def test_plan_run_never_treats_api_failure_as_absence(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    gh = FakeGh([(1, "", "gh: Forbidden (HTTP 403)")])
    with pytest.raises(release_pipeline.ReleaseError, match="never count as an absent release"):
        release_pipeline.plan_run(work, event_name="workflow_dispatch", event_tag="v0.20.0", runner=gh)


def test_plan_run_rejects_tag_before_release_lookup(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    gh = FakeGh([])
    with pytest.raises(release_pipeline.ReleaseError, match="origin/main history"):
        release_pipeline.plan_run(work, event_name="workflow_dispatch", event_tag="v0.21.0", runner=gh)
    assert gh.calls == []


# --- committed source-version gates ---------------------------------------------


def test_validate_tag_rejects_init_version_drift(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    with pytest.raises(release_pipeline.ReleaseError, match="__version__ '0.22.9'"):
        release_pipeline.validate_tag(work, event_name="workflow_dispatch", event_tag="v0.23.0")


def test_validate_tag_rejects_lock_version_drift(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    with pytest.raises(release_pipeline.ReleaseError, match="uv.lock portmap version '0.24.9'"):
        release_pipeline.validate_tag(work, event_name="workflow_dispatch", event_tag="v0.24.0")


def test_validate_tag_accepts_missing_uv_lock(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    result = release_pipeline.validate_tag(work, event_name="workflow_dispatch", event_tag="v0.25.0")
    assert result["version"] == "0.25.0"


# --- exact-tag-build source drift ----------------------------------------------


def test_check_source_clean_accepts_untagged_outputs(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "--quiet", "v0.20.0")
    (work / "node_modules").mkdir()
    (work / "node_modules" / "left.out").write_text("untracked", encoding="utf-8")
    assert release_pipeline.check_source_clean(work)["tracked_source"] == "identical-to-tag"


def test_check_source_clean_rejects_tracked_drift(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "--quiet", "v0.20.0")
    (work / "pyproject.toml").write_text(
        '[project]\nname = "portmap"\nversion = "0.99.0"\n', encoding="utf-8"
    )
    with pytest.raises(release_pipeline.ReleaseError, match="pyproject.toml"):
        release_pipeline.check_source_clean(work)


# --- publish revalidation --------------------------------------------------------


def test_publish_revalidates_main_ancestry_before_mutations(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    assets = tmp_path / "assets"
    assets.mkdir()
    gh = FakeGh([])
    with pytest.raises(release_pipeline.ReleaseError, match="origin/main history"):
        release_pipeline.publish_release(work, tag="v0.21.0", assets_dir=assets, run_id="5", runner=gh)
    assert gh.calls == []


# --- draft without its manifest asset (notes-bound recovery) ---------------------


def draft_body(anchor_text: str, *, run_id: str = "901", attempt: str = "1", bind_manifest: bool = True) -> str:
    body = (
        "Automated release v0.20.0 (portmap 0.20.0).\n\n"
        f"Portmap-Version: 0.20.0\nPortmap-Source: deadbeef\n"
        f"Portmap-Build-Run: {run_id}\nPortmap-Build-Attempt: {attempt}\n"
    )
    if bind_manifest:
        digest = release_pipeline.digest_bytes(anchor_text.encode("utf-8"))
        body += f"Portmap-Manifest-Sha256: {digest}\n"
    return body


def manifest_less_draft_doc(anchor_text: str, *, skip_archive: bool, body: str) -> tuple[int, str, str]:
    skip = {"release.json"}
    if skip_archive:
        skip.add("portmap-0.20.0-linux-amd64.tar.gz")
    return rest_response(anchor_text, skip=skip, draft=True, body=body)


def test_publish_recovers_draft_without_manifest_asset(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    original, anchor_text = make_assets(
        tmp_path / "original", "0.20.0", "v0.20.0", peeled, payload=b"original-attempt-bytes"
    )
    doc = manifest_less_draft_doc(anchor_text, skip_archive=True, body=draft_body(anchor_text))
    final = rest_response(anchor_text, draft=True)
    gh = FakeGh([doc, (0, "", ""), final, (0, "", "")])
    result = release_pipeline.publish_release(
        work, tag="v0.20.0", assets_dir=original, run_id="999", run_attempt="2", runner=gh
    )
    assert result["anchor"] == "recovered"
    assert sorted(result["uploaded"]) == [
        "portmap-0.20.0-linux-amd64.tar.gz",
        "release.json",
    ]
    upload = call_of(gh, "release", "upload")
    assert upload[2] == "v0.20.0"
    assert "--draft=false" in call_of(gh, "release", "edit")
    assert not any(call[:2] == ["release", "download"] for call in gh.calls)


def test_publish_refuses_unbound_draft_without_manifest_asset(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    original, anchor_text = make_assets(
        tmp_path / "original", "0.20.0", "v0.20.0", peeled, payload=b"original-attempt-bytes"
    )
    doc = manifest_less_draft_doc(
        anchor_text, skip_archive=False, body=draft_body(anchor_text, bind_manifest=False)
    )
    gh = FakeGh([doc])
    with pytest.raises(release_pipeline.TagConflictError, match="no manifest asset"):
        release_pipeline.publish_release(work, tag="v0.20.0", assets_dir=original, run_id="9", runner=gh)


def test_publish_refuses_expired_recovery_artifact(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    _, anchor_text = make_assets(tmp_path / "elsewhere", "0.20.0", "v0.20.0", peeled)
    empty = tmp_path / "expired"
    empty.mkdir()
    doc = manifest_less_draft_doc(anchor_text, skip_archive=True, body=draft_body(anchor_text))
    gh = FakeGh([doc])
    with pytest.raises(release_pipeline.ReleaseError, match="retained"):
        release_pipeline.publish_release(work, tag="v0.20.0", assets_dir=empty, run_id="9", runner=gh)


def test_publish_refuses_digest_mismatched_recovery_manifest(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    original, anchor_text = make_assets(
        tmp_path / "original", "0.20.0", "v0.20.0", peeled, payload=b"original-attempt-bytes"
    )
    # The re-run rebuilt the manifest: different bytes than the draft bound.
    tampered = json.loads(anchor_text)
    tampered["assets"][0]["size"] += 1
    (original / "release.json").write_text(json.dumps(tampered, indent=2) + "\n", encoding="utf-8")
    doc = manifest_less_draft_doc(anchor_text, skip_archive=True, body=draft_body(anchor_text))
    gh = FakeGh([doc])
    with pytest.raises(release_pipeline.ReleaseError, match="digest recorded"):
        release_pipeline.publish_release(work, tag="v0.20.0", assets_dir=original, run_id="9", runner=gh)


def test_inspect_manifest_less_draft_names_recovery(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.0^{commit}")
    _, anchor_text = make_assets(tmp_path / "original", "0.20.0", "v0.20.0", peeled)
    doc = manifest_less_draft_doc(anchor_text, skip_archive=True, body=draft_body(anchor_text))
    gh = FakeGh([doc])
    result = release_pipeline.inspect_release(work, tag="v0.20.0", runner=gh)
    assert result["release_present"] is True
    assert result["is_draft"] is True
    assert result["manifest_asset_present"] is False
    assert result["origin_run_id"] == "901"
    assert result["recover_artifact"] == "release-assets-901-1"


# --- selection/retry sequence ----------------------------------------------------


@pytest.mark.parametrize("tag,version", [("v0.20.0", "0.20.0"), ("v0.20.1", "0.20.1")])
def test_publish_then_plan_selects_verify_not_rebuild(tmp_path: Path, tag: str, version: str) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", f"{tag}^{{commit}}")
    assets, anchor_text = make_assets(tmp_path, version, tag, peeled)
    draft = rest_response(anchor_text, draft=True)
    published = rest_response(anchor_text, draft=False)
    create_gh = FakeGh([NOT_FOUND, (0, "", ""), draft, (0, "", "")])
    release_pipeline.publish_release(
        work, tag=tag, assets_dir=assets, run_id="77", run_attempt="1", runner=create_gh
    )
    # After publication the same tag plans as verify: never rebuilt.
    plan_gh = FakeGh([published])
    plan = release_pipeline.plan_run(
        work, event_name="workflow_dispatch", event_tag=tag, runner=plan_gh
    )
    assert plan["mode"] == "verify"
    verify_gh = FakeGh([published, published], download=lambda dest: (dest / "release.json").write_text(anchor_text, "utf-8"))
    result = release_pipeline.publish_release(
        work, tag=tag, assets_dir=assets, run_id="78", runner=verify_gh
    )
    assert result["created"] is False
    assert result["uploaded"] == []
    assert not any(call[:2] in (["release", "upload"], ["release", "create"], ["release", "edit"]) for call in verify_gh.calls)


def test_validate_tag_rejects_legacy_push(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "V0.19^{commit}")
    gh = FakeGh([])
    with pytest.raises(release_pipeline.ReleaseError, match="retry-only"):
        release_pipeline.plan_run(
            work, event_name="push", event_tag="V0.19", event_sha=peeled, runner=gh
        )
    assert gh.calls == []


def test_plan_run_existing_historical_release_selects_verify(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    gh = FakeGh([rest_response(historical_anchor(work))])
    result = release_pipeline.plan_run(
        work, event_name="workflow_dispatch", event_tag="V0.19", runner=gh
    )
    assert result["mode"] == "verify"
    assert result["historical"] is True


@pytest.mark.parametrize("operation", ["plan", "inspect", "publish"])
def test_missing_legacy_release_never_builds_or_publishes(tmp_path: Path, operation: str) -> None:
    work = make_repo(tmp_path)
    gh = FakeGh([NOT_FOUND])
    assets, _ = make_assets(tmp_path, "0.19.0", "V0.19", None)
    with pytest.raises(release_pipeline.ReleaseError, match="has no existing release"):
        if operation == "plan":
            release_pipeline.plan_run(
                work, event_name="workflow_dispatch", event_tag="V0.19", runner=gh
            )
        elif operation == "inspect":
            release_pipeline.inspect_release(work, tag="V0.19", runner=gh)
        else:
            release_pipeline.publish_release(
                work, tag="V0.19", assets_dir=assets, run_id="7", runner=gh
            )
    assert len(gh.calls) == 1
    assert not any(call[:2] in (["release", "create"], ["release", "upload"], ["release", "edit"]) for call in gh.calls)


def test_check_build_rejects_legacy_tag_even_with_generated_identity(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "V0.19^{commit}")
    assets, _ = make_assets(tmp_path, "0.19.0", "V0.19", peeled)
    with pytest.raises(release_pipeline.ReleaseError, match="new builds require"):
        release_pipeline.check_build(assets, tag="V0.19", commit=peeled)


@pytest.mark.parametrize("mutation", ["version", "source_tag", "source_url"])
def test_check_build_rejects_hotfix_identity_drift(tmp_path: Path, mutation: str) -> None:
    work = make_repo(tmp_path)
    peeled = git_text(work, "rev-parse", "v0.20.1^{commit}")
    assets, text = make_assets(tmp_path, "0.20.1", "v0.20.1", peeled)
    manifest = json.loads(text)
    if mutation == "version":
        manifest["version"] = "0.20.0"
    elif mutation == "source_tag":
        manifest["source"]["tag"] = "v0.20.0"
    else:
        asset = manifest["assets"][0]
        asset["source_url"] = asset["source_url"].replace("/v0.20.1/", "/v0.20.0/")
    (assets / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(release_pipeline.ReleaseError):
        release_pipeline.check_build(assets, tag="v0.20.1", commit=peeled)
