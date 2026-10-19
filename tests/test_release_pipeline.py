"""Behavior tests for the automated release pipeline (tools/release_pipeline.py).

These exercise the real git operations against temporary repositories with a
local bare ``origin`` and a scripted gh boundary, so version allocation,
idempotent retries, tag safety, atomic push refusal, immutable-asset gating
and tag-bound publication are verified as behavior, not as text matching.
"""

import importlib.util
import json
import os
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


def ref_exists(repo: Path, ref: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


def write_repo_files(repo: Path, version: str, manifest_version: str, lock_version: str | None = None) -> None:
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "portmap"\nversion = "{version}"\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    init = repo / "src" / "portmap" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (repo / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.11"\n\n[[package]]\n'
        f'name = "portmap"\nversion = "{lock_version or version}"\nsource = {{ editable = "." }}\n',
        encoding="utf-8",
    )
    (repo / "src" / "portmap" / "release.json").write_text(
        json.dumps({"version": manifest_version, "assets": []}, indent=2) + "\n", encoding="utf-8"
    )
    logs = repo / ".branch_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "dev.md").write_text("# dev\n", encoding="utf-8")
    (logs / "main.md").write_text("# main\n", encoding="utf-8")
    static = repo / "src" / "portmap" / "catalog_static"
    static.mkdir(parents=True, exist_ok=True)
    (static / "__init__.py").write_text("", encoding="utf-8")


def make_repo(tmp_path: Path, dev_version: str = "0.19.0") -> tuple[Path, Path, str, str]:
    """Create an initialized work repo with a bare origin, main at V0.18, dev ahead.

    The work repository carries its own local identity so both the helper and
    the production git() calls inside release_pipeline commit successfully
    regardless of ambient configuration. The dev commit deliberately leaves
    uv.lock at the previous version, mirroring real metadata drift that the
    release bump must repair.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "dev")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "dev")
    git(work, "config", "user.name", "Release Tester")
    git(work, "config", "user.email", "tester@example.com")
    git(work, "config", "commit.gpgsign", "false")
    write_repo_files(work, "0.18.0", "0.18.0")
    git(work, "add", "-A")
    git(work, "commit", "-m", "baseline 0.18.0")
    base = git_text(work, "rev-parse", "HEAD")
    git(work, "branch", "main")
    git(work, "tag", "V0.18")
    write_repo_files(work, dev_version, "0.18.0", lock_version="0.18.0")
    git(work, "add", "-A")
    git(work, "commit", "-m", f"bump source to {dev_version}")
    source = git_text(work, "rev-parse", "HEAD")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-u", "origin", "dev", "main", "V0.18")
    return work, origin, source, base


def make_assets(base_dir: Path, version: str, tag: str, payload: bytes = b"portmap-frozen-bundle") -> Path:
    assets = base_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    filename = f"portmap-{version}-linux-amd64.tar.gz"
    archive = assets / filename
    archive.write_bytes(payload)
    sha = release_pipeline.digest(archive)
    (assets / f"{filename}.sha256").write_text(f"{sha}  {filename}\n", encoding="utf-8")
    manifest = {
        "version": version,
        "assets": [
            {
                "os": "linux",
                "arch": "amd64",
                "filename": filename,
                "sha256": sha,
                "size": len(payload),
                "source_url": f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/{filename}",
            }
        ],
    }
    (assets / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return assets


def tagged_release(work: Path, assets: Path, run_id: str = "9") -> str:
    """Commit the asset manifest on dev and create the pipeline-tagged release.

    Mirrors what prepare+finalize produce, so publication tests operate on a
    tag whose committed manifest is the assets' manifest.
    """
    version = "0.19.0"
    tag = "V0.19"
    source = git_text(work, "rev-parse", "HEAD")
    manifest_bytes = (assets / "release.json").read_bytes()
    assert json.loads(manifest_bytes)["version"] == version
    (work / "src" / "portmap" / "release.json").write_bytes(manifest_bytes)
    git(work, "add", "--", "src/portmap/release.json")
    git(
        work,
        "commit",
        "-m",
        f"release: {tag} automated pipeline distribution\n\n"
        f"Portmap-Version: {version}\nPortmap-Source: {source}\nPortmap-Build-Run: {run_id}\n",
    )
    git(
        work,
        "tag",
        "-a",
        tag,
        "-m",
        f"release {tag} portmap {version}\n\n"
        f"Portmap-Version: {version}\nPortmap-Source: {source}\nPortmap-Build-Run: {run_id}\n",
    )
    return source


class FakeGh:
    """Records gh argv and answers from a scripted queue.

    Responses are (returncode, stdout, stderr) triples, matching the real
    gh_runner contract.
    """

    def __init__(self, responses: list[tuple[int, str, str]]):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], repo: Path) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if self.responses:
            return self.responses.pop(0)
        raise AssertionError(f"unexpected gh call: {argv}")


NOT_FOUND: tuple[int, str, str] = (1, "", "gh: Not Found (HTTP 404)")


def rest_response(assets_dir: Path, committed_text: str, skip: set[str] = set(), draft: bool = False) -> tuple[int, str, str]:
    """A REST release payload whose assets all match the tag-bound content."""
    manifest = json.loads(committed_text)
    entries = []
    for name in release_pipeline.release_file_names(manifest):
        if name in skip:
            continue
        if name == "release.json":
            content = committed_text.encode("utf-8")
        elif name.endswith(".sha256"):
            filename = name[: -len(".sha256")]
            entry = next(a for a in manifest["assets"] if a["filename"] == filename)
            content = f"{entry['sha256']}  {filename}\n".encode("utf-8")
        else:
            content = (assets_dir / name).read_bytes()
        entries.append(
            {"name": name, "size": len(content), "digest": "sha256:" + release_pipeline.digest_bytes(content)}
        )
    return 0, json.dumps({"draft": draft, "assets": entries}), ""


def run_release(work: Path, assets: Path, run_id: str, source: str, version: str, tag: str) -> None:
    release_pipeline.prepare_release_commit(
        work, version=version, tag=tag, source_sha=source, run_id=run_id, assets_dir=assets
    )
    release_pipeline.finalize_release(work, version=version, tag=tag, source_sha=source, run_id=run_id)


# --- version choice -------------------------------------------------------


def test_derive_pending_version_prefers_greater_pyproject() -> None:
    assert release_pipeline.derive_pending_version("0.19.3", ["V0.17", "V0.18"]) == "0.19.3"
    assert release_pipeline.derive_pending_version("1.0.0", ["V0.18"]) == "1.0.0"


def test_derive_pending_version_increments_minor_otherwise() -> None:
    assert release_pipeline.derive_pending_version("0.18.0", ["V0.18"]) == "0.19.0"
    assert release_pipeline.derive_pending_version("0.18.9", ["V0.18"]) == "0.19.0"
    assert release_pipeline.derive_pending_version("0.17.5", ["V0.18"]) == "0.19.0"
    assert release_pipeline.derive_pending_version("0.19.0", []) == "0.19.0"


def test_plan_allocates_new_release_from_dev_head(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    plan = release_pipeline.plan_release(work)
    assert plan["mode"] == "new"
    assert plan["version"] == "0.19.0"
    assert plan["tag"] == "V0.19"
    assert plan["source_sha"] == source
    assert plan["build_run_id"] == ""
    assert plan["base_main"] == git_text(work, "rev-parse", "refs/remotes/origin/main")


def test_plan_next_push_after_release_allocates_next_minor(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    run_release(work, assets, "42", source, "0.19.0", "V0.19")
    git(work, "checkout", "--quiet", "dev")
    plan = release_pipeline.plan_release(work)
    assert plan["mode"] == "new"
    assert plan["version"] == "0.20.0"
    assert plan["tag"] == "V0.20"


def test_plan_increments_past_a_foreign_tag_and_never_rewrites_it(tmp_path: Path) -> None:
    """Policy: an unowned tag is never reallocated; the next minor is used.

    The derived pending version is always greater than every existing tag,
    so a steady-state collision is impossible by construction and the plan
    check is a race guard. When a foreign V0.20 exists while pyproject says
    0.20.0, pyproject is no longer "greater than the latest tag", so the
    increment contract applies: 0.21.0 is allocated and V0.20 is untouched.
    Real never-rewrite enforcement lives in finalize (local tag) and the
    guarded atomic push (remote tag).
    """
    work, _origin, _source, base = make_repo(tmp_path, dev_version="0.20.0")
    git(work, "tag", "V0.20", base)
    plan = release_pipeline.plan_release(work)
    assert plan["mode"] == "new"
    assert plan["version"] == "0.21.0"
    assert plan["tag"] == "V0.21"
    assert git_text(work, "rev-parse", "refs/tags/V0.20") == base


# --- trusted source selection ---------------------------------------------


def test_select_source_rejects_ambiguous_or_unknown_input(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assert release_pipeline.select_source(work, "")["source_sha"] == source
    assert release_pipeline.select_source(work, source)["source_sha"] == source
    # Uppercase ids are valid hex and normalized.
    assert release_pipeline.select_source(work, source.upper())["source_sha"] == source
    for bad in ("deadbee", "zz" * 20, source[:39], "g" + source[1:]):
        with pytest.raises(release_pipeline.ReleaseError):
            release_pipeline.select_source(work, bad)
    with pytest.raises(release_pipeline.ReleaseError, match="does not exist"):
        release_pipeline.select_source(work, "1" * 40)


def test_select_source_defaults_to_origin_dev_head_not_local_head(tmp_path: Path) -> None:
    work, _origin, source, base = make_repo(tmp_path)
    # A dispatch from an older workflow revision: trusted HEAD is behind dev.
    git(work, "checkout", "--quiet", "--detach", base)
    assert git_text(work, "rev-parse", "HEAD") == base
    assert release_pipeline.select_source(work, "")["source_sha"] == source
    # Push events pin the exact event commit instead.
    assert release_pipeline.select_source(work, "", default_sha=source)["source_sha"] == source
    assert release_pipeline.select_source(work, "", default_sha=base)["source_sha"] == base


def test_select_source_rejects_history_outside_dev(tmp_path: Path) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    git(work, "checkout", "--quiet", "-b", "side-branch", "main")
    (work / "README.md").write_text("unmerged work\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-m", "side work")
    stray = git_text(work, "rev-parse", "HEAD")
    with pytest.raises(release_pipeline.ReleaseError, match="foreign history"):
        release_pipeline.select_source(work, stray)


# --- same-source retry identity -------------------------------------------


def test_plan_reuses_release_for_same_source(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    run_release(work, assets, "4242", source, "0.19.0", "V0.19")
    plan = release_pipeline.plan_release(work, source_sha=source)
    assert plan["mode"] == "existing"
    assert plan["tag"] == "V0.19"
    assert plan["version"] == "0.19.0"
    assert plan["build_run_id"] == "4242"
    tags = release_pipeline.list_release_tags(work)
    assert tags == ["V0.18", "V0.19"]


def test_verify_existing_requires_matching_source_marker(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    run_release(work, assets, "77", source, "0.19.0", "V0.19")
    committed = release_pipeline.committed_manifest_text(work, "V0.19")
    gh = FakeGh([rest_response(assets, committed)])
    result = release_pipeline.verify_existing_release(
        work, tag="V0.19", source_sha=source, run_id="77", runner=gh
    )
    assert result["release_present"] is True
    assert result["missing_files"] == []
    assert gh.calls[0][:3] == ["api", "repos/ylang-ylang/portmap/releases/tags/V0.19"]

    other = git_text(work, "rev-parse", "V0.19^{commit}^")
    with pytest.raises(release_pipeline.TagConflictError, match="different history"):
        release_pipeline.verify_existing_release(
            work, tag="V0.19", source_sha=other, run_id="77", runner=gh
        )
    with pytest.raises(release_pipeline.TagConflictError, match="build run"):
        release_pipeline.verify_existing_release(
            work, tag="V0.19", source_sha=source, run_id="99", runner=gh
        )


def test_verify_existing_treats_only_real_404_as_absent(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    run_release(work, assets, "77", source, "0.19.0", "V0.19")
    gh = FakeGh([NOT_FOUND])
    result = release_pipeline.verify_existing_release(
        work, tag="V0.19", source_sha=source, run_id="77", runner=gh
    )
    assert result["release_present"] is False
    assert result["missing_files"] == release_pipeline.release_file_names(
        json.loads(release_pipeline.committed_manifest_text(work, "V0.19"))
    )

    for failure in (
        (1, "", "gh: Bad credentials (HTTP 401)"),
        (1, "", "gh: Forbidden (HTTP 403)"),
        (1, "", "gh: Server Error (HTTP 500)"),
        (1, "", "fatal: unable to access: connection timed out"),
    ):
        gh = FakeGh([failure])
        with pytest.raises(release_pipeline.ReleaseError, match="never count as an absent release"):
            release_pipeline.verify_existing_release(
                work, tag="V0.19", source_sha=source, run_id="77", runner=gh
            )


# --- mismatched / preexisting tag safety ----------------------------------


def test_prepare_refuses_existing_release_branch(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    git(work, "branch", "feat/release-V0.19")
    with pytest.raises(release_pipeline.TagConflictError, match="release branch"):
        release_pipeline.prepare_release_commit(
            work, version="0.19.0", tag="V0.19", source_sha=source, run_id="9", assets_dir=assets
        )


def test_prepare_refuses_unexpected_worktree_changes(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    (work / "rogue.txt").write_text("stray change\n", encoding="utf-8")
    with pytest.raises(release_pipeline.ReleaseError, match="rogue.txt"):
        release_pipeline.prepare_release_commit(
            work, version="0.19.0", tag="V0.19", source_sha=source, run_id="9", assets_dir=assets
        )
    assert not ref_exists(work, "refs/heads/feat/release-V0.19")


def test_prepare_refuses_stale_manifest_version(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.18.0", "V0.18")
    with pytest.raises(release_pipeline.ReleaseError, match="stale manifest"):
        release_pipeline.prepare_release_commit(
            work, version="0.19.0", tag="V0.19", source_sha=source, run_id="9", assets_dir=assets
        )


@pytest.mark.parametrize(
    "mutate",
    [
        "traversal-filename",
        "duplicate-asset",
        "unsupported-platform",
        "symlink-archive",
        "malformed-checksum",
    ],
)
def test_prepare_refuses_non_canonical_asset_sets(tmp_path: Path, mutate: str) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    manifest_path = assets / "release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutate == "traversal-filename":
        manifest["assets"][0]["filename"] = "../portmap-0.19.0-linux-amd64.tar.gz"
    elif mutate == "duplicate-asset":
        manifest["assets"].append(dict(manifest["assets"][0]))
    elif mutate == "unsupported-platform":
        manifest["assets"][0]["arch"] = "riscv64"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    archive = assets / "portmap-0.19.0-linux-amd64.tar.gz"
    if mutate == "symlink-archive":
        archive.unlink()
        outside = tmp_path / "outside.tar.gz"
        outside.write_bytes(b"imposter")
        archive.symlink_to(outside)
    if mutate == "malformed-checksum":
        (assets / "portmap-0.19.0-linux-amd64.tar.gz.sha256").write_text(
            "0000000000000000000000000000000000000000000000000000000000000000  portmap-0.19.0-linux-amd64.tar.gz\n",
            encoding="utf-8",
        )
    with pytest.raises(release_pipeline.ReleaseError) as error:
        release_pipeline.prepare_release_commit(
            work, version="0.19.0", tag="V0.19", source_sha=source, run_id="9", assets_dir=assets
        )
    assert "failed validation" in str(error.value)
    assert not ref_exists(work, "refs/heads/feat/release-V0.19")


# --- metadata consistency -------------------------------------------------


def test_prepare_commits_consistent_release_state(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    compiled = work / "src" / "portmap" / "catalog_static" / "assets" / "catalog.js"
    compiled.parent.mkdir(parents=True, exist_ok=True)
    compiled.write_text("compiled-ui\n", encoding="utf-8")

    result = release_pipeline.prepare_release_commit(
        work, version="0.19.0", tag="V0.19", source_sha=source, run_id="31", assets_dir=assets
    )
    assert result["branch"] == "feat/release-V0.19"
    assert git_text(work, "rev-parse", "--abbrev-ref", "HEAD") == "feat/release-V0.19"

    release_pipeline.check_version_consistency(work, "0.19.0")
    committed_manifest = (work / "src" / "portmap" / "release.json").read_bytes()
    assert committed_manifest == (assets / "release.json").read_bytes()
    assert json.loads(committed_manifest)["version"] == "0.19.0"

    dev_log = (work / ".branch_logs" / "dev.md").read_text(encoding="utf-8")
    main_log = (work / ".branch_logs" / "main.md").read_text(encoding="utf-8")
    assert "Release V0.19 (portmap 0.19.0)" in dev_log
    assert "`V0.19`" in main_log

    message = git_text(work, "log", "-1", "--format=%B")
    assert f"Portmap-Source: {source}" in message
    assert "Portmap-Build-Run: 31" in message
    assert "Portmap-Version: 0.19.0" in message
    committed_files = git_text(work, "show", "--name-only", "--format=", "HEAD").splitlines()
    # pyproject/__init__ already sat at 0.19.0; the bump repairs the stale
    # uv.lock drift and commits manifest, logs and compiled catalog assets.
    assert "uv.lock" in committed_files
    assert "src/portmap/release.json" in committed_files
    assert "src/portmap/catalog_static/assets/catalog.js" in committed_files
    assert ".branch_logs/dev.md" in committed_files
    assert ".branch_logs/main.md" in committed_files
    assert git_text(work, "status", "--porcelain", "-uall") == ""


def test_prepare_accepts_deterministic_pre_tag_rerun_build(tmp_path: Path) -> None:
    """A failed pre-tag attempt never binds anything; the rerun rebuild wins.

    Once a run has failed before tagging, its retained artifact may be
    overwritten (workflow uploads with overwrite in mode=new only) and the
    retry prepares from the fresh bytes; only a published tag freezes them.
    """
    work, origin, source, _base = make_repo(tmp_path)
    first = make_assets(tmp_path / "attempt1", "0.19.0", "V0.19", payload=b"first-build")
    release_pipeline.prepare_release_commit(
        work, version="0.19.0", tag="V0.19", source_sha=source, run_id="60", assets_dir=first
    )
    # Fresh runner checkout of the same source with a rebuilt artifact.
    retry = work.parent / "retry-work"
    git(work, "clone", "--quiet", str(origin), str(retry))
    git(retry, "config", "user.name", "Release Tester")
    git(retry, "config", "user.email", "tester@example.com")
    git(retry, "config", "commit.gpgsign", "false")
    git(retry, "checkout", "--quiet", "--detach", source)
    second = make_assets(tmp_path / "attempt2", "0.19.0", "V0.19", payload=b"second-rebuilt")
    result = release_pipeline.prepare_release_commit(
        retry, version="0.19.0", tag="V0.19", source_sha=source, run_id="61", assets_dir=second
    )
    committed = (retry / "src" / "portmap" / "release.json").read_bytes()
    assert committed == (second / "release.json").read_bytes()
    assert committed != (first / "release.json").read_bytes()
    assert result["commit"] != git_text(work, "rev-parse", "feat/release-V0.19")


# --- git graph, trailers and atomic push -----------------------------------


def test_finalize_preserves_graph_and_records_trailers(tmp_path: Path) -> None:
    work, origin, source, base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    release_pipeline.prepare_release_commit(
        work, version="0.19.0", tag="V0.19", source_sha=source, run_id="55", assets_dir=assets
    )
    result = release_pipeline.finalize_release(
        work, version="0.19.0", tag="V0.19", source_sha=source, run_id="55"
    )

    remote = git_text(origin, "rev-parse", "refs/heads/dev", "refs/heads/main", "refs/tags/V0.19").splitlines()
    assert remote == [result["dev"], result["main"], result["tag_object"]]

    dev_parents = git_text(origin, "show", "-s", "--format=%P", f"{result['dev']}^{{commit}}").split()
    release_commit = git_text(work, "rev-parse", "feat/release-V0.19")
    assert set(dev_parents) == {source, release_commit}

    main_commit = f"{result['main']}"
    main_parents = git_text(origin, "show", "-s", "--format=%P", f"{main_commit}").split()
    assert set(main_parents) == {base, result["dev"]}

    main_message = git_text(origin, "show", "-s", "--format=%B", main_commit)
    assert main_message.splitlines()[0] == "release: V0.19 portmap 0.19.0"
    assert f"Portmap-Source: {source}" in main_message
    assert "Portmap-Build-Run: 55" in main_message

    assert git_text(origin, "cat-file", "-t", "refs/tags/V0.19") == "tag"
    tag_message = git_text(work, "cat-file", "tag", "V0.19").split("\n\n", 1)[1]
    assert f"Portmap-Source: {source}" in tag_message
    assert "Portmap-Version: 0.19.0" in tag_message

    for ref in (f"{result['dev']}^{{commit}}", main_commit, f"refs/tags/V0.19^{{commit}}"):
        assert git(origin, "merge-base", "--is-ancestor", source, ref).returncode == 0


def test_finalize_refuses_when_remote_dev_advanced(tmp_path: Path) -> None:
    work, origin, source, base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    release_pipeline.prepare_release_commit(
        work, version="0.19.0", tag="V0.19", source_sha=source, run_id="55", assets_dir=assets
    )
    competing = work.parent / "competing"
    git(work, "clone", "--quiet", str(origin), str(competing))
    (competing / "pyproject.toml").write_text(
        '[project]\nname = "portmap"\nversion = "0.19.0"\n', encoding="utf-8"
    )
    git(competing, "add", "-A")
    git(competing, "commit", "-m", "competing dev change")
    git(competing, "push", "origin", "dev")

    with pytest.raises(release_pipeline.RemoteAdvancedError, match="origin/dev moved"):
        release_pipeline.finalize_release(
            work, version="0.19.0", tag="V0.19", source_sha=source, run_id="55"
        )
    assert not ref_exists(origin, "refs/tags/V0.19")
    assert git_text(origin, "rev-parse", "refs/heads/main") == base
    assert git_text(origin, "rev-parse", "refs/heads/dev") == git_text(competing, "rev-parse", "HEAD")


def test_finalize_refuses_when_remote_main_advanced(tmp_path: Path) -> None:
    work, origin, source, base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    release_pipeline.prepare_release_commit(
        work, version="0.19.0", tag="V0.19", source_sha=source, run_id="55", assets_dir=assets
    )
    competing = work.parent / "competing-main"
    git(work, "clone", "--quiet", str(origin), str(competing))
    git(competing, "checkout", "--quiet", "-b", "main", "origin/main")
    (competing / "README.md").write_text("main hotfix\n", encoding="utf-8")
    git(competing, "add", "-A")
    git(competing, "commit", "-m", "competing main change")
    git(competing, "push", "origin", "main")

    with pytest.raises(release_pipeline.RemoteAdvancedError, match="origin/main moved"):
        release_pipeline.finalize_release(
            work, version="0.19.0", tag="V0.19", source_sha=source, run_id="55"
        )
    assert not ref_exists(origin, "refs/tags/V0.19")
    assert git_text(origin, "rev-parse", "refs/heads/dev") == source
    assert git_text(origin, "rev-parse", "refs/heads/main") != base


def test_finalize_refuses_when_planned_main_baseline_drifted(tmp_path: Path) -> None:
    """The planned base_main snapshot is enforced before any merge happens."""
    work, origin, source, base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    release_pipeline.prepare_release_commit(
        work, version="0.19.0", tag="V0.19", source_sha=source, run_id="55", assets_dir=assets
    )
    with pytest.raises(release_pipeline.RemoteAdvancedError, match="planned against"):
        release_pipeline.finalize_release(
            work,
            version="0.19.0",
            tag="V0.19",
            source_sha=source,
            run_id="55",
            base_main="5" * 40,
        )
    assert git_text(origin, "rev-parse", "refs/heads/main") == base
    assert not ref_exists(origin, "refs/tags/V0.19")
    assert git_text(work, "rev-parse", "--abbrev-ref", "HEAD") == "feat/release-V0.19"


def test_finalize_never_rewrites_existing_local_or_remote_tag(tmp_path: Path) -> None:
    work, origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    release_pipeline.prepare_release_commit(
        work, version="0.19.0", tag="V0.19", source_sha=source, run_id="55", assets_dir=assets
    )
    # A tag raced onto the remote after preparation.
    racing = work.parent / "racing"
    git(work, "clone", "--quiet", str(origin), str(racing))
    git(racing, "tag", "V0.19", "refs/remotes/origin/main")
    git(racing, "push", "origin", "refs/tags/V0.19")
    with pytest.raises(release_pipeline.TagConflictError, match="refusing to rewrite"):
        release_pipeline.finalize_release(
            work, version="0.19.0", tag="V0.19", source_sha=source, run_id="55"
        )
    assert git_text(origin, "rev-parse", "refs/heads/dev") == source

    # Part one left a LOCAL V0.19 behind before its push refusal; re-running
    # finalize on the prepared branch must refuse rather than move that tag.
    git(work, "checkout", "--quiet", "feat/release-V0.19")
    assert ref_exists(work, "refs/tags/V0.19")
    with pytest.raises(release_pipeline.TagConflictError, match="already exists locally"):
        release_pipeline.finalize_release(
            work, version="0.19.0", tag="V0.19", source_sha=source, run_id="55"
        )
    assert not ref_exists(origin, "refs/tags/V0.21")


# --- manifest and metadata validation --------------------------------------


def test_validate_manifest_gates_digests_urls_and_companions(tmp_path: Path) -> None:
    assets = make_assets(tmp_path / "validation", "0.19.0", "V0.19")
    manifest = json.loads((assets / "release.json").read_text(encoding="utf-8"))
    assert release_pipeline.validate_manifest(manifest, "0.19.0", assets) == []

    wrong_version = json.loads(json.dumps(manifest))
    wrong_version["version"] = "0.20.0"
    assert release_pipeline.validate_manifest(wrong_version, "0.19.0", assets)

    wrong_sha = json.loads(json.dumps(manifest))
    wrong_sha["assets"][0]["sha256"] = "0" * 64
    errors = release_pipeline.validate_manifest(wrong_sha, "0.19.0", assets)
    assert any("sha256 mismatch" in error for error in errors)

    wrong_size = json.loads(json.dumps(manifest))
    wrong_size["assets"][0]["size"] = 1
    assert any("size mismatch" in error for error in release_pipeline.validate_manifest(wrong_size, "0.19.0", assets))

    # A malicious origin with the correct path suffix must still be refused.
    evil_origin = json.loads(json.dumps(manifest))
    evil_origin["assets"][0]["source_url"] = (
        "https://attacker.example/V0.19/portmap-0.19.0-linux-amd64.tar.gz"
    )
    assert any("source_url must be exactly" in error for error in release_pipeline.validate_manifest(evil_origin, "0.19.0", assets))

    companion = assets / "portmap-0.19.0-linux-amd64.tar.gz.sha256"
    original = companion.read_text(encoding="utf-8")
    companion.write_text(original + "extra  other-file.tar.gz\n", encoding="utf-8")
    assert any("companion" in error for error in release_pipeline.validate_manifest(manifest, "0.19.0", assets))
    companion.write_text(original, encoding="utf-8")


# --- tag-bound publication --------------------------------------------------


def test_publish_creates_draft_then_publishes_with_full_asset_set(tmp_path: Path) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    source = tagged_release(work, assets, run_id="9")

    gh = FakeGh([NOT_FOUND, (0, "", ""), (0, "", "")])
    result = release_pipeline.publish_release(
        work, tag="V0.19", assets_dir=assets, version="0.19.0", source_sha=source, run_id="9", runner=gh
    )
    assert result["created"] is True
    assert sorted(result["uploaded"]) == [
        "portmap-0.19.0-linux-amd64.tar.gz",
        "portmap-0.19.0-linux-amd64.tar.gz.sha256",
        "release.json",
    ]
    create = next(call for call in gh.calls if call[:2] == ["release", "create"])
    assert "--draft" in create
    assert "--verify-tag" in create
    assert any(str(assets / name) in create for name in result["uploaded"])
    edit = next(call for call in gh.calls if call[:2] == ["release", "edit"])
    assert "--draft=false" in edit
    # The draft is only published after the create step attached every file.
    assert gh.calls.index(create) < gh.calls.index(edit)


def test_publish_publishes_a_complete_draft_without_any_artifact(tmp_path: Path) -> None:
    """A verified complete draft is published even with nothing to upload.

    Covers the interrupted-publication retry: create/upload succeeded but the
    final draft flip failed; the next run must finish publishing without the
    retained CI artifact.
    """
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    source = tagged_release(work, assets, run_id="9")
    committed = release_pipeline.committed_manifest_text(work, "V0.19")
    gh = FakeGh([rest_response(assets, committed, draft=True), (0, "", "")])
    result = release_pipeline.publish_release(
        work,
        tag="V0.19",
        assets_dir=tmp_path / "expired-artifact",
        version="0.19.0",
        source_sha=source,
        run_id="9",
        runner=gh,
    )
    assert result["created"] is False
    assert result["uploaded"] == []
    assert gh.calls[0][:2] == ["api", "repos/ylang-ylang/portmap/releases/tags/V0.19"]
    edit = next(call for call in gh.calls if call[:2] == ["release", "edit"])
    assert "--draft=false" in edit
    assert all(call[:2] not in (["release", "upload"], ["release", "create"]) for call in gh.calls)


def test_publish_complete_release_verifies_without_artifact_or_uploads(tmp_path: Path) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    source = tagged_release(work, assets, run_id="9")
    committed = release_pipeline.committed_manifest_text(work, "V0.19")
    # The retained CI artifact has expired; the release is already complete.
    gh = FakeGh([rest_response(assets, committed)])
    result = release_pipeline.publish_release(
        work,
        tag="V0.19",
        assets_dir=tmp_path / "expired-artifact",
        version="0.19.0",
        source_sha=source,
        run_id="9",
        runner=gh,
    )
    assert result["created"] is False
    assert result["uploaded"] == []
    assert all(
        call[:2] not in (["release", "upload"], ["release", "create"], ["release", "edit"])
        for call in gh.calls
    )


def test_publish_recovers_missing_archive_from_retained_artifact(tmp_path: Path) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    source = tagged_release(work, assets, run_id="12")
    committed = release_pipeline.committed_manifest_text(work, "V0.19")
    view = rest_response(
        assets, committed, skip={"portmap-0.19.0-linux-amd64.tar.gz", "release.json"}, draft=True
    )
    gh = FakeGh([view, (0, "", ""), (0, "", "")])
    result = release_pipeline.publish_release(
        work, tag="V0.19", assets_dir=assets, source_sha=source, run_id="12", runner=gh
    )
    assert sorted(result["uploaded"]) == ["portmap-0.19.0-linux-amd64.tar.gz", "release.json"]
    upload = next(call for call in gh.calls if call[:2] == ["release", "upload"])
    assert any(call.endswith("portmap-0.19.0-linux-amd64.tar.gz") for call in upload)
    # release.json bytes come from the tag, not from the retained artifact.
    assert (assets / "release.json").read_text(encoding="utf-8") == committed
    assert "--draft=false" in next(call for call in gh.calls if call[:2] == ["release", "edit"])


def test_publish_refuses_same_size_different_content_asset(tmp_path: Path) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    tagged_release(work, assets, run_id="9")
    committed = release_pipeline.committed_manifest_text(work, "V0.19")
    code, payload, err = rest_response(assets, committed)
    document = json.loads(payload)
    for entry in document["assets"]:
        if entry["name"] == "portmap-0.19.0-linux-amd64.tar.gz":
            # Same size, different bytes: only a digest check catches this.
            entry["digest"] = "sha256:" + "b" * 64
    gh = FakeGh([(code, json.dumps(document), err)])
    with pytest.raises(release_pipeline.ReleaseError, match="content differs from the tag's committed manifest"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, runner=gh)
    assert all(call[:2] != ["release", "upload"] for call in gh.calls)


def test_publish_fails_closed_without_published_digest_metadata(tmp_path: Path) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    tagged_release(work, assets, run_id="9")
    committed = release_pipeline.committed_manifest_text(work, "V0.19")
    code, payload, err = rest_response(assets, committed)
    view = json.loads(payload)
    for entry in view["assets"]:
        entry.pop("digest")
    gh = FakeGh([(code, json.dumps(view), err)])
    with pytest.raises(release_pipeline.ReleaseError, match="no digest"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, runner=gh)


def test_publish_refuses_recovered_archive_mismatching_tag_manifest(tmp_path: Path) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    source = tagged_release(work, assets, run_id="9")
    committed = release_pipeline.committed_manifest_text(work, "V0.19")
    view = rest_response(assets, committed, skip={"portmap-0.19.0-linux-amd64.tar.gz"})
    # The retained artifact holds a different fresh build of the same size.
    (assets / "portmap-0.19.0-linux-amd64.tar.gz").write_bytes(b"fresh-uncontrolled-build!")
    gh = FakeGh([view])
    with pytest.raises(release_pipeline.ReleaseError, match="does not match the sha256 committed"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, source_sha=source, run_id="9", runner=gh)
    assert all(call[:2] != ["release", "upload"] for call in gh.calls)


def test_publish_refuses_artifact_manifest_differing_from_tag(tmp_path: Path) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    tagged_release(work, assets, run_id="9")
    tampered = json.loads((assets / "release.json").read_text(encoding="utf-8"))
    tampered["assets"][0]["size"] += 1
    (assets / "release.json").write_text(json.dumps(tampered, indent=2) + "\n", encoding="utf-8")
    gh = FakeGh([NOT_FOUND])
    with pytest.raises(release_pipeline.TagConflictError, match="byte for byte"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, runner=gh)
    # Refused before any gh interaction.
    assert gh.calls == []


def test_publish_never_reads_or_writes_metadata_through_symlinks(tmp_path: Path) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    tagged_release(work, assets, run_id="9")

    # An existing symlink as the retained manifest is refused before reads.
    outside_manifest = tmp_path / "outside-release.json"
    outside_manifest.write_text('{"version": "9.9.9"}\n', encoding="utf-8")
    outside_bytes = outside_manifest.read_bytes()
    (assets / "release.json").unlink()
    (assets / "release.json").symlink_to(outside_manifest)
    gh = FakeGh([NOT_FOUND])
    with pytest.raises(release_pipeline.TagConflictError):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, runner=gh)
    assert gh.calls == []
    assert outside_manifest.read_bytes() == outside_bytes

    # A dangling companion symlink in the missing set is refused, never
    # written through, and the out-of-tree target stays untouched.
    committed = release_pipeline.committed_manifest_text(work, "V0.19")
    outside_companion = tmp_path / "outside.sha256"
    (assets / "release.json").unlink()
    (assets / "release.json").write_text(committed, encoding="utf-8")
    companion = assets / "portmap-0.19.0-linux-amd64.tar.gz.sha256"
    companion.unlink()
    companion.symlink_to(outside_companion)
    view = rest_response(assets, committed, skip={"portmap-0.19.0-linux-amd64.tar.gz.sha256"})
    gh = FakeGh([view])
    with pytest.raises(release_pipeline.ReleaseError):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, runner=gh)
    assert all(call[:2] != ["release", "upload"] for call in gh.calls)
    assert not outside_companion.exists()
    assert companion.is_symlink()
    assert outside_manifest.read_bytes() == outside_bytes


def test_publish_refuses_argument_conflicts_with_tag_markers(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    tagged_release(work, assets, run_id="9")
    gh = FakeGh([])
    with pytest.raises(release_pipeline.TagConflictError, match="conflicts with the version"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, version="0.20.0", runner=gh)
    with pytest.raises(release_pipeline.TagConflictError, match="conflicts with the source"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, source_sha="3" * 40, runner=gh)
    with pytest.raises(release_pipeline.TagConflictError, match="conflicts with the build run"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, run_id="999", runner=gh)


def test_publish_refuses_tags_without_pipeline_markers(tmp_path: Path) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    git(work, "tag", "V0.19")
    gh = FakeGh([])
    with pytest.raises(release_pipeline.TagConflictError, match="no pipeline markers"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, runner=gh)


def test_publish_refuses_foreign_gh_repo_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work, _origin, _source, _base = make_repo(tmp_path)
    assets = make_assets(tmp_path, "0.19.0", "V0.19")
    tagged_release(work, assets, run_id="9")
    gh = FakeGh([NOT_FOUND])
    monkeypatch.setenv("GH_REPO", "attacker/fork")
    with pytest.raises(release_pipeline.ReleaseError, match="foreign repository"):
        release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, runner=gh)
    monkeypatch.setenv("GH_REPO", "ylang-ylang/portmap")
    assert gh.calls == []  # the matching slug re-run below uses its own runner
    gh2 = FakeGh([NOT_FOUND, (0, "", ""), (0, "", "")])
    release_pipeline.publish_release(work, tag="V0.19", assets_dir=assets, runner=gh2)
    assert gh2.calls[0][:2] == ["api", "repos/ylang-ylang/portmap/releases/tags/V0.19"]


def test_resolve_source_rejects_foreign_history(tmp_path: Path) -> None:
    work, _origin, source, _base = make_repo(tmp_path)
    unrelated = "1" * 40
    assert git(work, "cat-file", "-e", f"{unrelated}^{{commit}}", check=False).returncode != 0
    with pytest.raises(release_pipeline.ReleaseError, match="does not exist"):
        release_pipeline.resolve_source(work, unrelated)
    assert release_pipeline.resolve_source(work, source) == source


# --- CLI boundary (exact workflow invocation ordering) ----------------------


def test_cli_accepts_workflow_repo_root_before_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    work, _origin, source, base = make_repo(tmp_path)
    assert release_pipeline.main(["--repo-root", str(work), "select-source", "--input", "", "--default-sha", source]) == 0
    assert json.loads(capsys.readouterr().out) == {"source_sha": source}
    assert release_pipeline.main(["--repo-root", str(work), "plan"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "new" and plan["tag"] == "V0.19" and plan["base_main"] == base
