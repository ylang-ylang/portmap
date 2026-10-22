import json
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from portmap import agent, catalog
from portmap.agent_client import AgentUnavailable, agent_health, agent_worktrees, request_json
from tests.test_catalog import run, serving, write_git_compose_repo


@pytest.fixture(autouse=True)
def inventory_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", "")
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(catalog, "docker_get", lambda path: [])


def override(path, repo_id, name="sample"):
    (path / ".portmap").mkdir(exist_ok=True)
    (path / ".portmap/repo.toml").write_text(
        f'repo_id = "{repo_id}"\ndisplay_name = "{name}"\n', encoding="utf-8",
    )


def linked_worktree(repo, path, branch):
    run(["git", "worktree", "add", "-b", branch, str(path)], cwd=repo)
    return path


def record_git_queries(monkeypatch):
    calls = Counter()
    actual_git = catalog.git

    def measured_git(path, *args):
        calls[(path, args)] += 1
        return actual_git(path, *args)

    monkeypatch.setattr(catalog, "git", measured_git)
    return calls


def test_root_container_candidates_are_unique_without_losing_nested_roots(tmp_path):
    root = tmp_path / "projects@wt"
    first = root / "sample@dev"
    nested = root / "other@wt" / "other@case"
    first.mkdir(parents=True)
    nested.mkdir(parents=True)
    candidates = catalog.root_worktree_candidates(root)
    assert len(candidates) == len(set(candidates))
    assert set(candidates) == {root, first, nested.parent, nested}


def test_family_scan_enumerates_submodules_once_per_branch_and_preserves_detached_roots(
    tmp_path, monkeypatch,
):
    source = tmp_path / "child-source"
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(source)
    write_git_compose_repo(repo)
    relative = "modules/child"
    run(["git", "-c", "protocol.file.allow=always", "submodule", "add", str(source), relative], cwd=repo)
    run(["git", "commit", "-am", "add child"], cwd=repo)
    siblings = [repo]
    for index in range(3):
        sibling = linked_worktree(repo, tmp_path / f"sample@case-{index}", f"case-{index}")
        run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init"], cwd=sibling)
        siblings.append(sibling)
    # Branch-specific .gitmodules and relative paths must not be cached by family.
    moved = siblings[-1]
    run(["git", "mv", relative, "modules/renamed"], cwd=moved)
    run(["git", "commit", "-am", "move child"], cwd=moved)
    run(["git", "checkout", "--detach"], cwd=siblings[-2])
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(repo))
    calls = record_git_queries(monkeypatch)

    records = agent.collect_agent_worktrees()["worktrees"]

    expected_children = {str(path / relative) for path in siblings[:-1]} | {str(moved / "modules/renamed")}
    children = [record for record in records if record["worktree_status"] == "submodule"]
    assert {record["worktree"] for record in children} == expected_children
    assert {record["worktree"] for record in records} == {str(path) for path in siblings} | expected_children
    assert any(record["branch"].startswith("detached-") for record in records)
    assert {record["submodule_relative_path"] for record in children} == {relative, "modules/renamed"}
    assert len({record["display_worktree_root"] for record in children}) == 2
    config_args = ("config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$")
    assert {path: count for (path, args), count in calls.items() if args == config_args} == {
        path: 1 for path in siblings
    }
    worktree_args = ("worktree", "list", "--porcelain")
    assert sum(count for (path, args), count in calls.items() if args == worktree_args and path in siblings) == 1


def test_history_current_and_duplicate_paths_are_materialized_once(tmp_path, monkeypatch):
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo)
    historical = {"worktree": str(repo), "branch": "dev", "last_seen_at": "old"}
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"worktrees": [historical, historical]}), encoding="utf-8")
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(repo))
    calls = record_git_queries(monkeypatch)

    current, = agent.collect_agent_worktrees()["worktrees"]

    tip_args = ("log", "-1", "--format=%ct%x00%cI%x00%H")
    assert calls[(repo, tip_args)] == 1
    assert current["source"] == "current"
    assert current["last_seen_at"] != "old"
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", "")
    history.write_text(json.dumps({"worktrees": [dict(historical, branch="wrong"), historical, historical]}))
    calls.clear()

    restored, = agent.collect_agent_worktrees()["worktrees"]

    assert restored["source"] == "history"
    assert restored["last_seen_at"] == "old"
    assert calls[(repo, tip_args)] == 1
    assert len(json.loads(history.read_text())["worktrees"]) == 1


def test_next_scan_refreshes_head_config_and_history_branch_filter(tmp_path, monkeypatch):
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo)
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(repo))
    first, = agent.collect_agent_worktrees()["worktrees"]
    run(["git", "checkout", "-b", "new-branch"], cwd=repo)
    override(repo, "new-identity", "New name")
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-m", "new config and HEAD"], cwd=repo)
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", "")
    assert agent.collect_agent_worktrees()["worktrees"] == []
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(repo))
    second, = agent.collect_agent_worktrees()["worktrees"]
    assert second["branch"] == "new-branch"
    assert second["branch_tip_sha"] != first["branch_tip_sha"]
    assert second["repo_id"] == second["display_repo_id"] == "new-identity"
    assert second["repo_name"] == second["display_repo_name"] == "New name"
    assert second["worktree_root"] == first["worktree_root"]
    (repo / ".portmap/repo.toml").unlink()
    third, = agent.collect_agent_worktrees()["worktrees"]
    assert third["repo_id"] == first["repo_id"]
    assert "display_repo_id" not in third
    assert "display_repo_name" not in third


def test_mixed_override_family_projects_display_only_and_exact_service_paths(tmp_path, monkeypatch):
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo)
    paths = [repo] + [
        linked_worktree(repo, tmp_path / f"sample@case-{index}", f"case-{index}")
        for index in range(14)
    ]
    for path in paths[:13]:
        # Hex-looking explicit IDs are still explicit; provenance is config.
        override(path, "deadbeef1234")
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(repo))
    records = agent.collect_agent_worktrees()["worktrees"]
    assert len(records) == 15
    assert len({record["repo_id"] for record in records}) == 2
    assert {record["display_repo_id"] for record in records} == {"deadbeef1234"}
    assert {record["display_repo_name"] for record in records} == {"sample"}
    assert len({record["worktree_root"] for record in records}) == 1
    fallback = next(record for record in records if record["repo_id"] != "deadbeef1234")
    services = [
        {"worktree": fallback["worktree"], "repo_id": "routing-label", "repo_name": "routing-name",
         "display_repo_id": "stale", "display_repo_name": "stale", "endpoints": [{"url": "http://original"}]},
        {"worktree": fallback["worktree"] + "/", "repo_id": "unmatched"},
    ]
    catalog.enrich_services_from_worktrees(services, records)
    assert services[0]["repo_id"] == "routing-label"
    assert services[0]["repo_name"] == "routing-name"
    assert services[0]["display_repo_id"] == "deadbeef1234"
    assert services[0]["display_repo_name"] == "sample"
    assert services[0]["endpoints"] == [{"url": "http://original"}]
    assert "display_repo_id" not in services[1]
    assert fallback["repo_id"] != fallback["display_repo_id"]
    saved = json.loads((tmp_path / "history.json").read_text())
    assert saved["version"] == 1
    assert all("display_repo_id" not in entry for entry in saved["worktrees"])


@pytest.mark.parametrize("conflict", ["identity", "name"])
def test_conflicting_explicit_family_is_not_arbitrarily_collapsed(tmp_path, monkeypatch, conflict):
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo)
    sibling = linked_worktree(repo, tmp_path / "sample@other", "other")
    linked_worktree(repo, tmp_path / "sample@fallback", "fallback")
    override(repo, "first", "First")
    override(sibling, "second" if conflict == "identity" else "first", "Second")
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(repo))
    records = agent.collect_agent_worktrees()["worktrees"]
    assert len(records) == 3
    assert all("display_repo_id" not in record and "display_repo_name" not in record for record in records)
    assert {record["repo_id"] for record in records} >= {"first"}
    if conflict == "identity":
        assert len({record["repo_id"] for record in records}) == 3


def test_separate_common_dirs_with_same_name_and_ancestry_are_not_reconciled(tmp_path, monkeypatch):
    repo = tmp_path / "sample@dev"
    clone = tmp_path / "sample@clone"
    write_git_compose_repo(repo)
    run(["git", "clone", str(repo), str(clone)], cwd=tmp_path)
    override(repo, "explicit")
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(tmp_path))
    records = {record["worktree"]: record for record in agent.collect_agent_worktrees()["worktrees"]}
    assert records[str(repo)]["display_repo_id"] == "explicit"
    assert "display_repo_id" not in records[str(clone)]
    assert records[str(repo)]["repo_name"] == records[str(clone)]["repo_name"]
    assert records[str(repo)]["worktree_root"] != records[str(clone)]["worktree_root"]


def test_history_only_explicit_sibling_reconciles_current_fallback(tmp_path, monkeypatch):
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo, with_compose=False, with_endpoints=False)
    sibling = linked_worktree(repo, tmp_path / "sample@explicit", "explicit")
    override(sibling, "explicit")
    # An incidental sibling without endpoints is deliberately history-only.
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"worktrees": [{"worktree": str(sibling), "branch": "explicit"}]}))
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(repo))
    records = agent.collect_agent_worktrees()["worktrees"]
    assert len(records) == 2
    assert {record["display_repo_id"] for record in records} == {"explicit"}
    assert {record["source"] for record in records} == {"current", "history"}
    assert len({record["repo_id"] for record in records}) == 2


def test_unqualified_conflicting_sibling_prevents_partial_family_projection(tmp_path, monkeypatch):
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo, with_compose=False, with_endpoints=False)
    sibling = linked_worktree(repo, tmp_path / "sample@hidden", "hidden")
    override(repo, "first")
    override(sibling, "second")
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(repo))
    record, = agent.collect_agent_worktrees()["worktrees"]
    assert record["worktree"] == str(repo)
    assert record["repo_id"] == "first"
    assert "display_repo_id" not in record


def test_concurrent_scans_own_independent_read_results(tmp_path, monkeypatch):
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo)
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(repo))
    barrier = threading.Barrier(2)

    def collect():
        scan = catalog.WorktreeScan()
        barrier.wait(timeout=5)
        records = catalog.discover_catalog_worktrees([], scan=scan)
        return scan, records

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(collect) for _ in range(2)]
        (first_scan, first), (second_scan, second) = [future.result(timeout=15) for future in futures]
    assert first == second
    assert first_scan is not second_scan
    first[0]["repo_name"] = "caller mutation"
    assert second[0]["repo_name"] == "sample"
    assert catalog.catalog_worktree_from_path(repo, {}, scan=first_scan)["repo_name"] == "sample"


def test_worktree_unix_http_budget_exceeds_two_seconds_without_changing_health_budget():
    class DelayedHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(2.4)
            payload = json.dumps({"ok": True, "path": self.path, "worktrees": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass  # The health/generic client's shorter budget closed it.

        def log_message(self, format, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="pmi-") as directory:
        socket_path = Path(directory) / "agent.sock"
        server = agent.ThreadingUnixHTTPServer(str(socket_path), DelayedHandler)
        with serving(server):
            assert agent_worktrees(socket_path=socket_path)["path"] == "/worktrees"
            with pytest.raises(AgentUnavailable, match="timed out"):
                agent_health(socket_path=socket_path)
            with pytest.raises(AgentUnavailable, match="timed out"):
                request_json("GET", "/generic", socket_path=socket_path)
