"""Consumer-visible behavior of portmap.runtime_compat.prepare_runtime.

Each test drives a real ``podman system service`` stand-in: a genuine unix
HTTP socket owned by a genuine process whose /proc identity, environment and
working directory the module must verify, preserve and (only after proof)
restart. Helper downloads are pinned to local fixture bytes with their real
SHA-256 digests so the checksum path is exercised without network access.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import signal
import subprocess
import textwrap
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import runtime_compat
from portmap.container_runtime import ContainerRuntime
from portmap.errors import PortmapError
from portmap.settings import socket_alive

HELPER_VERSION = runtime_compat.HELPER_VERSION

# Mirrors the startup-generated CONTAINERS_CONF the real workspace layout
# inherits as-is; the repair must leave it byte for byte untouched.
DEFAULT_BASE_CONFIG = (
    '[containers]\ncgroups = "enabled"\npidns = "host"\nvolumes = ["/proc:/proc:rw"]\n'
    '\n[engine]\nruntime = "crun"\ncgroup_manager = "cgroupfs"\n'
    '\n[engine.runtimes]\ncrun = ["/usr/bin/crun"]\nrunc = ["/usr/sbin/runc"]\n'
)

FOREIGN_CONTAINER = {
    "Id": "75c3f1a13624ee79d5f182ec972fac55dcad1093f1c93d29cca632deb0047839",
    "Names": ["ylang-container-t-1"],
    "State": "created",
    "Labels": {"com.docker.compose.project": "ylang-container"},
}


def _helper_archive(name: str) -> bytes:
    return gzip.compress(f"#!/bin/sh\necho '{name} {HELPER_VERSION}'\n".encode())


NETAVARK_ARCHIVE = _helper_archive("netavark")
AARDVARK_ARCHIVE = _helper_archive("aardvark-dns")
NETAVARK_FIXTURE_SHA = hashlib.sha256(NETAVARK_ARCHIVE).hexdigest()
AARDVARK_FIXTURE_SHA = hashlib.sha256(AARDVARK_ARCHIVE).hexdigest()

STUB_SOURCE = textwrap.dedent(
    """
    #!/usr/bin/env python3
    import json, os, signal, socket, sys, tomllib
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def read(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def native_info():
        payload = read(os.environ["STUB_OLD_INFO"])
        override = os.environ.get("CONTAINERS_CONF_OVERRIDE")
        if override and os.path.exists(override) and not os.environ.get("STUB_IGNORE_OVERRIDE"):
            # Mirror podman's loader: the override file always wins and its
            # first helper directory selects the network helpers.
            with open(override, "rb") as handle:
                helpers = tomllib.load(handle)["engine"]["helper_binaries_dir"]
            payload["host"]["networkBackendInfo"] = {
                "backend": "netavark",
                "version": "netavark %s" % os.environ["STUB_FIXED_VERSION"],
                "path": helpers[0] + "/netavark",
                "dns": {
                    "version": "aardvark-dns %s" % os.environ["STUB_FIXED_VERSION"],
                    "path": helpers[0] + "/aardvark-dns",
                },
            }
        return payload

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/_ping":
                self._reply(b"OK", "text/plain")
            elif self.path == "/info":
                self._reply(json.dumps(read(os.environ["STUB_DOCKER_INFO"])).encode())
            elif self.path.startswith("/v4.0.0/libpod/info"):
                self._reply(json.dumps(native_info()).encode())
            elif self.path.startswith("/containers/json"):
                self._reply(json.dumps(read(os.environ["STUB_CONTAINERS"])).encode())
            else:
                self._reply(b'{"message": "not found"}')

        def _reply(self, body, kind="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_UNIX
        daemon_threads = True

        def server_bind(self):
            if os.path.exists(self.server_address):
                os.unlink(self.server_address)
            self.socket.bind(self.server_address)
            self.server_name = "podman-stub"
            self.server_port = 0

    def main():
        url = sys.argv[4]
        assert url.startswith("unix://"), url
        signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
        Server(url[len("unix://"):], Handler).serve_forever()

    if __name__ == "__main__":
        main()
    """
).lstrip("\n")


def old_native_info(
    *,
    netavark_version="netavark 1.4.0",
    aardvark_version="aardvark-dns 1.4.0",
    netavark_path="/usr/lib/podman/netavark",
    aardvark_path="/usr/lib/podman/aardvark-dns",
) -> dict:
    return {
        "version": {"Version": "4.9.3", "APIVersion": "4.9.3"},
        "host": {
            "arch": "amd64",
            "os": "linux",
            "security": {"rootless": True, "apparmorEnabled": False, "seccompEnabled": True, "selinuxEnabled": False},
            "networkBackend": "netavark",
            "networkBackendInfo": {
                "backend": "netavark",
                "version": netavark_version,
                "package": "netavark_1.4.0-4_amd64",
                "path": netavark_path,
                "dns": {"version": aardvark_version, "package": "aardvark-dns_1.4.0-5_amd64", "path": aardvark_path},
            },
        },
    }


def write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def start_podman_stub(
    root: Path,
    *,
    native=None,
    containers=None,
    base_config=None,
    argv=None,
    env_extra=None,
    socket_name="podman.sock",
    cwd=None,
) -> SimpleNamespace:
    home = root / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".local" / "share").mkdir(parents=True)
    runtime_dir = root / "run"
    socket_dir = runtime_dir / ("podman" if socket_name == "podman.sock" else "engine")
    socket_dir.mkdir(parents=True)
    socket_path = socket_dir / socket_name
    bin_dir = root / "bin"
    bin_dir.mkdir()
    podman_bin = bin_dir / "podman"
    podman_bin.write_text(STUB_SOURCE, encoding="utf-8")
    podman_bin.chmod(0o755)

    base_conf = home / "containers-base.conf"
    base_conf.write_text(base_config if base_config is not None else DEFAULT_BASE_CONFIG, encoding="utf-8")
    write_json(root / "old-info.json", native or old_native_info())
    write_json(root / "containers.json", containers if containers is not None else [FOREIGN_CONTAINER])
    write_json(
        root / "docker-info.json",
        {"ID": "stub", "ServerVersion": "4.9.3", "SecurityOptions": [], "OperatingSystem": "Ubuntu 24.04"},
    )
    url = f"unix://{socket_path}"
    argv = argv or [str(podman_bin), "system", "service", "--time=0", url]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "CONTAINERS_CONF": str(base_conf),
        "STUB_OLD_INFO": str(root / "old-info.json"),
        "STUB_CONTAINERS": str(root / "containers.json"),
        "STUB_DOCKER_INFO": str(root / "docker-info.json"),
        "STUB_FIXED_VERSION": HELPER_VERSION,
        "PORTMAP_TEST_SECRET": "s3cret-value",
    }
    env.update(env_extra or {})
    process = subprocess.Popen(
        argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"podman stub exited with status {process.returncode}")
        if socket_alive(str(socket_path)):
            break
        time.sleep(0.05)
    else:
        process.kill()
        raise RuntimeError("podman stub never served its socket")
    return SimpleNamespace(
        process=process,
        argv=argv,
        env=env,
        socket_path=str(socket_path),
        url=url,
        home=home,
        config_home=home / ".config",
        data_home=home / ".local" / "share",
        runtime_dir=runtime_dir,
        podman_bin=podman_bin,
        base_conf=base_conf,
    )


def wire_deployment(monkeypatch, stub: SimpleNamespace, *, netavark_sha=None) -> list[str]:
    monkeypatch.setenv("HOME", str(stub.home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(stub.runtime_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(stub.config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(stub.data_home))
    for name in ("CONTAINERS_CONF", "CONTAINERS_CONF_OVERRIDE"):
        monkeypatch.delenv(name, raising=False)
    downloads: list[str] = []

    def recording_fetch(url: str) -> bytes:
        downloads.append(url)
        return NETAVARK_ARCHIVE if "netavark" in url else AARDVARK_ARCHIVE

    real_which = shutil.which
    monkeypatch.setattr(runtime_compat.shutil, "which", lambda name: None if name == "systemctl" else real_which(name))
    monkeypatch.setattr(runtime_compat, "_fetch", recording_fetch)
    monkeypatch.setattr(runtime_compat, "NETAVARK_SHA256", netavark_sha or NETAVARK_FIXTURE_SHA)
    monkeypatch.setattr(runtime_compat, "AARDVARK_SHA256", AARDVARK_FIXTURE_SHA)
    return downloads


def podman_runtime(stub: SimpleNamespace, *, backend: str = "podman", rootless: bool = True) -> ContainerRuntime:
    return ContainerRuntime(backend, stub.socket_path, rootless)


def helpers_dir(stub: SimpleNamespace) -> Path:
    return stub.data_home / "portmap-deploy" / "podman-helpers" / HELPER_VERSION


def drop_in_path(stub: SimpleNamespace) -> Path:
    return stub.config_home / "containers" / "containers.conf.d" / runtime_compat.DROP_IN_NAME


def override_path(stub: SimpleNamespace) -> Path:
    return helpers_dir(stub).parent / runtime_compat.OVERRIDE_NAME


def expected_search(stub: SimpleNamespace) -> list[str]:
    return [str(helpers_dir(stub)), "/usr/local/libexec/podman", "/usr/libexec/podman", "/usr/local/lib/podman", "/usr/lib/podman"]


def fetch_native(socket_path: str) -> dict:
    return ContainerRuntime("podman", socket_path, True).request("GET", "/v4.0.0/libpod/info")


def service_pids(socket_path: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if socket_path.encode() in cmdline and b"system\x00service" in cmdline:
            found.append(int(entry.name))
    return found


def process_environ(pid: int) -> dict:
    environ = {}
    for part in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if b"=" in part:
            key, value = part.split(b"=", 1)
            environ[key.decode()] = value.decode()
    return environ


def _pid_gone(pid: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return True
    fields = text[text.rfind(")") + 1 :].split()
    return not fields or fields[0] == "Z"


def stop_services(*pids: int) -> None:
    for pid in pids:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if _pid_gone(pid):
                    break
                time.sleep(0.05)
            if _pid_gone(pid):
                break


def reap(process: subprocess.Popen) -> None:
    process.poll()


def install_fixture_helpers(stub: SimpleNamespace) -> None:
    """Helper binaries a previous repair already staged (no downloads needed)."""
    directory = helpers_dir(stub)
    directory.mkdir(parents=True)
    for name, archive in ((runtime_compat.NETAVARK_NAME, NETAVARK_ARCHIVE), (runtime_compat.AARDVARK_NAME, AARDVARK_ARCHIVE)):
        target = directory / name
        target.write_bytes(gzip.decompress(archive))
        target.chmod(0o755)


def test_repairs_old_rootless_netavark_and_preserves_the_service(tmp_path, monkeypatch):
    stub = start_podman_stub(tmp_path)
    downloads = wire_deployment(monkeypatch, stub)
    try:
        result = runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")

        assert result["backend"] == "podman"
        assert result["socket_path"] == stub.socket_path
        assert result["rootless"] is True
        assert result["changed"] is True
        assert sorted(downloads) == sorted([runtime_compat.NETAVARK_URL, runtime_compat.AARDVARK_URL])

        repair = result["repair"]
        assert repair["api_restart"]["previous_pid"] == stub.process.pid
        replacement_pid = repair["api_restart"]["replacement_pid"]
        assert replacement_pid != stub.process.pid
        assert Path(f"/proc/{replacement_pid}").exists()
        assert stub.process.poll() is not None  # only the verified original exited

        # The repaired selection is proven by the selected service itself.
        assert result["netavark"] == {"path": str(helpers_dir(stub) / "netavark"), "version": f"netavark {HELPER_VERSION}"}
        assert result["aardvark"]["path"] == str(helpers_dir(stub) / "aardvark-dns")
        native = fetch_native(stub.socket_path)
        assert native["host"]["networkBackendInfo"]["version"] == f"netavark {HELPER_VERSION}"

        # Owned config keeps the distro helper search paths.
        drop_in = tomllib.loads(drop_in_path(stub).read_text())
        search = drop_in["engine"]["helper_binaries_dir"]
        assert search[0] == str(helpers_dir(stub))
        assert "/usr/libexec/podman" in search and "/usr/lib/podman" in search
        override = tomllib.loads(override_path(stub).read_text())
        assert override["engine"]["helper_binaries_dir"] == search

        # The replacement preserves the base config env and adds only the owned override.
        environ = process_environ(replacement_pid)
        assert environ["CONTAINERS_CONF"] == str(stub.base_conf)
        assert environ["CONTAINERS_CONF_OVERRIDE"] == str(override_path(stub))
        assert environ["PORTMAP_TEST_SECRET"] == "s3cret-value"

        # The startup-generated base config is inherited as-is, byte for byte.
        assert stub.base_conf.read_text() == DEFAULT_BASE_CONFIG

        # The receipt records owned metadata only, never the inherited environment.
        receipt_text = (tmp_path / "state" / runtime_compat.RECEIPT_NAME).read_text()
        receipt = json.loads(receipt_text)
        assert receipt["repair"]["api_restart"]["replacement_pid"] == replacement_pid
        assert "s3cret-value" not in receipt_text
    finally:
        stop_services(*service_pids(stub.socket_path))
        reap(stub.process)


def test_second_run_is_a_noop_when_the_service_already_selects_fixed_helpers(tmp_path, monkeypatch):
    stub = start_podman_stub(tmp_path)
    wire_deployment(monkeypatch, stub)
    try:
        first = runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert first["changed"] is True
        after_first = service_pids(stub.socket_path)

        second = runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert second["changed"] is False
        assert second["repair"] is None
        assert second["netavark"]["version"] == f"netavark {HELPER_VERSION}"
        assert service_pids(stub.socket_path) == after_first  # no further restart
    finally:
        stop_services(*service_pids(stub.socket_path))
        reap(stub.process)


def test_already_compatible_runtime_is_left_alone(tmp_path, monkeypatch):
    stub = start_podman_stub(
        tmp_path,
        native=old_native_info(netavark_version="netavark 1.10.0", aardvark_version="aardvark-dns 1.10.0"),
    )
    downloads = wire_deployment(monkeypatch, stub)
    try:
        result = runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert result["changed"] is False
        assert result["repair"] is None
        assert result["netavark"]["version"] == "netavark 1.10.0"
        assert downloads == []
        assert not drop_in_path(stub).exists()
        assert not override_path(stub).exists()
        assert not helpers_dir(stub).exists()
        assert service_pids(stub.socket_path) == [stub.process.pid]
    finally:
        stop_services(stub.process.pid)
        reap(stub.process)


def test_docker_backend_needs_no_repair(tmp_path, monkeypatch):
    stub = start_podman_stub(tmp_path)
    wire_deployment(monkeypatch, stub)
    docker_stub = start_podman_stub(tmp_path / "docker", socket_name="docker.sock")
    try:
        result = runtime_compat.prepare_runtime(podman_runtime(docker_stub, backend="docker", rootless=False), tmp_path / "state")
        assert result["backend"] == "docker"
        assert result["changed"] is False
        assert result["repair"] is None
    finally:
        stop_services(stub.process.pid, docker_stub.process.pid)
        reap(stub.process)
        reap(docker_stub.process)


def test_rootful_engine_with_fixed_helpers_is_compatible_but_old_helpers_are_refused(tmp_path, monkeypatch):
    fixed = old_native_info(netavark_version="netavark 1.10.0", aardvark_version="aardvark-dns 1.10.0")
    fixed["host"]["security"]["rootless"] = False
    old = old_native_info()
    old["host"]["security"]["rootless"] = False

    stub = start_podman_stub(tmp_path / "rootful-fixed", native=fixed)
    old_stub = start_podman_stub(tmp_path / "rootful-old", native=old)
    wire_deployment(monkeypatch, stub)
    try:
        result = runtime_compat.prepare_runtime(podman_runtime(stub, rootless=False), tmp_path / "state")
        assert result["rootless"] is False
        assert result["changed"] is False
        assert result["repair"] is None

        with pytest.raises(PortmapError) as error:
            runtime_compat.prepare_runtime(podman_runtime(old_stub, rootless=False), tmp_path / "state")
        assert "rootful" in str(error.value)
        assert not drop_in_path(old_stub).exists()
        assert old_stub.process.poll() is None
    finally:
        stop_services(stub.process.pid, old_stub.process.pid)
        reap(stub.process)
        reap(old_stub.process)


def test_running_foreign_workload_blocks_the_repair(tmp_path, monkeypatch):
    stub = start_podman_stub(tmp_path, containers=[dict(FOREIGN_CONTAINER, State="running")])
    wire_deployment(monkeypatch, stub)
    try:
        with pytest.raises(PortmapError) as error:
            runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert "running" in str(error.value)
        assert not drop_in_path(stub).exists()
        assert not override_path(stub).exists()
        assert not helpers_dir(stub).exists()
        assert service_pids(stub.socket_path) == [stub.process.pid]
    finally:
        stop_services(stub.process.pid)
        reap(stub.process)


def test_helper_checksum_mismatch_aborts_before_any_mutation(tmp_path, monkeypatch):
    stub = start_podman_stub(tmp_path)
    wire_deployment(monkeypatch, stub, netavark_sha="0" * 64)
    try:
        with pytest.raises(PortmapError) as error:
            runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert "SHA-256" in str(error.value)
        assert not drop_in_path(stub).exists()
        assert not override_path(stub).exists()
        assert not helpers_dir(stub).parent.exists()
        assert service_pids(stub.socket_path) == [stub.process.pid]
    finally:
        stop_services(stub.process.pid)
        reap(stub.process)


def test_conflicting_user_drop_in_is_refused_before_downloading(tmp_path, monkeypatch):
    stub = start_podman_stub(tmp_path)
    downloads = wire_deployment(monkeypatch, stub)
    conflict_dir = stub.config_home / "containers" / "containers.conf.d"
    conflict_dir.mkdir(parents=True)
    (conflict_dir / "zz-custom.conf").write_text('[engine]\nhelper_binaries_dir = ["/opt/custom"]\n', encoding="utf-8")
    try:
        with pytest.raises(PortmapError) as error:
            runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert "helper_binaries_dir" in str(error.value)
        assert downloads == []
        assert not helpers_dir(stub).parent.exists()
        assert (conflict_dir / "zz-custom.conf").read_text() == '[engine]\nhelper_binaries_dir = ["/opt/custom"]\n'
        assert service_pids(stub.socket_path) == [stub.process.pid]
    finally:
        stop_services(stub.process.pid)
        reap(stub.process)


def test_foreign_file_at_the_owned_drop_in_path_is_refused(tmp_path, monkeypatch):
    stub = start_podman_stub(tmp_path)
    downloads = wire_deployment(monkeypatch, stub)
    foreign = drop_in_path(stub)
    foreign.parent.mkdir(parents=True)
    foreign.write_text('# some other tool\n[engine]\nlogging_driver = "json"\n', encoding="utf-8")
    try:
        with pytest.raises(PortmapError) as error:
            runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert "unexpected content" in str(error.value)
        assert downloads == []
        assert foreign.read_text(encoding="utf-8") == '# some other tool\n[engine]\nlogging_driver = "json"\n'
        assert service_pids(stub.socket_path) == [stub.process.pid]
    finally:
        stop_services(stub.process.pid)
        reap(stub.process)


def test_predecessor_owned_config_is_refreshed_not_refused(tmp_path, monkeypatch):
    """A previous deployment of this transaction left owned files with a stale
    search list (its then-base config contributed preserved entries); the
    standard setup must reuse the staged helpers and refresh the owned files
    in place instead of failing forever on the byte difference."""
    stub = start_podman_stub(tmp_path)
    downloads = wire_deployment(monkeypatch, stub)
    install_fixture_helpers(stub)
    stale_search = [str(helpers_dir(stub)), "/opt/podman-helpers", "/usr/lib/podman"]
    stale = (
        f"{runtime_compat.DROP_IN_HEADER}\n[engine]\n"
        f"helper_binaries_dir = {json.dumps(stale_search)}\n"
    )
    drop_in_path(stub).parent.mkdir(parents=True)
    drop_in_path(stub).write_text(stale, encoding="utf-8")
    override_path(stub).parent.mkdir(parents=True, exist_ok=True)
    override_path(stub).write_text(stale, encoding="utf-8")
    try:
        result = runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert result["changed"] is True
        assert downloads == []  # the staged 1.9 helpers are reused verbatim
        assert tomllib.loads(drop_in_path(stub).read_text())["engine"]["helper_binaries_dir"] == expected_search(stub)
        assert tomllib.loads(override_path(stub).read_text())["engine"]["helper_binaries_dir"] == expected_search(stub)
        native = fetch_native(stub.socket_path)
        assert native["host"]["networkBackendInfo"]["version"] == f"netavark {HELPER_VERSION}"
    finally:
        stop_services(*service_pids(stub.socket_path))
        reap(stub.process)


def test_unexpected_service_argv_is_refused_never_killed(tmp_path, monkeypatch):
    stub = start_podman_stub(tmp_path)
    wire_deployment(monkeypatch, stub)
    odd_root = tmp_path / "odd"
    odd_root.mkdir()
    odd_socket = odd_root / "run" / "podman" / "podman.sock"
    # An argv shape the module must refuse to restart, on its own socket.
    odd = start_podman_stub(
        odd_root,
        argv=[str(stub.podman_bin), "system", "service", "--time=30", f"unix://{odd_socket}"],
    )
    try:
        with pytest.raises(PortmapError) as error:
            runtime_compat.prepare_runtime(podman_runtime(odd), tmp_path / "state")
        assert "refusing" in str(error.value)
        assert odd.process.poll() is None
        assert not drop_in_path(stub).exists()
        assert not helpers_dir(stub).exists()
    finally:
        stop_services(odd.process.pid, stub.process.pid)
        reap(odd.process)
        reap(stub.process)


def test_failed_selection_rolls_back_config_helpers_and_process(tmp_path, monkeypatch):
    stub = start_podman_stub(tmp_path, env_extra={"STUB_IGNORE_OVERRIDE": "1"})
    wire_deployment(monkeypatch, stub)
    try:
        with pytest.raises(PortmapError) as error:
            runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert "rolled back" in str(error.value)
        assert "restored the previous API service" in str(error.value)

        assert not drop_in_path(stub).exists()
        assert not override_path(stub).exists()
        assert not helpers_dir(stub).parent.exists()

        # The socket serves again, selecting the original old helpers.
        native = fetch_native(stub.socket_path)
        assert native["host"]["networkBackendInfo"]["version"] == "netavark 1.4.0"
        assert len(service_pids(stub.socket_path)) == 1
    finally:
        stop_services(*service_pids(stub.socket_path))
        reap(stub.process)


def test_failed_refresh_rolls_back_to_the_predecessor_owned_content(tmp_path, monkeypatch):
    stub = start_podman_stub(tmp_path, env_extra={"STUB_IGNORE_OVERRIDE": "1"})
    wire_deployment(monkeypatch, stub)
    install_fixture_helpers(stub)
    stale_search = [str(helpers_dir(stub)), "/opt/podman-helpers"]
    stale = (
        f"{runtime_compat.DROP_IN_HEADER}\n[engine]\n"
        f"helper_binaries_dir = {json.dumps(stale_search)}\n"
    )
    drop_in_path(stub).parent.mkdir(parents=True)
    drop_in_path(stub).write_text(stale, encoding="utf-8")
    override_path(stub).parent.mkdir(parents=True, exist_ok=True)
    override_path(stub).write_text(stale, encoding="utf-8")
    try:
        with pytest.raises(PortmapError) as error:
            runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert "rolled back" in str(error.value)
        # The predecessor-owned files are restored byte for byte, not deleted.
        assert drop_in_path(stub).read_text(encoding="utf-8") == stale
        assert override_path(stub).read_text(encoding="utf-8") == stale
        assert len(service_pids(stub.socket_path)) == 1
    finally:
        stop_services(*service_pids(stub.socket_path))
        reap(stub.process)


def test_explicit_base_config_helper_paths_are_preserved_not_rewritten(tmp_path, monkeypatch):
    base = (
        '[containers]\ncgroups = "disabled"\n\n[engine]\ncgroup_manager = "cgroupfs"\n'
        'helper_binaries_dir = ["/opt/podman-helpers", "/usr/lib/podman"]\n'
    )
    stub = start_podman_stub(tmp_path, base_config=base)
    wire_deployment(monkeypatch, stub)
    try:
        result = runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert result["changed"] is True

        # The startup-generated base config is preserved byte for byte.
        assert stub.base_conf.read_text() == base
        search = tomllib.loads(override_path(stub).read_text())["engine"]["helper_binaries_dir"]
        assert search[0] == str(helpers_dir(stub))
        assert "/opt/podman-helpers" in search
        assert "/usr/libexec/podman" in search and "/usr/lib/podman" in search

        native = fetch_native(stub.socket_path)
        assert native["host"]["networkBackendInfo"]["path"] == str(helpers_dir(stub) / "netavark")
    finally:
        stop_services(*service_pids(stub.socket_path))
        reap(stub.process)


def test_rebuilt_workspace_with_lost_base_config_repairs_again(tmp_path, monkeypatch):
    """A workspace rebuild replaces the API service (fresh CONTAINERS_CONF,
    distro 1.4 helpers) while the owned user-data layout survives: the check
    repairs again through the same owned paths without a permanent failure."""
    stub = start_podman_stub(tmp_path)
    downloads = wire_deployment(monkeypatch, stub)
    try:
        first = runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert first["changed"] is True
        rebuilt = start_podman_stub(tmp_path / "rebuilt")
        # The rebuilt service keeps its own fresh base config but the owned
        # user-data layout (helpers, drop-in, override) persists in the home.
        monkeypatch.setenv("HOME", str(stub.home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(stub.config_home))
        monkeypatch.setenv("XDG_DATA_HOME", str(stub.data_home))
        downloads.clear()
        second = runtime_compat.prepare_runtime(podman_runtime(rebuilt), tmp_path / "state2")
        assert second["changed"] is True
        assert downloads == []  # persisted helpers reused
        native = fetch_native(rebuilt.socket_path)
        assert native["host"]["networkBackendInfo"]["version"] == f"netavark {HELPER_VERSION}"
        stop_services(*service_pids(rebuilt.socket_path))
        reap(rebuilt.process)
    finally:
        stop_services(*service_pids(stub.socket_path))
        reap(stub.process)


def test_deleted_service_working_directory_keeps_original_api_alive(tmp_path, monkeypatch):
    working = tmp_path / "working"
    working.mkdir()
    stub = start_podman_stub(tmp_path / "engine", cwd=working)
    downloads = wire_deployment(monkeypatch, stub)
    working.rmdir()
    try:
        with pytest.raises(PortmapError, match="working directory"):
            runtime_compat.prepare_runtime(podman_runtime(stub), tmp_path / "state")
        assert stub.process.poll() is None
        assert fetch_native(stub.socket_path)["host"]["networkBackendInfo"]["version"] == "netavark 1.4.0"
        assert downloads == []
        assert not drop_in_path(stub).exists()
    finally:
        stop_services(*service_pids(stub.socket_path))
        reap(stub.process)
