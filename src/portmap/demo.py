"""Self-contained demo project for verifying a portmap installation.

`portmap demo up` creates a tiny git repo with a two-endpoint compose
project (HTTP + raw TCP on whoami) under the portmap state directory and
starts it through the same broker pipeline a real project uses:
ensure_generated_override + plan_docker_compose_command. `demo down`
stops it. The demo never touches the user's own projects.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .broker import ensure_generated_override
from .compose_takeover import plan_docker_compose_command
from .repo_identity import git
from .settings import PortmapSettings, load_portmap_settings

DEMO_ASSETS = Path(__file__).resolve().parent / "demo_assets"


@dataclass(frozen=True)
class DemoResult:
    ok: bool
    action: str
    project_dir: Path
    endpoints: dict[str, dict]
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "project_dir": str(self.project_dir),
            "endpoints": self.endpoints,
            "message": self.message,
        }


def demo_dir(settings: PortmapSettings) -> Path:
    return settings.state_dir / "demo"


def demo_up(settings: PortmapSettings) -> DemoResult:
    project_dir = ensure_demo_project(settings)
    ensure_generated_override(["up", "-d"], cwd=project_dir, environ=os.environ)
    plan = plan_docker_compose_command(["up", "-d"], cwd=project_dir)
    if not plan.injected:
        return DemoResult(
            ok=False,
            action="up",
            project_dir=project_dir,
            endpoints={},
            message="broker did not inject the generated override",
        )
    env = os.environ.copy()
    env["PORTMAP_BROKER_BYPASS"] = "1"
    env["DOCKER_HOST"] = settings.docker_host
    result = subprocess.run(plan.command, cwd=project_dir, env=env, check=False)
    if result.returncode != 0:
        return DemoResult(
            ok=False,
            action="up",
            project_dir=project_dir,
            endpoints={},
            message=f"compose up failed with exit code {result.returncode}",
        )
    return DemoResult(
        ok=True,
        action="up",
        project_dir=project_dir,
        endpoints=demo_endpoints(project_dir),
    )


def demo_down(settings: PortmapSettings, *, purge: bool = False) -> DemoResult:
    project_dir = demo_dir(settings)
    if not project_dir.exists():
        return DemoResult(ok=True, action="down", project_dir=project_dir, endpoints={}, message="no demo project")
    plan = plan_docker_compose_command(["down", "-v"], cwd=project_dir)
    env = os.environ.copy()
    env["PORTMAP_BROKER_BYPASS"] = "1"
    env["DOCKER_HOST"] = settings.docker_host
    result = subprocess.run(plan.command, cwd=project_dir, env=env, check=False)
    if purge:
        shutil.rmtree(project_dir, ignore_errors=True)
    return DemoResult(
        ok=result.returncode == 0,
        action="down",
        project_dir=project_dir,
        endpoints={},
        message="" if result.returncode == 0 else f"compose down failed with exit code {result.returncode}",
    )


def ensure_demo_project(settings: PortmapSettings) -> Path:
    project_dir = demo_dir(settings)
    project_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEMO_ASSETS / "docker-compose.yml", project_dir / "docker-compose.yml")
    portmap_dir = project_dir / ".portmap"
    portmap_dir.mkdir(exist_ok=True)
    shutil.copyfile(DEMO_ASSETS / "endpoints.toml", portmap_dir / "endpoints.toml")
    if not (project_dir / ".git").exists():
        git(project_dir, "init", "-q", "-b", "demo")
        git(project_dir, "add", "-A")
        git(
            project_dir,
            "-c",
            "user.email=demo@portmap.local",
            "-c",
            "user.name=portmap-demo",
            "commit",
            "-qm",
            "demo project",
        )
    return project_dir


def demo_endpoints(project_dir: Path) -> dict[str, dict]:
    state_file = project_dir / ".portmap" / "state.json"
    if not state_file.exists():
        return {}
    state = json.loads(state_file.read_text(encoding="utf-8"))
    endpoints = state.get("endpoints")
    return endpoints if isinstance(endpoints, dict) else {}


def load_demo_settings() -> PortmapSettings:
    return load_portmap_settings(environ=os.environ)
