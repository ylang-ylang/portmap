from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import pytest

from tests.fixtures.test_repo_manager import RepoLayout, build_test_repo


pytestmark = pytest.mark.integration


def test_generated_worktree_repo_serves_http_tcp_udp_and_range() -> None:
    if os.environ.get("PORTMAP_RUN_INTEGRATION") != "1":
        pytest.skip("set PORTMAP_RUN_INTEGRATION=1 to run Docker integration test")
    if not docker_available():
        pytest.skip("Docker daemon is not available")

    repo_root = Path(__file__).resolve().parents[1]
    cleanup_mock_compose_resources()
    layout = build_test_repo(repo_root / "test_repo")
    http_port = free_tcp_port()
    tcp_start = free_tcp_port()
    udp_start = 39000
    range_start = 50000
    env = integration_env(
        repo_root=repo_root,
        layout=layout,
        http_port=http_port,
        tcp_start=tcp_start,
        udp_start=udp_start,
        range_start=range_start,
    )

    try:
        install_compose_plugin_shim(layout=layout, repo_root=repo_root, env=env)
        compose(layout.gateway, env, "up", "-d", "--quiet-pull")
        docker_compose(layout.dev, env, "up", "-d", "--quiet-pull")
        docker_compose(layout.feat, env, "up", "-d", "--quiet-pull")

        dev_state = read_state(layout.dev)
        feat_state = read_state(layout.feat)
        assert dev_state["compose_project"] != feat_state["compose_project"]
        assert dev_state["branch"] == "dev"
        assert feat_state["branch"] == "feat-all-endpoints"

        wait_for(lambda: http_get(http_port, dev_state["endpoints"]["frontend"]["host"]))
        wait_for(lambda: http_get(http_port, feat_state["endpoints"]["frontend"]["host"]))

        mqtt = feat_state["endpoints"]["mqtt"]
        udp_echo = feat_state["endpoints"]["udp_echo"]
        turn = feat_state["endpoints"]["turn"]

        assert wait_for(lambda: tcp_roundtrip("127.0.0.1", mqtt["host_port"], b"hello")) == b"portmap-tcp-ok:hello"
        assert wait_for(lambda: udp_roundtrip("127.0.0.1", udp_echo["host_port"], b"hello")) == b"portmap-udp-ok:hello"
        assert wait_for(lambda: udp_roundtrip("127.0.0.1", turn["host_port"], b"entry")).startswith(
            b"portmap-range-ok-3478:entry"
        )

        relay_port = turn["range"]["min_port"]
        assert wait_for(lambda: udp_roundtrip("127.0.0.1", relay_port, b"relay")).startswith(
            f"portmap-range-ok-{relay_port}:relay".encode()
        )
    finally:
        run_no_fail_docker_compose(layout.feat, env, "down", "-v", "--remove-orphans")
        run_no_fail_docker_compose(layout.dev, env, "down", "-v", "--remove-orphans")
        run_no_fail(["docker", "compose", "down", "-v"], cwd=layout.gateway, env=env)
        cleanup_mock_compose_resources()


def integration_env(
    *,
    repo_root: Path,
    layout: RepoLayout,
    http_port: int,
    tcp_start: int,
    udp_start: int,
    range_start: int,
) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    env["PORTMAP_GATEWAY_NETWORK"] = "portmap_test_gateway"
    env["PORTMAP_HTTP_PORT"] = str(http_port)
    env["PORTMAP_TEST_HTTP_PORT"] = str(http_port)
    env["PORTMAP_DNS_TARGET_IP"] = "127.0.0.1"
    env["PORTMAP_DNS_DOMAIN"] = "integration.test"
    env["PORTMAP_TCP_PORT_START"] = str(tcp_start)
    env["PORTMAP_UDP_PORT_START"] = str(udp_start)
    env["PORTMAP_RANGE_PORT_START"] = str(range_start)
    env["PORTMAP_ALLOCATION_STATE_FILE"] = str(layout.allocation_state_file)
    env["PORTMAP_COMPOSE_TAKEOVER"] = "1"
    env["DOCKER_CONFIG"] = str(layout.root / "docker-config")
    return env


def docker_available() -> bool:
    return subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def compose(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return run_checked(["docker", "compose", *args], cwd=cwd, env=env)


def docker_compose(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return run_checked(["docker", "compose", *args], cwd=cwd, env=env)


def install_compose_plugin_shim(*, layout: RepoLayout, repo_root: Path, env: dict[str, str]) -> None:
    docker_config = Path(env["DOCKER_CONFIG"])
    docker_config.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            sys.executable,
            "-m",
            "portmap.cli",
            "broker",
            "install",
            "--docker-config",
            str(docker_config),
            "--portmap-root",
            str(repo_root),
            "--force",
        ],
        cwd=layout.root,
        env=env,
    )


def run_checked(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(args)}\n"
            f"cwd: {cwd}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def run_no_fail(args: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def run_no_fail_docker_compose(cwd: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def cleanup_mock_compose_resources() -> None:
    projects = docker_compose_projects_by_label("portmap.repo_name=mock-compose-app")
    for project in projects:
        env = os.environ.copy()
        env["PORTMAP_BROKER_BYPASS"] = "1"
        subprocess.run(
            ["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def docker_compose_projects_by_label(label: str) -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label={label}", "--format", "{{.Label \"com.docker.compose.project\"}}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return []
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def read_state(worktree: Path) -> dict:
    return json.loads((worktree / ".portmap" / "state.json").read_text(encoding="utf-8"))


def wait_for(action: Callable[[], object], *, timeout: float = 60.0) -> object:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise AssertionError(f"condition did not become ready: {last_error}")


def http_get(port: int, host: str) -> str:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("GET", "/", headers={"Host": host})
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    connection.close()
    assert response.status == 200
    assert "portmap-http-ok" in body
    return body


def tcp_roundtrip(host: str, port: int, payload: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(payload)
        return sock.recv(1024)


def udp_roundtrip(host: str, port: int, payload: bytes) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        sock.sendto(payload, (host, port))
        response, _ = sock.recvfrom(2048)
        return response


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
