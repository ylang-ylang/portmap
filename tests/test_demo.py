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
    monkeypatch.setattr("portmap.broker.container_dns_address", lambda state_dir: "172.20.0.53")
    return load_portmap_settings(
        environ={
            "PORTMAP_ROOT": str(tmp_path),
            "PORTMAP_STATE_DIR": str(tmp_path / "state"),
            "XDG_RUNTIME_DIR": str(tmp_path / "xdg"),
        }
    )






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
