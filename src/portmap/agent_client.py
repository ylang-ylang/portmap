from __future__ import annotations

import http.client
import json
import os
import socket
from pathlib import Path
from typing import Any


DEFAULT_CONTAINER_AGENT_SOCKET = "/run/portmap/agent.sock"


class AgentUnavailable(RuntimeError):
    pass


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, *, timeout: float = 2.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def configured_agent_socket(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("PORTMAP_AGENT_SOCKET") or DEFAULT_CONTAINER_AGENT_SOCKET).expanduser()


def request_json(
    method: str,
    path: str,
    *,
    socket_path: Path | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    resolved_socket = socket_path or configured_agent_socket()
    if not resolved_socket.exists() or resolved_socket.is_dir():
        raise AgentUnavailable(f"agent socket unavailable: {resolved_socket}")

    body = b""
    headers = {"content-type": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    try:
        connection = UnixHTTPConnection(str(resolved_socket), timeout=timeout)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        connection.close()
    except OSError as exc:
        raise AgentUnavailable(f"agent unavailable: {exc}") from exc

    if response.status >= 400:
        detail = response_body.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"agent returned HTTP {response.status}")
    try:
        result = json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("agent returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("agent returned non-object JSON")
    return result


def agent_health(*, socket_path: Path | None = None, timeout: float = 1.0) -> dict[str, Any]:
    return request_json("GET", "/healthz", socket_path=socket_path, timeout=timeout)


def agent_worktrees(*, socket_path: Path | None = None) -> dict[str, Any]:
    return request_json("GET", "/worktrees", socket_path=socket_path)


def agent_compose_up_worktree(worktree: str, *, socket_path: Path | None = None) -> dict[str, Any]:
    return request_json("POST", "/compose-up", socket_path=socket_path, payload={"worktree": worktree}, timeout=30.0)
