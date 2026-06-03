from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


REPO_NAME = "mock-compose-app"


@dataclass(frozen=True)
class RepoLayout:
    root: Path
    container: Path
    main: Path
    dev: Path
    feat: Path
    gateway: Path
    allocation_state_file: Path


def build_test_repo(root: Path) -> RepoLayout:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    container = root / f"{REPO_NAME}@wt"
    main = container / f"{REPO_NAME}@main"
    dev = container / f"{REPO_NAME}@dev"
    feat = container / f"{REPO_NAME}@feat-all-endpoints"
    gateway = root / "gateway"

    container.mkdir()
    run(["git", "init", "-b", "main", str(main)], cwd=root)
    configure_git(main)
    write_app_files(main, include_raw=False)
    run(["git", "add", "."], cwd=main)
    run(["git", "commit", "-m", "init http-only mock app"], cwd=main)

    run(["git", "branch", "dev"], cwd=main)
    run(["git", "worktree", "add", str(dev), "dev"], cwd=main)

    run(["git", "branch", "feat/all-endpoints", "dev"], cwd=main)
    run(["git", "worktree", "add", str(feat), "feat/all-endpoints"], cwd=main)
    write_app_files(feat, include_raw=True)
    run(["git", "add", "."], cwd=feat)
    run(["git", "commit", "-m", "add tcp udp and range endpoints"], cwd=feat)

    write_gateway(gateway)
    return RepoLayout(
        root=root,
        container=container,
        main=main,
        dev=dev,
        feat=feat,
        gateway=gateway,
        allocation_state_file=root / "allocations.json",
    )


def configure_git(repo: Path) -> None:
    run(["git", "config", "user.email", "portmap-test@example.invalid"], cwd=repo)
    run(["git", "config", "user.name", "portmap test"], cwd=repo)


def write_app_files(repo: Path, *, include_raw: bool) -> None:
    (repo / ".portmap").mkdir(parents=True, exist_ok=True)
    (repo / "servers.py").write_text(SERVERS_PY, encoding="utf-8")
    (repo / "docker-compose.yml").write_text(compose_yaml(include_raw=include_raw), encoding="utf-8")
    (repo / ".portmap" / "endpoints.toml").write_text(endpoints_toml(include_raw=include_raw), encoding="utf-8")


def write_gateway(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "docker-compose.yml").write_text(GATEWAY_COMPOSE, encoding="utf-8")


def compose_yaml(*, include_raw: bool) -> str:
    services = [
        """
  frontend:
    image: python:3.12-alpine
    working_dir: /app
    command: ["python", "/app/servers.py", "http", "8000"]
    volumes:
      - "./servers.py:/app/servers.py:ro"
    expose:
      - "8000"
""".rstrip(),
    ]
    if include_raw:
        services.extend(
            [
                """
  mqtt:
    image: python:3.12-alpine
    working_dir: /app
    command: ["python", "/app/servers.py", "tcp", "1883"]
    volumes:
      - "./servers.py:/app/servers.py:ro"
    expose:
      - "1883"
""".rstrip(),
                """
  udp-echo:
    image: python:3.12-alpine
    working_dir: /app
    command: ["python", "/app/servers.py", "udp", "9999"]
    volumes:
      - "./servers.py:/app/servers.py:ro"
    expose:
      - "9999/udp"
""".rstrip(),
                """
  turn-range:
    image: python:3.12-alpine
    working_dir: /app
    command: ["python", "/app/servers.py", "range", "3478"]
    volumes:
      - "./servers.py:/app/servers.py:ro"
    expose:
      - "3478/udp"
""".rstrip(),
            ]
        )
    return "services:\n" + "\n".join(services) + "\n"


def endpoints_toml(*, include_raw: bool) -> str:
    parts = [
        """
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 8000
""".strip()
    ]
    if include_raw:
        parts.extend(
            [
                """
[endpoints.mqtt]
kind = "tcp"
service = "mqtt"
container_port = 1883
""".strip(),
                """
[endpoints.udp_echo]
kind = "udp"
service = "udp-echo"
container_port = 9999
""".strip(),
                """
[endpoints.turn]
kind = "range"
service = "turn-range"
container_port = 3478
protocol = "udp"
range_size = 3
""".strip(),
            ]
        )
    return "\n\n".join(parts) + "\n"


def run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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
        raise RuntimeError(
            f"command failed: {' '.join(args)}\n"
            f"cwd: {cwd}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


SERVERS_PY = r'''from __future__ import annotations

import http.server
import os
import socket
import sys
import threading


def http_server(port: int) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = (
                "portmap-http-ok\n"
                f"host={self.headers.get('Host', '')}\n"
                f"url={os.environ.get('PORTMAP_FRONTEND_URL', '')}\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


def tcp_server(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", port))
        server.listen()
        while True:
            connection, _ = server.accept()
            threading.Thread(target=handle_tcp, args=(connection,), daemon=True).start()


def handle_tcp(connection: socket.socket) -> None:
    with connection:
        payload = connection.recv(1024)
        connection.sendall(b"portmap-tcp-ok:" + payload)


def udp_server(port: int, prefix: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", port))
        while True:
            payload, address = server.recvfrom(2048)
            server.sendto(prefix + b":" + payload, address)


def range_server(entry_port: int) -> None:
    ports = [entry_port]
    range_min = int(os.environ["PORTMAP_TURN_RANGE_MIN_PORT"])
    range_max = int(os.environ["PORTMAP_TURN_RANGE_MAX_PORT"])
    ports.extend(range(range_min, range_max + 1))
    for port in ports:
        threading.Thread(target=udp_server, args=(port, f"portmap-range-ok-{port}".encode()), daemon=True).start()
    threading.Event().wait()


def main() -> None:
    mode = sys.argv[1]
    port = int(sys.argv[2])
    if mode == "http":
        http_server(port)
    elif mode == "tcp":
        tcp_server(port)
    elif mode == "udp":
        udp_server(port, b"portmap-udp-ok")
    elif mode == "range":
        range_server(port)
    else:
        raise SystemExit(f"unsupported mode: {mode}")


if __name__ == "__main__":
    main()
'''


GATEWAY_COMPOSE = """name: portmap-test-gateway

services:
  traefik:
    image: traefik:v3.6
    command:
      - "--providers.docker=true"
      - "--providers.docker.exposedbydefault=false"
      - "--entrypoints.web.address=:80"
    ports:
      - "127.0.0.1:${PORTMAP_TEST_HTTP_PORT:-18080}:80"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - portmap_test_gateway

networks:
  portmap_test_gateway:
    name: "${PORTMAP_GATEWAY_NETWORK:-portmap_test_gateway}"
"""
