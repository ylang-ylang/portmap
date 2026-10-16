"""Volume-reset permission recovery behind the shared state gates.

A persistent-home restore re-applies group access to state portmap created
owner-only: private directories come back 2770/2775 and records 0660. The
gates must recover them, with content intact, whenever the chain is provably
protected by a private owned ancestor (the real rebuilt layout: HOME stays
0700/2700, everything below was relaxed, no symlinks), and must keep failing
closed for shared roots, symlinks, foreign owners and owner bits a reset
stripped.
"""
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from portmap import (
    agent,
    client,
    connection_config,
    infrastructure,
    ingress,
    legacy_native,
    legacy_server,
    lifecycle,
    permissions,
    runtime_ingress,
    server,
)
from portmap.errors import PortmapError
from portmap.infrastructure_config import IMAGES
from portmap.settings import load_portmap_settings


@contextmanager
def restored_home(tmp_path, *, home_mode=0o700):
    """A private home whose volume restore relaxed every level below it."""
    home = tmp_path / "home"
    home.mkdir(mode=home_mode)
    home.chmod(home_mode)
    chain = home / ".local" / "state" / "portmap"
    chain.mkdir(parents=True, mode=0o700)
    for directory in (home / ".local", home / ".local" / "state", chain):
        directory.chmod(0o2770)
    yield home, chain


@contextmanager
def shared_root():
    """A shared-root layout with no private owned ancestor above the state."""
    root = Path(tempfile.mkdtemp(prefix="portmap-shared-", dir="/tmp"))
    root.chmod(0o755)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def home_settings(home: Path, chain: Path):
    return load_portmap_settings(environ={
        "PORTMAP_ROOT": str(home),
        "PORTMAP_STATE_DIR": str(chain),
        "PORTMAP_AGENT_RUNTIME_DIR": str(home / "agent"),
        "PORTMAP_HOST_IP": "192.0.2.10",
    })


# --- recovery under a private home --------------------------------------------------------


@pytest.mark.parametrize("home_mode", [0o700, 0o2700])
def test_client_setup_gate_recovers_relaxed_client_state(tmp_path, home_mode):
    with restored_home(tmp_path, home_mode=home_mode) as (home, chain):
        state = chain / "client"
        state.mkdir(mode=0o700)
        profile = {"domain": "remote.portmap", "via": "direct", "target": "192.0.2.10"}
        with client.client_lock(state):
            client._save(state, {"remote.portmap": profile})
        payload = (state / "connections.json").read_bytes()
        state.chmod(0o2770)
        (state / "connections.json").chmod(0o0660)

        with client.client_lock(state):
            assert client._load(state)["remote.portmap"]["domain"] == "remote.portmap"

        assert (state / "connections.json").read_bytes() == payload
        mode = stat.S_IMODE(state.stat().st_mode)
        assert mode & 0o777 == 0o700
        assert mode & ~0o777 in (0, 0o2000)  # setgid kept when the kernel allows it
        with client.client_lock(state):
            pass  # idempotent: the recovered state keeps passing
        assert stat.S_IMODE(state.stat().st_mode) == mode


@pytest.mark.parametrize("gate", ["setup-client", "run", "up-server"])
def test_setup_run_up_directory_gates_share_the_recovery(tmp_path, gate):
    with restored_home(tmp_path) as (home, chain):
        directory = chain / ("client" if gate != "up-server" else "server")
        directory.mkdir(mode=0o700)
        directory.chmod(0o2770)
        if gate == "setup-client":
            with client.client_lock(directory):
                pass
        elif gate == "run":
            lifecycle._private_state_directory(directory)
        else:
            with server._server_lock(home_settings(home, chain)):
                pass
        assert directory.stat().st_mode & 0o777 == 0o700


def test_up_role_recovers_the_server_record(tmp_path):
    with restored_home(tmp_path) as (home, chain):
        client_dir = chain / "client"
        client_dir.mkdir(mode=0o700)
        settings = home_settings(home, chain)
        record = {
            "version": 2,
            "uid": os.getuid(),
            "state_dir": str(chain.resolve()),
            "client_state_dir": str(client_dir),
            "http_port": 80,
            "lan": None,
            "settings": server._settings_record(settings),
        }
        server_dir = chain / "server"
        server_dir.mkdir(mode=0o700)
        record_path = server_dir / "instance.json"
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        server_dir.chmod(0o2770)
        record_path.chmod(0o0660)

        with server._server_lock(settings):
            loaded = server._load(settings)

        assert loaded["http_port"] == 80
        assert server_dir.stat().st_mode & 0o777 == 0o700
        assert stat.S_IMODE(record_path.stat().st_mode) & 0o077 == 0
        assert json.loads(record_path.read_text(encoding="utf-8")) == record


def test_setup_read_path_recovers_the_infrastructure_record(tmp_path):
    with restored_home(tmp_path) as (home, chain):
        state = chain / "client"
        state.mkdir(mode=0o700)
        owner = "a" * 32
        instance = state / "infrastructure" / owner
        instance.mkdir(parents=True, mode=0o700)
        record = {
            "version": 1,
            "uid": os.getuid(),
            "phase": "ready",
            "owner": owner,
            "state_dir": str(state),
            "instance_dir": str(instance),
            "runtime": {"backend": "docker", "socket_path": "/test/docker.sock", "rootless": False},
            "http_port": 80,
            "listen_port": 18080,
            "public_backend_port": 18081,
            "management_port": 18082,
            "dns_port": 1053,
            "names": {role: f"test-{role}" for role in IMAGES},
            "containers": {},
        }
        record_path = state / "infrastructure.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        state.chmod(0o2775)
        instance.chmod(0o2770)
        record_path.chmod(0o0660)

        infrastructure._private_directory(state)  # the setup lock gate
        loaded = infrastructure.load_infrastructure(state)

        assert loaded["owner"] == owner
        assert state.stat().st_mode & 0o777 == 0o700
        assert instance.stat().st_mode & 0o777 == 0o700
        assert stat.S_IMODE(record_path.stat().st_mode) & 0o077 == 0
        assert json.loads(record_path.read_text(encoding="utf-8")) == record


def test_portal_mutation_lock_recovers_its_exact_mode(tmp_path):
    with restored_home(tmp_path) as (home, chain):
        state = chain / "client"
        state.mkdir(mode=0o700)
        lock = state / "connection-operations.lock"
        lock.write_text("", encoding="utf-8")
        lock.chmod(0o0660)
        mutate = connection_config._serialized_mutation(lambda state_dir: "done")

        assert mutate(state) == "done"
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        assert mutate(state) == "done"  # idempotent on the recovered lock


def test_ingress_record_and_container_config_recover(tmp_path):
    with restored_home(tmp_path) as (home, chain):
        state = chain / "client"
        state.mkdir(mode=0o700)
        ingress_dir = state / "ingress"
        ingress_dir.mkdir(mode=0o700)
        record_path = ingress_dir / "public.json"
        record_path.write_text(json.dumps({"version": 1, "backend": "systemd"}), encoding="utf-8")
        record_path.chmod(0o0660)

        assert ingress._load_state(state, "public") == {"version": 1, "backend": "systemd"}
        assert stat.S_IMODE(record_path.stat().st_mode) == 0o600

        config = ingress_dir / "public.cfg"
        config.write_text("previous configuration\n", encoding="utf-8")
        config.chmod(0o0660)
        runtime_ingress._write_config(config, "replacement configuration\n")
        assert config.read_text(encoding="utf-8") == "replacement configuration\n"
        assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_legacy_state_recovers_with_minimal_tightening(tmp_path):
    with restored_home(tmp_path) as (home, chain):
        legacy = home / ".local" / "state" / "portmap-client"
        legacy.mkdir(mode=0o700)
        legacy.chmod(0o2770)
        dns = legacy / "dns.json"
        dns.write_text(json.dumps({"pid": 100, "owner": "a" * 32}), encoding="utf-8")
        dns.chmod(0o0660)
        legacy_native._safe_descriptor_file(dns)
        # Only the write bits are stripped; group-read is not relaxed further.
        assert stat.S_IMODE(dns.stat().st_mode) & 0o777 == 0o640

        gateway = legacy / "gateway" / "ownertoken"
        (gateway / "dynamic").mkdir(parents=True, mode=0o700)
        record = gateway / "gateway.json"
        record.write_text("{}", encoding="utf-8")
        record.chmod(0o0660)
        for directory in (legacy / "gateway", gateway):
            directory.chmod(0o2775)

        legacy_native._private_dir(gateway)
        assert stat.S_IMODE(gateway.stat().st_mode) & 0o077 == 0
        assert legacy_native._read_bytes(record) == b"{}"
        assert stat.S_IMODE(record.stat().st_mode) & 0o077 == 0


def test_legacy_server_record_recovers(tmp_path):
    with restored_home(tmp_path) as (home, chain):
        settings = home_settings(home, chain)
        server_dir = chain / "server"
        server_dir.mkdir(mode=0o700)
        server_dir.chmod(0o2770)
        record = {
            "version": 1,
            "uid": os.getuid(),
            "state_dir": str(chain.resolve()),
            "backend_port": 47123,
            "client_state_dir": str(chain / "client"),
            "settings": {"root": str(home)},
        }
        record_path = server_dir / "instance.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        record_path.chmod(0o0660)

        assert legacy_server.load_legacy_record(settings) == record
        assert stat.S_IMODE(record_path.stat().st_mode) & 0o077 == 0


# --- fail-closed without a provable ancestor -----------------------------------------------


def test_shared_root_directory_stays_refused():
    with shared_root() as root:
        directory = root / "client"
        directory.mkdir()
        directory.chmod(0o2770)
        saved = directory / "connections.json"
        saved.write_text("existing private data\n", encoding="utf-8")
        with pytest.raises(PortmapError, match="owned private directory"):
            with client.client_lock(directory):
                pytest.fail("unprovable state must not be opened")
        assert saved.read_text(encoding="utf-8") == "existing private data\n"
        assert directory.stat().st_mode & 0o7777 == 0o2770
        assert not (directory / "client.lock").exists()


def test_shared_root_file_stays_refused():
    with shared_root() as root:
        record = root / "instance.json"
        record.write_text("{}", encoding="utf-8")
        record.chmod(0o0660)
        with pytest.raises(PortmapError, match="unsafe server state file"):
            permissions.ensure_private_file(record, message=f"unsafe server state file: {record}")
        assert record.read_text(encoding="utf-8") == "{}"
        assert record.stat().st_mode & 0o7777 == 0o0660


@pytest.mark.parametrize("home_mode", [0o700, 0o2700])
def test_symlinked_chain_stays_refused(tmp_path, home_mode):
    home = tmp_path / "home"
    home.mkdir(mode=home_mode)
    real = home / "real-state"
    real.mkdir(mode=0o700)
    link = home / "linked-state"
    link.symlink_to(real)
    directory = link / "client"
    directory.mkdir(mode=0o700)
    directory.chmod(0o2770)
    with pytest.raises(PortmapError, match="owned private directory"):
        with client.client_lock(directory):
            pytest.fail("a symlinked hop must not be trusted")
    assert directory.stat().st_mode & 0o777 == 0o770


def test_symlinked_target_stays_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    real = tmp_path / "real-client"
    real.mkdir(mode=0o700)
    target = home / "client"
    target.symlink_to(real)
    with pytest.raises(PortmapError, match="owned private directory"):
        with client.client_lock(target):
            pytest.fail("a symlinked state directory must not be opened")
    assert target.is_symlink()


def test_foreign_paths_stay_refused():
    with pytest.raises(PortmapError, match="unsafe test record"):
        permissions.ensure_private_file(Path("/etc/hosts"), message="unsafe test record")
    with pytest.raises(PortmapError, match="owned private directory"):
        with client.client_lock(Path("/etc")):
            pytest.fail("a foreign directory must not be adopted")


def test_owner_stripped_paths_are_not_regranted(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    directory = home / "client"
    directory.mkdir(mode=0o700)
    directory.chmod(0o0170)
    with pytest.raises(PortmapError, match="owned private directory"):
        with client.client_lock(directory):
            pytest.fail("owner access must not be re-granted")
    assert directory.stat().st_mode & 0o7777 == 0o0170
    record = home / "record.json"
    record.write_text("data", encoding="utf-8")
    record.chmod(0o0400)
    with pytest.raises(PortmapError, match="unsafe test record"):
        permissions.ensure_private_file(record, message="unsafe test record", force=0o600)
    assert record.stat().st_mode & 0o7777 == 0o0400


# --- idempotent compliant paths ------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o700, 0o2700])
def test_compliant_directories_pass_untouched(tmp_path, mode):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    directory = home / "client"
    directory.mkdir(mode=0o700)
    directory.chmod(mode)
    with client.client_lock(directory):
        pass
    lifecycle._private_state_directory(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == mode


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700])
def test_compliant_files_pass_untouched(tmp_path, mode):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    record = home / "record.json"
    record.write_text("data", encoding="utf-8")
    record.chmod(mode)
    permissions.ensure_private_file(record, message="unsafe test record")
    assert stat.S_IMODE(record.stat().st_mode) == mode


def test_persisted_agent_metadata_recovers_without_changing_identity(tmp_path):
    with restored_home(tmp_path) as (home, chain):
        settings = home_settings(home, chain)
        runtime = settings.agent_runtime_dir
        runtime.mkdir(mode=0o700)
        pid = os.getpid()
        identity = agent.process_identity(pid)
        uid, started, command = identity
        settings.agent_pid_file.write_text(str(pid))
        record = runtime / "agent-owner.json"
        record.write_text(json.dumps({"version": 1, "uid": uid, "pid": pid,
                                     "start_time": started, "command": command,
                                     "socket": str(settings.agent_socket)}))
        before = record.read_bytes()
        runtime.chmod(0o2770)
        settings.agent_pid_file.chmod(0o660)
        record.chmod(0o660)
        agent._private_agent_directory(runtime)
        assert agent.read_pid(settings.agent_pid_file) == pid
        assert agent._owned_agent_identity(settings, pid) == identity
        assert record.read_bytes() == before
        assert stat.S_IMODE(record.stat().st_mode) == 0o600


def test_recovery_rejects_a_descriptor_for_a_replaced_path(tmp_path):
    with restored_home(tmp_path) as (home, chain):
        record = chain / "record.json"
        record.write_bytes(b"current record")
        record.chmod(0o660)
        other = tmp_path / "other.json"
        other.write_bytes(b"unrelated record")
        other.chmod(0o660)
        fd = os.open(other, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with pytest.raises(PortmapError):
                permissions.tighten_private_descriptor(record, fd, message="changed identity")
        finally:
            os.close(fd)
        assert stat.S_IMODE(other.stat().st_mode) == 0o660
        assert stat.S_IMODE(record.stat().st_mode) == 0o660
        assert other.read_bytes() == b"unrelated record"


def test_recovery_refuses_a_multiply_linked_record(tmp_path):
    with restored_home(tmp_path) as (home, chain):
        record = chain / "record.json"
        record.write_bytes(b"shared inode")
        record.chmod(0o660)
        alias = tmp_path / "alias.json"
        os.link(record, alias)
        with pytest.raises(PortmapError):
            permissions.ensure_private_file(record, message="shared record")
        assert record.stat().st_nlink == 2
        assert stat.S_IMODE(alias.stat().st_mode) == 0o660
        assert alias.read_bytes() == b"shared inode"


def test_persisted_agent_runtime_refuses_unrelated_entries(tmp_path):
    with restored_home(tmp_path) as (home, chain):
        runtime = home / "agent"
        runtime.mkdir(mode=0o700)
        unrelated = runtime / "user-data"
        unrelated.write_bytes(b"keep")
        runtime.chmod(0o2770)
        with pytest.raises(PortmapError):
            agent._private_agent_directory(runtime)
        assert stat.S_IMODE(runtime.stat().st_mode) == 0o2770
        assert unrelated.read_bytes() == b"keep"


def test_parent_traversal_cannot_supply_a_false_private_ancestor():
    with shared_root() as root:
        private = root / "private"
        private.mkdir(mode=0o700)
        exposed = root / "exposed"
        exposed.mkdir(mode=0o770)
        exposed.chmod(0o770)
        escaped = private / ".." / "exposed"
        with pytest.raises(PortmapError):
            permissions.ensure_private_directory(escaped, message="escaped private ancestor", create=False)
        assert stat.S_IMODE(exposed.stat().st_mode) == 0o770
