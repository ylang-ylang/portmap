from __future__ import annotations

import errno
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping
from urllib.parse import urlsplit

from .client_runtime import (
    find_native_binary,
    frozen,
    native_environment,
    serve_catalog_arguments,
    serve_catalog_command,
    worker_environment,
)
from .errors import PortmapError


_ADDRESS = "127.0.0.1"
_DEFAULT_HTTP_PORT = 80
_HEALTH_HOST = "portmap-gateway.invalid"
_INGRESS_NAME = "private"
_MANAGED_VIA = ("ssh", "local")
_PRIVILEGED_PORT = 1024
_START_TIMEOUT = 15.0
_STOP_TIMEOUT = 5.0
_MAX_JSON = 4 * 1024 * 1024
_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.portmap\Z")
_ORIGIN = re.compile(r"http://(?:\[[0-9A-Fa-f:.]+\]|[0-9.]+)(?::[0-9]{1,5})?/?\Z")
_CHILDREN: dict[int, subprocess.Popen[bytes]] = {}


def sync_gateway(state_dir: Path, profiles: Mapping[str, dict], *, http_port: int | None = None, use_sudo: bool = True) -> dict:
    """Publish the SSH/local routes, returning only after the gateway loads them.

    Only SSH and local-server profiles are managed here: direct connections
    resolve straight to their verified remote address and never need this
    gateway or the native Traefik binary. With no managed profile left, the
    owned gateway and ingress are stopped and a stopped state is returned while
    direct DNS stays intact. The browser-facing ``http_port`` is separated from
    the unprivileged ``listen_port`` Traefik binds: a privileged public port is
    owned by the private systemd ingress socket forwarding to that listener,
    and there is never a silent fallback to another port. A legacy (version 1)
    descriptor is only ever stopped through ownership validation, never reused.
    """
    backends, local_domains = _backends(_managed_profiles(profiles))
    if http_port is not None:
        _port(http_port)
    state_dir = Path(state_dir).expanduser().resolve()
    with _lock(state_dir):
        previous = _load(state_dir)
        if previous is not None:
            active = any(_owned_process(previous, kind) for kind in ("traefik", "view"))
            if active and previous["version"] == 2:
                if http_port is not None and http_port != previous["http_port"]:
                    raise PortmapError("The local gateway is active; stop it before changing --http-port")
                if not all(_owned_process(previous, kind) for kind in ("traefik", "view")):
                    raise PortmapError("The local gateway is incomplete; stop it before reconnecting")
                if not backends:
                    _stop(state_dir, previous, use_sudo=use_sudo)
                    return _stopped_state()
                return _publish(state_dir, previous, backends, local_domains)
            # A live legacy descriptor, or a dead one of any version, is
            # replaced. Stopping validates ownership before signaling anything.
            _stop(state_dir, previous, use_sudo=use_sudo)
        if not backends:
            return _stopped_state()
        if http_port is None:
            http_port = preferred_http_port(state_dir)
            if http_port is None and previous is not None and previous["version"] == 2:
                http_port = previous["http_port"]
            if http_port is None:
                http_port = _DEFAULT_HTTP_PORT
        binary = find_native_binary("traefik")
        if binary is None:
            raise PortmapError("Native Traefik is required: use a portmap release bundle or run 'brew install traefik' and put traefik on PATH")
        return _start(state_dir, backends, local_domains, str(binary.resolve()), http_port, use_sudo=use_sudo)


def gateway_status(state_dir: Path) -> dict:
    """Report verified ownership, child liveness, and the loaded route revision."""
    state_dir = Path(state_dir).expanduser().resolve()
    if not state_dir.exists():
        return _stopped_state()
    try:
        with _lock(state_dir):
            state = _load(state_dir)
            if state is None:
                return _stopped_state()
            config = _read_json(Path(state["dynamic_path"]))
            return _status(state, config)
    except (PortmapError, OSError, ValueError) as exc:
        return {**_stopped_state(), "error": str(exc)}


def stop_gateway(state_dir: Path, *, use_sudo: bool = True) -> None:
    """Stop only children matching this user's private descriptor and start identity."""
    state_dir = Path(state_dir).expanduser().resolve()
    if not state_dir.exists():
        return
    with _lock(state_dir):
        state = _load(state_dir)
        if state is not None:
            _stop(state_dir, state, use_sudo=use_sudo)


def save_public_http_port(state_dir: Path, port: int) -> None:
    """Persist the preferred browser-facing port without starting a gateway."""
    _port(port)
    state_dir = Path(state_dir).expanduser().resolve()
    _atomic_write(state_dir / "settings.json", _json_bytes({"version": 1, "public_http_port": port}))


def preferred_http_port(state_dir: Path) -> int | None:
    state_dir = Path(state_dir).expanduser().resolve()
    try:
        settings = _read_json(state_dir / "settings.json")
    except FileNotFoundError:
        return None
    if settings.get("version") != 1:
        raise PortmapError(f"Invalid gateway settings in {state_dir / 'settings.json'}")
    return _port(settings.get("public_http_port"))


def _stopped_state() -> dict:
    return {
        "running": False, "address": _ADDRESS, "http_port": None, "listen_port": None,
        "view_port": None, "management_port": None, "ingress": None,
    }


def _port(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise PortmapError("Local gateway ports must be integers between 1 and 65535")
    return value


def _managed_profiles(profiles: Mapping[str, dict]) -> dict[str, dict]:
    if not isinstance(profiles, Mapping):
        raise PortmapError("Gateway profiles must be a mapping of domains to connection profiles")
    managed = {}
    for domain, profile in profiles.items():
        if not isinstance(profile, dict):
            raise PortmapError(f"Gateway profile for {domain!r} must be an object")
        if profile.get("via") in _MANAGED_VIA:
            managed[domain] = profile
    return managed


def _backends(managed: Mapping[str, dict]) -> tuple[dict[str, str], set[str]]:
    backends: dict[str, str] = {}
    local_domains: set[str] = set()
    for domain, profile in managed.items():
        if not isinstance(domain, str) or not _DOMAIN.fullmatch(domain):
            raise PortmapError("Gateway domains must be lowercase <hostname>.portmap DNS names")
        if profile.get("domain", domain) != domain:
            raise PortmapError(f"Gateway profile does not match its domain {domain!r}")
        value = profile.get("backend_url")
        if not isinstance(value, str) or not _ORIGIN.fullmatch(value):
            raise PortmapError(f"Backend for {domain} must be a numeric plain HTTP origin")
        try:
            parsed = urlsplit(value)
            address = ipaddress.ip_address(parsed.hostname or "")
            port = _port(parsed.port if parsed.port is not None else 80)
        except ValueError as exc:
            raise PortmapError(f"Backend for {domain} has an invalid IP address or port") from exc
        if address.is_unspecified or address.is_multicast:
            raise PortmapError(f"Backend for {domain} must be a unicast IP address")
        if profile.get("via") == "local" and not address.is_loopback:
            raise PortmapError(f"Local backend for {domain} must be a loopback address")
        host = f"[{address}]" if address.version == 6 else str(address)
        backends[domain] = f"http://{host}:{port}"
        if profile.get("via") == "local":
            local_domains.add(domain)
    return backends, local_domains


def _routes(state: dict, backends: Mapping[str, str], local_domains: set[str]) -> tuple[str, dict]:
    services = {
        "catalog-view": {
            "loadBalancer": {
                "servers": [{"url": f"http://{_ADDRESS}:{state['view_port']}"}],
                "passHostHeader": True,
            }
        }
    }
    routers = {}
    fallback: str | None = None
    for domain, backend in sorted(backends.items()):
        name = hashlib.sha256(domain.encode("ascii")).hexdigest()[:20]
        remote = f"remote-{name}"
        services[remote] = {"loadBalancer": {"servers": [{"url": backend}], "passHostHeader": True}}
        apex = f"Host(`{domain}`)"
        routers[f"backend-{name}"] = {
            "entryPoints": ["web"],
            "rule": f"{apex} || HostRegexp(`^.+\\.{re.escape(domain)}$`)",
            "service": remote,
            "priority": 100,
        }
        if domain in local_domains:
            # The local server owns its whole domain: catalog, actions, and app
            # paths all proxy straight to its backend instead of a read-only
            # client view, and bare-IP requests may fall back to it privately.
            fallback = fallback or remote
            continue
        routers[f"catalog-{name}"] = {
            "entryPoints": ["web"], "rule": apex, "service": "catalog-view", "priority": 200,
        }
        routers[f"actions-{name}"] = {
            "entryPoints": ["web"],
            "rule": f"{apex} && PathPrefix(`/actions/`)",
            "service": remote,
            "priority": 300,
        }
    if fallback is not None:
        routers["local-bare-ip"] = {
            "entryPoints": ["web"],
            "rule": "Host(`127.0.0.1`) || Host(`localhost`)",
            "service": fallback,
            "priority": 1,
        }
    config = {
        "http": {
            "routers": routers,
            "services": services,
            "middlewares": {"readiness-path": {"replacePath": {"path": "/healthz"}}},
        }
    }
    revision = hashlib.sha256(_json_bytes({"owner": state["owner"], "config": config})).hexdigest()
    routers[f"ready-{revision}"] = {
        "entryPoints": ["web"],
        "rule": f"Host(`{_HEALTH_HOST}`) && Path(`{_ready_path(state, revision)}`)",
        "service": "catalog-view",
        "middlewares": ["readiness-path"],
        "priority": 400,
    }
    return revision, config


def _ready_path(state: dict, revision: str | None = None) -> str:
    return f"/__portmap_ready/{state['owner']}/{revision or state['revision']}"


def _static_config(state: dict) -> dict:
    return {
        "global": {"checkNewVersion": False, "sendAnonymousUsage": False},
        "entryPoints": {
            "web": {"address": f"{_ADDRESS}:{state['listen_port']}"},
            "traefik": {"address": f"{_ADDRESS}:{state['management_port']}"},
        },
        "api": {"insecure": True, "dashboard": False},
        "providers": {
            "providersThrottleDuration": "100ms",
            "file": {"directory": str(Path(state["dynamic_path"]).parent), "watch": True},
        },
        "log": {"level": "INFO", "format": "json"},
    }


def _private_dir(path: Path, *, create: bool = False, strict: bool = True) -> None:
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    forbidden = 0o077 if strict else 0o022
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & forbidden:
        raise PortmapError(f"Gateway state directory is not private or owned by this user: {path}")


def _private_file(fd: int, path: Path) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PortmapError(f"Gateway state file is not private or owned by this user: {path}")


@contextmanager
def _lock(state_dir: Path) -> Iterator[None]:
    try:
        _private_dir(state_dir, create=True, strict=False)
        path = state_dir / "gateway.lock"
        with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), "rb") as lock:
            _private_file(lock.fileno(), path)
            deadline = time.monotonic() + _START_TIMEOUT + _STOP_TIMEOUT + 2
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PortmapError("Timed out waiting for another local gateway operation")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise PortmapError(f"Local gateway operation failed: {exc}") from exc


def _read_bytes(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        _private_file(stream.fileno(), path)
        data = stream.read(_MAX_JSON + 1)
    if len(data) > _MAX_JSON:
        raise PortmapError(f"Gateway state file is too large: {path}")
    return data


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(_read_bytes(path))
    except ValueError as exc:
        raise PortmapError(f"Invalid gateway JSON in {path}") from exc
    if not isinstance(value, dict):
        raise PortmapError(f"Invalid gateway state object in {path}")
    return value


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _save(state_dir: Path, state: dict, *, ownership: bool = False) -> None:
    if ownership:
        marker = {key: value for key, value in state.items() if key != "revision"}
        _atomic_write(Path(state["instance_dir"]) / "owner.json", _json_bytes(marker))
    _atomic_write(state_dir / "gateway.json", _json_bytes(state))


def _load(state_dir: Path) -> dict | None:
    try:
        state = _read_json(state_dir / "gateway.json")
    except FileNotFoundError:
        return None
    owner = state.get("owner")
    if (
        type(state.get("version")) is not int or state["version"] not in (1, 2)
        or type(state.get("uid")) is not int or state["uid"] != os.getuid()
        or not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner)
        or state.get("state_dir") != str(state_dir) or state.get("address") != _ADDRESS
    ):
        raise PortmapError("Invalid local gateway ownership descriptor; refusing to control its processes")
    runtime = state_dir / "gateway" / owner
    if (
        state.get("instance_dir") != str(runtime)
        or state.get("config_path") != str(runtime / "traefik.yaml")
        or state.get("dynamic_path") != str(runtime / "dynamic" / "routes.yaml")
    ):
        raise PortmapError("Gateway configuration is not inside its private instance directory")
    if state["version"] == 2:
        for name in ("http_port", "listen_port", "view_port", "management_port"):
            _port(state.get(name))
        if len({state["listen_port"], state["view_port"], state["management_port"]}) != 3:
            raise PortmapError("Gateway listeners must use distinct private ports")
        ingress = state.get("ingress")
        if ingress is not None:
            if (
                not isinstance(ingress, dict)
                or ingress.get("name") != _INGRESS_NAME
                or ingress.get("port") != state["http_port"]
                or ingress.get("target_port") != state["listen_port"]
                or state["http_port"] == state["listen_port"]
            ):
                raise PortmapError("Invalid private ingress record in local gateway descriptor")
    else:
        for name in ("http_port", "view_port", "management_port"):
            _port(state.get(name))
        if len({state[name] for name in ("http_port", "view_port", "management_port")}) != 3:
            raise PortmapError("Gateway listeners must use distinct ports")
    if not isinstance(state.get("revision"), str) or not re.fullmatch(r"[0-9a-f]{64}", state["revision"]):
        raise PortmapError("Invalid local gateway route revision")
    for directory in (runtime.parent, runtime, runtime / "dynamic"):
        _private_dir(directory)
    marker = _read_json(runtime / "owner.json")
    if marker != {key: value for key, value in state.items() if key != "revision"}:
        raise PortmapError("Gateway descriptor does not match its private ownership record")
    for kind in ("traefik", "view"):
        process = state.get(kind)
        if process is not None and (
            not isinstance(process, dict)
            or type(process.get("pid")) is not int or process["pid"] <= 1
            or not isinstance(process.get("start_time"), str) or not process["start_time"]
            or not _valid_recorded_command(state, kind, process.get("command"))
        ):
            raise PortmapError(f"Invalid {kind} process identity in local gateway descriptor")
    return state


def _listen_port(state: dict) -> int:
    return state["listen_port"] if "listen_port" in state else state["http_port"]


def _ingress(state: dict) -> dict | None:
    return state.get("ingress")


def _ensure_ingress(state_dir: Path, *, name: str, bind_address: str, port: int, target_port: int, use_sudo: bool = True) -> dict:
    # Imported lazily: direct-only clients never load the systemd ingress path.
    from .ingress import ensure_ingress

    return ensure_ingress(state_dir, name=name, bind_address=bind_address, port=port, target_port=target_port, use_sudo=use_sudo)


def _stop_ingress(state_dir: Path, *, name: str, use_sudo: bool = True) -> dict:
    from .ingress import stop_ingress

    return stop_ingress(state_dir, name=name, use_sudo=use_sudo)


def _valid_recorded_command(state: dict, kind: str, command: object) -> bool:
    """Accept the exact command shapes this controller records, in either the
    frozen (`_serve-catalog`) or source (`-m`) worker form, without assuming
    the verifying process runs in the recorder's mode."""
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        return False
    if not Path(command[0]).is_absolute():
        return False
    if kind == "traefik":
        return len(command) == 2 and command[1] == f"--configFile={state['config_path']}"
    arguments = serve_catalog_arguments(command)
    return (
        arguments is not None
        and arguments["state_dir"] == state["state_dir"]
        and arguments["port"] == str(state["view_port"])
        and arguments["owner"] == state["owner"]
    )


def _process_identity(pid: int) -> tuple[int, str, list[str] | str] | None:
    try:
        if sys.platform.startswith("linux"):
            process = Path(f"/proc/{pid}")
            # comm can contain spaces and parentheses; starttime is field 22.
            fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            argv = (process / "cmdline").read_bytes().rstrip(b"\x00").split(b"\x00")
            return process.stat().st_uid, fields[19], [os.fsdecode(arg) for arg in argv]
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "uid=,lstart=,command="],
            capture_output=True, text=True, timeout=1, check=False,
            env={**native_environment(), "LC_ALL": "C"},
        )
        fields = result.stdout.strip().split(None, 6)
        if result.returncode == 0 and len(fields) == 7:
            return int(fields[0]), " ".join(fields[1:6]), fields[6]
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    return None


def _owned_process(state: dict, kind: str) -> bool:
    process = state.get(kind)
    if not process:
        return False
    child = _CHILDREN.get(process["pid"])
    if child is not None and child.poll() is not None:
        _CHILDREN.pop(process["pid"], None)
    identity = _process_identity(process["pid"])
    if identity is None or identity[:2] != (os.getuid(), process["start_time"]):
        return False
    command = process["command"]
    return identity[2] == (command if isinstance(identity[2], list) else " ".join(command))


def _child_environment(kind: str) -> dict[str, str]:
    proxy_names = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    environment = {key: value for key, value in os.environ.items() if key.lower() not in proxy_names and not key.startswith("TRAEFIK_")}
    environment.update({"NO_PROXY": "*", "no_proxy": "*"})
    if kind == "view":
        # A frozen self-spawn shares this executable; PYINSTALLER_RESET_ENVIRONMENT
        # keeps the child bootloader from stacking inherited loader paths. In
        # source mode sys.executable must still find the package, whose root is
        # src/ in a checkout or site-packages/ in an installation.
        if not frozen():
            package_root = str(Path(__file__).resolve().parent.parent)
            inherited = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = package_root + (os.pathsep + inherited if inherited else "")
        return worker_environment(environment)
    # Native Traefik must never inherit the frozen bundle's loader search path.
    return native_environment(environment)


def _reserve(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Match the native HTTP servers: TIME_WAIT from a prior owned listener
        # must not prevent a restart. listen() still excludes a live listener.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((_ADDRESS, port))
        listener.listen(1)
        return listener
    except BaseException:
        listener.close()
        raise


def _launch(state: dict, kind: str, command: list[str], children: list[subprocess.Popen[bytes]]) -> None:
    log_path = Path(state["instance_dir"]) / f"{kind}.log"
    with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as log:
        child = subprocess.Popen(
            command, cwd=state["instance_dir"], env=_child_environment(kind),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    children.append(child)
    _CHILDREN[child.pid] = child
    identity = _process_identity(child.pid)
    if identity is None:
        raise PortmapError(f"The local {kind} process exited before its ownership could be recorded")
    # Record the exact argv: ownership checks must reconstruct it from this
    # descriptor, never from the (possibly different) mode of a later process.
    state[kind] = {"pid": child.pid, "start_time": identity[1], "command": list(command)}


def _start(state_dir: Path, backends: dict[str, str], local_domains: set[str], binary: str, http_port: int, *, use_sudo: bool = True) -> dict:
    reservations: list[socket.socket] = []
    children: list[subprocess.Popen[bytes]] = []
    state: dict | None = None
    ingress_active = False
    try:
        if http_port < _PRIVILEGED_PORT:
            # The browser-facing port is owned by the private ingress socket;
            # native Traefik stays unprivileged on a high private listener.
            listen = _reserve(0)
        else:
            # An explicit unprivileged public port binds natively with no
            # privileged ingress in the path.
            listen = _reserve(http_port)
        reservations.append(listen)
        view = _reserve(0)
        reservations.append(view)
        management = _reserve(0)
        reservations.append(management)
        owner = secrets.token_hex(16)
        runtime = state_dir / "gateway" / owner
        _private_dir(runtime.parent, create=True)
        runtime.mkdir(mode=0o700)
        (runtime / "dynamic").mkdir(mode=0o700)
        state = {
            "version": 2, "owner": owner, "uid": os.getuid(), "state_dir": str(state_dir),
            "address": _ADDRESS, "http_port": http_port, "listen_port": listen.getsockname()[1],
            "view_port": view.getsockname()[1], "management_port": management.getsockname()[1],
            "ingress": None,
            "instance_dir": str(runtime), "config_path": str(runtime / "traefik.yaml"),
            "dynamic_path": str(runtime / "dynamic" / "routes.yaml"), "traefik": None, "view": None,
        }
        state["revision"], config = _routes(state, backends, local_domains)
        # Traefik accepts JSON syntax as YAML but ignores .json file-provider files.
        _atomic_write(Path(state["config_path"]), _json_bytes(_static_config(state)))
        _atomic_write(Path(state["dynamic_path"]), _json_bytes(config))
        _save(state_dir, state, ownership=True)
        view.close()
        _launch(state, "view", serve_catalog_command(state_dir=state["state_dir"], port=state["view_port"], owner=state["owner"]), children)
        _save(state_dir, state, ownership=True)
        listen.close()
        management.close()
        _launch(state, "traefik", [binary, f"--configFile={state['config_path']}"], children)
        _save(state_dir, state, ownership=True)
        # Prove the private listener serves the route revision before anything
        # faces the public port.
        status = _wait_ready(state, config, probe_port=state["listen_port"])
        if state["http_port"] != state["listen_port"]:
            state["ingress"] = _ensure_ingress(
                state_dir, name=_INGRESS_NAME, bind_address=_ADDRESS,
                port=http_port, target_port=state["listen_port"], use_sudo=use_sudo,
            )
            ingress_active = True
            _save(state_dir, state, ownership=True)
            # The final probe crosses ingress -> proxyd -> Traefik -> view,
            # proving the whole browser-facing chain, not just the listener.
            status = _wait_ready(state, config, probe_port=http_port)
        return status
    except BaseException as exc:
        cleanup_errors = []
        if ingress_active:
            try:
                _stop_ingress(state_dir, name=_INGRESS_NAME, use_sudo=use_sudo)
            except Exception as cleanup:
                cleanup_errors.append(str(cleanup))
        for child in reversed(children):
            try:
                _terminate_child(child)
            except (OSError, subprocess.TimeoutExpired) as cleanup:
                cleanup_errors.append(str(cleanup))
        if state is not None and not cleanup_errors:
            (state_dir / "gateway.json").unlink(missing_ok=True)
        detail = _diagnostic(state) if state is not None else ""
        if isinstance(exc, (OSError, PortmapError)):
            message = f"Could not start local gateway: {exc}{detail}"
            if cleanup_errors:
                message += "; cleanup failed: " + "; ".join(cleanup_errors)
            raise PortmapError(message) from exc
        raise
    finally:
        for reservation in reservations:
            reservation.close()


def _json_get(port: int, path: str, *, host: str | None = None, timeout: float = 0.4) -> dict:
    connection = http.client.HTTPConnection(_ADDRESS, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Host": host or f"{_ADDRESS}:{port}", "Connection": "close"})
        response = connection.getresponse()
        payload = response.read(_MAX_JSON + 1)
        if response.status != 200 or len(payload) > _MAX_JSON:
            raise ValueError(f"Readiness endpoint returned HTTP {response.status} or an oversized response")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("Readiness endpoint did not return a JSON object")
        return value
    finally:
        connection.close()


def _loaded(raw: dict, config: dict) -> bool:
    routers = raw.get("routers")
    services = raw.get("services")
    if not isinstance(routers, dict) or not isinstance(services, dict):
        return False
    expected = config["http"]
    if {name for name in routers if name.endswith("@file")} != {f"{name}@file" for name in expected["routers"]}:
        return False
    for name, desired in expected["routers"].items():
        actual = routers.get(f"{name}@file", {})
        if not isinstance(actual, dict) or actual.get("status") != "enabled":
            return False
        for field in ("rule", "priority", "entryPoints"):
            if actual.get(field) != desired[field]:
                return False
        if actual.get("service") not in (desired["service"], desired["service"] + "@file"):
            return False
        if actual.get("middlewares", []) not in (
            desired.get("middlewares", []), [f"{name}@file" for name in desired.get("middlewares", [])],
        ):
            return False
    for name, desired in expected["services"].items():
        actual = services.get(f"{name}@file", {})
        if not isinstance(actual, dict) or actual.get("status") != "enabled":
            return False
        balancer = actual.get("loadBalancer", {})
        if not isinstance(balancer, dict) or any(balancer.get(field) != desired["loadBalancer"][field] for field in ("servers", "passHostHeader")):
            return False
    return True


def _health(state: dict, config: dict, *, port: int | None = None, timeout: float = 0.4) -> tuple[bool, str | None]:
    try:
        expected = {"ok": True, "owner": state["owner"]}
        if _json_get(state["view_port"], "/healthz", timeout=timeout) != expected:
            return False, "Catalog-view ownership readiness failed"
        if not _loaded(_json_get(state["management_port"], "/api/rawdata", timeout=timeout), config):
            return False, "Traefik has not enabled the requested route revision"
        if _json_get(port if port is not None else state["http_port"], _ready_path(state), host=_HEALTH_HOST, timeout=timeout) != expected:
            return False, "HTTP gateway ownership readiness failed"
        return True, None
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
        return False, f"Local gateway readiness failed: {exc}"


def _status(state: dict, config: dict) -> dict:
    traefik_running = _owned_process(state, "traefik")
    view_running = _owned_process(state, "view")
    ready, error = _health(state, config) if traefik_running and view_running else (False, "An owned gateway process is not running")
    status = {
        **state, "listen_port": _listen_port(state), "running": ready, "traefik_running": traefik_running,
        "view_running": view_running, "routes_loaded": ready,
    }
    if error:
        status["error"] = error
    return status


def _wait_ready(state: dict, config: dict, *, probe_port: int | None = None) -> dict:
    deadline = time.monotonic() + _START_TIMEOUT
    error = "Gateway readiness timed out"
    while time.monotonic() < deadline:
        if not all(_owned_process(state, kind) for kind in ("traefik", "view")):
            raise PortmapError("An owned local gateway process exited before becoming ready")
        ready, error = _health(state, config, port=probe_port, timeout=min(0.4, max(0.01, (deadline - time.monotonic()) / 3)))
        if ready:
            return {
                **state, "running": True, "traefik_running": True,
                "view_running": True, "routes_loaded": True,
            }
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    raise PortmapError(error or "Gateway readiness timed out")


def _publish(state_dir: Path, state: dict, backends: dict[str, str], local_domains: set[str]) -> dict:
    path = Path(state["dynamic_path"])
    previous = _read_bytes(path)
    revision, config = _routes(state, backends, local_domains)
    candidate = {**state, "revision": revision}
    payload = _json_bytes(config)
    try:
        if previous != payload:
            _atomic_write(path, payload)
        status = _wait_ready(candidate, config)
        _save(state_dir, candidate)
    except BaseException as exc:
        try:
            _atomic_write(path, previous)
            _save(state_dir, state)
            _wait_ready(state, json.loads(previous))
        except (OSError, ValueError, PortmapError) as rollback:
            raise PortmapError(f"Gateway route update failed ({exc}); previous configuration was restored on disk but rollback readiness failed: {rollback}") from exc
        if isinstance(exc, (OSError, PortmapError)):
            raise PortmapError(f"Gateway route update failed; previous configuration restored: {exc}{_diagnostic(state)}") from exc
        raise
    return status


def _signal_owned(state: dict, kind: str, sig: int) -> None:
    process = state.get(kind)
    if not process:
        return
    pidfd = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                pidfd = os.pidfd_open(process["pid"])
            except OSError as exc:
                if exc.errno not in (errno.ENOSYS, errno.EINVAL):
                    raise
        if not _owned_process(state, kind):
            return
        if pidfd is not None:
            signal.pidfd_send_signal(pidfd, sig)
        else:
            os.kill(process["pid"], sig)
    except ProcessLookupError:
        pass
    finally:
        if pidfd is not None:
            os.close(pidfd)


def _stop(state_dir: Path, state: dict, *, use_sudo: bool = True) -> None:
    for kind in ("traefik", "view"):
        _signal_owned(state, kind, signal.SIGTERM)
    deadline = time.monotonic() + _STOP_TIMEOUT
    while time.monotonic() < deadline and any(_owned_process(state, kind) for kind in ("traefik", "view")):
        time.sleep(0.05)
    for kind in ("traefik", "view"):
        if _owned_process(state, kind):
            _signal_owned(state, kind, signal.SIGKILL)
    deadline = time.monotonic() + 1
    while any(_owned_process(state, kind) for kind in ("traefik", "view")):
        if time.monotonic() >= deadline:
            raise PortmapError("Local gateway processes did not exit; ownership state and logs were retained")
        time.sleep(0.05)
    for kind in ("traefik", "view"):
        process = state.get(kind)
        child = _CHILDREN.pop(process["pid"], None) if process else None
        if child is not None:
            child.wait(timeout=1)
    if _ingress(state) is not None:
        # The ingress unit is only removed once the backend it proxies is gone;
        # its own removal revalidates unit ownership before touching systemd.
        _stop_ingress(state_dir, name=_INGRESS_NAME, use_sudo=use_sudo)
    (state_dir / "gateway.json").unlink(missing_ok=True)


def _terminate_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=1)
    _CHILDREN.pop(child.pid, None)


def _diagnostic(state: dict | None) -> str:
    if state is None:
        return ""
    runtime = Path(state["instance_dir"])
    details = []
    for name in ("traefik.log", "view.log"):
        try:
            with (runtime / name).open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 2000))
                text = stream.read().decode("utf-8", errors="replace").strip()
            text = "".join(character if character.isprintable() or character in "\n\t" else "?" for character in text)
            if text:
                details.append(f"{name}: {text}")
        except OSError:
            pass
    return f" (logs: {runtime})" + ("\n" + "\n".join(details) if details else "")
