from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap.broker import ensure_generated_override
from portmap.catalog import (
    catalog_worktree_from_path,
    compose_up_worktree,
    discover_catalog_worktrees,
    merge_catalog_history,
)
from portmap.cli import main
from portmap.compose import render_compose_json
from portmap.compose_takeover import plan_docker_compose_command
from portmap.errors import ConfigError
from portmap.model import GenerateRequest
from portmap.planner import generate_plan
from portmap.project_compose import (
    COMPOSE_FILENAMES,
    ENDPOINT_CONFIG,
    compose_config_text,
    find_compose_file,
)


ENDPOINTS = '# Keep the project endpoint contract.\n[endpoints.web]\nkind = "http"\nservice = "web"\ncontainer_port = 8080\n'
COMPOSE = 'services:\n  web:\n    image: busybox:latest\n    expose: ["8080"]\n'


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "portmap-test@example.invalid"],
        ["git", "config", "user.name", "portmap test"],
        ["git", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)
    (root / ".portmap").mkdir()
    (root / ENDPOINT_CONFIG).write_text(ENDPOINTS, encoding="utf-8")
    monkeypatch.setenv("PORTMAP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PORTMAP_CLIENT_STATE_DIR", str(tmp_path / "client"))
    monkeypatch.setenv("PORTMAP_ALLOCATION_STATE_FILE", str(tmp_path / "allocations.json"))
    monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(tmp_path / "missing-agent.sock"))
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(root))
    return root


def nested_compose(root: Path) -> Path:
    path = root / "deployment" / "dev.compose.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(COMPOSE, encoding="utf-8")
    return path


def select_compose(root: Path, value: str) -> None:
    (root / ENDPOINT_CONFIG).write_text(ENDPOINTS + f'\n[compose]\nfile = {json.dumps(value)}\n', encoding="utf-8")


@pytest.fixture
def compose_renderer(monkeypatch):
    calls = []

    def render(compose_file, project_directory):
        calls.append((compose_file, project_directory))
        return {"services": {"web": {"image": "busybox:latest", "expose": ["8080"]}}}

    monkeypatch.setattr("portmap.compose.render_compose_json", render)
    # Like the existing demo/planner unit fixtures, supply this dependency at
    # the generation seam; production readiness checks remain untouched.
    monkeypatch.setattr("portmap.cli.container_dns_address", lambda state_dir: "172.20.0.53")
    monkeypatch.setattr("portmap.broker.container_dns_address", lambda state_dir: "172.20.0.53")
    return calls


@pytest.mark.parametrize("filename", COMPOSE_FILENAMES)
def test_root_default_discovery_is_unchanged(project: Path, filename: str, compose_renderer) -> None:
    compose = project / filename
    compose.write_text(COMPOSE, encoding="utf-8")
    assert find_compose_file(project) == compose
    assert main(["generate", "--project-dir", str(project), "--config", ENDPOINT_CONFIG]) == 0
    assert compose_renderer == [(compose, project)]
    assert "compose" not in tomllib.loads((project / ENDPOINT_CONFIG).read_text())


def test_root_discovery_order_and_no_recursive_scan(project: Path) -> None:
    nested_compose(project)
    assert find_compose_file(project) is None
    for name in reversed(COMPOSE_FILENAMES):
        (project / name).write_text(COMPOSE, encoding="utf-8")
        assert find_compose_file(project) == project / name


def test_nested_toml_selection_drives_generation_and_broker(project: Path, compose_renderer) -> None:
    compose = nested_compose(project)
    (project / "docker-compose.yml").write_text("services: {decoy: {image: busybox}}\n", encoding="utf-8")
    select_compose(project, "deployment/dev.compose.yaml")
    assert main(["generate", "--project-dir", str(project), "--config", ENDPOINT_CONFIG]) == 0
    assert compose_renderer == [(compose, project)]
    plan = plan_docker_compose_command(["up", "-d"], cwd=project)
    assert plan.compose_file == compose
    assert plan.command[plan.command.index("-f") + 1] == str(compose)
    assert "--project-directory" not in plan.command
    assert catalog_worktree_from_path(project, {})["startable"] is True


def test_init_cli_choice_persists_without_overwriting_endpoints(project: Path) -> None:
    compose = nested_compose(project)
    select_compose(project, "old/missing.yaml")
    assert main(["init", "--project-dir", str(project), "--compose-file", "deployment/dev.compose.yaml"]) == 0
    text = (project / ENDPOINT_CONFIG).read_text()
    assert text.startswith(ENDPOINTS)
    assert find_compose_file(project) == compose
    assert tomllib.loads(text)["compose"]["file"] == "deployment/dev.compose.yaml"


def test_generate_cli_choice_overrides_toml_and_survives_down_restart(
    project: Path, compose_renderer, monkeypatch,
) -> None:
    compose = nested_compose(project)
    select_compose(project, "old/missing.yaml")
    assert main([
        "generate", "--project-dir", str(project), "--config", ENDPOINT_CONFIG,
        "--compose-file", "deployment/dev.compose.yaml",
    ]) == 0
    first = plan_docker_compose_command(["down"], cwd=project)
    assert first.compose_file == compose
    first_project = first.compose_project
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    with monkeypatch.context() as invocation:
        invocation.chdir(project)
        invocation.setattr("portmap.cli.subprocess.run", run)
        assert main(["docker-compose", "--", "down"]) == 0
        assert calls[-1] == first.command
    # A new broker/agent invocation has no in-memory selection to reuse. Even
    # deleting generated output must leave the persisted endpoint contract.
    (project / ".portmap/docker-compose.override.generated.yml").unlink()
    (project / ".portmap/state.json").unlink()
    result = ensure_generated_override(["restart"], cwd=project, environ={
        "PORTMAP_ALLOCATION_STATE_FILE": str(project / ".portmap/allocations.json"),
        "PORTMAP_STATE_DIR": str(project / ".isolated-state"),
    })
    assert result.compose_file == compose
    assert plan_docker_compose_command(["restart"], cwd=project).compose_project == first_project


def test_custom_generate_config_copies_full_contract_to_canonical(
    project: Path, compose_renderer,
) -> None:
    compose = nested_compose(project)
    custom = project / "project-endpoints.toml"
    custom.write_text(ENDPOINTS, encoding="utf-8")
    (project / ENDPOINT_CONFIG).unlink()
    assert main([
        "generate", "--project-dir", str(project), "--config", custom.name,
        "--compose-file", "deployment/dev.compose.yaml",
    ]) == 0
    canonical = (project / ENDPOINT_CONFIG).read_text()
    assert canonical.startswith(ENDPOINTS)
    assert tomllib.loads(canonical)["endpoints"]["web"]["service"] == "web"
    assert find_compose_file(project) == compose
    assert find_compose_file(project, config_file=custom) == compose
    assert catalog_worktree_from_path(project, {})["startable"] is True


def test_custom_config_preserves_existing_canonical_endpoints(project: Path, compose_renderer) -> None:
    nested_compose(project)
    select_compose(project, "old/missing-canonical.yaml")
    custom = project / "custom.toml"
    custom.write_text(ENDPOINTS.replace("# Keep", "# Custom") + '\n[compose]\nfile = "deployment/dev.compose.yaml"\n')
    assert main(["generate", "--project-dir", str(project), "--config", custom.name]) == 0
    assert (project / ENDPOINT_CONFIG).read_text().startswith(ENDPOINTS)
    assert tomllib.loads(custom.read_text())["compose"]["file"] == "deployment/dev.compose.yaml"


@pytest.mark.parametrize("operation", ["init", "generate", "broker", "down", "ui"])
def test_missing_configured_path_never_uses_root_decoy(
    project: Path, operation: str, compose_renderer, capsys,
) -> None:
    (project / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    select_compose(project, "deployment/missing.yaml")
    resolved = str(project / "deployment/missing.yaml")
    if operation in {"init", "generate"}:
        args = [operation, "--project-dir", str(project)]
        if operation == "generate":
            args += ["--config", ENDPOINT_CONFIG]
        assert main(args) == 2
        error = capsys.readouterr().err
        assert "[compose].file" in error and resolved in error
    elif operation == "ui":
        with pytest.raises(ValueError, match=r"\[compose\]\.file") as exc:
            compose_up_worktree(str(project))
        assert resolved in str(exc.value)
    else:
        with pytest.raises(ConfigError, match=r"\[compose\]\.file") as exc:
            if operation == "broker":
                ensure_generated_override(["up", "-d"], cwd=project, environ={})
            else:
                plan_docker_compose_command(["down"], cwd=project)
        assert resolved in str(exc.value)
    assert compose_renderer == []


def test_catalog_retains_config_error_across_discovery_and_history(project: Path, monkeypatch) -> None:
    select_compose(project, "deployment/missing.yaml")
    (project / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    rows = discover_catalog_worktrees([])
    assert len(rows) == 1
    assert rows[0]["startable"] is False
    assert "[compose].file" in rows[0]["start_error"]
    assert str(project / "deployment/missing.yaml") in rows[0]["start_error"]
    merge_catalog_history(rows, [], generated_at="2026-10-20T00:00:00+00:00")
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(project / "not-scanned"))
    restored = merge_catalog_history([], [], generated_at="2026-10-20T01:00:00+00:00")
    assert len(restored) == 1
    assert restored[0]["start_error"] == rows[0]["start_error"]


@pytest.mark.parametrize("setting", ['file = ""', 'file = 123', 'file = []'])
def test_invalid_configured_path_types_are_specific(project: Path, setting: str) -> None:
    (project / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    (project / ENDPOINT_CONFIG).write_text(ENDPOINTS + "\n[compose]\n" + setting + "\n")
    with pytest.raises(ConfigError, match=r"\[compose\]\.file must be a non-empty string"):
        find_compose_file(project)


def test_explicit_missing_file_error_includes_field_and_resolved_path(project: Path) -> None:
    (project / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    with pytest.raises(ConfigError, match="--compose-file") as exc:
        find_compose_file(project, Path("deployment/missing.yaml"))
    assert str(project / "deployment/missing.yaml") in str(exc.value)


def test_different_cwd_uses_explicit_project_root(project: Path, compose_renderer, monkeypatch, tmp_path) -> None:
    compose = nested_compose(project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.chdir(elsewhere)
    assert main([
        "init", "--project-dir", str(project), "--compose-file", "deployment/dev.compose.yaml",
    ]) == 0
    assert main(["generate", "--project-dir", str(project), "--config", ENDPOINT_CONFIG]) == 0
    assert compose_renderer == [(compose, project)]
    assert find_compose_file(project) == compose
    assert not (elsewhere / ".portmap").exists()


def test_ui_start_broker_uses_selected_base_file(project: Path, compose_renderer, monkeypatch) -> None:
    compose = nested_compose(project)
    select_compose(project, "deployment/dev.compose.yaml")
    real_run = subprocess.run
    calls = []

    def run(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="started\n", stderr="")

    monkeypatch.setattr("portmap.catalog.subprocess.run", run)
    result = compose_up_worktree(str(project))
    assert result["ok"] is True
    assert compose_renderer == [(compose, project)]
    command, kwargs = calls[-1]
    assert command[command.index("-f") + 1] == str(compose)
    assert "--project-directory" not in command
    assert kwargs["cwd"] == project


@pytest.mark.parametrize("existing", [
    '[compose]\nfile = "old.yaml" # selection\n',
    '["compose"]\nfile = \'old.yaml\'\n',
    'compose.file = "old.yaml"\n',
    'compose = { file = "old.yaml", note = "keep" }\n',
    "compose = { file = 'old.yaml', note = 'keep' }\n",
    'compose = { file = 123, note = "keep" }\n',
    'compose = { file = [], note = "keep" }\n',
    '[compose]\nnote = "keep"\n',
    'compose = { note = "keep" }\n',
    '[compose]\nfile = """old.yaml"""\n',
    'compose.note = "keep"\n',
    '[compose]\nfile = false\n',
    '[compose]\nfile = []\n',
])
def test_persistence_preserves_other_toml_content(existing: str) -> None:
    text = existing + '\n[endpoints.web]\nkind = "http"\nservice = "old.yaml"\ncontainer_port = 8080\n'
    updated = compose_config_text(text, "deployment/dev.compose.yaml")
    before = tomllib.loads(text)
    after = tomllib.loads(updated)
    assert after["compose"] == {**before["compose"], "file": "deployment/dev.compose.yaml"}
    assert after["endpoints"] == before["endpoints"]
    assert 'service = "old.yaml"' in updated


def test_render_keeps_compose_file_relative_semantics(project: Path, monkeypatch) -> None:
    compose = nested_compose(project)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"services": {}}', stderr="")

    monkeypatch.setattr("portmap.compose.compose_entrypoint", lambda: ("docker", "compose"))
    monkeypatch.setattr("portmap.compose.subprocess.run", run)
    render_compose_json(compose, project)
    command, kwargs = calls[0]
    assert command == ["docker", "compose", "-f", str(compose), "config", "--format", "json"]
    assert kwargs["cwd"] == project
    assert "--project-directory" not in command


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("PORTMAP_RUN_COMPOSE_PATH_INTEGRATION") != "1", reason="opt-in real Compose config proof")
def test_real_compose_nested_build_and_bind_paths(project: Path) -> None:
    compose = nested_compose(project)
    (compose.parent / "context").mkdir()
    (compose.parent / "context/Dockerfile").write_text("FROM scratch\n")
    (compose.parent / "assets").mkdir()
    compose.write_text(
        'services:\n  web:\n    image: busybox:latest\n    build: ./context\n'
        '    volumes: ["./assets:/assets:ro"]\n    expose: ["8080"]\n',
        encoding="utf-8",
    )
    select_compose(project, "deployment/dev.compose.yaml")
    base = render_compose_json(find_compose_file(project), project)
    # This integration proves Compose config path resolution, not controller
    # readiness. Supply the planner's explicit DNS input without inventing a
    # ready infrastructure snapshot or invoking host setup.
    generated = generate_plan(GenerateRequest(
        compose_file=find_compose_file(project),
        compose_json_file=None,
        project_directory=project,
        out_dir=project / ".portmap",
        config_file=project / ENDPOINT_CONFIG,
        container_dns_server="172.20.0.53",
    ))
    generated.write(project / ".portmap")
    plan = plan_docker_compose_command(["config", "--format", "json"], cwd=project)
    from portmap.container_runtime import compose_entrypoint
    from portmap.client_runtime import native_environment
    result = subprocess.run(
        [*compose_entrypoint(), *plan.command[2:]], cwd=project,
        env=native_environment(), capture_output=True, text=True, check=True,
    )
    merged = json.loads(result.stdout)
    for payload in (base, merged):
        web = payload["services"]["web"]
        assert Path(web["build"]["context"]) == compose.parent / "context"
        bind = next(volume for volume in web["volumes"] if volume["target"] == "/assets")
        assert Path(bind["source"]) == compose.parent / "assets"
        assert bind["read_only"] is True


def test_init_fresh_nested_project_and_force_keep_selection(project: Path) -> None:
    compose = nested_compose(project)
    (project / ENDPOINT_CONFIG).unlink()
    assert main([
        "init", "--project-dir", str(project), "--compose-file", "deployment/dev.compose.yaml",
    ]) == 0
    assert find_compose_file(project) == compose
    assert not tomllib.loads((project / ENDPOINT_CONFIG).read_text()).get("endpoints")
    assert main(["init", "--project-dir", str(project), "--force"]) == 0
    assert find_compose_file(project) == compose


@pytest.mark.parametrize("setting", ['file = false', 'file = []', 'file = 123'])
def test_cli_selection_overrides_invalid_lower_priority_value(
    project: Path, compose_renderer, setting: str,
) -> None:
    compose = nested_compose(project)
    (project / ENDPOINT_CONFIG).write_text(ENDPOINTS + "\n[compose]\n" + setting + "\n")
    assert main([
        "generate", "--project-dir", str(project), "--config", ENDPOINT_CONFIG,
        "--compose-file", "deployment/dev.compose.yaml",
    ]) == 0
    assert find_compose_file(project) == compose


def test_invalid_compose_table_reports_config_field(project: Path) -> None:
    (project / ENDPOINT_CONFIG).write_text('compose = false\n' + ENDPOINTS)
    with pytest.raises(ConfigError, match=r"\[compose\] must be a table") as exc:
        find_compose_file(project)
    assert str(project / ENDPOINT_CONFIG) in str(exc.value)


def test_configured_directory_is_not_a_compose_file(project: Path) -> None:
    nested_compose(project)
    select_compose(project, "deployment")
    (project / "docker-compose.yml").write_text(COMPOSE)
    with pytest.raises(ConfigError, match="not a regular file") as exc:
        find_compose_file(project)
    assert str(project / "deployment") in str(exc.value)


def test_custom_config_without_endpoints_does_not_create_compose_only_canonical(
    project: Path, monkeypatch,
) -> None:
    compose = nested_compose(project)
    custom = project / "custom.toml"
    custom.write_text('[compose]\nfile = "deployment/dev.compose.yaml"\n')
    (project / ENDPOINT_CONFIG).unlink()
    from portmap.project_compose import persist_compose_file
    with pytest.raises(ConfigError, match="endpoints"):
        persist_compose_file(project, compose, config_file=custom)
    assert not (project / ENDPOINT_CONFIG).exists()


@pytest.mark.parametrize("endpoints", ["", "[endpoints]\n"])
def test_init_persistence_accepts_empty_scaffold_but_generate_stays_strict(
    project: Path, endpoints: str,
) -> None:
    from portmap.project_compose import persist_compose_file

    compose = nested_compose(project)
    config = project / ENDPOINT_CONFIG
    original = "# Endpoint declarations require user review.\n" + endpoints
    config.write_text(original)
    with pytest.raises(ConfigError, match="endpoints"):
        persist_compose_file(project, compose, config_file=config)
    assert config.read_text() == original
    persist_compose_file(project, compose, config_file=config, allow_empty_endpoints=True)
    assert config.read_text().startswith(original)
    assert find_compose_file(project) == compose
    with pytest.raises(ConfigError, match="endpoints"):
        persist_compose_file(project, compose, config_file=config)
