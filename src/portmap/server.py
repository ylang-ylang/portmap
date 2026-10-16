"""Local publishing as a role of the shared container infrastructure.

One owned container infrastructure serves the access role (private portal,
DNS, remotes) and the local-publishing role. ``up`` applies the local role on
top of :func:`~portmap.infrastructure.sync_infrastructure`, proves the shared
public backend, and installs a LAN ingress only when one is explicitly
configured or was preserved from a migrated legacy server; ``down`` removes
exactly that role and ingress while the infrastructure, DNS, private HTTP
entry, agent and remote connections stay untouched. Server state is version
2; the removed version-1 second Compose stack is migrated by
:mod:`portmap.legacy_server` through :func:`migrate_legacy_server`, which the
client setup calls once the shared infrastructure is ready.
"""
from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from .agent import agent_status
from .client import _load_local as _load_client_local
from .client import set_local_server, setup_client
from .client_discovery import probe_gateway
from .container_runtime import ContainerRuntime
from .errors import PortmapError
from .infrastructure import _START_TIMEOUT, infrastructure_status, load_infrastructure
from .ingress import ensure_ingress, ingress_status, stop_ingress
from .legacy_server import (
    capture_legacy_server,
    clear_migration,
    load_legacy_record,
    load_migration,
    public_dns_bindings,
    restore_legacy_stack,
    retire_legacy_server,
    save_migration,
    stop_legacy_stack,
    verify_public_dns_bindings,
)
from .settings import PortmapSettings

_STATE_VERSION = 2
_QUERY_COMMANDS = ("config", "images", "logs", "ps", "top")
_INGRESS_NAME = "public"


@contextmanager
def _server_lock(settings: PortmapSettings) -> Iterator[None]:
    directory = settings.state_dir / "server"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o777 != 0o700:
        raise PortmapError(f"server state must be an owned private directory with mode 0700: {directory}")
    fd = os.open(directory / "server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _record_path(settings: PortmapSettings) -> Path:
    return settings.state_dir / "server" / "instance.json"


def _load(settings: PortmapSettings) -> dict[str, Any] | None:
    path = _record_path(settings)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PortmapError(f"unsafe server state file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid server state")
        if data.get("version") == 1:
            # A legacy record belongs to portmap.legacy_server, not this role.
            return None
        if (data.get("version") != _STATE_VERSION or data.get("uid") != os.getuid()
                or data.get("state_dir") != str(settings.state_dir.expanduser().resolve())
                or not isinstance(data.get("settings"), dict)):
            raise ValueError("invalid owner or schema")
        if not Path(data["client_state_dir"]).is_absolute():
            raise ValueError("invalid client state directory")
        for key in ("http_port",):
            port = data[key]
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError(f"invalid {key}")
        lan = data.get("lan")
        if lan is not None:
            if (not isinstance(lan, dict) or not isinstance(lan.get("bind_address"), str)
                    or isinstance(lan.get("port"), bool) or not isinstance(lan.get("port"), int)
                    or not 1 <= lan["port"] <= 65535):
                raise ValueError("invalid LAN ingress record")
            address = ipaddress.ip_address(lan["bind_address"])
            if address.is_loopback or address.is_unspecified:
                raise ValueError("invalid LAN ingress address")
        _saved_settings(settings, data)
        return data
    except (OSError, ValueError, TypeError, KeyError) as exc:
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
    return PortmapSettings(**values)


def _explicit_bind(settings: PortmapSettings) -> str | None:
    """The explicitly configured HTTP bind address, or None for the wildcard default."""
    bind = settings.http_bind.strip()
    if not bind or bind in {"0.0.0.0", "::"}:
        return None
    try:
        return str(ipaddress.ip_address(bind))
    except ValueError as exc:
        raise PortmapError(f"invalid HTTP bind address {bind!r}: expected an IPv4/IPv6 address") from exc


def lan_ingress_spec(settings: PortmapSettings) -> dict[str, Any] | None:
    """LAN ingress target only when the user explicitly configured one.

    The wildcard default never publishes to the LAN by itself; loopback binds
    publish nothing. A LAN entry preserved from a migrated legacy server is
    reported through the server record, not recreated from defaults here.
    """
    address = _explicit_bind(settings)
    if address is None or ipaddress.ip_address(address).is_loopback:
        return None
    return {"bind_address": address, "port": settings.http_port}


def _effective_lan(settings: PortmapSettings, previous: dict[str, Any] | None, capture: dict[str, Any] | None) -> dict[str, Any] | None:
    """Explicit configuration wins; an explicit loopback clears; the wildcard
    default keeps LAN publishing that a previous record or a legacy capture
    proves existed, so an unchanged upgraded machine keeps publishing."""
    address = _explicit_bind(settings)
    if address is not None:
        if ipaddress.ip_address(address).is_loopback:
            return None
        return {"bind_address": address, "port": settings.http_port}
    if previous is not None and isinstance(previous.get("lan"), dict):
        return {"bind_address": previous["lan"]["bind_address"], "port": settings.http_port}
    if capture is not None and isinstance(capture.get("ingress"), dict):
        return {"bind_address": capture["ingress"]["bind_address"], "port": settings.http_port}
    return None


def local_role(settings: PortmapSettings) -> dict[str, Any]:
    """The local-publishing profile handed to the shared infrastructure."""
    return {"domain": settings.dns_domain, "http_port": settings.http_port}


def _require_public_backend(status: dict[str, Any], settings: PortmapSettings) -> None:
    gateway = status.get("gateway") or {}
    port = gateway.get("public_backend_port")
    if not status.get("running") or not isinstance(port, int):
        raise PortmapError("the shared container infrastructure did not report a running public backend")
    # A cold catalog includes runtime discovery and an agent worktree scan.
    # Use the stack's startup budget, not interactive remote discovery's 3s.
    probe_gateway(f"http://127.0.0.1:{port}", timeout=_START_TIMEOUT, expected_domain=settings.dns_domain)


def _set_public_dns_bindings(state: dict[str, Any], bindings: dict[str, Any] | None) -> dict[str, Any]:
    """Controller hook: recreate only the owned DNS container (or clear the
    public-only view when bindings is None). Provided by the infrastructure
    owner; there is no local fallback."""
    from .infrastructure import set_public_dns_bindings

    return set_public_dns_bindings(state, bindings)


def run_gateway_compose(settings: PortmapSettings, compose_args: list[str]) -> int:
    """Run read-only Compose queries against the generated owned compose file.

    Destructive lifecycle (up/down/restart/kill/rm/...) must go through the
    role functions so ownership and rollback stay enforced; overriding the
    generated project or files is refused.
    """
    if not compose_args:
        raise PortmapError("portmap gateway requires a compose query command")
    if any(token in {"-f", "--file", "-p", "--project-name"} or token.startswith(("--file=", "--project-name=")) for token in compose_args):
        raise PortmapError("portmap gateway queries cannot override the generated compose project or files")
    if compose_args[0] not in _QUERY_COMMANDS:
        raise PortmapError(
            f"portmap gateway only runs read-only queries ({', '.join(_QUERY_COMMANDS)}); use portmap up/down for gateway lifecycle changes"
        )
    state = load_infrastructure(settings.client_state_dir)
    if state is None:
        if load_legacy_record(settings) is not None:
            raise PortmapError("the legacy server stack still owns local publishing; run portmap setup or portmap up to cut it over first")
        raise PortmapError("the shared container infrastructure is not configured; run portmap setup or portmap up first")
    if state.get("phase") != "ready":
        raise PortmapError("the shared container infrastructure has an incomplete transaction; inspect portmap status before querying it")
    runtime = ContainerRuntime.from_record(state["runtime"])
    return runtime.compose(Path(state["instance_dir"]) / "compose.yaml", list(compose_args)).returncode


def _apply_role(settings: PortmapSettings, *, use_sudo: bool) -> dict[str, Any]:
    """Apply or refresh the local-publishing role on the shared infrastructure."""
    access_dir = settings.client_state_dir
    previous = _load(settings)
    if previous is not None:
        saved = _saved_settings(settings, previous)
        immutable = ("http_port", "dns_domain", "runtime_socket", "gateway_network")
        if (any(getattr(saved, key) != getattr(settings, key) for key in immutable)
                or previous["client_state_dir"] != str(access_dir)):
            raise PortmapError(
                "local publishing configuration changed; stop local publishing and teardown the container infrastructure "
                "before changing the runtime, network, domain or HTTP port"
            )
    lan = _effective_lan(settings, previous, None)
    previous_local = _load_client_local(access_dir)
    role_installed = False
    ingress_installed = False
    try:
        role = {**local_role(settings), "http_address": lan["bind_address"] if lan else None}
        status = set_local_server(access_dir, role, settings=settings, use_sudo=use_sudo)
        role_installed = True
        _require_public_backend(status, settings)
        if lan is not None:
            ensure_ingress(
                access_dir, name=_INGRESS_NAME, bind_address=lan["bind_address"], port=lan["port"],
                target_port=status["gateway"]["public_backend_port"], use_sudo=use_sudo,
            )
            # Idempotent for an unchanged entry; only a changed or fresh
            # entry is removed again if a later step fails.
            ingress_installed = previous is None or previous.get("lan") != lan
        if lan is None and previous is not None and previous.get("lan") is not None:
            # An explicit loopback bind withdraws previously published LAN
            # ingress; the wildcard default preserves it via _effective_lan.
            stop_ingress(access_dir, name=_INGRESS_NAME, use_sudo=use_sudo)
        _save(settings, {
            "version": _STATE_VERSION, "uid": os.getuid(),
            "state_dir": str(settings.state_dir.expanduser().resolve()),
            "client_state_dir": str(access_dir), "http_port": settings.http_port,
            "lan": lan, "settings": _settings_record(settings),
        })
        # A healthy role application also settles any retained failure
        # metadata from an earlier interrupted migration attempt.
        clear_migration(settings.state_dir)
    except BaseException as exc:
        failures: list[str] = []
        if ingress_installed:
            try:
                stop_ingress(access_dir, name=_INGRESS_NAME, use_sudo=use_sudo)
            except Exception as cleanup:
                failures.append(f"LAN ingress cleanup failed: {cleanup}")
        if role_installed:
            try:
                set_local_server(access_dir, previous_local, settings=settings, use_sudo=use_sudo)
            except Exception as cleanup:
                failures.append(f"local role restore failed: {cleanup}")
        suffix = f"; cleanup needs attention: {'; '.join(failures)}" if failures else ""
        raise PortmapError(f"local publishing failed: {exc}{suffix}") from exc
    return status


def _finish_legacy_retirement(settings: PortmapSettings, migration: dict[str, Any]) -> dict[str, Any]:
    """Resume irreversible retirement without rolling back the proven new chain."""
    details = migration["details"]
    role_record, capture = details["server"], details["capture"]
    effective = _saved_settings(settings, role_record)
    access_dir = Path(role_record["client_state_dir"])
    state = load_infrastructure(access_dir)
    if state is None or state["owner"] != details["infrastructure_owner"]:
        raise PortmapError("legacy retirement requires the infrastructure that passed the cutover checks")
    _require_public_backend(infrastructure_status(access_dir, check=True), effective)
    if state.get("public_dns") is not None:
        verify_public_dns_bindings(state["public_dns"])
    # Durable role ownership precedes deletion. A partial deletion is retried
    # from the captured IDs, never by bringing an incomplete old stack back up.
    _save(settings, role_record)
    retire_legacy_server(settings, migration["record"], capture, ContainerRuntime.from_record(state["runtime"]))
    clear_migration(settings.state_dir)
    return {"migrated": True, "lan": role_record["lan"], "dns_publish": (state.get("public_dns") or {}).get("publish"),
            "retired": [entry["service"] for entry in capture["containers"].values() if entry]}


def _migrate_legacy(settings: PortmapSettings, *, use_sudo: bool) -> dict[str, Any] | None:
    """Cut a legacy version-1 server stack over to the shared infrastructure.

    Must run with the server lock held and client setup already complete; it
    never calls ``setup_client`` itself. Stages and proves the new backend
    first, then stops the old owned resources, applies the preserved public
    DNS bindings, verifies, and only afterwards removes the old resources.
    Failures before commit restore the old chain. After commit the proven new
    chain stays active while interrupted retirement is resumed from saved IDs.
    """
    pending = load_migration(settings.state_dir)
    if pending is not None and pending["phase"] == "retire":
        return _finish_legacy_retirement(settings, pending)
    record = load_legacy_record(settings)
    if record is None:
        return None
    access_dir = settings.client_state_dir
    runtime = ContainerRuntime.connect(settings)
    capture = capture_legacy_server(settings, record, runtime)
    legacy_dir = Path(record["client_state_dir"])
    phase = "stage"
    role_set = False
    ingress_moved = False
    ingress_installed = False
    containers_stopped = False
    bindings_applied = False
    committed = False
    previous_local = _load_client_local(access_dir)
    legacy_ingress = capture.get("ingress")
    try:
        lan = _effective_lan(settings, None, capture)
        role = {**local_role(settings), "http_address": lan["bind_address"] if lan else None}
        status = set_local_server(access_dir, role, settings=settings, use_sudo=use_sudo)
        role_set = True
        _require_public_backend(status, settings)
        phase = "ingress"
        if legacy_ingress is not None:
            stop_ingress(legacy_dir, name=_INGRESS_NAME, use_sudo=use_sudo)
            ingress_moved = True
        if lan is not None:
            ensure_ingress(
                access_dir, name=_INGRESS_NAME, bind_address=lan["bind_address"], port=lan["port"],
                target_port=status["gateway"]["public_backend_port"], use_sudo=use_sudo,
            )
            ingress_installed = True
        phase = "stop"
        containers_stopped = True
        stop_legacy_stack(capture, runtime)
        phase = "dns"
        bindings = public_dns_bindings(_saved_settings(settings, record), capture)
        if bindings is not None:
            bindings_applied = True
            _set_public_dns_bindings(load_infrastructure(access_dir), bindings)
        phase = "verify"
        if bindings is not None:
            verify_public_dns_bindings(bindings)
        healthy = infrastructure_status(access_dir, check=True)
        if not healthy.get("running"):
            raise PortmapError(f"the shared infrastructure lost readiness after the DNS cutover: {healthy.get('error', 'not running')}")
        role_record = {
            "version": _STATE_VERSION, "uid": os.getuid(),
            "state_dir": str(settings.state_dir.expanduser().resolve()),
            "client_state_dir": str(access_dir), "http_port": settings.http_port,
            "lan": lan, "settings": _settings_record(settings),
        }
        save_migration(settings.state_dir, record=record, phase="retire", error="", details={
            "server": role_record, "capture": capture,
            "infrastructure_owner": load_infrastructure(access_dir)["owner"],
        })
        committed = True
        return _finish_legacy_retirement(settings, load_migration(settings.state_dir))
    except BaseException as exc:
        if committed:
            raise PortmapError(f"the new infrastructure is active; legacy retirement needs a setup retry: {exc}") from exc
        failures: list[str] = []
        dns_released = True
        if bindings_applied:
            try:
                _set_public_dns_bindings(load_infrastructure(access_dir), None)
            except Exception as cleanup:
                dns_released = False
                failures.append(f"public DNS binding rollback failed: {cleanup}")
        if containers_stopped and dns_released:
            try:
                restore_legacy_stack(capture, runtime)
            except Exception as cleanup:
                failures.append(f"legacy stack restore failed: {cleanup}")
        if ingress_installed:
            try:
                stop_ingress(access_dir, name=_INGRESS_NAME, use_sudo=use_sudo)
            except Exception as cleanup:
                failures.append(f"LAN ingress cleanup failed: {cleanup}")
        if ingress_moved and legacy_ingress is not None:
            try:
                ensure_ingress(
                    legacy_dir, name=_INGRESS_NAME, bind_address=legacy_ingress["bind_address"],
                    port=legacy_ingress["port"], target_port=legacy_ingress["target_port"], use_sudo=use_sudo,
                )
            except Exception as cleanup:
                failures.append(f"legacy ingress restore failed: {cleanup}")
        if role_set:
            try:
                set_local_server(access_dir, previous_local, settings=settings, use_sudo=use_sudo)
            except Exception as cleanup:
                failures.append(f"local role restore failed: {cleanup}")
                if previous_local is not None:
                    try:
                        set_local_server(access_dir, None, settings=settings, use_sudo=use_sudo)
                    except Exception as fallback:
                        failures.append(f"local role removal failed: {fallback}")
        try:
            save_migration(settings.state_dir, record=record, phase=phase, error=str(exc), details={
                "stopped": containers_stopped, "bindings_applied": bindings_applied,
            })
        except Exception as cleanup:
            failures.append(f"migration metadata retention failed: {cleanup}")
        suffix = f"; rollback needs attention: {'; '.join(failures)}" if failures else ""
        raise PortmapError(f"legacy server migration failed at {phase}: {exc}{suffix}") from exc


def migrate_legacy_server(settings: PortmapSettings, *, use_sudo: bool = True) -> dict[str, Any] | None:
    """Complete the legacy server cutover once client setup finished.

    Takes the server lock, then the client lock (local role) and the
    infrastructure lock; never calls ``setup_client``. Returns None when no
    legacy version-1 server record exists, otherwise a summary dict for the
    caller's ``legacy_server_migration`` result key. Failures raise after
    restoring the legacy resources and retaining recovery metadata.
    """
    with _server_lock(settings):
        return _migrate_legacy(settings, use_sudo=use_sudo)


def start_server(
    settings: PortmapSettings, compose_args: list[str], *, use_sudo: bool = True,
) -> dict[str, Any]:
    if not compose_args or compose_args[0] != "up":
        raise PortmapError("portmap up accepts gateway compose 'up' arguments; use portmap gateway for queries")
    if not 1 <= settings.http_port <= 65535:
        raise PortmapError(f"invalid HTTP port: {settings.http_port}")
    if "://" in settings.runtime_socket:
        raise PortmapError("local publishing needs a local Docker/Podman socket; use portmap connect for remote machines")
    access_dir = settings.client_state_dir
    # Client setup takes the client lock and may itself run the legacy server
    # migration; it must finish before the server lock is taken.
    setup_client(access_dir, settings=settings, use_sudo=use_sudo, http_port=settings.http_port, activate_host_dns=False)
    with _server_lock(settings):
        if _migrate_legacy(settings, use_sudo=use_sudo) is not None:
            return server_status(settings, check=True)
        _apply_role(settings, use_sudo=use_sudo)
    return server_status(settings, check=True)


def stop_server(
    settings: PortmapSettings, compose_args: list[str], *, use_sudo: bool = True,
) -> dict[str, Any]:
    if not compose_args or compose_args[0] != "down":
        raise PortmapError("portmap down accepts gateway compose 'down' arguments")
    with _server_lock(settings):
        record = _load(settings)
        if record is None:
            if load_legacy_record(settings) is not None:
                raise PortmapError(
                    "local publishing is still owned by the legacy server stack; run portmap setup or portmap up to cut it over to the shared container infrastructure first"
                )
            return {"configured": False, "running": False, "state_dir": str(settings.state_dir)}
        effective = _saved_settings(settings, record)
        access_dir = Path(record["client_state_dir"])
        lan = record.get("lan")
        previous_local = _load_client_local(access_dir)
        lan_removed = False
        role_removed = False
        try:
            if lan is not None:
                stop_ingress(access_dir, name=_INGRESS_NAME, use_sudo=use_sudo)
                lan_removed = True
            set_local_server(access_dir, None, settings=effective, use_sudo=use_sudo)
            role_removed = True
            _record_path(settings).unlink(missing_ok=True)
        except BaseException as exc:
            failures: list[str] = []
            if role_removed:
                try:
                    set_local_server(access_dir, previous_local, settings=effective, use_sudo=use_sudo)
                except Exception as cleanup:
                    failures.append(f"local role restore failed: {cleanup}")
            if lan_removed and lan is not None:
                try:
                    state = load_infrastructure(access_dir)
                    target = (state or {}).get("public_backend_port")
                    if isinstance(target, int):
                        ensure_ingress(
                            access_dir, name=_INGRESS_NAME, bind_address=lan["bind_address"],
                            port=lan["port"], target_port=target, use_sudo=use_sudo,
                        )
                    else:
                        failures.append("LAN ingress restore skipped: the shared infrastructure is not available")
                except Exception as cleanup:
                    failures.append(f"LAN ingress restore failed: {cleanup}")
            suffix = f"; rollback needs attention: {'; '.join(failures)}" if failures else ""
            raise PortmapError(f"local publishing could not be stopped: {exc}{suffix}") from exc
    return {"configured": False, "running": False, "state_dir": str(settings.state_dir)}


def server_status(settings: PortmapSettings, *, check: bool = False) -> dict[str, Any]:
    record = _load(settings)
    if record is None:
        legacy = load_legacy_record(settings)
        if legacy is not None:
            saved = _saved_settings(settings, legacy)
            return {
                "configured": True, "running": None, "state_dir": str(settings.state_dir),
                "migration_required": True,
                "legacy": {"version": 1, "domain": saved.dns_domain, "http_port": saved.http_port},
                "migration": load_migration(settings.state_dir),
            }
        return {"configured": False, "running": False, "state_dir": str(settings.state_dir)}
    effective = _saved_settings(settings, record)
    lan = record.get("lan")
    infra = infrastructure_status(Path(record["client_state_dir"]), check=check)
    result: dict[str, Any] = {
        "configured": True, "version": _STATE_VERSION, "domain": effective.dns_domain,
        "http_port": record["http_port"], "state_dir": str(settings.state_dir),
        "catalog_url": f"http://{effective.dns_domain}" + (f":{record['http_port']}" if record["http_port"] != 80 else "") + "/",
        "public_address": lan["bind_address"] if lan else None,
        "infrastructure": infra,
        "agent": agent_status(effective).as_dict(),
        "lan_ingress": ingress_status(Path(record["client_state_dir"]), name=_INGRESS_NAME) if lan else None,
        "migration": load_migration(settings.state_dir),
    }
    lan_running = not lan or bool(result["lan_ingress"].get("running"))
    if check:
        result["running"] = bool(infra.get("running") and result["agent"]["running"] and lan_running)
    else:
        result["running"] = bool(infra.get("gateway", {}).get("running") and result["agent"]["running"] and lan_running)
    return result
