"""Host-side control plane for web-managed Portmap updates.

Everything here runs inside the user-owned host agent (or a batch worker it
spawned). The unprivileged catalog forwards browser control and authenticated
remote ``/api/update-agent`` traffic over the existing Unix socket; capability
secrets never leave host-private state, and no package manager is ever
executed here - the target machine's own updater (``portmap.updates``)
performs the fixed install operations. Remote requests and responses are
HMAC-authenticated with a paired per-machine capability, bound to a timestamp
and a single-use nonce, so a direct-network intermediary cannot forge update
results and plaintext capabilities never travel over direct-network HTTP.

The module stays importable and side-effect free: the engine's durable batch
worker re-executes ``run_batch`` from a code snapshot of this package.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import stat
import tempfile
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .errors import PortmapError
from .permissions import ensure_private_directory, ensure_private_file, tighten_private_descriptor

LOCAL_MACHINE_ID = "local"

# Control-owned files under <state_dir>/updates (engine owns jobs/, logs/,
# check.json, archives/, snapshots/, worker.lock, start.lock).
CAPABILITY_FILE_NAME = "capability.json"
CREDENTIALS_FILE_NAME = "credentials.json"
BATCHES_FILE_NAME = "batches.json"
NONCES_FILE_NAME = "nonces.json"
CONTROL_LOCK_NAME = "updates.lock"

CAPABILITY_PREFIX = "pm1."
CAPABILITY_BODY_RE = re.compile(r"[0-9a-f]{64}\Z")

AUTH_SCHEME = "portmap-update-v1"
RESPONSE_AUTH_SCHEME = "portmap-update-response-v1"
SIGNATURE_HEADER = "X-Portmap-Signature"
TIMESTAMP_HEADER = "X-Portmap-Timestamp"
NONCE_HEADER = "X-Portmap-Nonce"
RESPONSE_SIGNATURE_HEADER = "X-Portmap-Response-Signature"

SIGNATURE_RE = re.compile(r"[0-9a-f]{64}\Z")
TIMESTAMP_RE = re.compile(r"[0-9]{8,12}\Z")
NONCE_RE = re.compile(r"[0-9a-f]{16,64}\Z")
CLOCK_SKEW_SECONDS = 300
NONCE_RETENTION_SECONDS = 2 * CLOCK_SKEW_SECONDS
MAX_NONCE_ENTRIES = 20_000

REMOTE_MAX_BYTES = 262_144
REMOTE_STATUS_TIMEOUT = 8.0
# A cold check resolves the release tag and fetches its manifest (30s each).
# Keep cached display reads short, but let explicit checks and starts finish.
REMOTE_CHECK_TIMEOUT = 100.0
REMOTE_PROBE_TIMEOUT = 5.0
REMOTE_START_TIMEOUT = 100.0
REMOTE_JOB_TIMEOUT = 15.0
REMOTE_ENROLL_TIMEOUT = 20.0

MAX_BATCH_MACHINES = 32
MAX_BATCHES_KEPT = 50
RECENT_BATCHES_LISTED = 20
TARGET_VERSION_RE = re.compile(r"(?:latest|\d+\.\d+\.\d+)\Z")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
BATCH_ID_RE = re.compile(r"b[0-9a-z]{12}\Z")
UPDATE_AGENT_JOB_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
REMOTE_JOB_STATES_RUNNING = ("queued", "preflight", "installing", "restarting", "verifying")
JOB_STATES_TERMINAL = ("succeeded", "failed", "interrupted")
BATCH_STATES_TERMINAL = ("succeeded", "partial", "failed")
LEGACY_UNSUPPORTED_REASON = (
    "remote Portmap has no update agent; upgrade it on the machine itself before managing it here"
)

TARGET_POLL_INTERVAL = 3.0
TARGET_TIMEOUT_SECONDS = 1800.0
TARGET_POLL_FAILURE_LIMIT = 5
RESUME_RETRY_SECONDS = 10.0
BATCH_LOG_TAIL_CHARS = 2000

UPDATE_AGENT_PREFIX = "/api/update-agent/"
_UPDATE_AGENT_FIXED_PATHS = {
    ("GET", "/api/update-agent/version"),
    ("GET", "/api/update-agent/status"),
    ("POST", "/api/update-agent/start"),
    ("POST", "/api/update-agent/pair"),
}
UPDATE_AGENT_JOBS_PREFIX = "/api/update-agent/jobs/"
LEGACY_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[.+~-][0-9A-Za-z.-]+)?\Z")


class BatchConflictError(PortmapError):
    """A batch is already active; carries that batch for HTTP 409 bodies."""

    def __init__(self, batch: dict[str, Any]) -> None:
        super().__init__(f"an update batch is already active ({batch.get('id')})")
        self.batch = batch



class RequestAuthError(Exception):
    """A remote update-agent request failed authentication."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RemoteUpdateError(RuntimeError):
    """A remote machine's update agent could not be used."""

    def __init__(self, machine: str, message: str) -> None:
        super().__init__(f"machine {machine}: {message}")
        self.machine = machine
        self.message = message


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _engine_module() -> Any:
    """The target updater implementation, imported lazily per call."""
    from . import updates

    return updates


def _updates_dir(state_dir: Path) -> Path:
    path = state_dir / "updates"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    ensure_private_directory(path, message=f"unsafe update state directory: {path}")
    return path


@contextmanager
def _private_file(path: Path, *, create: bool = False):
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    flags |= os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "r+" if create else "r", encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
            raise PortmapError(f"unsafe update control state: {path.name}")
        tighten_private_descriptor(path, handle.fileno(), message=f"unsafe update control state: {path.name}")
        yield handle


@contextmanager
def _control_lock(state_dir: Path):
    directory = _updates_dir(state_dir)
    with _private_file(directory / CONTROL_LOCK_NAME, create=True) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_json(path: Path) -> Any:
    try:
        with _private_file(path) as handle:
            value = json.load(handle)
            if not isinstance(value, dict):
                raise PortmapError(f"invalid update control state: {path.name}")
            return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PortmapError(f"invalid update control state: {path.name}") from exc


def _write_json(path: Path, payload: Any) -> None:
    ensure_private_file(path, message=f"unsafe update control state: {path.name}")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(name, 0o600)
        Path(name).replace(path)
        ensure_private_file(path, message=f"unsafe update control state: {path.name}")
    finally:
        Path(name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Capability pairing


def _valid_capability(token: Any) -> bool:
    return (
        isinstance(token, str)
        and token.startswith(CAPABILITY_PREFIX)
        and CAPABILITY_BODY_RE.fullmatch(token[len(CAPABILITY_PREFIX):]) is not None
    )


def _new_capability() -> str:
    return CAPABILITY_PREFIX + secrets.token_hex(32)


def local_capability(*, state_dir: Path) -> str:
    """This machine's management capability; created once, never rotated."""
    path = _updates_dir(state_dir) / CAPABILITY_FILE_NAME
    with _control_lock(state_dir):
        document = _read_json(path)
        if document is not None:
            token = document.get("token") if isinstance(document, dict) and document.get("version") == 1 else None
            if not _valid_capability(token):
                raise PortmapError("invalid local update capability; refusing to replace it")
            return str(token)
        token = _new_capability()
        _write_json(path, {"version": 1, "token": token, "created_at": _now()})
        return token


def _capability_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _credentials_path(state_dir: Path) -> Path:
    return _updates_dir(state_dir) / CREDENTIALS_FILE_NAME


def _load_credentials(state_dir: Path) -> dict[str, dict[str, Any]]:
    document = _read_json(_credentials_path(state_dir))
    if not isinstance(document, dict):
        return {}
    machines = document.get("machines")
    if not isinstance(machines, dict):
        return {}
    return {
        machine_id: entry
        for machine_id, entry in machines.items()
        if isinstance(machine_id, str) and isinstance(entry, dict) and _valid_capability(entry.get("token"))
    }


def _save_credentials(state_dir: Path, credentials: dict[str, dict[str, Any]]) -> None:
    _write_json(_credentials_path(state_dir), {"version": 1, "machines": credentials})


def credential_for(machine_id: str, *, state_dir: Path) -> str | None:
    """The stored capability that authorizes managing ``machine_id``."""
    with _control_lock(state_dir):
        entry = _load_credentials(state_dir).get(machine_id)
    return str(entry["token"]) if entry and entry.get("verified_at") else None


def _store_credential(machine_id: str, token: str, *, state_dir: Path) -> str:
    if not _valid_capability(token):
        raise PortmapError("capability must be a portmap update token")
    with _control_lock(state_dir):
        credentials = _load_credentials(state_dir)
        entry = {
            "token": token,
            "stored_at": _now(),
            "fingerprint": _capability_fingerprint(token),
            "verified_at": _now(),
        }
        credentials[machine_id] = entry
        _save_credentials(state_dir, credentials)
    return entry["fingerprint"]


def store_remote_credential(payload: Any, *, state_dir: Path) -> dict[str, Any]:
    """Verify and store the capability that authorizes managing one machine.

    The capability is proven against the target with a signed request before
    it is accepted; it is never reflected back.
    """
    if not isinstance(payload, dict) or set(payload) != {"machine", "token"}:
        raise PortmapError("credentials require exactly machine and token")
    machine_id = payload["machine"]
    token = payload["token"]
    if not isinstance(machine_id, str) or not machine_id:
        raise PortmapError("machine must be a configured remote id")
    if not _valid_capability(token):
        raise PortmapError("token must be a portmap update capability")
    snapshot = _host_snapshot_or_local_only(state_dir)
    machine = snapshot.machines.get(machine_id) if snapshot is not None else None
    if machine is None:
        raise PortmapError(f"unknown machine {machine_id}")
    if not machine.backend_url:
        raise PortmapError(f"machine {machine_id} is not connected")
    status, body, _raw, _server = _request_machine(machine, "/api/update-agent/version", state_dir=state_dir, capability=token,
    machine=machine.id,
    timeout=REMOTE_PROBE_TIMEOUT,)
    if status == 404:
        raise PortmapError(f"machine {machine_id} has no update agent; upgrade it on the machine first")
    if status in (401, 403):
        raise PortmapError(f"machine {machine_id} rejected this capability")
    if status != 200 or not isinstance(body, dict) or body.get("update_api") is not True:
        raise PortmapError(f"machine {machine_id} could not verify this capability")
    fingerprint = _store_credential(machine.id, token, state_dir=state_dir)
    return {"ok": True, "machine": machine.id, "fingerprint": fingerprint}


def reveal_local_capability(payload: Any, *, state_dir: Path) -> dict[str, Any]:
    """Reveal only this host's capability; never grant reciprocal access."""
    if not isinstance(payload, dict) or payload:
        raise PortmapError("token reveal requires an empty JSON object")
    token = local_capability(state_dir=state_dir)
    return {"token": token, "fingerprint": _capability_fingerprint(token)}


# ---------------------------------------------------------------------------
# Request/response authentication


def request_mac(capability: str, method: str, path: str, body: bytes, timestamp: str, nonce: str) -> str:
    canonical = "\n".join(
        (AUTH_SCHEME, method.upper(), path, hashlib.sha256(body).hexdigest(), timestamp, nonce)
    )
    return hmac.new(capability.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def response_mac(capability: str, nonce: str, status: int, body: bytes) -> str:
    canonical = "\n".join(
        (RESPONSE_AUTH_SCHEME, nonce, str(status), hashlib.sha256(body).hexdigest())
    )
    return hmac.new(capability.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _remember_nonce(nonce: str, *, state_dir: Path) -> bool:
    """Record one single-use nonce; False when it was already seen."""
    path = _updates_dir(state_dir) / NONCES_FILE_NAME
    now = time.time()
    with _control_lock(state_dir):
        document = _read_json(path)
        entries = document.get("entries") if document is not None else {}
        if not isinstance(entries, dict):
            raise PortmapError("invalid update replay state")
        entries = {
            key: value
            for key, value in entries.items()
            if isinstance(key, str) and isinstance(value, (int, float)) and value > now
        }
        if nonce in entries:
            return False
        if len(entries) >= MAX_NONCE_ENTRIES:
            raise RequestAuthError(429, "update replay window is full; retry after existing requests expire")
        entries[nonce] = now + NONCE_RETENTION_SECONDS
        _write_json(path, {"version": 1, "entries": entries})
    return True


def verify_signed_request(
    *,
    method: str,
    path: str,
    body: bytes,
    timestamp: Any,
    nonce: Any,
    signature: Any,
    capability: str,
    state_dir: Path,
) -> None:
    """Authenticate one remote update request; raises ``RequestAuthError``.

    The MAC comparison runs first and in constant time; freshness and replay
    state are only consulted for a well-signed request.
    """
    if not isinstance(signature, str) or not isinstance(timestamp, str) or not isinstance(nonce, str):
        raise RequestAuthError(401, "update request authentication headers are missing")
    if not SIGNATURE_RE.fullmatch(signature) or not TIMESTAMP_RE.fullmatch(timestamp) or not NONCE_RE.fullmatch(nonce):
        raise RequestAuthError(401, "update request authentication is malformed")
    expected = request_mac(capability, method, path, body, timestamp, nonce)
    if not hmac.compare_digest(signature, expected):
        raise RequestAuthError(401, "update request signature is invalid")
    if abs(time.time() - int(timestamp)) > CLOCK_SKEW_SECONDS:
        raise RequestAuthError(401, "update request timestamp is stale")
    if not _remember_nonce(nonce, state_dir=state_dir):
        raise RequestAuthError(403, "update request nonce was already used")


def _request_machine(machine_info: Any, path: str, *, state_dir: Path, **kwargs: Any):
    """Use host-owned SSH sockets, not container-only relay addresses."""
    socket_path = None
    if machine_info.via == "ssh":
        from .client import _load
        from .client_tunnel import _http_identity, _http_record, http_socket_dir, tunnel_alive

        try:
            profile = _load(state_dir).get(machine_info.id)
            if not profile or profile.get("via") != "ssh" or profile.get("target") != machine_info.target:
                raise PortmapError("the saved SSH connection changed")
            tunnel = profile.get("tunnel")
            if not isinstance(tunnel, dict) or not tunnel_alive(tunnel):
                raise PortmapError("SSH tunnel is down")
            socket_path = Path(profile.get("http_socket") or "")
            directory = http_socket_dir(state_dir)
            expected = hashlib.sha256(f"{machine_info.id}:{tunnel['owner_token']}".encode()).hexdigest()[:24] + ".sock"
            if socket_path.parent != directory or socket_path.name != expected:
                raise PortmapError("SSH socket does not belong to this connection")
            identity = _http_identity(tunnel, profile.get("remote_http_port") or 80)
            if _http_record(socket_path.with_suffix(".json")) != identity:
                raise PortmapError("SSH forward ownership changed")
            metadata = socket_path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise PortmapError("SSH socket is not private and owned")
        except (OSError, PortmapError, KeyError, TypeError, ValueError) as exc:
            raise RemoteUpdateError(machine_info.id, "SSH HTTP transport is unavailable; reconnect this machine") from exc
    return remote_request(machine_info.backend_url, path, socket_path=socket_path, **kwargs)


def remote_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    capability: str | None = None,
    machine: str = "remote",
    timeout: float = REMOTE_PROBE_TIMEOUT,
    socket_path: Path | None = None,
) -> tuple[int, dict[str, Any] | None, bytes, str]:
    """One HTTP call to a machine's saved backend origin, signed when capable.

    Redirects are never followed and oversized responses are rejected; a
    successful signed call must carry a nonce-bound response signature.
    """
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise RemoteUpdateError(machine, "saved backend origin is not usable")
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "host": "localhost",
        "accept": "application/json",
        "content-length": str(len(body)),
        "connection": "close",
    }
    if payload is not None:
        headers["content-type"] = "application/json"
    if method == "POST" and path == "/api/update-agent/pair":
        headers["origin"] = "http://localhost"
    nonce: str | None = None
    if capability is not None:
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        headers[TIMESTAMP_HEADER] = timestamp
        headers[NONCE_HEADER] = nonce
        headers[SIGNATURE_HEADER] = request_mac(capability, method, path, body, timestamp, nonce)
    connection = None
    try:
        if socket_path is None:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
        else:
            from .agent_client import UnixHTTPConnection

            connection = UnixHTTPConnection(str(socket_path), timeout=timeout)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(REMOTE_MAX_BYTES + 1)
        server = response.getheader("Server") or ""
    except (OSError, http.client.HTTPException) as exc:
        raise RemoteUpdateError(machine, "did not answer its update agent") from exc
    finally:
        if connection is not None:
            connection.close()
    if len(raw) > REMOTE_MAX_BYTES or 300 <= response.status < 400:
        raise RemoteUpdateError(machine, "returned an unusable response")
    if capability is not None and nonce is not None:
        signature = response.getheader(RESPONSE_SIGNATURE_HEADER)
        if response.status < 400 and not signature:
            raise RemoteUpdateError(machine, "response was not authenticated")
        if signature and not hmac.compare_digest(signature, response_mac(capability, nonce, response.status, raw)):
            raise RemoteUpdateError(machine, "response authentication failed")
    parsed_json: dict[str, Any] | None = None
    if raw:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            parsed_json = None
        else:
            parsed_json = value if isinstance(value, dict) else None
    return response.status, parsed_json, raw, server


# ---------------------------------------------------------------------------
# Host snapshot (machine identities and transports)


def _host_snapshot_or_local_only(state_dir: Path) -> Any:
    """The controller snapshot re-read from this host's durable state."""
    from .catalog_access import parse_access_snapshot
    from .infrastructure import load_infrastructure

    try:
        state = load_infrastructure(state_dir.expanduser().resolve())
    except PortmapError as exc:
        raise PortmapError(f"update control cannot read the controller state: {exc}") from exc
    if state is None:
        return None
    path = Path(state["instance_dir"]) / "config" / "catalog-access.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PortmapError("update control cannot read the controller access snapshot") from exc
    return parse_access_snapshot(document)


def _machine_label(domain: str | None, fallback: str) -> str:
    return domain.split(".")[0] if domain else fallback


# ---------------------------------------------------------------------------
# Machine status listing


def _local_machine_item(*, state_dir: Path, snapshot: Any, check: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": LOCAL_MACHINE_ID,
        "label": _machine_label(snapshot.local.domain if snapshot else None, LOCAL_MACHINE_ID),
        "via": "local",
        "current_version": None,
        "installed_version": None,
        "build_id": None,
        "latest_version": None,
        "install_method": None,
        "update_available": False,
        "can_update": False,
        "reason": None,
        "authorized": True,
        "supported": True,
        "active_job": None,
        "last_job": None,
        "error": None,
    }
    try:
        status = _engine_module().local_status(state_dir=state_dir, check=check)
    except Exception as exc:  # The updater itself must never break the listing.
        item["supported"] = False
        item["error"] = f"local updater unavailable: {exc}"
        return item
    for field in (
        "current_version",
        "installed_version",
        "build_id",
        "latest_version",
        "install_method",
        "update_available",
        "can_update",
        "reason",
        "active_job",
        "last_job",
    ):
        item[field] = status.get(field)
    return item


def _minimal_version_probe(machine: Any, *, state_dir: Path) -> tuple[dict[str, Any], str | None]:
    """Best-effort minimal version from a machine's public version endpoint.

    Older remotes have no update agent; their build still answers with a
    ``Server: portmap-catalog/<version>`` header carrying the running
    catalog version (the registry's version field is a schema version and
    is never consulted). Returns the parsed body (or {}) and the legacy
    version when the endpoint is missing.
    """
    if not machine.backend_url:
        return {}, None
    status, body, _raw, server = _request_machine(
        machine, "/api/version", state_dir=state_dir, machine=machine.id,
        timeout=REMOTE_PROBE_TIMEOUT,
    )
    if status == 200 and isinstance(body, dict):
        return body, None
    legacy = None
    if server.startswith("portmap-catalog/"):
        tokens = server[len("portmap-catalog/"):].split()
        candidate = tokens[0] if tokens else ""
        if LEGACY_VERSION_RE.fullmatch(candidate):
            legacy = candidate
    return {}, legacy


def _remote_machine_item(machine: Any, *, state_dir: Path, check: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": machine.id,
        "label": _machine_label(machine.domain, machine.id),
        "via": machine.via,
        "current_version": None,
        "installed_version": None,
        "build_id": None,
        "latest_version": None,
        "install_method": None,
        "update_available": False,
        "can_update": False,
        "reason": None,
        "authorized": False,
        "supported": False,
        "active_job": None,
        "last_job": None,
        "error": None,
    }
    if not machine.backend_url:
        item["reason"] = "machine is not connected"
        return item
    credential = credential_for(machine.id, state_dir=state_dir)
    if credential is not None:
        query = "?check=1" if check else ""
        try:
            status, body, _raw, _server = _request_machine(machine, f"/api/update-agent/status{query}", state_dir=state_dir, capability=credential,
            machine=machine.id,
            timeout=REMOTE_CHECK_TIMEOUT if check else REMOTE_STATUS_TIMEOUT,)
        except RemoteUpdateError as exc:
            item["error"] = exc.message
            item["reason"] = "machine could not be reached with the stored authorization"
        else:
            if status == 200 and isinstance(body, dict):
                item["authorized"] = True
                item["supported"] = body.get("update_api") is True
                for field in (
                    "current_version",
                    "installed_version",
                    "build_id",
                    "latest_version",
                    "install_method",
                    "update_available",
                    "can_update",
                    "reason",
                    "active_job",
                    "last_job",
                ):
                    item[field] = body.get(field)
                return item
            if status in (401, 403):
                item["reason"] = "the machine rejected the stored authorization; re-pair it"
                credential = None
            elif status == 404:
                item["reason"] = LEGACY_UNSUPPORTED_REASON
            else:
                item["reason"] = f"machine answered HTTP {status}"
    if credential is None and machine.via == "ssh":
        # An unpaired SSH machine still shows its real update readiness:
        # the tunnel is authenticated, and the target's private listener
        # serves display-grade status without jobs. Pairing happens on the
        # explicit update action, so the first update is never blocked.
        query = "?check=1" if check else ""
        try:
            status, body, _raw, _server = _request_machine(machine, f"/api/update-agent/status{query}", state_dir=state_dir, machine=machine.id,
            timeout=REMOTE_CHECK_TIMEOUT if check else REMOTE_STATUS_TIMEOUT,)
        except RemoteUpdateError as exc:
            item["error"] = exc.message
            body = None
        else:
            if status == 200 and isinstance(body, dict) and body.get("update_api") is True:
                for field in (
                    "current_version",
                    "installed_version",
                    "build_id",
                    "latest_version",
                    "install_method",
                    "update_available",
                    "can_update",
                    "reason",
                ):
                    item[field] = body.get(field)
                item["supported"] = True
                return item
    if item["current_version"] is None and not item.get("error") and item["reason"] != LEGACY_UNSUPPORTED_REASON:
        body, legacy = _minimal_version_probe(machine, state_dir=state_dir)
        if body.get("version"):
            item["current_version"] = body.get("version")
            item["build_id"] = body.get("build_id")
            item["supported"] = body.get("update_api") is True
            if not item["authorized"] and item["supported"] and not item["reason"]:
                item["reason"] = "authorization required to manage this machine"
        elif legacy:
            item["current_version"] = legacy
            item["reason"] = LEGACY_UNSUPPORTED_REASON
    elif item["reason"] == LEGACY_UNSUPPORTED_REASON:
        _body, legacy = _minimal_version_probe(machine, state_dir=state_dir)
        if legacy:
            item["current_version"] = legacy
    if not item["authorized"] and item["supported"] and not item["reason"]:
        item["reason"] = "authorization required to manage this machine"
    return item


def version_info(*, state_dir: Path) -> dict[str, Any]:
    """Minimal version/build identity for ``/api/version``."""
    try:
        status = _engine_module().local_status(state_dir=state_dir, check=False)
        version = status.get("current_version")
        build_id = status.get("build_id")
    except Exception:
        version, build_id = None, None
    return {
        "version": version if isinstance(version, str) and version else __version__,
        "build_id": build_id if isinstance(build_id, str) else None,
        "update_api": True,
    }


def _safe_remote_machine_item(machine: Any, *, state_dir: Path, check: bool) -> dict[str, Any]:
    """One remote item that never raises: an unreachable machine is that
    machine's bounded error, never a failed or stalled whole listing."""
    try:
        return _remote_machine_item(machine, state_dir=state_dir, check=check)
    except Exception as exc:  # Bounded per-machine failure, listing continues.
        return {
            "id": machine.id,
            "label": _machine_label(machine.domain, machine.id),
            "via": machine.via,
            "current_version": None,
            "installed_version": None,
            "build_id": None,
            "latest_version": None,
            "install_method": None,
            "update_available": False,
            "can_update": False,
            "reason": "machine status failed",
            "authorized": False,
            "supported": False,
            "active_job": None,
            "last_job": None,
            "error": str(exc),
        }


def updates_status(*, state_dir: Path, check: bool = False) -> dict[str, Any]:
    """The manager-facing machines list plus recent durable batches.

    Remote machines are probed concurrently with per-machine timeouts so one
    offline or legacy machine can neither fail the listing nor stretch it by
    the sum of every machine's timeout.
    """
    snapshot = _host_snapshot_or_local_only(state_dir)
    machines = [_local_machine_item(state_dir=state_dir, snapshot=snapshot, check=check)]
    if snapshot is not None:
        remotes = list(snapshot.machines.values())
        if remotes:
            with ThreadPoolExecutor(max_workers=min(8, len(remotes))) as pool:
                futures = [pool.submit(_safe_remote_machine_item, machine, state_dir=state_dir, check=check) for machine in remotes]
                machines.extend(future.result() for future in futures)
    return {
        "update_api": True,
        "generated_at": _now(),
        "machines": machines,
        "batches": recent_batches(state_dir=state_dir),
    }


# ---------------------------------------------------------------------------
# Durable batches


def _batches_path(state_dir: Path) -> Path:
    return _updates_dir(state_dir) / BATCHES_FILE_NAME


def _load_batches(state_dir: Path) -> list[dict[str, Any]]:
    document = _read_json(_batches_path(state_dir))
    if not isinstance(document, dict):
        return []
    batches = document.get("batches")
    if not isinstance(batches, list):
        return []
    return [batch for batch in batches if isinstance(batch, dict) and isinstance(batch.get("id"), str)]


def _save_batches(state_dir: Path, batches: list[dict[str, Any]]) -> None:
    ordered = sorted(batches, key=lambda batch: str(batch.get("created_at") or ""), reverse=True)
    terminal = [batch for batch in ordered if batch.get("state") in BATCH_STATES_TERMINAL]
    active = [batch for batch in ordered if batch.get("state") not in BATCH_STATES_TERMINAL]
    kept = active + terminal[: MAX_BATCHES_KEPT - len(active)]
    _write_json(_batches_path(state_dir), {"version": 1, "batches": kept})


def _mutate_batch(state_dir: Path, batch_id: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any] | None:
    with _control_lock(state_dir):
        batches = _load_batches(state_dir)
        batch = next((item for item in batches if item.get("id") == batch_id), None)
        if batch is None:
            return None
        mutate(batch)
        batch["updated_at"] = _now()
        _save_batches(state_dir, batches)
        return dict(batch)


def _worker_alive(pid: Any, start: Any) -> bool:
    from .client_runtime import process_identity

    if type(pid) is not int or pid <= 0 or not isinstance(start, str):
        return False
    identity = process_identity(pid)
    return identity is not None and identity[0] == os.getuid() and identity[1] == start


def start_batch(payload: Any, *, state_dir: Path) -> dict[str, Any]:
    """Validate and durably enqueue one update batch, then launch its worker."""
    if not isinstance(payload, dict):
        raise PortmapError("update request body must be a JSON object")
    unknown_keys = set(payload) - {"machines", "version"}
    if unknown_keys:
        raise PortmapError("update request accepts only machines and version")
    machine_ids = payload.get("machines")
    version = payload.get("version", "latest")
    if not isinstance(machine_ids, list) or not machine_ids:
        raise PortmapError("machines must be a non-empty list of configured machine ids")
    if not all(isinstance(item, str) and item for item in machine_ids):
        raise PortmapError("machines must be a non-empty list of configured machine ids")
    if len(machine_ids) > MAX_BATCH_MACHINES:
        raise PortmapError(f"an update batch is limited to {MAX_BATCH_MACHINES} machines")
    if not isinstance(version, str) or not TARGET_VERSION_RE.fullmatch(version):
        raise PortmapError("version must be 'latest' or a concrete release like 0.16.0")
    snapshot = _host_snapshot_or_local_only(state_dir)
    configured = snapshot.machines if snapshot is not None else {}
    seen: set[str] = set()
    targets: list[dict[str, Any]] = []
    for machine_id in machine_ids:
        if machine_id in seen:
            continue
        if machine_id != LOCAL_MACHINE_ID and machine_id not in configured:
            raise PortmapError(f"unknown machine {machine_id}")
        seen.add(machine_id)
        machine = configured.get(machine_id)
        label = _machine_label(machine.domain, machine.id) if machine is not None else (
            _machine_label(snapshot.local.domain if snapshot else None, LOCAL_MACHINE_ID)
        )
        targets.append(
            {
                "machine_id": machine_id,
                "label": label,
                "state": "queued",
                "job_id": None,
                "target_version": None,
                "current_version": None,
                "message": "",
                "error": None,
                "log_tail": None,
            }
        )
    if not targets:
        raise PortmapError("machines must be a non-empty list of configured machine ids")
    batch_id = "b" + secrets.token_hex(6)
    batch = {
        "id": batch_id,
        "state": "queued",
        "created_at": _now(),
        "updated_at": _now(),
        "version_requested": version,
        "worker_pid": None,
        "worker_start": None,
        "targets": targets,
    }
    with _control_lock(state_dir):
        existing = _load_batches(state_dir)
        active = next((item for item in existing if item.get("state") in ("queued", "running")), None)
        if active is not None:
            # One active batch per manager: two concurrent batches must not
            # update the same host. The conflict carries the live job so the
            # UI can keep showing it instead of starting a duplicate.
            raise BatchConflictError(dict(active))
        _save_batches(state_dir, existing + [batch])
    try:
        _engine_module().launch_durable_worker("batch", batch_id, state_dir=state_dir)
    except Exception as exc:
        message = f"update worker could not be launched: {exc}"
        _mutate_batch(
            state_dir,
            batch_id,
            lambda record: (
                record.__setitem__("state", "failed"),
                [target.update(state="failed", error=message) for target in record["targets"]],
            ),
        )
        raise PortmapError(message) from exc
    stored = get_batch(batch_id, state_dir=state_dir, heal=False)
    assert stored is not None
    return stored


def _batch_target(batch: dict[str, Any], machine_id: str) -> dict[str, Any] | None:
    return next((target for target in batch["targets"] if target["machine_id"] == machine_id), None)


def _set_target(state_dir: Path, batch_id: str, machine_id: str, **fields: Any) -> None:
    def mutate(record: dict[str, Any]) -> None:
        target = _batch_target(record, machine_id)
        if target is not None:
            target.update(fields)

    _mutate_batch(state_dir, batch_id, mutate)


def _job_terminal(job: dict[str, Any] | None) -> bool:
    return job is not None and job.get("state") in JOB_STATES_TERMINAL


def _job_to_target_state(job: dict[str, Any]) -> str:
    state = job.get("state")
    return state if state in {"queued", "preflight", "installing", "restarting", "verifying", "succeeded"} else "failed"


def _bounded_log(job: dict[str, Any] | None) -> str | None:
    if not job:
        return None
    tail = job.get("log_tail")
    if not isinstance(tail, str) or not tail:
        return None
    return tail[-BATCH_LOG_TAIL_CHARS:]


def _target_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    if job is None:
        return {"state": "failed", "message": "", "error": "the machine reported no job", "log_tail": None, "job_id": None, "target_version": None}
    return {
        "state": _job_to_target_state(job),
        "message": str(job.get("message") or ""),
        "error": job.get("error") if isinstance(job.get("error"), str) else None,
        "log_tail": _bounded_log(job),
        "job_id": job.get("id") if isinstance(job.get("id"), str) else None,
        "target_version": job.get("target_version") if isinstance(job.get("target_version"), str) else None,
        "current_version": job.get("version_before") if isinstance(job.get("version_before"), str) else None,
    }


def _poll_remote_job(machine: Any, credential: str, batch_id: str, machine_id: str, job_id: str, *, state_dir: Path) -> dict[str, Any]:
    """Poll one remote engine job until it reaches a terminal state."""
    deadline = time.monotonic() + TARGET_TIMEOUT_SECONDS
    failures = 0
    while True:
        try:
            status, body, _raw, _server = _request_machine(machine, f"/api/update-agent/jobs/{job_id}", state_dir=state_dir, capability=credential,
            machine=machine.id,
            timeout=REMOTE_JOB_TIMEOUT,)
        except RemoteUpdateError:
            failures += 1
            if failures >= TARGET_POLL_FAILURE_LIMIT:
                return {
                    "state": "failed",
                    "message": "",
                    "error": f"lost contact with machine {machine_id} while updating",
                    "log_tail": None,
                    "job_id": job_id,
                    "target_version": None,
                }
            if failures == 1:
                _set_target(state_dir, batch_id, machine_id, message="Waiting for the remote updater to reconnect")
            time.sleep(TARGET_POLL_INTERVAL)
            continue
        failures = 0
        if status == 200 and isinstance(body, dict) and isinstance(body.get("job"), dict):
            job = body["job"]
            if _job_terminal(job):
                return _target_from_job(job)
            _set_target(state_dir, batch_id, machine_id, **_target_from_job(job))
        elif status == 404:
            return {
                "state": "failed",
                "message": "",
                "error": f"machine {machine_id} no longer reports the update job",
                "log_tail": None,
                "job_id": job_id,
                "target_version": None,
            }
        if time.monotonic() >= deadline:
            return {
                "state": "failed",
                "message": "",
                "error": f"update on machine {machine_id} did not finish in time; it continues on the machine",
                "log_tail": None,
                "job_id": job_id,
                "target_version": None,
            }
        time.sleep(TARGET_POLL_INTERVAL)


def _pair_over_tunnel(machine: Any, *, state_dir: Path) -> str | None:
    """Bootstrap the target's capability over its authenticated SSH tunnel.

    The exchange happens only on the explicit update action, only against
    the machine's saved backend origin (the private tunnel), and the result
    is proven with a signed request before it is stored.
    """
    if machine.via != "ssh" or not machine.backend_url:
        return None
    try:
        status, body, _raw, _server = _request_machine(machine, "/api/update-agent/pair", state_dir=state_dir, method="POST",
        payload={},
        machine=machine.id,
        timeout=REMOTE_ENROLL_TIMEOUT,)
    except RemoteUpdateError:
        return None
    if status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
        return None
    token = body.get("token")
    if not _valid_capability(token):
        return None
    try:
        status, body, _raw, _server = _request_machine(machine, "/api/update-agent/version", state_dir=state_dir, capability=str(token),
        machine=machine.id,
        timeout=REMOTE_PROBE_TIMEOUT,)
    except RemoteUpdateError:
        return None
    if status != 200 or not isinstance(body, dict) or body.get("update_api") is not True:
        return None
    _store_credential(machine.id, str(token), state_dir=state_dir)
    return str(token)


def _run_remote_target(batch_id: str, machine_id: str, *, state_dir: Path) -> None:
    snapshot = _host_snapshot_or_local_only(state_dir)
    machine = snapshot.machines.get(machine_id) if snapshot is not None else None
    if machine is None:
        _set_target(state_dir, batch_id, machine_id, state="failed", error="machine is no longer configured on this controller")
        return
    if not machine.backend_url:
        _set_target(state_dir, batch_id, machine_id, state="failed", error=f"machine {machine_id} is not connected")
        return
    credential = credential_for(machine_id, state_dir=state_dir)
    if machine.via == "ssh" and machine.backend_url:
        _set_target(state_dir, batch_id, machine_id, state="running", message="Authorizing over the existing SSH connection")
        # Explicit updates may refresh a capability after a workspace rebuild.
        # Only the authenticated private tunnel can reveal the target's key.
        credential = _pair_over_tunnel(machine, state_dir=state_dir) or credential
    if credential is None:
        if machine.via == "ssh":
            _set_target(
                state_dir,
                batch_id,
                machine_id,
                state="failed",
                error=f"machine {machine_id} could not be reached or authorized over its SSH tunnel; connect it and retry",
            )
        else:
            _set_target(
                state_dir,
                batch_id,
                machine_id,
                state="failed",
                error=f"no stored authorization for machine {machine_id}; pair it with its capability first",
            )
        return
    request_id = f"{batch_id}:{machine_id}"
    version = _mutate_batch_value(state_dir, batch_id, "version_requested")
    _set_target(state_dir, batch_id, machine_id, state="running", message="Starting the remote updater")
    try:
        status, body, _raw, _server = _request_machine(machine, "/api/update-agent/start", state_dir=state_dir, method="POST",
        payload={"version": version, "request_id": request_id},
        capability=credential,
        machine=machine.id,
        timeout=REMOTE_START_TIMEOUT,)
    except RemoteUpdateError as exc:
        _set_target(state_dir, batch_id, machine_id, state="failed", error=f"machine {machine_id} {exc.message}")
        return
    if status == 404:
        _set_target(state_dir, batch_id, machine_id, state="failed", error=LEGACY_UNSUPPORTED_REASON)
        return
    if status != 200 and status != 202:
        message = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), str) else f"machine answered HTTP {status}"
        _set_target(state_dir, batch_id, machine_id, state="failed", error=message)
        return
    job = body.get("job") if isinstance(body, dict) else None
    if not isinstance(job, dict) or not isinstance(job.get("id"), str):
        _set_target(state_dir, batch_id, machine_id, state="failed", error=f"machine {machine_id} reported no update job")
        return
    result = _target_from_job(job)
    _set_target(state_dir, batch_id, machine_id, **result)
    if result["state"] == "succeeded":
        return
    if _job_terminal(job):
        return
    result = _poll_remote_job(machine, credential, batch_id, machine_id, job["id"], state_dir=state_dir)
    _set_target(state_dir, batch_id, machine_id, **result)


def _run_local_target(batch_id: str, *, state_dir: Path) -> None:
    request_id = f"{batch_id}:{LOCAL_MACHINE_ID}"
    version = _mutate_batch_value(state_dir, batch_id, "version_requested")
    _set_target(state_dir, batch_id, LOCAL_MACHINE_ID, state="running", message="Starting the local updater")
    # A snapshot worker must ask the real installed agent to start updates.
    # Never infer an installation from its own snapshot or run a fallback.
    answer = _agent_local_start(version, request_id, state_dir=state_dir)
    if answer is not None:
        status, body = answer
        job = body.get("job") if isinstance(body, dict) else None
        if status not in (200, 202) or not isinstance(job, dict) or not isinstance(job.get("id"), str):
            message = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), str) else f"agent answered HTTP {status}"
            _set_target(state_dir, batch_id, LOCAL_MACHINE_ID, state="failed", error=str(message))
            return
    else:
        _set_target(
            state_dir, batch_id, LOCAL_MACHINE_ID, state="failed",
            error="the host agent could not confirm the local update request",
        )
        return
    result = _target_from_job(job)
    _set_target(state_dir, batch_id, LOCAL_MACHINE_ID, **result)
    if _job_terminal(job):
        return
    _poll_local_job(batch_id, str(job["id"]), state_dir=state_dir)


def _poll_local_job(batch_id: str, job_id: str, *, state_dir: Path) -> None:
    engine = _engine_module()
    deadline = time.monotonic() + TARGET_TIMEOUT_SECONDS
    while True:
        try:
            job = engine.get_job(job_id, state_dir=state_dir)
        except Exception as exc:
            _set_target(state_dir, batch_id, LOCAL_MACHINE_ID, state="failed", error=str(exc))
            return
        result = _target_from_job(job)
        _set_target(state_dir, batch_id, LOCAL_MACHINE_ID, **result)
        if _job_terminal(job):
            return
        if time.monotonic() >= deadline:
            _set_target(
                state_dir,
                batch_id,
                LOCAL_MACHINE_ID,
                state="failed",
                error="local update did not finish in time; it continues in the update worker",
            )
            return
        time.sleep(TARGET_POLL_INTERVAL)


def _mutate_batch_value(state_dir: Path, batch_id: str, key: str) -> Any:
    with _control_lock(state_dir):
        batches = _load_batches(state_dir)
        batch = next((item for item in batches if item.get("id") == batch_id), None)
        return batch.get(key) if batch else None


def _run_batch_target(batch_id: str, target: dict[str, Any], *, state_dir: Path) -> None:
    machine_id = str(target["machine_id"])
    if target.get("job_id"):
        if machine_id == LOCAL_MACHINE_ID:
            _poll_local_job(batch_id, str(target["job_id"]), state_dir=state_dir)
            return
        snapshot = _host_snapshot_or_local_only(state_dir)
        machine = snapshot.machines.get(machine_id) if snapshot is not None else None
        credential = credential_for(machine_id, state_dir=state_dir) if machine is not None else None
        if machine is None or credential is None:
            _set_target(state_dir, batch_id, machine_id, state="failed", error="the saved update connection or authorization is unavailable")
            return
        result = _poll_remote_job(machine, credential, batch_id, machine_id, str(target["job_id"]), state_dir=state_dir)
        _set_target(state_dir, batch_id, machine_id, **result)
    elif machine_id == LOCAL_MACHINE_ID:
        _run_local_target(batch_id, state_dir=state_dir)
    else:
        _run_remote_target(batch_id, machine_id, state_dir=state_dir)


def run_batch(identifier: str, *, state_dir: Path) -> None:
    """The durable batch body: remotes serially, the local machine last.

    Per-target failures are explicit and never cancel the remaining targets.
    A dead worker's batch resumes idempotently through engine request ids.
    """
    if not isinstance(identifier, str) or not BATCH_ID_RE.fullmatch(identifier):
        raise PortmapError("unknown update batch")
    with _control_lock(state_dir):
        batches = _load_batches(state_dir)
        batch = next((item for item in batches if item.get("id") == identifier), None)
        if batch is None:
            raise PortmapError("unknown update batch")
        if batch.get("state") in BATCH_STATES_TERMINAL:
            return
        pid = batch.get("worker_pid")
        if batch.get("state") == "running" and _worker_alive(pid, batch.get("worker_start")) and pid != os.getpid():
            return
        batch["state"] = "running"
        batch["worker_pid"] = os.getpid()
        from .client_runtime import process_identity

        identity = process_identity(os.getpid())
        if identity is None or identity[0] != os.getuid():
            raise PortmapError("cannot establish update batch worker ownership")
        batch["worker_start"] = identity[1]
        batch["updated_at"] = _now()
        targets = [dict(target) for target in batch["targets"]]
        _save_batches(state_dir, batches)

    ordered = [target for target in targets if target["machine_id"] != LOCAL_MACHINE_ID]
    ordered.extend(target for target in targets if target["machine_id"] == LOCAL_MACHINE_ID)
    for target in ordered:
        if target.get("state") in ("succeeded", "failed"):
            continue
        try:
            _run_batch_target(identifier, target, state_dir=state_dir)
        except Exception as exc:
            _set_target(state_dir, identifier, str(target["machine_id"]), state="failed", error=str(exc))

    def finalize(record: dict[str, Any]) -> None:
        states = [str(target.get("state")) for target in record["targets"]]
        for target in record["targets"]:
            if target.get("state") not in ("succeeded", "failed"):
                target["state"] = "failed"
                target["error"] = target.get("error") or "update worker stopped before this machine"
        succeeded = states.count("succeeded")
        if succeeded == len(states):
            record["state"] = "succeeded"
        elif succeeded == 0:
            record["state"] = "failed"
        else:
            record["state"] = "partial"
        record["worker_pid"] = None
        record["worker_start"] = None

    _mutate_batch(state_dir, identifier, finalize)


def get_batch(identifier: str, *, state_dir: Path, heal: bool = True) -> dict[str, Any] | None:
    """One durable batch, healing a batch whose worker died with the host.

    ``heal`` relaunches the durable worker for a queued/running batch whose
    worker is gone; callers that just launched the worker pass ``False``.
    """
    if not isinstance(identifier, str) or not BATCH_ID_RE.fullmatch(identifier):
        raise PortmapError("unknown update batch")
    with _control_lock(state_dir):
        batches = _load_batches(state_dir)
        batch = next((item for item in batches if item.get("id") == identifier), None)
    if batch is None:
        return None
    if not heal:
        return dict(batch)
    if batch.get("state") in ("queued", "running"):
        pid = batch.get("worker_pid")
        if pid is None or not _worker_alive(pid, batch.get("worker_start")):
            attempted = batch.get("resume_attempted_at")
            if attempted is None and pid is None:
                attempted = batch.get("created_at")
            try:
                due = not isinstance(attempted, str) or (
                    dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(attempted)
                ).total_seconds() > RESUME_RETRY_SECONDS
            except ValueError:
                due = True
            if due:
                _mutate_batch(state_dir, identifier, lambda record: record.__setitem__("resume_attempted_at", _now()))
                try:
                    _engine_module().launch_durable_worker("batch", identifier, state_dir=state_dir)
                except Exception:
                    _mutate_batch(
                        state_dir,
                        identifier,
                        lambda record: (
                            record.__setitem__("state", "failed"),
                            [
                                target.update(
                                    state="failed",
                                    error=target.get("error") or "update worker is not running; retry the update",
                                )
                                for target in record["targets"]
                                if target.get("state") not in ("succeeded", "failed")
                            ],
                        ),
                    )
        with _control_lock(state_dir):
            batches = _load_batches(state_dir)
            batch = next((item for item in batches if item.get("id") == identifier), None)
    return dict(batch) if batch is not None else None


def recent_batches(*, state_dir: Path) -> list[dict[str, Any]]:
    """The most recent durable batches, newest first."""
    with _control_lock(state_dir):
        batches = _load_batches(state_dir)
    ordered = sorted(batches, key=lambda batch: str(batch.get("created_at") or ""), reverse=True)
    return [dict(batch) for batch in ordered[:RECENT_BATCHES_LISTED]]


# ---------------------------------------------------------------------------
# Remote update API (this machine as the managed target)


def _resolve_start_version(engine: Any, version: str, *, state_dir: Path) -> str:
    if version == "latest":
        status = engine.local_status(state_dir=state_dir, check=True)
        latest = status.get("latest_version")
        if not isinstance(latest, str) or not TARGET_VERSION_RE.fullmatch(latest):
            raise PortmapError("no trusted latest release is known for this machine")
        return latest
    return version


def _json_response(
    status: int, payload: dict[str, Any], *, capability: str | None = None, nonce: str | None = None
) -> tuple[int, bytes, dict[str, str]]:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    headers: dict[str, str] = {}
    if capability is not None and nonce is not None:
        headers[RESPONSE_SIGNATURE_HEADER] = response_mac(capability, nonce, status, body)
    return status, body, headers


def _parse_body_json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeError, ValueError) as exc:
        raise PortmapError("request body must be a JSON object") from exc
    if not isinstance(value, dict):
        raise PortmapError("request body must be a JSON object")
    return value


def _remote_version_payload(status: dict[str, Any]) -> dict[str, Any]:
    """The minimal identity a version query may reveal: no jobs or logs."""
    payload: dict[str, Any] = {"update_api": True}
    for field in ("current_version", "installed_version", "build_id", "latest_version", "install_method", "update_available", "can_update", "reason"):
        payload[field] = status.get(field)
    return payload


def _remote_status_payload(state_dir: Path, *, check: bool) -> dict[str, Any]:
    status = _engine_module().local_status(state_dir=state_dir, check=check)
    payload: dict[str, Any] = {"update_api": True}
    for field in (
        "current_version",
        "installed_version",
        "build_id",
        "latest_version",
        "install_method",
        "update_available",
        "can_update",
        "reason",
        "active_job",
        "last_job",
    ):
        payload[field] = status.get(field)
    return payload

def engine_error_status(exc: PortmapError, default: int) -> int:
    """Map an engine refusal: malformed input is 400, the rest keep default."""
    invalid = getattr(_engine_module(), "InvalidUpdateRequest", None)
    if isinstance(invalid, type) and isinstance(exc, invalid):
        return 400
    return default


def start_local_job(payload: Any, *, state_dir: Path) -> dict[str, Any]:
    """Start the local machine's update from the real installed agent.

    The durable batch worker runs from a code snapshot whose paths no longer
    identify the installed package, so the local leg is started through the
    host agent over its Unix socket; the running agent's updater resolves
    the true install. Progress keeps flowing from the durable job files.
    """
    if not isinstance(payload, dict) or set(payload) != {"version", "request_id", "expected_state"}:
        raise PortmapError("local start requires version, request_id and expected_state")
    if payload["expected_state"] != str(state_dir.expanduser().resolve()):
        raise PortmapError("the host agent serves a different state directory")
    version = payload["version"]
    request_id = payload["request_id"]
    if not isinstance(version, str) or not TARGET_VERSION_RE.fullmatch(version):
        raise PortmapError("version must be 'latest' or a concrete release like 0.16.0")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise PortmapError("request_id is not acceptable")
    engine = _engine_module()
    job = engine.start_update(
        _resolve_start_version(engine, version, state_dir=state_dir), request_id, state_dir=state_dir
    )
    return {"job": job}


UPDATE_LOCAL_START_TIMEOUT = 100.0


def _agent_local_start(version: str, request_id: str, *, state_dir: Path) -> tuple[int, dict[str, Any]] | None:
    """Address the host socket, not the container's /run/portmap mount."""
    from .agent_client import AgentUnavailable, request_json_status
    from .infrastructure import load_infrastructure
    from .settings import load_portmap_settings

    try:
        infrastructure = load_infrastructure(state_dir)
        socket_path = (
            Path(infrastructure["agent_runtime_dir"]) / "agent.sock"
            if infrastructure is not None else load_portmap_settings().agent_socket
        )
        return request_json_status(
            "POST", "/updates/local/start", socket_path=socket_path,
            payload={"version": version, "request_id": request_id, "expected_state": str(state_dir.expanduser().resolve())},
            timeout=UPDATE_LOCAL_START_TIMEOUT,
        )
    except (AgentUnavailable, OSError, RuntimeError, PortmapError):
        return None


def handle_remote_request(
    *,
    method: str,
    path: str,
    body: bytes,
    scope: str,
    signature: Any = None,
    timestamp: Any = None,
    nonce: Any = None,
    state_dir: Path,
) -> tuple[int, bytes, dict[str, str]]:
    """Verify and execute one remote update-agent operation.

    ``scope`` is the listener the request arrived on: capability enrollment
    is accepted only from the private (SSH tunnel / loopback) scope, while
    authenticated operations are accepted anywhere with a valid signature.
    Returns a relay-ready ``(status, body, headers)`` response.
    """
    if scope not in ("private", "public"):
        return _json_response(400, {"error": "unknown update-agent scope"})
    parsed = urllib.parse.urlsplit(path)
    op_path = parsed.path
    jobs_id = None
    if op_path.startswith(UPDATE_AGENT_JOBS_PREFIX):
        jobs_id = op_path[len(UPDATE_AGENT_JOBS_PREFIX):]
        known = jobs_id != "" and UPDATE_AGENT_JOB_ID_RE.fullmatch(jobs_id) is not None
    else:
        known = (method, op_path) in _UPDATE_AGENT_FIXED_PATHS
    if not known:
        return _json_response(404, {"error": "unknown update-agent operation"})
    if op_path.startswith(UPDATE_AGENT_JOBS_PREFIX) and method != "GET":
        return _json_response(405, {"error": "update-agent job operations are read-only"})

    if op_path == "/api/update-agent/pair":
        if scope != "private":
            return _json_response(403, {"error": "capability pairing requires the private tunnel"})
        try:
            payload = _parse_body_json(body)
            if payload:
                raise PortmapError("pairing requires an empty JSON object")
        except PortmapError as exc:
            return _json_response(400, {"error": str(exc)})
        # The catalog enforces same-origin JSON before this host-owned IPC.
        # The manager learns our key; we never receive its key or gain rights
        # to update it merely because it manages this machine.
        return _json_response(200, {"ok": True, "token": local_capability(state_dir=state_dir)})

    capability = local_capability(state_dir=state_dir)
    has_auth = any(value is not None for value in (signature, timestamp, nonce))
    display_read = method == "GET" and op_path in ("/api/update-agent/version", "/api/update-agent/status")
    signed = True
    if not has_auth and display_read and scope == "private":
        # Unsigned display reads exist only on the private (SSH tunnel /
        # loopback) listener, the same audience the private portal already
        # serves its connection listings to. They carry no jobs or logs and
        # authorize no mutation; every other path must sign.
        signed = False
    else:
        try:
            verify_signed_request(
                method=method,
                path=path,
                body=body,
                timestamp=timestamp,
                nonce=nonce,
                signature=signature,
                capability=capability,
                state_dir=state_dir,
            )
        except RequestAuthError as exc:
            return _json_response(exc.status, {"error": exc.message})

    def respond(status: int, payload: dict[str, Any]) -> tuple[int, bytes, dict[str, str]]:
        if signed and isinstance(nonce, str):
            return _json_response(status, payload, capability=capability, nonce=nonce)
        return _json_response(status, payload)

    # Malformed caller input maps to 400; updater refusals keep 409/404.

    try:
        if op_path == "/api/update-agent/version":
            engine_status = _engine_module().local_status(state_dir=state_dir, check=False)
            return respond(200, _remote_version_payload(engine_status))
        if op_path == "/api/update-agent/status":
            values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("check", [])
            if values and not (len(values) == 1 and values[0] == "1"):
                return respond(400, {"error": "check must be exactly 1"})
            if signed:
                return respond(200, _remote_status_payload(state_dir, check=bool(values)))
            engine_status = _engine_module().local_status(state_dir=state_dir, check=bool(values))
            return respond(200, _remote_version_payload(engine_status))
        if op_path == "/api/update-agent/start":
            payload = _parse_body_json(body)
            if set(payload) != {"version", "request_id"}:
                raise PortmapError("start requires exactly version and request_id")
            version = payload["version"]
            request_id = payload["request_id"]
            if not isinstance(version, str) or not TARGET_VERSION_RE.fullmatch(version):
                raise PortmapError("version must be 'latest' or a concrete release like 0.16.0")
            if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
                raise PortmapError("request_id is not acceptable")
            engine = _engine_module()
            job = engine.start_update(
                _resolve_start_version(engine, version, state_dir=state_dir), request_id, state_dir=state_dir
            )
            return respond(202, {"job": job})
        if op_path.startswith(UPDATE_AGENT_JOBS_PREFIX) and jobs_id is not None:
            job = _engine_module().get_job(jobs_id, state_dir=state_dir)
            return respond(200, {"job": job})
    except PortmapError as exc:
        default = 404 if op_path.startswith(UPDATE_AGENT_JOBS_PREFIX) else 409
        return respond(engine_error_status(exc, default), {"error": str(exc)})
    except Exception as exc:  # pragma: no cover - unexpected updater failures.
        return respond(500, {"error": str(exc)})
    return respond(404, {"error": "unknown update-agent operation"})
