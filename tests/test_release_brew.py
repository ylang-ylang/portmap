"""Behavioral tests for tools/release_brew.py.

Covers the behaviors the release-distribution workflow depends on:
formula mutation (class-level only), idempotent and identity-preserving tap
update decisions, strict release/asset verification, branch/PR provenance
validation, pre-merge race guards, and bounded check gating. Additional
regressions pin the workflow file's security boundary (tag validated before
use, trusted-revision checkout, token contract, global tap concurrency).
All tests run without network access.
"""

import hashlib
import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "release_brew.py"
SPEC = importlib.util.spec_from_file_location("release_brew", MODULE_PATH)
release_brew = importlib.util.module_from_spec(SPEC)
sys.modules["release_brew"] = release_brew
assert SPEC.loader is not None
SPEC.loader.exec_module(release_brew)

SOURCE_REPO = "ylang-ylang/portmap"
TAP_REPO = "ylang-ylang/homebrew-tap"
TAG = "V0.19"
TAG_SHA = "1" * 40
VERSION = "0.19.0"
ARCHIVE_URL = f"https://github.com/{SOURCE_REPO}/archive/refs/tags/{TAG}.tar.gz"
ARCHIVE_SHA = "f" * 64
MANIFEST_SHA = "a" * 64
MANIFEST_SIZE = 9000000
REQUIRED_CHECK = "tap-checks"
BRANCH = "brew/portmap-V0.19"

FORMULA = '''class Portmap < Formula
  include Language::Python::Virtualenv

  desc "Unified local development gateway and split-DNS remote access"
  homepage "https://github.com/ylang-ylang/portmap"
  url "https://github.com/ylang-ylang/portmap/archive/refs/tags/V0.18.tar.gz"
  version "0.18.0"
  sha256 "4fe133712f5a25cbd239af358f652bf61dcd4a543e4bd249258980c638d5b441"
  license "MIT"

  depends_on :linux
  depends_on "python@3.12"

  allow_network_access! [:build, :postinstall]

  resource "brew-setup" do
    url "https://raw.githubusercontent.com/ylang-ylang/homebrew-tap/12f2701bcbfb39c4825e39e8ce612767ede4a16a/libexec/portmap_brew_setup.py"
    sha256 "bc68d3a6b182b9732cd6b4b9220cb6b676f8fda4adbc2a7e4568944e53b16575"
  end

  resource "brew-postinstall" do
    url "https://raw.githubusercontent.com/ylang-ylang/homebrew-tap/12f2701bcbfb39c4825e39e8ce612767ede4a16a/libexec/portmap_brew_postinstall.py"
    sha256 "03812f2fd81bbc2a35e4a6f2aee4473b8ff8ea529fa594ad230aeff758ff00fe6"
  end

  def install
    virtualenv_install_with_resources without: ["brew-setup", "brew-postinstall"]
    resource("brew-setup").stage { libexec.install "portmap_brew_setup.py" }
  end

  def caveats
    <<~EOS
      Installation uses your existing local Docker or Podman API.
    EOS
  end

  test do
    system bin/"portmap", "init"
  end
end
'''

MANIFEST = {
    "version": VERSION,
    "assets": [
        {
            "os": "linux",
            "arch": "amd64",
            "filename": f"portmap-{VERSION}-linux-amd64.tar.gz",
            "sha256": MANIFEST_SHA,
            "size": MANIFEST_SIZE,
            "source_url": (
                f"https://github.com/{SOURCE_REPO}/releases/download/{TAG}/"
                f"portmap-{VERSION}-linux-amd64.tar.gz"
            ),
        }
    ],
}


def manifest_document(mutate: Any = None) -> str:
    payload = json.loads(json.dumps(MANIFEST))
    if mutate is not None:
        mutate(payload)
    return json.dumps(payload)


def target_formula(
    url: str = ARCHIVE_URL, version: str = VERSION, sha256: str = ARCHIVE_SHA
) -> str:
    return release_brew.mutate_formula(FORMULA, url=url, version=version, sha256=sha256)


# ---------------------------------------------------------------------------
# Formula mutation
# ---------------------------------------------------------------------------


def test_mutate_formula_replaces_only_class_level_fields() -> None:
    mutated = release_brew.mutate_formula(
        FORMULA, url=ARCHIVE_URL, version=VERSION, sha256=ARCHIVE_SHA
    )
    original_lines = FORMULA.splitlines()
    mutated_lines = mutated.splitlines()
    assert len(original_lines) == len(mutated_lines)
    changed = [
        (before, after)
        for before, after in zip(original_lines, mutated_lines)
        if before != after
    ]
    assert changed == [
        (
            f'  url "https://github.com/{SOURCE_REPO}/archive/refs/tags/V0.18.tar.gz"',
            f'  url "{ARCHIVE_URL}"',
        ),
        ('  version "0.18.0"', f'  version "{VERSION}"'),
        (
            '  sha256 "4fe133712f5a25cbd239af358f652bf61dcd4a543e4bd249258980c638d5b441"',
            f'  sha256 "{ARCHIVE_SHA}"',
        ),
    ]
    # resource stanzas (deeper-indented url/sha256) are preserved verbatim
    for resource_line in (
        '    url "https://raw.githubusercontent.com/ylang-ylang/homebrew-tap/12f2701bcbfb39c4825e39e8ce612767ede4a16a/libexec/portmap_brew_setup.py"',
        '    sha256 "bc68d3a6b182b9732cd6b4b9220cb6b676f8fda4adbc2a7e4568944e53b16575"',
    ):
        assert resource_line in mutated


def test_mutate_formula_is_idempotent() -> None:
    once = release_brew.mutate_formula(
        FORMULA, url=ARCHIVE_URL, version=VERSION, sha256=ARCHIVE_SHA
    )
    twice = release_brew.mutate_formula(
        once, url=ARCHIVE_URL, version=VERSION, sha256=ARCHIVE_SHA
    )
    assert once == twice


def test_mutate_formula_with_current_values_is_a_noop() -> None:
    current = target_formula()
    assert (
        release_brew.mutate_formula(
            current, url=ARCHIVE_URL, version=VERSION, sha256=ARCHIVE_SHA
        )
        == current
    )
    assert release_brew.formula_is_current(
        current, url=ARCHIVE_URL, version=VERSION, sha256=ARCHIVE_SHA
    )


def test_formula_is_current_detects_stale_checksum() -> None:
    current = target_formula()
    assert not release_brew.formula_is_current(
        current, url=ARCHIVE_URL, version=VERSION, sha256="0" * 64
    )


@pytest.mark.parametrize(
    "remove", ["url", "version", "sha256"], ids=lambda name: f"missing-{name}"
)
def test_mutate_formula_missing_class_level_key_raises(remove: str) -> None:
    text = "\n".join(
        line
        for line in FORMULA.splitlines()
        if not line.lstrip().startswith(f'{remove} "')
    )
    with pytest.raises(release_brew.FormulaError, match=remove):
        release_brew.mutate_formula(
            text, url=ARCHIVE_URL, version=VERSION, sha256=ARCHIVE_SHA
        )


def test_mutate_formula_duplicate_class_level_key_raises() -> None:
    text = FORMULA + f'\n  sha256 "{ARCHIVE_SHA}"\n'
    with pytest.raises(release_brew.FormulaError, match="more than once"):
        release_brew.mutate_formula(
            text, url=ARCHIVE_URL, version=VERSION, sha256=ARCHIVE_SHA
        )


def test_parse_formula_version() -> None:
    assert release_brew.parse_formula_version(FORMULA) == (0, 18, 0)
    broken = FORMULA.replace('  version "0.18.0"', '  version "0.18"')
    with pytest.raises(release_brew.FormulaError):
        release_brew.parse_formula_version(broken)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", ["V0.19", "V1.2", "V0.0"])
def test_parse_tag_accepts_project_tags(tag: str) -> None:
    assert release_brew.parse_tag(tag) == tuple(int(part) for part in tag[1:].split("."))


@pytest.mark.parametrize(
    "tag",
    ["v0.19", "V0.19.0", "V0.19.0.1", "0.19", "../../etc", "refs/pull/1/head", "V0_19", "", "V0.19 ", "V0.19\nx=1"],
)
def test_parse_tag_rejects_other_tags(tag: str) -> None:
    with pytest.raises(release_brew.InputError):
        release_brew.parse_tag(tag)


@pytest.mark.parametrize("repo", ["a/b", "ylang-ylang/portmap", "a.b-c/d_e.f"])
def test_validate_repo_accepts_owner_name(repo: str) -> None:
    assert release_brew.validate_repo(repo) == repo


@pytest.mark.parametrize(
    "repo", ["", "portmap", "https://github.com/a/b", "a/b/c", "../escape"]
)
def test_validate_repo_rejects_other_shapes(repo: str) -> None:
    with pytest.raises(release_brew.InputError):
        release_brew.validate_repo(repo)


def test_validate_formula_path() -> None:
    assert release_brew.validate_formula_path("Formula/portmap.rb") == "Formula/portmap.rb"
    for bad in ("portmap.rb", "Formula/../x.rb", "Formula/x.rb.j2", ""):
        with pytest.raises(release_brew.InputError):
            release_brew.validate_formula_path(bad)


def test_validate_archive_url_requires_exact_form() -> None:
    assert release_brew.validate_archive_url(ARCHIVE_URL, SOURCE_REPO, TAG) == ARCHIVE_URL
    for bad in (
        ARCHIVE_URL.replace("https://", "http://"),
        ARCHIVE_URL.replace("github.com", "evil.example"),
        ARCHIVE_URL + "?t=1",
        ARCHIVE_URL.replace(f"/{TAG}.tar.gz", "/V0.18.tar.gz"),
        f"https://github.com/other/portmap/archive/refs/tags/{TAG}.tar.gz",
        f"https://user@github.com/{SOURCE_REPO}/archive/refs/tags/{TAG}.tar.gz",
    ):
        with pytest.raises(release_brew.InputError):
            release_brew.validate_archive_url(bad, SOURCE_REPO, TAG)


# ---------------------------------------------------------------------------
# Release manifest
# ---------------------------------------------------------------------------


def test_parse_release_manifest_accepts_project_manifest() -> None:
    manifest = release_brew.parse_release_manifest(
        manifest_document(), source_repo=SOURCE_REPO, tag=TAG
    )
    assert manifest["version"] == VERSION
    assert manifest["filename"] == f"portmap-{VERSION}-linux-amd64.tar.gz"
    assert manifest["sha256"] == MANIFEST_SHA
    assert manifest["size"] == MANIFEST_SIZE


def test_parse_release_manifest_rejects_version_tag_mismatch() -> None:
    document = manifest_document(lambda payload: payload.update(version="0.18.0"))
    with pytest.raises(release_brew.InputError, match="does not match tag"):
        release_brew.parse_release_manifest(document, source_repo=SOURCE_REPO, tag=TAG)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda p: p["assets"][0].update(filename="portmap-0.19.0-linux-arm64.tar.gz"), "filename"),
        (lambda p: p["assets"][0].update(sha256="XYZ"), "sha256"),
        (lambda p: p["assets"][0].update(size=0), "size"),
        (lambda p: p["assets"][0].update(os="darwin"), "exactly one"),
        (lambda p: p.update(assets=[]), "no assets"),
        (lambda p: p["assets"][0].update(source_url="http://github.com/ylang-ylang/portmap/releases/download/V0.19/x.tar.gz"), "source_url"),
        (lambda p: p["assets"][0].update(source_url="https://github.com/evil/portmap/releases/download/V0.19/portmap-0.19.0-linux-amd64.tar.gz"), "source_url"),
        (lambda p: p["assets"][0].update(source_url="https://github.com/ylang-ylang/portmap/releases/download/V0.18/portmap-0.19.0-linux-amd64.tar.gz"), "source_url"),
        (lambda p: p.update(version="0.19"), "X.Y.Z"),
    ],
)
def test_parse_release_manifest_rejects_bad_manifests(mutate: Any, match: str) -> None:
    with pytest.raises(release_brew.InputError, match=match):
        release_brew.parse_release_manifest(
            manifest_document(mutate), source_repo=SOURCE_REPO, tag=TAG
        )


def test_parse_release_manifest_rejects_non_json() -> None:
    with pytest.raises(release_brew.InputError, match="not valid JSON"):
        release_brew.parse_release_manifest("<html>", source_repo=SOURCE_REPO, tag=TAG)


# ---------------------------------------------------------------------------
# Update classification: never downgrade, never re-identity a version
# ---------------------------------------------------------------------------


def test_classify_tap_update_refuses_downgrades() -> None:
    assert release_brew.classify_tap_update((0, 19, 0), (0, 18, 0)) == "superseded"
    assert release_brew.classify_tap_update((0, 20, 1), (0, 19, 0)) == "superseded"


def test_classify_tap_update_allows_forward_and_equal_updates() -> None:
    assert release_brew.classify_tap_update((0, 18, 0), (0, 19, 0)) == "update"
    assert release_brew.classify_tap_update((0, 19, 0), (0, 19, 0)) == "update"


# ---------------------------------------------------------------------------
# Pull request identity and selection
# ---------------------------------------------------------------------------


def pull(
    number: int,
    state: str,
    merged_at: str | None = None,
    *,
    base_ref: str = "main",
    head_ref: str = BRANCH,
    base_repo: str = TAP_REPO,
    head_repo: str = TAP_REPO,
) -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "merged_at": merged_at,
        "base": {"ref": base_ref, "repo": {"full_name": base_repo}},
        "head": {"ref": head_ref, "repo": {"full_name": head_repo}},
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"base_ref": "release"},
        {"head_ref": "other/branch"},
        {"base_repo": "evil/tap"},
        {"head_repo": "fork/tap"},
    ],
    ids=["match", "wrong-base", "wrong-head", "foreign-base-repo", "fork-head-repo"],
)
def test_pull_matches_enforces_full_identity(kwargs: dict[str, Any]) -> None:
    should_match = not kwargs
    assert (
        release_brew.pull_matches(
            pull(1, "open", **kwargs),
            tap_repo=TAP_REPO,
            branch=BRANCH,
            default_branch="main",
        )
        is should_match
    )


def test_require_distribution_pr_raises_on_foreign_pr() -> None:
    with pytest.raises(release_brew.ReleaseError, match="expected ylang-ylang/homebrew-tap:main"):
        release_brew.require_distribution_pr(
            pull(1, "open", base_ref="release"),
            tap_repo=TAP_REPO,
            branch=BRANCH,
            default_branch="main",
            context="test",
        )


def test_choose_pull_action_reuses_open_pr() -> None:
    assert release_brew.choose_pull_action(
        [pull(7, "closed", "2026-01-01T00:00:00Z"), pull(9, "open")]
    ) == ("reuse", 9)


def test_choose_pull_action_reopens_closed_unmerged_pr() -> None:
    assert release_brew.choose_pull_action([pull(7, "closed", None)]) == ("reopen", 7)


def test_choose_pull_action_creates_when_merged_or_empty() -> None:
    assert release_brew.choose_pull_action([pull(7, "closed", "2026-01-01T00:00:00Z")]) == (
        "create",
        None,
    )
    assert release_brew.choose_pull_action([]) == ("create", None)


# ---------------------------------------------------------------------------
# Check gating
# ---------------------------------------------------------------------------


def run(name: str, status: str, conclusion: str | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


def status(context: str, state: str, identifier: int = 1) -> dict[str, Any]:
    return {"context": context, "state": state, "id": identifier}


def test_evaluate_checks_ready_when_required_green_and_all_others_pass() -> None:
    verdict = release_brew.evaluate_checks(
        [run(REQUIRED_CHECK, "completed", "success"), run("lint", "completed", "success")],
        [status("ci/other", "success")],
        REQUIRED_CHECK,
    )
    assert verdict.state == "ready"
    assert verdict.problems == ()


def test_evaluate_checks_waiting_while_required_missing_or_pending() -> None:
    assert release_brew.evaluate_checks([], [], REQUIRED_CHECK).state == "waiting"
    assert (
        release_brew.evaluate_checks(
            [run(REQUIRED_CHECK, "in_progress"), run("lint", "completed", "success")],
            [],
            REQUIRED_CHECK,
        ).state
        == "waiting"
    )
    assert (
        release_brew.evaluate_checks(
            [run(REQUIRED_CHECK, "completed", "success")],
            [status("ci/other", "pending")],
            REQUIRED_CHECK,
        ).state
        == "waiting"
    )


def test_evaluate_checks_failing_beats_waiting() -> None:
    verdict = release_brew.evaluate_checks(
        [run(REQUIRED_CHECK, "queued"), run("lint", "completed", "failure")],
        [],
        REQUIRED_CHECK,
    )
    assert verdict.state == "failing"
    assert any("lint" in problem for problem in verdict.problems)


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "timed_out", "cancelled", "action_required", "startup_failure", "skipped", "neutral"],
)
def test_evaluate_checks_required_must_conclude_exactly_success(conclusion: str) -> None:
    verdict = release_brew.evaluate_checks(
        [run(REQUIRED_CHECK, "completed", conclusion)], [], REQUIRED_CHECK
    )
    assert verdict.state == "failing"


def test_evaluate_checks_second_required_run_must_not_be_ignored() -> None:
    """Duplicate check names (other apps/workflows) cannot smuggle a green
    verdict past a failing or pending same-name run."""
    failing = release_brew.evaluate_checks(
        [run(REQUIRED_CHECK, "completed", "success"), run(REQUIRED_CHECK, "completed", "failure")],
        [],
        REQUIRED_CHECK,
    )
    assert failing.state == "failing"

    pending = release_brew.evaluate_checks(
        [run(REQUIRED_CHECK, "completed", "success"), run(REQUIRED_CHECK, "in_progress")],
        [],
        REQUIRED_CHECK,
    )
    assert pending.state == "waiting"


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_evaluate_checks_tolerates_non_blocking_conclusions(conclusion: str) -> None:
    verdict = release_brew.evaluate_checks(
        [run(REQUIRED_CHECK, "completed", "success"), run("optional", "completed", conclusion)],
        [],
        REQUIRED_CHECK,
    )
    assert verdict.state == "ready"


def test_latest_statuses_by_context_keeps_newest_report() -> None:
    superseded = release_brew.latest_statuses_by_context(
        [status("ci/x", "failure", 1), status("ci/x", "success", 2)]
    )
    assert [entry["state"] for entry in superseded] == ["success"]

    regressed = release_brew.latest_statuses_by_context(
        [status("ci/x", "success", 1), status("ci/x", "failure", 2)]
    )
    assert [entry["state"] for entry in regressed] == ["failure"]


def test_evaluate_checks_uses_latest_status_per_context() -> None:
    verdict = release_brew.evaluate_checks(
        [run(REQUIRED_CHECK, "completed", "success")],
        [status("ci/x", "failure", 1), status("ci/x", "success", 2)],
        REQUIRED_CHECK,
    )
    assert verdict.state == "ready"


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds


def test_wait_for_checks_returns_once_green() -> None:
    clock = FakeClock()
    states = iter(
        [
            ([run(REQUIRED_CHECK, "queued")], []),
            ([run(REQUIRED_CHECK, "in_progress")], []),
            ([run(REQUIRED_CHECK, "completed", "success")], []),
        ]
    )
    verdict = release_brew.wait_for_checks(
        lambda: next(states),
        required_name=REQUIRED_CHECK,
        timeout_seconds=300,
        poll_seconds=20,
        sleep=clock.sleep,
        now=clock.now,
    )
    assert verdict.state == "ready"
    assert clock.sleeps == [20, 20]


def test_wait_for_checks_times_out_bounded() -> None:
    clock = FakeClock()

    def stuck() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [run(REQUIRED_CHECK, "in_progress")], []

    with pytest.raises(release_brew.CheckTimeout, match="in_progress"):
        release_brew.wait_for_checks(
            stuck,
            required_name=REQUIRED_CHECK,
            timeout_seconds=300,
            poll_seconds=20,
            sleep=clock.sleep,
            now=clock.now,
        )
    assert clock.elapsed == 300
    assert clock.sleeps == [20] * 15


def test_wait_for_checks_fails_fast_on_failure() -> None:
    clock = FakeClock()

    def broken() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [run(REQUIRED_CHECK, "completed", "failure")], []

    with pytest.raises(release_brew.CheckFailure, match="failure"):
        release_brew.wait_for_checks(
            broken,
            required_name=REQUIRED_CHECK,
            timeout_seconds=300,
            poll_seconds=20,
            sleep=clock.sleep,
            now=clock.now,
        )
    assert clock.sleeps == []


# ---------------------------------------------------------------------------
# End-to-end update-tap flow against fake repositories
# ---------------------------------------------------------------------------


class FakeGitHub(release_brew.GitHub):
    """Minimal state machine over the REST paths update_tap touches."""

    def __init__(self, *, default_branch: str, formula: str) -> None:
        self.default_branch = default_branch
        self.base_sha = "2" * 40
        self.branches: dict[str, str] = {default_branch: self.base_sha}
        self.files: dict[str, str] = {default_branch: formula, self.base_sha: formula}
        self.pull: dict[str, Any] | None = None
        self.merged = False
        self.check_runs: list[list[dict[str, Any]]] = []
        self.statuses: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.writes = 0
        self.last_push_branch: str | None = None
        self.last_commit_sha: str | None = None
        self.merge_bodies: list[dict[str, Any]] = []
        self.compare: dict[str, Any] = {
            "status": "ahead",
            "files": [{"filename": "Formula/portmap.rb"}],
        }
        self.pull_files: list[dict[str, Any]] = [{"filename": "Formula/portmap.rb"}]
        self.heads_responses: list[str] = []
        self.merge_on_fetch = False
        self.pull_number = 12

    def _identity_pull(self, **overrides: Any) -> dict[str, Any]:
        head_ref = BRANCH
        head_sha = self.branches.get(head_ref, "c" * 40)
        payload: dict[str, Any] = {
            "number": self.pull_number,
            "state": "open",
            "merged_at": None,
            "mergeable": True,
            "base": {"ref": self.default_branch, "repo": {"full_name": TAP_REPO}},
            "head": {
                "ref": head_ref,
                "repo": {"full_name": TAP_REPO},
                "sha": head_sha,
            },
        }
        payload.update(overrides)
        return payload

    def request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        route = path.split("?")[0]
        self.calls.append((method, route))
        if method == "GET" and route == f"/repos/{TAP_REPO}":
            return {"default_branch": self.default_branch}
        if method == "GET" and route.startswith(f"/repos/{TAP_REPO}/git/ref/heads/"):
            branch = route.removeprefix(f"/repos/{TAP_REPO}/git/ref/heads/")
            if branch == self.default_branch and self.heads_responses:
                return {"object": {"sha": self.heads_responses.pop(0)}}
            if branch not in self.branches:
                raise release_brew.ApiError(
                    f"GitHub API GET {path} failed with HTTP 404: Branch not found"
                )
            return {"object": {"sha": self.branches[branch]}}
        if method == "POST" and route == f"/repos/{TAP_REPO}/git/refs":
            self.writes += 1
            self.branches[json_body["ref"].removeprefix("refs/heads/")] = json_body["sha"]
            return {}
        if method == "GET" and route == f"/repos/{TAP_REPO}/contents/Formula/portmap.rb":
            ref = urllib.parse.unquote(path.rsplit("ref=", 1)[1])
            if ref not in self.files:
                raise release_brew.ApiError(
                    f"GitHub API GET {path} failed with HTTP 404: Not Found"
                )
            import base64

            return {
                "encoding": "base64",
                "sha": "blob-" + ref[:8],
                "content": base64.b64encode(self.files[ref].encode()).decode(),
            }
        if method == "PUT" and route == f"/repos/{TAP_REPO}/contents/Formula/portmap.rb":
            self.writes += 1
            import base64

            branch = json_body["branch"]
            commit_sha = "c" * 39 + str(self.writes % 10)
            self.files[branch] = base64.b64decode(json_body["content"]).decode()
            self.files[commit_sha] = self.files[branch]
            self.branches[branch] = commit_sha
            self.last_push_branch = branch
            self.last_commit_sha = commit_sha
            return {"commit": {"sha": commit_sha}}
        if method == "GET" and route == f"/repos/{TAP_REPO}/pulls":
            return [self.pull] if self.pull else []
        if method == "POST" and route == f"/repos/{TAP_REPO}/pulls":
            self.writes += 1
            self.pull = self._identity_pull()
            return self.pull
        if method == "PATCH" and route == f"/repos/{TAP_REPO}/pulls/{self.pull_number}":
            assert self.pull is not None
            self.pull["state"] = json_body.get("state", self.pull["state"])
            return self.pull
        if method == "GET" and route == f"/repos/{TAP_REPO}/pulls/{self.pull_number}":
            assert self.pull is not None
            if self.merge_on_fetch and not self.merged:
                self.merged = True
                self.pull.update(
                    state="closed",
                    merged_at="2026-10-19T00:00:00Z",
                    merge_commit_sha="5" * 40,
                )
            return self.pull
        if method == "GET" and route == f"/repos/{TAP_REPO}/pulls/{self.pull_number}/files":
            return self.pull_files
        if method == "GET" and route.startswith(f"/repos/{TAP_REPO}/compare/"):
            return self.compare
        if method == "GET" and route.endswith("/check-runs"):
            if len(self.check_runs) > 1:
                return {"check_runs": self.check_runs.pop(0)}
            return {"check_runs": self.check_runs[0]}
        if method == "GET" and route.endswith("/statuses"):
            return self.statuses
        if method == "PUT" and route == f"/repos/{TAP_REPO}/pulls/{self.pull_number}/merge":
            self.writes += 1
            self.merge_bodies.append(json_body)
            if json_body["sha"] != self.pull["head"]["sha"]:
                raise release_brew.ApiError(
                    f"GitHub API PUT {path} failed with HTTP 409: Head sha mismatch"
                )
            self.merged = True
            assert self.last_push_branch is not None
            self.pull.update(
                state="closed", merged_at="2026-10-19T00:00:00Z", merge_commit_sha="5" * 40
            )
            self.files[self.default_branch] = self.files[self.last_push_branch]
            self.branches[self.default_branch] = "5" * 40
            self.files["5" * 40] = self.files[self.default_branch]
            return {"merged": True, "sha": "5" * 40}
        raise AssertionError(f"unexpected request {method} {route}")


class FakeSourceGitHub:
    def __init__(self, *, tag_sha: str = TAG_SHA) -> None:
        self.tag_sha = tag_sha

    def request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        route = path.split("?")[0]
        if method == "GET" and route == f"/repos/{SOURCE_REPO}/git/ref/tags/{TAG}":
            return {"object": {"sha": self.tag_sha, "type": "commit"}}
        if method == "GET" and route == f"/repos/{SOURCE_REPO}/git/tags/{self.tag_sha}":
            return {"object": {"sha": "4" * 40, "type": "commit"}}
        if method == "GET" and route == f"/repos/{SOURCE_REPO}/releases/tags/{TAG}":
            return {
                "id": 99,
                "tag_name": TAG,
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "id": 501,
                        "name": "release.json",
                        "browser_download_url": f"https://github.com/{SOURCE_REPO}/releases/download/{TAG}/release.json",
                    },
                    {
                        "id": 502,
                        "name": f"portmap-{VERSION}-linux-amd64.tar.gz",
                        "state": "uploaded",
                        "size": MANIFEST_SIZE,
                        "browser_download_url": f"https://github.com/{SOURCE_REPO}/releases/download/{TAG}/portmap-{VERSION}-linux-amd64.tar.gz",
                    },
                ],
            }
        raise AssertionError(f"unexpected source request {method} {route}")


class MutatingSource(FakeSourceGitHub):
    """Returns a mutated release payload from the second release GET on."""

    def __init__(self) -> None:
        super().__init__()
        self.release_gets = 0
        self.mutate_release: Any = None  # callable(dict) -> dict

    def request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        route = path.split("?")[0]
        if method == "GET" and route == f"/repos/{SOURCE_REPO}/releases/tags/{TAG}":
            self.release_gets += 1
            if self.release_gets > 1 and self.mutate_release is not None:
                payload = json.loads(
                    json.dumps(super().request(method, path, json_body, accept))
                )
                return self.mutate_release(payload)
        return super().request(method, path, json_body, accept)


def run_update_tap(
    monkeypatch: pytest.MonkeyPatch,
    tap: FakeGitHub,
    *,
    formula_on_main: str,
    **overrides: Any,
) -> int:
    monkeypatch.setattr(
        release_brew, "download_bytes", lambda url: manifest_document().encode()
    )
    clock = FakeClock()
    tap.files[tap.default_branch] = formula_on_main
    tap.files[tap.base_sha] = formula_on_main
    params: dict[str, Any] = dict(
        source_repo=SOURCE_REPO,
        tap_repo=TAP_REPO,
        tag=TAG,
        formula_path="Formula/portmap.rb",
        required_check=REQUIRED_CHECK,
        check_timeout_seconds=300,
        poll_seconds=20,
        tap=tap,
        source=FakeSourceGitHub(),
        sha256_of=lambda url: ARCHIVE_SHA,
        digest_of=lambda url: MANIFEST_SHA,
        sleep=clock.sleep,
        now=clock.now,
    )
    params.update(overrides)
    main_override = params.pop("main_branch_formula", None)
    if main_override is not None:
        tap.files[tap.default_branch] = main_override
    download_override = params.pop("download", None)
    if download_override is not None:
        monkeypatch.setattr(release_brew, "download_bytes", download_override)
    return release_brew.update_tap(**params)


def test_update_tap_merges_pr_after_green_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [
        [run(REQUIRED_CHECK, "queued")],
        [run(REQUIRED_CHECK, "completed", "success")],
    ]
    assert run_update_tap(monkeypatch, tap, formula_on_main=FORMULA) == 0

    assert tap.merged is True
    assert BRANCH in tap.branches, "deterministic per-tag branch"
    # merge is pinned to the commit returned by the contents PUT
    assert tap.last_commit_sha is not None
    assert tap.merge_bodies == [{"sha": tap.last_commit_sha, "merge_method": "merge"}]
    merged_formula = tap.files["main"]
    assert f'  url "{ARCHIVE_URL}"' in merged_formula
    assert f'  version "{VERSION}"' in merged_formula
    assert f'  sha256 "{ARCHIVE_SHA}"' in merged_formula
    assert merged_formula.count("raw.githubusercontent.com") == 2
    assert tap.pull is not None and tap.pull["merged_at"]


def test_update_tap_retry_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    assert run_update_tap(monkeypatch, tap, formula_on_main=FORMULA) == 0
    writes_after_first = tap.writes
    branches_after_first = dict(tap.branches)

    # A retry after the merge finds the tap current and performs no writes.
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    assert run_update_tap(monkeypatch, tap, formula_on_main=tap.files["main"]) == 0
    assert tap.writes == writes_after_first
    assert tap.branches == branches_after_first


def test_update_tap_never_downgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    newer = release_brew.mutate_formula(
        FORMULA,
        url="https://github.com/ylang-ylang/portmap/archive/refs/tags/V0.20.tar.gz",
        version="0.20.0",
        sha256="1" * 64,
    )
    tap = FakeGitHub(default_branch="main", formula=newer)
    assert run_update_tap(monkeypatch, tap, formula_on_main=newer) == 0
    assert tap.writes == 0
    assert tap.pull is None
    assert tap.files["main"] == newer


def test_update_tap_rejects_equal_version_identity_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    same_version_wrong_sha = release_brew.mutate_formula(
        FORMULA, url=ARCHIVE_URL, version=VERSION, sha256="0" * 64
    )
    tap = FakeGitHub(default_branch="main", formula=same_version_wrong_sha)
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(monkeypatch, tap, formula_on_main=same_version_wrong_sha)
    assert tap.writes == 0
    assert tap.pull is None


def test_update_tap_rejects_foreign_deterministic_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.branches[BRANCH] = "3" * 40
    tap.files["3" * 40] = FORMULA
    tap.compare = {
        "status": "ahead",
        "files": [
            {"filename": "Formula/portmap.rb"},
            {"filename": "libexec/evil.py"},
        ],
    }
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(monkeypatch, tap, formula_on_main=FORMULA)
    assert tap.writes == 0
    assert tap.pull is None
    assert not tap.merged


def test_update_tap_rejects_diverged_deterministic_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.branches[BRANCH] = "3" * 40
    tap.files["3" * 40] = FORMULA
    tap.compare = {"status": "diverged", "files": [{"filename": "Formula/portmap.rb"}]}
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(monkeypatch, tap, formula_on_main=FORMULA)
    assert tap.writes == 0
    assert not tap.merged


def test_update_tap_rejects_pr_with_unrelated_files_before_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    tap.pull_files = [
        {"filename": "Formula/portmap.rb"},
        {"filename": ".github/workflows/check.yml"},
    ]
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(monkeypatch, tap, formula_on_main=FORMULA)
    assert not tap.merged
    assert tap.writes == 3  # branch ref + formula commit + PR; never merged


def test_update_tap_refuses_downgrade_when_default_advances_during_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    newer = release_brew.mutate_formula(
        FORMULA,
        url="https://github.com/ylang-ylang/portmap/archive/refs/tags/V0.20.tar.gz",
        version="0.20.0",
        sha256="2" * 64,
    )
    advanced_sha = "d" * 40
    tap.files[advanced_sha] = newer
    # base read uses the old head; the pre-merge re-read sees the advanced head
    tap.heads_responses = [tap.base_sha, advanced_sha]
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(monkeypatch, tap, formula_on_main=FORMULA)
    assert not tap.merged


def test_update_tap_noops_when_default_already_serves_target_mid_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    advanced_sha = "d" * 40
    tap.files[advanced_sha] = target_formula()
    tap.heads_responses = [tap.base_sha, advanced_sha]
    assert run_update_tap(monkeypatch, tap, formula_on_main=FORMULA) == 0
    assert not tap.merged, "target already on default; merge is unnecessary"


def test_update_tap_merged_concurrent_pr_verifies_default_formula(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    tap.merge_on_fetch = True
    assert (
        run_update_tap(
            monkeypatch,
            tap,
            formula_on_main=FORMULA,
            main_branch_formula=target_formula(),
        )
        == 0
    )
    assert tap.merge_bodies == []


def test_update_tap_merged_concurrent_pr_fails_when_default_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    tap.merge_on_fetch = True
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(monkeypatch, tap, formula_on_main=FORMULA)
    assert tap.merge_bodies == []


def test_update_tap_reuses_open_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    clock = FakeClock()
    monkeypatch.setattr(
        release_brew, "download_bytes", lambda url: manifest_document().encode()
    )
    params = dict(
        source_repo=SOURCE_REPO,
        tap_repo=TAP_REPO,
        tag=TAG,
        formula_path="Formula/portmap.rb",
        required_check=REQUIRED_CHECK,
        check_timeout_seconds=20,
        poll_seconds=20,
        tap=tap,
        source=FakeSourceGitHub(),
        sha256_of=lambda url: ARCHIVE_SHA,
        digest_of=lambda url: MANIFEST_SHA,
        sleep=clock.sleep,
        now=clock.now,
    )

    # First attempt times out waiting for checks and leaves the PR open.
    tap.check_runs = [[run(REQUIRED_CHECK, "in_progress")]]
    with pytest.raises(release_brew.CheckTimeout):
        release_brew.update_tap(**params)
    assert tap.pull is not None and tap.pull["state"] == "open"
    pr_create_calls = [
        call for call in tap.calls if call == ("POST", f"/repos/{TAP_REPO}/pulls")
    ]
    assert len(pr_create_calls) == 1

    # The retry finds the same open PR on the same deterministic branch and
    # merges it once the required check turns green.
    params["check_timeout_seconds"] = 300
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    assert release_brew.update_tap(**params) == 0
    assert tap.pull["merged_at"] is not None
    assert tap.merged is True
    pr_create_calls = [
        call for call in tap.calls if call == ("POST", f"/repos/{TAP_REPO}/pulls")
    ]
    assert len(pr_create_calls) == 1, "retry must reuse the open PR, not open another"


def test_update_tap_fails_when_checks_time_out(monkeypatch: pytest.MonkeyPatch) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "in_progress")]]
    with pytest.raises(release_brew.CheckTimeout):
        run_update_tap(monkeypatch, tap, formula_on_main=FORMULA)
    assert not tap.merged


def test_update_tap_fails_when_required_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "failure")]]
    with pytest.raises(release_brew.CheckFailure):
        run_update_tap(monkeypatch, tap, formula_on_main=FORMULA)
    assert not tap.merged


def test_update_tap_fails_on_identity_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(
            monkeypatch,
            tap,
            formula_on_main=FORMULA,
            expected_tag_sha="9" * 40,
        )
    assert tap.calls == [], "identity mismatch must fail before any tap access"


class MovingTagSource(FakeSourceGitHub):
    """Flips the tag object SHA after the first tag-ref lookup."""

    def __init__(self) -> None:
        super().__init__()
        self.lookups = 0

    def request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        route = path.split("?")[0]
        if method == "GET" and route == f"/repos/{SOURCE_REPO}/git/ref/tags/{TAG}":
            self.lookups += 1
            if self.lookups > 1:
                return {"object": {"sha": "9" * 40, "type": "commit"}}
        return super().request(method, path, json_body, accept)


def test_update_tap_fails_when_tag_moves_during_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    monkeypatch.setattr(
        release_brew, "download_bytes", lambda url: manifest_document().encode()
    )
    clock = FakeClock()
    with pytest.raises(release_brew.ReleaseError):
        release_brew.update_tap(
            source_repo=SOURCE_REPO,
            tap_repo=TAP_REPO,
            tag=TAG,
            formula_path="Formula/portmap.rb",
            required_check=REQUIRED_CHECK,
            check_timeout_seconds=300,
            poll_seconds=20,
            tap=tap,
            source=MovingTagSource(),
            sha256_of=lambda url: ARCHIVE_SHA,
            digest_of=lambda url: MANIFEST_SHA,
            sleep=clock.sleep,
            now=clock.now,
        )
    assert not tap.merged


def test_update_tap_fails_when_asset_digest_differs_from_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(
            monkeypatch,
            tap,
            formula_on_main=FORMULA,
            digest_of=lambda url: "b" * 64,
        )
    assert tap.calls == [], "asset digest mismatch must fail before any tap access"


def test_verify_release_binds_full_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        release_brew, "download_bytes", lambda url: manifest_document().encode()
    )
    spec = release_brew.verify_published_release(
        FakeSourceGitHub(),
        SOURCE_REPO,
        TAG,
        asset_wait_seconds=0,
        poll_seconds=1,
        digest_of=lambda url: MANIFEST_SHA,
    )
    assert spec.release_id == 99
    assert spec.manifest_asset_id == 501
    assert spec.archive_asset_id == 502
    assert spec.manifest_sha256 == hashlib.sha256(manifest_document().encode()).hexdigest()
    assert spec.archive_sha256 == MANIFEST_SHA
    assert spec.archive_size == MANIFEST_SIZE
    assert spec.tag_object_sha == TAG_SHA
    assert spec.commit_sha == TAG_SHA


def test_update_tap_binds_expected_release_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    assert (
        run_update_tap(
            monkeypatch,
            tap,
            formula_on_main=FORMULA,
            expected_tag_sha=TAG_SHA,
            expected_commit_sha=TAG_SHA,
            expected_release_id=99,
            expected_manifest_sha256=hashlib.sha256(
                manifest_document().encode()
            ).hexdigest(),
            expected_manifest_asset_id=501,
            expected_archive_asset_id=502,
            expected_archive_sha256=MANIFEST_SHA,
            expected_archive_size=MANIFEST_SIZE,
        )
        == 0
    )
    assert tap.merged is True


def test_update_tap_fails_on_release_id_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(
            monkeypatch,
            tap,
            formula_on_main=FORMULA,
            expected_release_id=98,
        )
    assert tap.calls == [], "identity mismatch must fail before any tap access"


def test_update_tap_rejects_release_replacement_before_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    source = MutatingSource()
    source.mutate_release = lambda payload: {**payload, "id": 100}
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(monkeypatch, tap, formula_on_main=FORMULA, source=source)
    assert not tap.merged


def test_update_tap_rejects_draft_flip_before_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    source = MutatingSource()
    source.mutate_release = lambda payload: {**payload, "draft": True}
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(monkeypatch, tap, formula_on_main=FORMULA, source=source)
    assert not tap.merged


def test_update_tap_rejects_asset_replacement_before_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    source = MutatingSource()

    def replace_archive(payload: dict[str, Any]) -> dict[str, Any]:
        assets = [
            dict(asset, id=503) if asset["name"] != "release.json" else asset
            for asset in payload["assets"]
        ]
        return {**payload, "assets": assets}

    source.mutate_release = replace_archive
    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(monkeypatch, tap, formula_on_main=FORMULA, source=source)
    assert not tap.merged


def test_update_tap_rejects_manifest_body_change_before_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = FakeGitHub(default_branch="main", formula=FORMULA)
    tap.check_runs = [[run(REQUIRED_CHECK, "completed", "success")]]
    downloads = {"count": 0}

    def flipping_download(url: str) -> bytes:
        downloads["count"] += 1
        if downloads["count"] == 1:
            return manifest_document().encode()
        return manifest_document(
            lambda payload: payload["assets"][0].update(sha256="c" * 64)
        ).encode()

    with pytest.raises(release_brew.ReleaseError):
        run_update_tap(
            monkeypatch, tap, formula_on_main=FORMULA, download=flipping_download
        )
    assert not tap.merged


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------


def test_load_tap_token_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(release_brew.TAP_TOKEN_ENV, raising=False)
    with pytest.raises(release_brew.InputError, match="HOMEBREW_TAP_TOKEN"):
        release_brew.load_tap_token()

    monkeypatch.setenv(release_brew.TAP_TOKEN_ENV, "token with spaces")
    with pytest.raises(release_brew.InputError, match="whitespace"):
        release_brew.load_tap_token()

    monkeypatch.setenv(release_brew.TAP_TOKEN_ENV, "github_pat_example")
    assert release_brew.load_tap_token() == "github_pat_example"


