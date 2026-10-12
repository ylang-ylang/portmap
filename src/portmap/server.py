"""Local publishing behind separate private-access and public ingress sockets."""
from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterator

from .agent import agent_status, ensure_agent_started, stop_agent
from .client import set_local_server, setup_client
from .client_discovery import probe_gateway
from .client_runtime import client_state_dir, frozen, native_environment
from .errors import PortmapError
from .ingress import ensure_ingress, ingress_status, stop_ingress
from .settings import PortmapSettings


@contextmanager
def _server_lock(settings: PortmapSettings) -> Iterator[None]:
    directory = settings.state_dir / "server"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PortmapError(f"server state must be an owned private directory with mode 0700: {directory}")
    fd = os.open(directory / "server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load(settings: PortmapSettings) -> dict[str, Any] | None:
    path = settings.state_dir / "server" / "instance.json"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PortmapError(f"unsafe server state file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1 or data.get("uid") != os.getuid()
                or data.get("state_dir") != str(settings.state_dir.resolve())
                or not isinstance(data.get("settings"), dict)):
            raise ValueError("invalid owner or schema")
        port = data["backend_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("invalid backend port")
        if not Path(data["client_state_dir"]).is_absolute():
            raise ValueError("invalid client state directory")
        _saved_settings(settings, data)
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PortmapError(f"invalid server state: {path}: {exc}") from exc


def _save(settings: PortmapSettings, data: dict[str, Any]) -> None:
    directory = settings.state_dir / "server"
    fd, name = tempfile.mkstemp(prefix=".instance-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        Path(name).replace(directory / "instance.json")
    finally:
        Path(name).unlink(missing_ok=True)


def _settings_record(settings: PortmapSettings) -> dict[str, Any]:
    return {key: str(value) if isinstance(value, Path) else value for key, value in asdict(settings).items()}


def _saved_settings(current: PortmapSettings, data: dict[str, Any]) -> PortmapSettings:
    values = dict(data["settings"])
    for key in ("root", "state_dir", "agent_runtime_dir"):
        values[key] = Path(values[key])
    saved = PortmapSettings(**values)
    # Bundle upgrades may remove an old asset root. Resource names remain stable;
    # stopping them with the current compose asset still uses their saved runtime.
    if not (saved.root / "docker-compose.yml").is_file():
        saved = replace(saved, root=current.root)
    return saved


def _public_address(settings: PortmapSettings) -> str | None:
    value = settings.host_ip if settings.http_bind in {"", "0.0.0.0"} else settings.http_bind
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise PortmapError("HTTP publishing requires an explicit IPv4 bind address or 0.0.0.0 with an IPv4 host address") from exc
    if address.is_unspecified or address.is_multicast:
        raise PortmapError(f"invalid public HTTP bind address: {value}")
    return None if address.is_loopback else str(address)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def run_gateway_compose(
    settings: PortmapSettings, compose_args: list[str], *, backend_port: int | None = None,
) -> int:
    """Run the container-network backend; never bind it to a public address."""
    if shutil.which("docker") is None:
        raise PortmapError("local publishing needs Docker Compose (with Docker or Podman); setup/connect need neither")
    saved = _load(settings) if backend_port is None else None
    port = backend_port or (saved["backend_port"] if saved else _available_port())
    environment = native_environment()
    environment.update(settings.gateway_env())
    environment.update({
        "PORTMAP_ROOT": str(settings.root),
        "PORTMAP_BROKER_BYPASS": "1",
        "PORTMAP_BACKEND_HTTP_PORT": str(port),
    })
    command = ["docker", "compose", "-f", str(settings.root / "docker-compose.yml")]
    if frozen():
        environment["PORTMAP_BUNDLE_DIR"] = str(Path(sys.executable).resolve().parent)
        command += ["-f", str(settings.root / "docker-compose.frozen.yml")]
    command += compose_args
    try:
        return subprocess.run(command, check=False, env=environment).returncode
    except OSError as exc:
        raise PortmapError(f"cannot run the local container gateway: {exc}") from exc


def _ready(port: int, domain: str) -> bool:
    try:
        probe_gateway(f"http://127.0.0.1:{port}", expected_domain=domain)
        return True
    except PortmapError:
        return False


def _wait_ready(port: int, domain: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _ready(port, domain):
            return
        time.sleep(0.2)
    raise PortmapError(f"local catalog did not become ready at 127.0.0.1:{port}; inspect portmap gateway logs")


def _local_profile(settings: PortmapSettings, port: int) -> dict[str, Any]:
    return {"domain": settings.dns_domain, "backend_port": port, "http_port": settings.http_port}


def start_server(
    settings: PortmapSettings, compose_args: list[str], *, use_sudo: bool = True,
) -> dict[str, Any]:
    if not compose_args or compose_args[0] != "up":
        raise PortmapError("portmap up accepts gateway compose 'up' arguments; use portmap gateway for queries")
    if not 1 <= settings.http_port <= 65535:
        raise PortmapError(f"invalid HTTP port: {settings.http_port}")
    if "://" in settings.runtime_socket:
        raise PortmapError("local publishing needs a local Docker/Podman socket; use portmap connect for remote machines")
    public_address = _public_address(settings)
    access_dir = client_state_dir()
    with _server_lock(settings):
        previous = _load(settings)
        if previous:
            saved = _saved_settings(settings, previous)
            immutable = ("http_port", "dns_domain", "runtime_socket", "gateway_network", "dns_bind", "dns_port", "agent_runtime_dir")
            if (any(getattr(saved, key) != getattr(settings, key) for key in immutable)
                    or _public_address(saved) != public_address or previous["client_state_dir"] != str(access_dir)):
                raise PortmapError("local publishing configuration changed; run portmap restart to apply it without changing remote connections")
        if run_gateway_compose(settings, ["config", "--quiet"], backend_port=previous["backend_port"] if previous else 10240):
            raise PortmapError("invalid local gateway configuration")
        setup_client(access_dir, use_sudo=use_sudo, http_port=settings.http_port)
        was_running = agent_status(settings).running
        agent = ensure_agent_started(settings, use_sudo=use_sudo)
        if not agent.running:
            raise PortmapError(agent.message)
        port = previous["backend_port"] if previous else _available_port()
        record = {
            "version": 1, "uid": os.getuid(), "state_dir": str(settings.state_dir.resolve()),
            "backend_port": port, "client_state_dir": str(access_dir),
            "settings": _settings_record(settings),
        }
        local_added = False
        public_added = False
        backend_attempted = False
        try:
            backend_attempted = True
            if run_gateway_compose(settings, compose_args, backend_port=port):
                raise PortmapError("local gateway compose up failed")
            _wait_ready(port, settings.dns_domain)
            set_local_server(access_dir, _local_profile(settings, port), use_sudo=use_sudo)
            local_added = True
            if public_address:
                ensure_ingress(access_dir, name="public", bind_address=public_address,
                               port=settings.http_port, target_port=port, use_sudo=use_sudo)
                public_added = True
            _save(settings, record)
        except Exception as exc:
            failures = []
            if previous is None:
                cleanup = []
                if public_added:
                    cleanup.append(lambda: stop_ingress(access_dir, name="public", use_sudo=use_sudo))
                if local_added:
                    cleanup.append(lambda: set_local_server(access_dir, None, use_sudo=use_sudo))
                # An existing agent can belong to a legacy publishing instance.
                # Keep its backend recoverable rather than destroying it on a
                # first-upgrade ingress conflict; record its new private port.
                if backend_attempted and was_running:
                    _save(settings, record)
                elif backend_attempted:
                    cleanup.append(lambda: run_gateway_compose(settings, ["down"], backend_port=port))
                if not was_running:
                    cleanup.append(lambda: stop_agent(settings))
                for action in cleanup:
                    try:
                        result = action()
                        if isinstance(result, int) and result:
                            failures.append(f"cleanup command returned {result}")
                    except Exception as cleanup_error:
                        failures.append(str(cleanup_error))
            suffix = f"; cleanup needs attention: {'; '.join(failures)}" if failures else ""
            raise PortmapError(f"local publishing failed: {exc}{suffix}") from exc
    return server_status(settings, check=True)


def stop_server(
    settings: PortmapSettings, compose_args: list[str], *, use_sudo: bool = True,
) -> dict[str, Any]:
    if not compose_args or compose_args[0] != "down":
        raise PortmapError("portmap down accepts gateway compose 'down' arguments")
    with _server_lock(settings):
        record = _load(settings)
        if record is None:
            return {"configured": False, "running": False, "state_dir": str(settings.state_dir)}
        effective = _saved_settings(settings, record)
        access_dir = Path(record["client_state_dir"])
        stop_ingress(access_dir, name="public", use_sudo=use_sudo)
        set_local_server(access_dir, None, use_sudo=use_sudo)
        port = record["backend_port"]
        result = run_gateway_compose(effective, compose_args, backend_port=port)
        if result:
            raise PortmapError("local gateway compose down failed; remote connections are unchanged")
        agent = stop_agent(effective)
        if agent.running:
            raise PortmapError(agent.message)
        (settings.state_dir / "server" / "instance.json").unlink(missing_ok=True)
    return {"configured": False, "running": False, "state_dir": str(settings.state_dir)}


def server_status(settings: PortmapSettings, *, check: bool = False) -> dict[str, Any]:
    record = _load(settings)
    if record is None:
        return {"configured": False, "running": False, "state_dir": str(settings.state_dir)}
    effective = _saved_settings(settings, record)
    address = _public_address(effective)
    result: dict[str, Any] = {
        "configured": True, "domain": effective.dns_domain,
        "http_port": effective.http_port, "backend_port": record["backend_port"],
        "catalog_url": f"http://{effective.dns_domain}" + (f":{effective.http_port}" if effective.http_port != 80 else "") + "/",
        "public_address": address, "state_dir": str(settings.state_dir),
        "agent": agent_status(effective).as_dict(),
        "public_ingress": ingress_status(Path(record["client_state_dir"]), name="public") if address else None,
    }
    if check:
        result["backend_reachable"] = _ready(record["backend_port"], effective.dns_domain)
        result["running"] = bool(result["backend_reachable"] and result["agent"]["running"]
                                 and (not address or result["public_ingress"].get("running")))
    return result
