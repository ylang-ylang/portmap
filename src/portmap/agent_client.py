from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.parse
from pathlib import Path
from typing import Any


DEFAULT_CONTAINER_AGENT_SOCKET = "/run/portmap/agent.sock"
CONNECTION_LIST_TIMEOUT = 15.0
CONNECTION_CHECK_TIMEOUT = 120.0
CONNECTION_MUTATE_TIMEOUT = 120.0


class AgentUnavailable(RuntimeError):
    pass


class AgentRequestError(RuntimeError):
    """The agent answered a control request with a non-2xx JSON error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


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
    return request_json("GET", "/worktrees", socket_path=socket_path, timeout=4.0)


def agent_compose_up_worktree(worktree: str, *, socket_path: Path | None = None) -> dict[str, Any]:
    return request_json("POST", "/compose-up", socket_path=socket_path, payload={"worktree": worktree}, timeout=30.0)


def request_json_status(
    method: str,
    path: str,
    *,
    socket_path: Path | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict[str, Any]]:
    """``request_json`` for status-aware control endpoints.

    Transport failures raise ``AgentUnavailable`` exactly like
    ``request_json``; a non-2xx answer returns ``(status, body)`` so callers
    can forward the agent's error status instead of collapsing it.
    """
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

    parsed: dict[str, Any] = {}
    if response_body:
        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, dict):
            parsed = decoded
        elif response.status < 400:
            raise RuntimeError("agent returned invalid JSON" if decoded is None else "agent returned non-object JSON")
    return response.status, parsed


def agent_connection_targets(*, check: bool = False, socket_path: Path | None = None) -> dict[str, Any]:
    """The host agent's durable connection list; ``check`` runs HTTP checks."""
    path = "/access/connections?check=1" if check else "/access/connections"
    status, body = request_json_status(
        "GET",
        path,
        socket_path=socket_path,
        timeout=CONNECTION_CHECK_TIMEOUT if check else CONNECTION_LIST_TIMEOUT,
    )
    if status >= 400:
        raise AgentRequestError(status, str(body.get("error") or f"agent returned HTTP {status}"))
    return body


def agent_mutate_connection(payload: dict[str, Any], *, socket_path: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Forward one connection control action; returns the agent's status and body."""
    status, body = request_json_status(
        "POST",
        "/access/connections",
        socket_path=socket_path,
        payload=payload,
        timeout=CONNECTION_MUTATE_TIMEOUT,
    )
    if status >= 400 and "error" not in body:
        return status, {"error": f"agent returned HTTP {status}"}
    return status, body


UPDATE_STATUS_LIST_TIMEOUT = 60.0
# The manager checks itself, then bounded concurrent waves of remote machines.
UPDATE_STATUS_CHECK_TIMEOUT = 600.0
UPDATE_START_TIMEOUT = 30.0
UPDATE_JOB_TIMEOUT = 30.0
UPDATE_TOKEN_TIMEOUT = 60.0
UPDATE_CREDENTIALS_TIMEOUT = 30.0
UPDATE_VERSION_TIMEOUT = 10.0
# Target dispatch may resolve "latest" through two sequential 30s HTTP calls.
UPDATE_AGENT_DISPATCH_TIMEOUT = 90.0


def agent_update_version(*, socket_path: Path | None = None) -> dict[str, Any]:
    """Minimal local version/build identity from the host agent."""
    status, body = request_json_status("GET", "/version", socket_path=socket_path, timeout=UPDATE_VERSION_TIMEOUT)
    if status >= 400:
        raise AgentRequestError(status, str(body.get("error") or f"agent returned HTTP {status}"))
    return body


def agent_update_status(*, check: bool = False, socket_path: Path | None = None) -> dict[str, Any]:
    """The update machines list and recent batches from the host agent."""
    path = "/updates/status?check=1" if check else "/updates/status"
    status, body = request_json_status(
        "GET",
        path,
        socket_path=socket_path,
        timeout=UPDATE_STATUS_CHECK_TIMEOUT if check else UPDATE_STATUS_LIST_TIMEOUT,
    )
    if status >= 400:
        raise AgentRequestError(status, str(body.get("error") or f"agent returned HTTP {status}"))
    return body


def agent_update_start(payload: dict[str, Any], *, socket_path: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Start one durable update batch; returns the agent's status and body."""
    return request_json_status(
        "POST",
        "/updates/start",
        socket_path=socket_path,
        payload=payload,
        timeout=UPDATE_START_TIMEOUT,
    )


def agent_update_job(batch_id: str, *, socket_path: Path | None = None) -> tuple[int, dict[str, Any]]:
    """One durable update batch; returns the agent's status and body."""
    return request_json_status(
        "GET",
        f"/updates/jobs/{urllib.parse.quote(batch_id, safe='')}",
        socket_path=socket_path,
        timeout=UPDATE_JOB_TIMEOUT,
    )


def agent_update_token(payload: dict[str, Any], *, socket_path: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Reveal (and optionally tunnel-deliver) the local capability."""
    return request_json_status(
        "POST",
        "/updates/token",
        socket_path=socket_path,
        payload=payload,
        timeout=UPDATE_TOKEN_TIMEOUT,
    )


def agent_update_credentials(payload: dict[str, Any], *, socket_path: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Verify and store one remote machine capability."""
    return request_json_status(
        "POST",
        "/updates/credentials",
        socket_path=socket_path,
        payload=payload,
        timeout=UPDATE_CREDENTIALS_TIMEOUT,
    )


def agent_update_agent_dispatch(payload: dict[str, Any], *, socket_path: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Forward one remote ``/api/update-agent`` request for verification."""
    return request_json_status(
        "POST",
        "/update-agent",
        socket_path=socket_path,
        payload=payload,
        timeout=UPDATE_AGENT_DISPATCH_TIMEOUT,
    )
