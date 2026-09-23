import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from portmap.demo import demo_down, demo_dir, demo_up
from portmap.settings import load_portmap_settings

REAL_RUN = subprocess.run


def compose_fake(calls: list | None = None, returncode: int = 0):
    """Fake compose subprocess; git calls delegate to the real subprocess."""

    def fake_run(command, **kwargs):
        if command and command[0] == "git":
            return REAL_RUN(command, **kwargs)
        if calls is not None:
            calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode)

    return fake_run


class FakePlan:
    compose_project = "demo_demo"

    def write(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "docker-compose.override.generated.yml").write_text("services: {}\n", encoding="utf-8")
        (out_dir / "state.json").write_text(
            json.dumps(
                {
                    "compose_project": "demo_demo",
                    "endpoints": {
                        "web": {"kind": "http", "url": "http://web.demo.demo.debug.lan:8080"},
                        "raw": {"kind": "tcp", "host": "192.0.2.1", "host_port": 18000},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )


def demo_settings(tmp_path: Path, monkeypatch) -> object:
    monkeypatch.setattr("portmap.settings.detect_host_ip", lambda: "192.0.2.1")
    return load_portmap_settings(
        environ={
            "PORTMAP_ROOT": str(tmp_path),
            "PORTMAP_STATE_DIR": str(tmp_path / "state"),
            "XDG_RUNTIME_DIR": str(tmp_path / "xdg"),
        }
    )


def test_demo_up_creates_project_and_runs_injected_compose(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("portmap.broker.generate_plan", lambda request: FakePlan())
    calls = []
    monkeypatch.setattr("portmap.demo.subprocess.run", compose_fake(calls))
    settings = demo_settings(tmp_path, monkeypatch)

    result = demo_up(settings)

    project_dir = demo_dir(settings)
    assert result.ok is True
    assert (project_dir / "docker-compose.yml").exists()
    assert (project_dir / ".portmap" / "endpoints.toml").exists()
    assert (project_dir / ".git").exists()
    # The real broker pipeline: override file injected, branch-scoped name.
    command, kwargs = calls[0]
    assert command[:2] == ["docker", "compose"]
    assert "-p" in command and "demo_demo" in command
    assert str(project_dir / ".portmap" / "docker-compose.override.generated.yml") in command
    assert command[-2:] == ["up", "-d"]
    assert kwargs["cwd"] == project_dir
    assert kwargs["env"]["DOCKER_HOST"].startswith("unix://")
    assert kwargs["env"]["PORTMAP_BROKER_BYPASS"] == "1"
    assert result.endpoints["web"]["url"] == "http://web.demo.demo.debug.lan:8080"
    assert result.endpoints["raw"]["host_port"] == 18000


def test_demo_up_idempotent_on_existing_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("portmap.broker.generate_plan", lambda request: FakePlan())
    monkeypatch.setattr("portmap.demo.subprocess.run", compose_fake())
    settings = demo_settings(tmp_path, monkeypatch)

    first = demo_up(settings)
    second = demo_up(settings)

    assert first.ok and second.ok
    # Same compose project name across runs: the demo repo is reused.
    assert len(list(demo_dir(settings).iterdir())) > 0


def test_demo_down_without_project_is_noop(tmp_path: Path, monkeypatch) -> None:
    settings = demo_settings(tmp_path, monkeypatch)
    result = demo_down(settings)
    assert result.ok is True
    assert result.message == "no demo project"


def test_demo_down_runs_compose_down_and_purges(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("portmap.broker.generate_plan", lambda request: FakePlan())
    calls = []
    monkeypatch.setattr("portmap.demo.subprocess.run", compose_fake(calls))
    settings = demo_settings(tmp_path, monkeypatch)
    demo_up(settings)

    result = demo_down(settings, purge=True)

    assert result.ok is True
    assert calls[-1][0][-2:] == ["down", "-v"]
    assert not demo_dir(settings).exists()


def test_demo_up_reports_compose_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("portmap.broker.generate_plan", lambda request: FakePlan())
    monkeypatch.setattr("portmap.demo.subprocess.run", compose_fake(returncode=17))
    settings = demo_settings(tmp_path, monkeypatch)

    result = demo_up(settings)

    assert result.ok is False
    assert "17" in result.message


def test_demo_assets_parse_with_real_loaders() -> None:
    from portmap.config import load_endpoint_declarations
    from portmap.demo import DEMO_ASSETS

    endpoints = load_endpoint_declarations(DEMO_ASSETS / "endpoints.toml")
    assert {e.name for e in endpoints} == {"web", "raw"}
    assert (DEMO_ASSETS / "docker-compose.yml").read_text(encoding="utf-8").startswith("services:")
