import hashlib
import json
import os
import secrets
import shutil
import signal
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

import pytest

from portmap import legacy_native
from portmap.errors import PortmapError

SIGTERM = int(signal.SIGTERM)
SIGKILL = int(signal.SIGKILL)


class FakeProcesses:
    """Stand-in for /proc identity, signal delivery, and process exit."""

    def __init__(self):
        self.table: dict[int, dict] = {}
        self.kills: list[tuple[int, int]] = []
        self.exit_on = SIGTERM

    def add(self, pid, *, uid=None, start="100.10", argv=()):
        self.table[pid] = {"uid": uid if uid is not None else os.getuid(), "start": start, "argv": list(argv)}

    def identity(self, pid):
        entry = self.table.get(pid)
        if entry is None:
            return None
        return entry["uid"], entry["start"], list(entry["argv"])

    def kill(self, pid, sig):
        self.kills.append((pid, int(sig)))
        if int(sig) == self.exit_on:
            self.table.pop(pid, None)


@pytest.fixture
def procs(monkeypatch):
    fake = FakeProcesses()
    monkeypatch.setattr(legacy_native, "process_identity", fake.identity)
    monkeypatch.setattr(legacy_native, "ensure_persistent_scope", lambda **kwargs: None)
    monkeypatch.setattr(legacy_native.os, "kill", fake.kill)
    # Force the portable os.kill path unless a test provides pidfd fakes.
    monkeypatch.delattr(legacy_native.os, "pidfd_open", raising=False)
    monkeypatch.delattr(legacy_native.signal, "pidfd_send_signal", raising=False)

    def no_subprocess(*args, **kwargs):
        raise AssertionError("retirement must not run subprocesses or systemctl")

    monkeypatch.setattr("subprocess.run", no_subprocess)
    return fake


def write_dns_state(tmp_path, pid, owner=None):
    path = tmp_path / "dns.json"
    path.write_text(json.dumps({"pid": pid, "owner": owner or secrets.token_hex(16)}), encoding="utf-8")
    path.chmod(0o600)
    return path


def write_gateway_state(
    tmp_path,
    *,
    version=2,
    traefik_pid=101,
    view_pid=102,
    owner=None,
    uid=None,
    ingress=None,
    mismatch_owner_record=False,
):
    owner = owner or secrets.token_hex(16)
    runtime = tmp_path / "gateway" / owner
    (runtime / "dynamic").mkdir(parents=True, exist_ok=True)
    for directory in (runtime.parent, runtime, runtime / "dynamic"):
        directory.chmod(0o700)
    state = {
        "version": version,
        "owner": owner,
        "uid": uid if uid is not None else os.getuid(),
        "state_dir": str(tmp_path),
        "address": "127.0.0.1",
        "http_port": 80,
        "instance_dir": str(runtime),
        "config_path": str(runtime / "traefik.yaml"),
        "dynamic_path": str(runtime / "dynamic" / "routes.yaml"),
        "revision": secrets.token_hex(32),
        "traefik": {"pid": traefik_pid, "start_time": "200.20", "command": ["/opt/traefik", f"--configFile={runtime / 'traefik.yaml'}"]},
        "view": {
            "pid": view_pid,
            "start_time": "300.30",
            "command": [sys.executable, "-m", "portmap.client_view", "--state-dir", str(tmp_path), "--port", "41001", "--owner", owner],
        },
    }
    if version == 2:
        state.update({"listen_port": 41000, "view_port": 41001, "management_port": 41002, "ingress": ingress})
    else:
        state.update({"view_port": 41001, "management_port": 41002})
    record = {key: value for key, value in state.items() if key != "revision"}
    if mismatch_owner_record:
        record = {**record, "revision": state["revision"]}
    owner_json = runtime / "owner.json"
    owner_json.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    owner_json.chmod(0o600)
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path, state


def add_gateway_processes(procs, state):
    procs.add(state["traefik"]["pid"], start=state["traefik"]["start_time"], argv=state["traefik"]["command"])
    procs.add(state["view"]["pid"], start=state["view"]["start_time"], argv=state["view"]["command"])


# --- capture -----------------------------------------------------------------------------


def test_capture_without_legacy_state_is_empty_and_retire_is_a_noop(tmp_path, procs):
    snapshot = legacy_native.capture_native_workers(tmp_path)

    assert snapshot == {"version": 1, "uid": os.getuid(), "state_dir": str(tmp_path), "dns": None, "gateway": None}
    legacy_native.retire_native_workers(tmp_path, snapshot)
    assert procs.kills == []


def test_capture_records_validated_identity_of_dns_and_gateway_workers(tmp_path, procs):
    dns = write_dns_state(tmp_path, 100)
    (tmp_path / "Corefile").write_text(".:1053 {}\n", encoding="utf-8")
    procs.add(100, start="1.5", argv=["/opt/coredns", "-conf", str(tmp_path / "Corefile")])
    _, state = write_gateway_state(tmp_path, ingress={"name": "private", "port": 80, "target_port": 41000})
    add_gateway_processes(procs, state)

    snapshot = legacy_native.capture_native_workers(tmp_path)

    assert snapshot["dns"]["state"] == "owned"
    assert snapshot["dns"]["pid"] == 100
    assert snapshot["dns"]["command"] == ["/opt/coredns", "-conf", str(tmp_path / "Corefile")]
    assert snapshot["dns"]["sha256"] == hashlib.sha256(dns.read_bytes()).hexdigest()
    assert snapshot["gateway"]["version"] == 2
    assert [entry["state"] for entry in snapshot["gateway"]["processes"]] == ["owned", "owned"]
    assert {entry["kind"] for entry in snapshot["gateway"]["processes"]} == {"traefik", "view"}


def test_capture_reports_dead_and_foreign_workers_without_stopping_them(tmp_path, procs):
    write_dns_state(tmp_path, 100)
    procs.add(100, start="1.5", argv=["/opt/coredns", "-conf", "/elsewhere/Corefile"])  # a foreign coredns
    _, state = write_gateway_state(tmp_path, traefik_pid=201, view_pid=102)  # traefik already gone
    procs.add(state["view"]["pid"], start=state["view"]["start_time"], argv=state["view"]["command"])

    snapshot = legacy_native.capture_native_workers(tmp_path)

    assert snapshot["dns"]["state"] == "mismatch"
    by_kind = {entry["kind"]: entry["state"] for entry in snapshot["gateway"]["processes"]}
    assert by_kind == {"traefik": "dead", "view": "owned"}
    assert procs.kills == []


@pytest.mark.parametrize(
    "damage",
    ["dns-pid", "dns-owner", "dns-group-writable"],
)
def test_capture_refuses_invalid_dns_descriptors(tmp_path, procs, damage):
    if damage == "dns-group-writable":
        # A relaxed mode is only recoverable under a provably private owned
        # ancestor; a shared root keeps the unsafe-descriptor refusal.
        root = Path(tempfile.mkdtemp(prefix="portmap-shared-", dir="/tmp"))
        root.chmod(0o755)
    else:
        root = tmp_path
    try:
        if damage == "dns-pid":
            write_dns_state(root, 0)
        elif damage == "dns-owner":
            write_dns_state(root, 100, owner="not-hex")
        else:
            write_dns_state(root, 100).chmod(0o666)
        with pytest.raises(PortmapError, match="legacy client DNS"):
            legacy_native.capture_native_workers(root)
    finally:
        if damage == "dns-group-writable":
            shutil.rmtree(root)


def test_capture_refuses_invalid_gateway_descriptors(tmp_path, procs):
    write_gateway_state(tmp_path, uid=os.getuid() + 4242)
    with pytest.raises(PortmapError, match="Invalid legacy local gateway descriptor"):
        legacy_native.capture_native_workers(tmp_path)

    write_gateway_state(tmp_path, mismatch_owner_record=True)
    with pytest.raises(PortmapError, match="ownership record"):
        legacy_native.capture_native_workers(tmp_path)


# --- retire ---------------------------------------------------------------------------------


def test_retire_stops_only_the_captured_owned_workers(tmp_path, procs):
    write_dns_state(tmp_path, 100)
    procs.add(100, start="1.5", argv=["/opt/coredns", "-conf", str(tmp_path / "Corefile")])
    gateway_path, state = write_gateway_state(tmp_path)
    add_gateway_processes(procs, state)
    procs.add(999, start="9.9", argv=["ssh", "-W"])  # an SSH master, never ours
    snapshot = legacy_native.capture_native_workers(tmp_path)

    legacy_native.retire_native_workers(tmp_path, snapshot)

    assert sorted(procs.kills) == [(100, SIGTERM), (101, SIGTERM), (102, SIGTERM)]
    assert 999 in procs.table  # uncaptured processes stay untouched
    assert not (tmp_path / "dns.json").exists()
    assert not gateway_path.exists()
    # Private instance directories (logs, ownership record) are preserved.
    assert Path(state["instance_dir"]).is_dir()


def test_retire_removes_descriptors_of_dead_workers_without_signaling(tmp_path, procs):
    write_dns_state(tmp_path, 100)
    write_gateway_state(tmp_path, traefik_pid=201, view_pid=202)
    snapshot = legacy_native.capture_native_workers(tmp_path)

    legacy_native.retire_native_workers(tmp_path, snapshot)

    assert procs.kills == []
    assert not (tmp_path / "dns.json").exists()
    assert not (tmp_path / "gateway.json").exists()


def test_retire_never_signals_a_foreign_live_pid_and_keeps_its_descriptor(tmp_path, procs):
    write_dns_state(tmp_path, 100)
    procs.add(100, start="1.5", argv=["/opt/coredns", "-conf", "/elsewhere/Corefile"])
    snapshot = legacy_native.capture_native_workers(tmp_path)

    with pytest.raises(PortmapError, match="does not match the captured identity"):
        legacy_native.retire_native_workers(tmp_path, snapshot)

    assert procs.kills == []
    assert (tmp_path / "dns.json").exists()


def test_retire_retains_a_descriptor_that_changed_since_capture(tmp_path, procs):
    dns = write_dns_state(tmp_path, 100)  # dead worker, but the file changes afterwards
    gateway_path, _ = write_gateway_state(tmp_path, traefik_pid=201, view_pid=202)
    snapshot = legacy_native.capture_native_workers(tmp_path)
    dns.write_text(json.dumps({"pid": 101, "owner": snapshot["dns"]["owner"]}), encoding="utf-8")
    dns.chmod(0o600)

    with pytest.raises(PortmapError, match="changed since capture; retained"):
        legacy_native.retire_native_workers(tmp_path, snapshot)

    assert dns.exists()
    assert not gateway_path.exists()  # the unchanged descriptor was still cleaned


def test_retire_escalates_to_sigkill_and_reports_unstoppable_workers(tmp_path, procs, monkeypatch):
    monkeypatch.setattr(legacy_native, "_STOP_TIMEOUT", 0.05)
    monkeypatch.setattr(legacy_native, "_KILL_TIMEOUT", 0.05)
    write_dns_state(tmp_path, 100)
    procs.add(100, start="1.5", argv=["/opt/coredns", "-conf", str(tmp_path / "Corefile")])
    procs.exit_on = SIGKILL
    snapshot = legacy_native.capture_native_workers(tmp_path)

    legacy_native.retire_native_workers(tmp_path, snapshot)  # SIGTERM then SIGKILL suffices
    assert procs.kills == [(100, SIGTERM), (100, SIGKILL)]
    assert not (tmp_path / "dns.json").exists()

    write_dns_state(tmp_path, 100)
    procs.add(100, start="1.5", argv=["/opt/coredns", "-conf", str(tmp_path / "Corefile")])
    procs.exit_on = None
    snapshot = legacy_native.capture_native_workers(tmp_path)

    with pytest.raises(PortmapError, match="did not exit"):
        legacy_native.retire_native_workers(tmp_path, snapshot)
    assert (tmp_path / "dns.json").exists()


def test_retire_uses_pidfd_signaling_where_supported(tmp_path, procs, monkeypatch):
    sent: list[tuple[int, int]] = []
    closed: list[int] = []

    def send_signal(fd, sig):
        sent.append((fd, int(sig)))
        if int(sig) == procs.exit_on:
            procs.table.pop(fd - 9000, None)

    monkeypatch.setattr(legacy_native.os, "pidfd_open", lambda pid: 9000 + pid, raising=False)
    monkeypatch.setattr(legacy_native.signal, "pidfd_send_signal", send_signal, raising=False)
    monkeypatch.setattr(legacy_native.os, "close", lambda fd: closed.append(fd))
    write_dns_state(tmp_path, 100)
    procs.add(100, start="1.5", argv=["/opt/coredns", "-conf", str(tmp_path / "Corefile")])
    snapshot = legacy_native.capture_native_workers(tmp_path)

    legacy_native.retire_native_workers(tmp_path, snapshot)

    assert sent == [(9100, SIGTERM)]
    assert closed == [9100]
    assert procs.kills == []  # os.kill is never reached when pidfd works


def test_retire_validates_the_snapshot_against_the_state_directory(tmp_path, procs):
    write_dns_state(tmp_path, 100)
    snapshot = legacy_native.capture_native_workers(tmp_path)

    with pytest.raises(PortmapError, match="snapshot does not match"):
        legacy_native.retire_native_workers(tmp_path, {"version": 2, "uid": os.getuid(), "state_dir": str(tmp_path)})
    with pytest.raises(PortmapError, match="snapshot does not match"):
        legacy_native.retire_native_workers(tmp_path / "elsewhere", snapshot)
    assert (tmp_path / "dns.json").exists()


def test_retire_handles_a_legacy_version_one_gateway_descriptor(tmp_path, procs):
    gateway_path, state = write_gateway_state(tmp_path, version=1, traefik_pid=201, view_pid=202)
    procs.add(state["traefik"]["pid"], start=state["traefik"]["start_time"], argv=state["traefik"]["command"])
    procs.add(state["view"]["pid"], start=state["view"]["start_time"], argv=state["view"]["command"])
    snapshot = legacy_native.capture_native_workers(tmp_path)

    assert snapshot["gateway"]["version"] == 1
    legacy_native.retire_native_workers(tmp_path, snapshot)

    assert procs.kills == [(201, SIGTERM), (202, SIGTERM)]
    assert not gateway_path.exists()


@pytest.mark.parametrize("version,port", [(1, 80), (2, 41000)])
def test_direct_http_handover_preserves_dns_view_and_ownership_until_commit(tmp_path, procs, version, port):
    write_dns_state(tmp_path, 100)
    procs.add(100, argv=["/opt/coredns", "-conf", str(tmp_path / "Corefile")])
    path, state = write_gateway_state(tmp_path, version=version)
    add_gateway_processes(procs, state)
    original = path.read_bytes()
    snapshot = legacy_native.capture_native_workers(tmp_path)

    legacy_native.release_native_http(tmp_path, snapshot, port)

    assert procs.kills == [(101, SIGTERM)]
    assert set(procs.table) == {100, 102}
    assert path.read_bytes() == original
    assert (tmp_path / "dns.json").exists()
    legacy_native.retire_native_workers(tmp_path, snapshot)
    assert procs.table == {}
    assert not path.exists()
    assert not (tmp_path / "dns.json").exists()


def test_http_handover_does_not_release_a_different_native_listener(tmp_path, procs):
    path, state = write_gateway_state(tmp_path)
    add_gateway_processes(procs, state)
    original = path.read_bytes()
    snapshot = legacy_native.capture_native_workers(tmp_path)

    legacy_native.release_native_http(tmp_path, snapshot, 18081)
    legacy_native.restore_native_http(tmp_path, snapshot)

    assert procs.kills == []
    assert set(procs.table) == {101, 102}
    assert path.read_bytes() == original


@pytest.mark.parametrize("change", ["descriptor", "reused-pid"])
def test_http_handover_refuses_changed_ownership(tmp_path, procs, change):
    path, state = write_gateway_state(tmp_path)
    add_gateway_processes(procs, state)
    snapshot = legacy_native.capture_native_workers(tmp_path)
    if change == "descriptor":
        path.write_text(json.dumps({**state, "revision": secrets.token_hex(32)}))
    else:
        procs.add(101, uid=os.getuid() + 1, argv=state["traefik"]["command"])
    retained = path.read_bytes()

    with pytest.raises(PortmapError):
        legacy_native.release_native_http(tmp_path, snapshot, 41000)
    legacy_native.restore_native_http(tmp_path, snapshot)

    assert procs.kills == []
    assert path.read_bytes() == retained


def restored_launcher(monkeypatch, procs, state):
    Path(state["config_path"]).write_text("entryPoints: {}\n")
    Path(state["config_path"]).chmod(0o600)

    class Child:
        pid = 501

        def poll(self):
            return None if self.pid in procs.table else 0

        def wait(self, timeout):
            if self.pid in procs.table:
                raise legacy_native.subprocess.TimeoutExpired(["restored-traefik"], timeout)
            return 0

        def terminate(self):
            procs.kill(self.pid, SIGTERM)

        def kill(self):
            procs.kill(self.pid, SIGKILL)

    def start(command, **kwargs):
        procs.add(501, start="500.50", argv=command)
        return Child()

    monkeypatch.setattr(legacy_native.subprocess, "Popen", start)
    monkeypatch.setattr(legacy_native.socket, "create_connection", lambda *args, **kwargs: nullcontext())


def test_interrupted_http_release_restores_controllable_native_process(tmp_path, procs, monkeypatch):
    path, state = write_gateway_state(tmp_path)
    add_gateway_processes(procs, state)
    restored_launcher(monkeypatch, procs, state)
    snapshot = legacy_native.capture_native_workers(tmp_path)
    stop = legacy_native._stop_captured

    def interrupt(entry):
        stop(entry)
        raise KeyboardInterrupt

    monkeypatch.setattr(legacy_native, "_stop_captured", interrupt)
    with pytest.raises(KeyboardInterrupt):
        legacy_native.release_native_http(tmp_path, snapshot, 41000)
    monkeypatch.setattr(legacy_native, "_stop_captured", stop)

    legacy_native.restore_native_http(tmp_path, snapshot)

    restored = json.loads(path.read_text())
    assert restored["traefik"]["pid"] == 501
    assert restored["view"] == state["view"]
    assert set(procs.table) == {102, 501}
    new_snapshot = legacy_native.capture_native_workers(tmp_path)
    assert all(entry["state"] == "owned" for entry in new_snapshot["gateway"]["processes"])
    legacy_native.retire_native_workers(tmp_path, new_snapshot)
    assert procs.table == {}
    assert not path.exists()


def test_interrupted_pending_stop_is_settled_before_http_restoration(tmp_path, procs, monkeypatch):
    path, state = write_gateway_state(tmp_path)
    add_gateway_processes(procs, state)
    restored_launcher(monkeypatch, procs, state)
    snapshot = legacy_native.capture_native_workers(tmp_path)
    stop = legacy_native._stop_captured
    procs.exit_on = None

    def pending_stop(entry):
        legacy_native._signal_captured(entry, SIGTERM)
        raise KeyboardInterrupt

    monkeypatch.setattr(legacy_native, "_stop_captured", pending_stop)
    with pytest.raises(KeyboardInterrupt):
        legacy_native.release_native_http(tmp_path, snapshot, 41000)
    assert 101 in procs.table
    monkeypatch.setattr(legacy_native, "_stop_captured", stop)
    procs.exit_on = SIGTERM

    legacy_native.restore_native_http(tmp_path, snapshot)

    assert set(procs.table) == {102, 501}
    assert json.loads(path.read_text())["traefik"]["pid"] == 501
    assert all(entry["state"] == "owned" for entry in legacy_native.capture_native_workers(tmp_path)["gateway"]["processes"])


def test_restore_identity_interruption_kills_unrecorded_child(tmp_path, procs, monkeypatch):
    path, state = write_gateway_state(tmp_path)
    add_gateway_processes(procs, state)
    restored_launcher(monkeypatch, procs, state)
    original = path.read_bytes()
    snapshot = legacy_native.capture_native_workers(tmp_path)
    legacy_native.release_native_http(tmp_path, snapshot, 41000)
    identity = procs.identity
    interruption = KeyboardInterrupt()

    def interrupted_identity(pid):
        if pid == 501:
            raise interruption
        return identity(pid)

    procs.exit_on = SIGKILL
    monkeypatch.setattr(legacy_native, "process_identity", interrupted_identity)
    with pytest.raises(KeyboardInterrupt) as caught:
        legacy_native.restore_native_http(tmp_path, snapshot)

    assert caught.value is interruption
    assert set(procs.table) == {102}
    assert path.read_bytes() == original


@pytest.mark.parametrize("after_owner", [False, True])
def test_failed_http_restore_keeps_matching_legacy_ownership_records(tmp_path, procs, monkeypatch, after_owner):
    path, state = write_gateway_state(tmp_path)
    add_gateway_processes(procs, state)
    restored_launcher(monkeypatch, procs, state)
    original = path.read_bytes()
    owner = Path(state["instance_dir"]) / "owner.json"
    original_owner = owner.read_bytes()
    snapshot = legacy_native.capture_native_workers(tmp_path)
    legacy_native.release_native_http(tmp_path, snapshot, 41000)
    atomic = legacy_native._atomic_bytes
    failure = KeyboardInterrupt() if after_owner else OSError("cannot publish restored gateway descriptor")

    def fail_descriptor(target, content):
        if target == path and not after_owner:
            raise failure
        atomic(target, content)
        if after_owner and target == owner and json.loads(content)["traefik"]["pid"] == 501:
            raise failure

    monkeypatch.setattr(legacy_native, "_atomic_bytes", fail_descriptor)
    with pytest.raises(type(failure)) as caught:
        legacy_native.restore_native_http(tmp_path, snapshot)

    assert caught.value is failure
    assert set(procs.table) == {102}
    assert path.read_bytes() == original
    assert owner.read_bytes() == original_owner
    captured = legacy_native.capture_native_workers(tmp_path)
    assert {entry["kind"]: entry["state"] for entry in captured["gateway"]["processes"]} == {"traefik": "dead", "view": "owned"}
