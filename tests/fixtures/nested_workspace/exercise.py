#!/usr/bin/env python3
"""In-workspace acceptance program for the nested portmap fixture.

Runs INSIDE a fixture workspace container as the ordinary uid 1000 user
(the driver execs it; it never runs as root). Three roles:

* ``workspace``: the full host-access acceptance chain — target-profile
  assertions, inner rootless Podman startup, PLAIN ``portmap run`` (the
  new defaults: DNS forward takeover on real loopback 53, container
  ingress on 80), plain ``portmap up`` publishing the local role with the
  public entry on the exact pod IP, project publishing through the agent's
  compose API, DNS/HTTP/raw-port checks on both ingresses, the non-vacuous
  private/public boundary, outbound SSH created through the agent mutation
  API (masters belong to the supervised worker), SIGTERM/restart lifetime
  semantics, project-triggered demo setup, host-access teardown that
  RETAINS consumer-needed CoreDNS while projects still resolve, and the
  final down with exact resolver restoration.
* ``peer``: a second isolated workspace from the same image proving the
  fresh SERVER-ONLY default: plain ``portmap up`` on a fresh state must
  publish HTTP and supply container DNS while loopback :53 is held busy by
  another process and the workspace user has no sudo elevation at all, and
  the visible resolver file must remain byte-identical (no host takeover).
  It stays reachable as a real SSH/gateway peer and tears itself down on
  the stop signal.
* ``recreated``: the same workspace pod recreated (persistent portmap state
  under /workspace, a NEW resolver bind file with a fresh inode and variant
  file — teardown may never restore the dead pod's old identity.

The program always writes one JSON receipt into ``$NESTED_RECEIPT_DIR``
and exits 0 only when every check passed. Missing engines, unpublishable
low ports, unexpected storage drivers or resolver states fail loudly; no
high-port, DNS-disabled or mocked fallback exists anywhere in this file.
"""
from __future__ import annotations

import http.client
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dns_fixture  # noqa: E402  staged next to this file in both roles

ROLE = os.environ["NESTED_ROLE"]
RECEIPT_DIR = Path(os.environ["NESTED_RECEIPT_DIR"])
SOURCE = Path(os.environ["NESTED_SOURCE"])
POD_IP = os.environ["NESTED_POD_IP"]
DNS_IP = os.environ["NESTED_DNS_IP"]
WS_IP = os.environ["NESTED_WS_IP"]
PEER_IP = os.environ["NESTED_PEER_IP"]
ZONE = os.environ["NESTED_ZONE"]
DOMAIN = os.environ["NESTED_DOMAIN"]
GATEWAY_NETWORK = os.environ["NESTED_GATEWAY_NETWORK"]
DUAL_A = os.environ["NESTED_DUAL_A"]
DUAL_AAAA = os.environ["NESTED_DUAL_AAAA"]
V6ONLY = os.environ["NESTED_V6ONLY"]
PREVIOUS_RESOLV_SHA256 = os.environ.get("NESTED_PREVIOUS_RESOLV_SHA256", "")

HOME = Path("/home/workspace-user")
# Persistent across pod recreation: the state root lives on the mounted
# /workspace scratch, not the container filesystem.
STATE = Path("/workspace/portmap-state")
CLIENT_STATE = STATE / "client"
AGENT_RUNTIME = Path("/tmp/portmap-agent-1000")
AGENT_SOCKET = AGENT_RUNTIME / "agent.sock"
XDG_RUNTIME = Path("/tmp/podman-run-1000")
PODMAN_SOCKET = XDG_RUNTIME / "podman" / "podman.sock"
LOGS = Path("/workspace/logs")
WORKSPACE = Path("/workspace")
# Inner engine storage lives on the REAL-filesystem /workspace bind (the
# observed target's graphRoot is real workspace storage, not overlay-on-
# overlay inside the outer image). One directory per role/generation so the
# recreated pod stays genuinely cold and never reuses the old graph.
ENGINE_DATA = WORKSPACE / f"engine-data-{ROLE}"
RESOLV = Path("/etc/resolv.conf")
EXPECTED_CAP_BND = "00000000802001db"
PROOF_MODE = "source-mode: python3 -m portmap.cli from the staged current source tree; plain setup/run/up, no backend flags"

STEPS: dict = {"role": ROLE, "proof_mode": PROOF_MODE, "checks": {}, "environment": {
    "pod_ip": POD_IP, "dns_ip": DNS_IP, "peer_ip": PEER_IP, "ws_ip": WS_IP,
    "domain": DOMAIN, "zone": ZONE,
}}
ORIGINAL_RESOLV: dict = {}


def check(name: str, ok, detail=None) -> bool:
    STEPS["checks"][name] = {"ok": bool(ok), "detail": detail if detail is not None else ("" if ok else "see steps")}
    return bool(ok)


def rec(name: str, value) -> None:
    STEPS[name] = value


def sh(argv, *, env=None, timeout=180, cwd=None, stdin=None) -> dict:
    try:
        result = subprocess.run(
            [str(item) for item in argv], env=env, cwd=str(cwd) if cwd else None,
            input=stdin, capture_output=True, text=True, timeout=timeout,
        )
        return {"rc": result.returncode, "out": result.stdout, "err": result.stderr}
    except subprocess.TimeoutExpired:
        return {"rc": "timeout", "out": "", "err": f"deadline {timeout}s"}
    except OSError as exc:
        return {"rc": "oserror", "out": "", "err": str(exc)}


def wait_for(action, timeout: float, what: str, interval: float = 2.0, *, process: subprocess.Popen | None = None):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError(f"{what}: process exited with {process.returncode}; see its run log")
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - report the last failure
            last = exc
            time.sleep(interval)
    raise AssertionError(f"{what} not ready after {timeout:.0f}s: {last}")


def portmap_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "HOME": str(HOME), "USER": "workspace-user", "LOGNAME": "workspace-user",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH": str(SOURCE / "src"), "PYTHONDONTWRITEBYTECODE": "1",
        "XDG_RUNTIME_DIR": str(XDG_RUNTIME),
        # Single source for the inner engine's storage location: drives the
        # podman CLI, `podman system service` and every product subprocess
        # (including spawned agent workers) to the same real-fs graph root.
        "XDG_DATA_HOME": str(ENGINE_DATA),
        "PORTMAP_ROOT": str(SOURCE),
        "PORTMAP_RUNTIME": "podman",
        "PORTMAP_RUNTIME_SOCKET": str(PODMAN_SOCKET),
        "PORTMAP_DOCKER_SOCKET": str(PODMAN_SOCKET),
        # New defaults: NO resolver/ingress backend variables, flags, or
        # HTTP/DNS port overrides here. Plain setup/run/up must select
        # forward DNS on 53 and container ingress on 80 by themselves.
        "PORTMAP_HTTP_BIND": POD_IP if ROLE in ("workspace", "recreated") else "127.0.0.1",
        "PORTMAP_DNS_DOMAIN": DOMAIN,
        "PORTMAP_GATEWAY_NETWORK": GATEWAY_NETWORK,
        "PORTMAP_STATE_DIR": str(STATE),
        "PORTMAP_AGENT_RUNTIME_DIR": str(AGENT_RUNTIME),
        "PORTMAP_TCP_PORT_START": "18200",
        "PORTMAP_UDP_PORT_START": "19200",
        "PORTMAP_RANGE_PORT_START": "50100",
    })
    for key in list(env):
        if key.startswith("NESTED_"):
            env.pop(key, None)
    return env


def pm(args, *, timeout=300, env=None) -> dict:
    return sh(["podman", *args], env=env or portmap_env(), timeout=timeout)


def portmap(args, *, timeout=300, env=None) -> dict:
    return sh(["python3", "-m", "portmap.cli", *args], env=env or portmap_env(),
              cwd=str(WORKSPACE), timeout=timeout)


def portmap_json(result: dict) -> dict:
    if result["rc"] != 0:
        raise AssertionError(f"portmap command failed rc={result['rc']}\nstdout:\n{result['out']}\nstderr:\n{result['err']}")
    try:
        return json.loads(result["out"])
    except ValueError as exc:
        raise AssertionError(f"portmap command printed no JSON: {exc}\nstdout:\n{result['out']}") from exc


# --- small clients ----------------------------------------------------------

def http_fetch(address: str, port: int, host: str, path: str = "/", timeout: float = 8.0) -> dict:
    connection = http.client.HTTPConnection(address, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Host": host})
        response = connection.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        return {"status": response.status, "body": body}
    finally:
        connection.close()


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 15.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def agent_request(method: str, path: str, payload=None) -> dict:
    """Real HTTP against the agent's Unix socket (the product control API)."""
    connection = _UnixHTTP(str(AGENT_SOCKET))
    try:
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"content-type": "application/json"} if body else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        return {"status": response.status, "json": json.loads(raw) if raw else None}
    finally:
        connection.close()


def resolves(name: str, family: int = socket.AF_INET) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(name, None, family)})
    except socket.gaierror:
        return []


def raw_query(name: str, qtype: int, *, server: str = "127.0.0.1", tcp: bool = False) -> dict:
    return dns_fixture.query(server, name, qtype, tcp=tcp, timeout=5)


def process_status(pid: int) -> dict:
    status: dict[str, str] = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        status[key] = value.strip()
    return status


def ancestors(pid: int) -> list[int]:
    chain = [pid]
    current = pid
    for _ in range(64):
        try:
            parent = int(process_status(current)["PPid"])
        except (OSError, KeyError, ValueError):
            break
        if parent <= 1:
            break
        chain.append(parent)
        current = parent
    return chain


def ssh_masters() -> list[dict]:
    masters = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\x00")
        except OSError:
            continue
        args = [item.decode("utf-8", "replace") for item in cmdline if item]
        if len(args) > 4 and Path(args[0]).name == "ssh" and "-M" in args and "-N" in args:
            control = args[args.index("-S") + 1] if "-S" in args else ""
            masters.append({"pid": int(entry.name), "controlpath": control, "args": args[:8]})
    return masters


def port_free(address: str, port: int, kind: int) -> bool:
    with socket.socket(socket.AF_INET, kind) as probe:
        if kind == socket.SOCK_STREAM:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((address, port))
            if kind == socket.SOCK_STREAM:
                probe.listen(1)
            return True
        except OSError:
            return False


def tcp_roundtrip(port: int, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(payload)
        return sock.recv(1024)


def udp_roundtrip(port: int, payload: bytes) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(10)
        sock.sendto(payload, ("127.0.0.1", port))
        response, _ = sock.recvfrom(2048)
        return response


def resolv_identity() -> dict:
    info = RESOLV.stat()
    return {"content": RESOLV.read_bytes(), "inode": info.st_ino, "uid": info.st_uid,
            "gid": info.st_gid, "mode": oct(info.st_mode & 0o777)}


def infrastructure_state() -> dict:
    return json.loads((CLIENT_STATE / "infrastructure.json").read_text())



def saved_ssh_rows() -> tuple[list[dict], dict]:
    """Real product state files and schemas only: connection-targets.json
    (connection_config.TARGETS_FILE_NAME, {'version':1,'targets':[rows]})
    and connections.json (client._save, {'version':1,'connections':{domain:
    profile}}). No invented filenames or fields."""
    targets_payload = json.loads((CLIENT_STATE / "connection-targets.json").read_text())
    connections_payload = json.loads((CLIENT_STATE / "connections.json").read_text())
    return targets_payload.get("targets", []), connections_payload.get("connections", {})

def container_side_probe(coredns: str, host: str, gateway: str, *, expect_body: str = "portmap-http-ok",
                         http_port: int = 8080) -> dict:
    probe = (
        "import socket, sys, urllib.request\n"
        "host, coredns, gateway = sys.argv[1:4]\n"
        "port = int(sys.argv[4])\n"
        "expect = sys.argv[5]\n"
        "resolved = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]\n"
        "assert resolved == gateway, f'container DNS view mismatch: {resolved}'\n"
        "request = urllib.request.Request(f'http://{host}:{port}/')\n"
        "body = urllib.request.urlopen(request, timeout=10).read().decode()\n"
        "assert expect in body, body[:200]\n"
        "print('container-side-ok', resolved)\n"
    )
    return pm(["run", "--rm", "--network", GATEWAY_NETWORK, "--dns", coredns,
               "docker.io/library/python:3.12-alpine", "python3", "-c", probe,
               host, coredns, gateway, str(http_port), expect_body], timeout=300)


# --- phase 1: target profile ------------------------------------------------

def assert_profile(*, include_sudo: bool = True) -> None:
    check("pid1_is_tini", Path("/proc/1/comm").read_text().strip() == "tini",
          Path("/proc/1/comm").read_text().strip())
    check("system_tools_absent", all(shutil.which(name) is None for name in ("systemctl", "systemd-run", "resolvectl")),
          {name: shutil.which(name) for name in ("systemctl", "systemd-run", "resolvectl")})
    check("uid_1000", os.getuid() == 1000 and os.getgid() == 1000, f"{os.getuid()}:{os.getgid()}")
    uid_map = [tuple(int(v) for v in line.split()) for line in Path("/proc/self/uid_map").read_text().splitlines()]
    gid_map = [tuple(int(v) for v in line.split()) for line in Path("/proc/self/gid_map").read_text().splitlines()]
    def identity_coverage(rows: list[tuple[int, ...]]) -> bool:
        end = 0
        for inner, parent, size in rows:
            if inner != end or parent != end or size <= 0:
                return False
            end += size
        return end == 65536

    check("userns_65536_identity_coverage", identity_coverage(uid_map) and identity_coverage(gid_map),
          {"uid_map": uid_map, "gid_map": gid_map})
    status = process_status(os.getpid())
    cap_ok = (status.get("CapBnd") == EXPECTED_CAP_BND
              and status.get("CapEff") == status.get("CapPrm") == "0000000000000000")
    check("ordinary_caps_zero_bound_profile", cap_ok,
          {key: status.get(key) for key in ("CapPrm", "CapEff", "CapBnd")})
    check("no_new_privs_off_seccomp_off", status.get("NoNewPrivs") == "0" and status.get("Seccomp") == "0",
          {"NoNewPrivs": status.get("NoNewPrivs"), "Seccomp": status.get("Seccomp")})
    apparmor = "unconfined"
    try:
        apparmor = Path("/proc/self/attr/current").read_text().strip()
    except OSError:
        pass
    check("apparmor_unconfined", apparmor == "unconfined" or apparmor.endswith(" (unconfined)"), apparmor)
    fuse = Path("/dev/fuse")
    fuse_ok = False
    if fuse.exists():
        info = fuse.stat()
        fuse_ok = os.major(info.st_rdev) == 10 and os.minor(info.st_rdev) == 229
    check("dev_fuse_present", fuse_ok, str(fuse))
    lowport = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start").read_text().strip()
    check("namespace_lowport_zero", lowport == "0", lowport)
    expected_ranges = {"workspace-user:1:999", "workspace-user:1001:64535"}
    subuid_entries = {line for line in Path("/etc/subuid").read_text().split()
                      if line.startswith("workspace-user:")}
    subgid_entries = {line for line in Path("/etc/subgid").read_text().split()
                      if line.startswith("workspace-user:")}
    check("split_subid_ranges",
          subuid_entries == expected_ranges and subgid_entries == expected_ranges,
          {"subuid": sorted(subuid_entries), "subgid": sorted(subgid_entries)})
    if include_sudo:
        sudo = sh(["sudo", "-n", "sh", "-c", "grep -E 'CapBnd|CapEff' /proc/self/status"], timeout=30)
        sudo_caps = dict(line.split(":", 1) for line in sudo["out"].split("\n") if ":" in line)
        check("sudo_capped_to_profile",
              sudo["rc"] == 0 and sudo_caps.get("CapBnd", "").strip() == EXPECTED_CAP_BND
              and sudo_caps.get("CapEff", "").strip() == EXPECTED_CAP_BND,
              sudo["out"].strip() or sudo["err"].strip())
    identity = resolv_identity()
    ORIGINAL_RESOLV.update(identity)
    resolver_ok = (
        identity["uid"] == 0 and identity["gid"] == 0 and identity["mode"] == "0o644"
        and f"nameserver {DNS_IP}".encode() in identity["content"]
        and b"options ndots:5" in identity["content"]
    )
    check("root_owned_bind_resolver_0644", resolver_ok, {**identity, "content": identity["content"].decode()})
    check("nss_hosts_files_dns",
          all(token in Path("/etc/nsswitch.conf").read_text().split("hosts:")[1].split("\n")[0] for token in ("files", "dns")),
          Path("/etc/nsswitch.conf").read_text().splitlines())
    mounts = [line for line in Path("/proc/mounts").read_text().splitlines()
              if any(token in line for token in ("/etc/resolv.conf", "/workspace", "/fixture-keys", "/receipts"))]
    rec("mounts_evidence", mounts)
    check("no_host_engine_socket_mounted",
          not any(line.split()[1].endswith(("docker.sock", "podman.sock"))
                  for line in Path("/proc/mounts").read_text().splitlines() if len(line.split()) > 1),
          "no host engine socket appears in /proc/mounts")


# --- phase 2: inner engine --------------------------------------------------

def start_engine() -> subprocess.Popen:
    XDG_RUNTIME.mkdir(parents=True, exist_ok=True)
    PODMAN_SOCKET.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    AGENT_RUNTIME.mkdir(parents=True, exist_ok=True)
    os.chmod(XDG_RUNTIME, 0o700)
    ENGINE_DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(ENGINE_DATA, 0o700)
    # Backing-mount evidence: the graph root must sit on the real-filesystem
    # /workspace bind, not inside the outer image overlay.
    workspace_mount = next((line for line in Path("/proc/mounts").read_text().splitlines()
                            if line.split()[1] == "/workspace"), "")
    rec("engine_backing_mount", workspace_mount)
    check("engine_data_on_workspace_bind", bool(workspace_mount) and str(ENGINE_DATA).startswith("/workspace/"),
          {"mount": workspace_mount, "engine_data": str(ENGINE_DATA)})
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / "podman-service.log", "ab")  # noqa: SIM115 - closed in stop_engine
    proc = subprocess.Popen(
        ["podman", "system", "service", "--time=0", f"unix://{PODMAN_SOCKET}"],
        env=portmap_env(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    def engine_socket_ready() -> None:
        # Raise while not ready so wait_for actually retries; a plain
        # False-returning lambda would end the wait immediately.
        if not PODMAN_SOCKET.exists() or not _engine_probe():
            raise AssertionError("inner podman API socket not accepting yet")

    wait_for(engine_socket_ready, 240, "inner podman API socket")
    info_result = pm(["info", "--format", "json"], timeout=120)
    info = json.loads(info_result["out"])
    version = str(info.get("version", {}).get("Version", ""))
    host = info.get("host", {})
    storage = info.get("store", {})
    driver = storage.get("graphDriverName", "")
    graph_root = str(storage.get("graphRoot") or "")
    rec("inner_engine", {
        "version": version, "rootless": host.get("security", {}).get("rootless"),
        "graph_root": graph_root,
        "graph_driver": driver, "cgroup_manager": host.get("cgroupManager"),
        "events_logger": host.get("eventLogger"), "runtime": (host.get("ociRuntime") or {}).get("name"),
        "graph_options": storage.get("graphOptions"), "graph_status": storage.get("graphStatus"),
        "network_backend_info": host.get("networkBackendInfo"),
    })
    check("inner_podman_4_9_rootless", version.startswith("4.9.") and host.get("security", {}).get("rootless") is True,
          version)
    check("engine_graphroot_on_workspace_bind",
          graph_root.startswith(str(ENGINE_DATA) + "/"),
          {"graph_root": graph_root, "engine_data": str(ENGINE_DATA)})
    check("inner_overlay_storage",
          driver == "overlay" and not any("mount_program" in key and value for key, value in (storage.get("graphOptions") or {}).items()),
          {"driver": driver, "options": storage.get("graphOptions")})
    check("inner_cgroupfs_events_file",
          host.get("cgroupManager") == "cgroupfs" and host.get("eventLogger") == "file"
          and (host.get("ociRuntime") or {}).get("name") == "crun",
          {"cgroupManager": host.get("cgroupManager"), "eventLogger": host.get("eventLogger"),
           "ociRuntime": (host.get("ociRuntime") or {}).get("name")})
    for image in INNER_IMAGES:
        pull = pm(["pull", image], timeout=900)
        if pull["rc"] != 0:
            raise AssertionError(f"inner image pull failed for {image}: {pull['err']}")
    smoke = pm(["run", "--rm", "docker.io/library/python:3.12-alpine", "python3", "-c", "print('inner-container-ok')"], timeout=300)
    check("inner_real_container_runs", smoke["rc"] == 0 and "inner-container-ok" in smoke["out"], smoke["out"] or smoke["err"])
    return proc


INNER_IMAGES = (
    "docker.io/library/python:3.12-alpine",
    "docker.io/library/python:3.12-slim-bookworm",
    "docker.io/coredns/coredns:1.12.1",
    "docker.io/library/traefik:v3.6",
    "docker.io/library/haproxy:3.2-alpine",
    "docker.io/traefik/whoami:latest",
)


def _engine_probe() -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(3)
    try:
        probe.connect(str(PODMAN_SOCKET))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def stop_engine(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()


# --- phase 3: portmap lifecycle ---------------------------------------------

def start_run(log_name: str) -> subprocess.Popen:
    """Plain foreground run: the new defaults must need no backend flags."""
    log = open(LOGS / log_name, "ab")  # noqa: SIM115 - container is disposable
    return subprocess.Popen(
        ["python3", "-m", "portmap.cli", "run"],
        env=portmap_env(), cwd=str(WORKSPACE), stdout=log, stderr=subprocess.STDOUT,
    )


def status_reports_running(*, require_resolver: bool) -> dict:
    """Authoritative readiness: `portmap status --check` must report the
    gateway actually running (and, for host access, the forward resolver
    installed). The agent starts BEFORE setup and the catalog can serve
    while the infrastructure phase is still starting, so agent+HTTP alone
    is not readiness. Never falls back to state-file existence."""
    status = portmap_json(portmap(["status", "--check"], timeout=180))
    gateway = status.get("gateway") or {}
    if gateway.get("running") is not True:
        raise AssertionError(f"portmap status --check: gateway not running: {gateway}")
    if require_resolver:
        resolver = status.get("resolver") or {}
        if resolver.get("installed") is not True or resolver.get("backend") != "forward":
            raise AssertionError(f"portmap status --check: host resolver not installed: {resolver}")
    return status


def run_ready() -> dict:
    health = agent_request("GET", "/healthz")
    if health["status"] != 200:
        raise AssertionError(f"agent not healthy: {health['status']}")
    page = http_fetch("127.0.0.1", 80, "localhost")
    if page["status"] != 200:
        raise AssertionError(f"private catalog not serving: {page['status']}")
    answer = raw_query(DOMAIN, dns_fixture.QTYPE_A)
    if answer["rcode"] != 0 or (dns_fixture.QTYPE_A, "127.0.0.1") not in answer["answers"]:
        raise AssertionError(f"loopback DNS not answering for {DOMAIN}: {answer}")
    status_reports_running(require_resolver=True)
    if resolves(DOMAIN) != ["127.0.0.1"]:
        raise AssertionError(f"OS does not resolve {DOMAIN} to loopback yet: {resolves(DOMAIN)}")
    return health


def pre_portmap_dns_checks() -> None:
    peer_answer = raw_query(f"peer-fixture.{ZONE}", dns_fixture.QTYPE_A, server=DNS_IP)
    check("original_upstream_authoritative_a",
          peer_answer["rcode"] == 0 and peer_answer["answers"] == [(dns_fixture.QTYPE_A, PEER_IP)], peer_answer)
    check("original_search_semantics", resolves("peer-fixture") == [PEER_IP], resolves("peer-fixture"))
    dual = raw_query(f"dual.{ZONE}", dns_fixture.QTYPE_A, server=DNS_IP, tcp=True)
    check("original_upstream_a_tcp", dual["answers"] == [(dns_fixture.QTYPE_A, DUAL_A)], dual)
    dual6 = raw_query(f"dual.{ZONE}", dns_fixture.QTYPE_AAAA, server=DNS_IP)
    check("original_upstream_aaaa", dual6["answers"] == [(dns_fixture.QTYPE_AAAA, DUAL_AAAA)], dual6)
    nodata = raw_query(f"dual.{ZONE}", 4, server=DNS_IP)  # MX for an A/AAAA-only name
    check("original_upstream_nodata", nodata["rcode"] == 0 and not nodata["answers"], nodata)
    nxdomain = raw_query(f"missing.{ZONE}", dns_fixture.QTYPE_A, server=DNS_IP)
    check("original_upstream_nxdomain", nxdomain["rcode"] == 3, nxdomain)
    v6 = resolves("v6only", socket.AF_INET6)
    check("original_v6_search_resolution", v6 == [V6ONLY], v6)


def forward_resolver_checks() -> None:
    identity = resolv_identity()
    lines = identity["content"].decode().splitlines()
    nameservers = [line.split()[1] for line in lines if line.startswith("nameserver")]
    search = [line for line in lines if line.startswith("search")]
    options = [line for line in lines if line.startswith("options")]
    original_lines = ORIGINAL_RESOLV["content"].decode().splitlines()
    original_search = [line for line in original_lines if line.startswith("search")]
    original_options = [line for line in original_lines if line.startswith("options")]
    check("resolver_forward_taken_over", nameservers == ["127.0.0.1"], nameservers)
    check("resolver_search_options_preserved",
          search == original_search and options == original_options,
          {"search": search, "options": options,
           "original_search": original_search, "original_options": original_options})
    check("resolver_bind_inode_stable", identity["inode"] == ORIGINAL_RESOLV["inode"],
          {"inode": identity["inode"], "original": ORIGINAL_RESOLV["inode"]})
    check("resolver_owner_preserved", identity["uid"] == 0 and identity["mode"] == "0o644", identity)
    status = portmap_json(portmap(["status"], timeout=120))
    resolver = status.get("resolver", {})
    rec("portmap_status_after_run", status)
    check("status_reports_forward_installed",
          resolver.get("installed") is True and resolver.get("backend") == "forward", resolver)
    check("os_resolves_portmap_domain", resolves(DOMAIN) == ["127.0.0.1"], resolves(DOMAIN))


def dns_data_plane_checks(frontend_host: str) -> None:
    udp = raw_query(DOMAIN, dns_fixture.QTYPE_A)
    check("dns_udp_a_loopback", udp["rcode"] == 0 and (dns_fixture.QTYPE_A, "127.0.0.1") in udp["answers"], udp)
    tcp = raw_query(DOMAIN, dns_fixture.QTYPE_A, tcp=True)
    check("dns_tcp_a_loopback", tcp["rcode"] == 0 and (dns_fixture.QTYPE_A, "127.0.0.1") in tcp["answers"], tcp)
    aaaa = raw_query(DOMAIN, dns_fixture.QTYPE_AAAA)
    check("dns_aaaa_nodata", aaaa["rcode"] == 0 and not aaaa["answers"], aaaa)
    nx = raw_query("nonexistent.portmap", dns_fixture.QTYPE_A)
    check("dns_nxdomain_outside_zone", nx["rcode"] == 3, nx)
    host_a = raw_query(frontend_host, dns_fixture.QTYPE_A)
    check("dns_project_host_a", host_a["rcode"] == 0 and (dns_fixture.QTYPE_A, "127.0.0.1") in host_a["answers"], host_a)
    forwarded = raw_query(f"dual.{ZONE}", dns_fixture.QTYPE_A)
    check("dns_original_upstream_forwarded",
          forwarded["rcode"] == 0 and forwarded["answers"] == [(dns_fixture.QTYPE_A, DUAL_A)], forwarded)
    forwarded6 = raw_query(f"v6only.{ZONE}", dns_fixture.QTYPE_AAAA)
    check("dns_original_upstream_aaaa_forwarded",
          forwarded6["answers"] == [(dns_fixture.QTYPE_AAAA, V6ONLY)], forwarded6)
    check("os_resolves_project_host", resolves(frontend_host) == ["127.0.0.1"], resolves(frontend_host))


# --- phase 4: projects, ingress, boundary -----------------------------------

def prepare_repo() -> dict:
    sys.path.insert(0, str(SOURCE / "tests" / "fixtures"))
    import test_repo_manager

    layout = test_repo_manager.build_test_repo(WORKSPACE / "test_repo")
    install = portmap(["broker", "install", "--docker-config", str(HOME / ".docker"),
                       "--portmap-root", str(SOURCE), "--force"], timeout=120)
    check("broker_shim_installed", install["rc"] == 0, install["out"] + install["err"])
    return {"layout": layout, "module": test_repo_manager.__name__}


def agent_compose_up(worktree: Path) -> dict:
    worktrees = agent_request("GET", "/worktrees")
    check("agent_worktrees_api", worktrees["status"] == 200 and worktrees["json"].get("ok") is True,
          {"status": worktrees["status"]})
    result = agent_request("POST", "/compose-up", {"worktree": str(worktree)})
    if result["status"] != 200 or not result["json"].get("ok"):
        raise AssertionError(f"agent compose-up failed for {worktree}: {result}")
    state = json.loads((worktree / ".portmap" / "state.json").read_text())
    return {"result": result["json"], "state": state}


def wait_http_response(address: str, host: str, marker: str) -> dict:
    def application_ready() -> dict:
        page = http_fetch(address, 80, host)
        if page["status"] != 200 or marker not in page["body"]:
            raise AssertionError(f"{host}: HTTP {page['status']}, body={page['body'][:200]!r}")
        return page

    return wait_for(application_ready, 90, f"application route {host}")


def project_checks(states: dict) -> None:
    dev_state, feat_state = states["dev"], states["feat"]
    check("compose_projects_distinct", dev_state["compose_project"] != feat_state["compose_project"],
          [dev_state["compose_project"], feat_state["compose_project"]])
    for label, state in (("dev", dev_state), ("feat", feat_state)):
        listing = pm(["ps", "--filter", f"label=com.docker.compose.project={state['compose_project']}",
                      "--format", "{{.Names}}"], timeout=120)
        names = [line for line in listing["out"].splitlines() if line.strip()]
        check(f"project_{label}_containers_running", bool(names), names)
    dev_host = dev_state["endpoints"]["frontend"]["host"]
    feat_host = feat_state["endpoints"]["frontend"]["host"]
    private_dev = wait_http_response("127.0.0.1", dev_host, f"url=http://{dev_host}:80")
    private_feat = wait_http_response("127.0.0.1", feat_host, f"url=http://{feat_host}:80")
    check("project_http_private_ingress",
          private_dev["status"] == 200 and "portmap-http-ok" in private_dev["body"]
          and private_feat["status"] == 200 and "portmap-http-ok" in private_feat["body"],
          {"dev": private_dev, "feat": private_feat})
    check("project_response_is_not_catalog",
          "portmap-http-ok" in private_dev["body"] and "/assets/" not in private_dev["body"]
          and 'id="root"' not in private_dev["body"],
          private_dev["body"][:160])
    public_feat = wait_http_response(POD_IP, feat_host, f"url=http://{feat_host}:80")
    check("project_http_public_ingress",
          public_feat["status"] == 200 and "portmap-http-ok" in public_feat["body"], public_feat)


def raw_port_checks(feat_state: dict) -> None:
    mqtt = feat_state["endpoints"]["mqtt"]
    udp_echo = feat_state["endpoints"]["udp_echo"]
    turn = feat_state["endpoints"]["turn"]
    try:
        reply = wait_for(lambda: tcp_roundtrip(mqtt["host_port"], b"hello"), 60, "mqtt tcp")
        check("raw_tcp_roundtrip", reply == b"portmap-tcp-ok:hello", reply)
        reply = wait_for(lambda: udp_roundtrip(udp_echo["host_port"], b"hello"), 60, "udp echo")
        check("raw_udp_roundtrip", reply == b"portmap-udp-ok:hello", reply)
        entry = wait_for(lambda: udp_roundtrip(turn["host_port"], b"entry"), 60, "turn base port")
        check("raw_udp_range_base", entry.startswith(b"portmap-range-ok-3478:entry"), entry)
        relay_port = turn["range"]["min_port"]
        relay = wait_for(lambda: udp_roundtrip(relay_port, b"relay"), 90, "turn relay port")
        check("raw_udp_range_member", relay.startswith(f"portmap-range-ok-{relay_port}:relay".encode()), relay)
    except AssertionError as exc:
        check("raw_port_checks", False, str(exc))


def container_side_checks(dev_state: dict) -> None:
    infrastructure = infrastructure_state()
    coredns = infrastructure["addresses"]["dns"]
    gateway = infrastructure["addresses"]["gateway"]
    host = dev_state["endpoints"]["frontend"]["host"]
    result = container_side_probe(coredns, host, gateway)
    check("container_side_dns_and_http",
          result["rc"] == 0 and "container-side-ok" in result["out"], (result["out"] or result["err"])[-800:])


def ssh_outbound_checks() -> dict:
    save = agent_request("POST", "/access/connections",
                         {"action": "save", "target": f"workspace-user@{PEER_IP}", "via": "ssh"})
    check("agent_save_ssh_target", save["status"] == 200, save)
    row = next((row for row in (save["json"] or {}).get("connections", [])
                if row.get("target") == f"workspace-user@{PEER_IP}"), None)
    check("agent_target_row_created", row is not None, save["json"])
    if row is not None:
        connect = agent_request("POST", "/access/connections", {"action": "connect", "id": row["id"]})
        check("agent_connect_ok", connect["status"] == 200,
              {"status": connect["status"], "body": connect["json"]})
        listing = agent_request("GET", "/access/connections?check=1")
        profile = next((item for item in (listing["json"] or {}).get("connections", [])
                        if item.get("actual_via") == "ssh" and item.get("has_connection")
                        and item.get("status") == "reachable"), None)
        check("peer_profile_connected", profile is not None, listing["json"])
    if not any(item.get("ok") for name, item in STEPS["checks"].items() if name.startswith("agent_")) or row is None:
        return {}
    health = agent_request("GET", "/healthz")
    agent_pid = int((health["json"] or {}).get("pid"))
    masters = ssh_masters()
    supervised = [master for master in masters if agent_pid in ancestors(master["pid"])]
    check("ssh_master_owned_by_supervised_worker",
          bool(supervised) and Path(supervised[0]["controlpath"]).exists(),
          {"agent_pid": agent_pid, "masters": masters})
    domain = "peer.portmap"
    check("peer_domain_maps_to_loopback", resolves(domain) == ["127.0.0.1"], resolves(domain))
    page = http_fetch("127.0.0.1", 80, domain)
    check("peer_endpoint_reachable", page["status"] == 200, {"status": page["status"], "body": page["body"][:200]})
    rows, profiles = saved_ssh_rows()
    check("saved_connection_config_retained",
          any(item.get("target") == f"workspace-user@{PEER_IP}" and item.get("via") == "ssh" for item in rows)
          and any(profile.get("via") == "ssh" for profile in profiles.values()),
          {"targets": rows, "profile_domains": sorted(profiles)})
    return {"domain": domain, "agent_pid": agent_pid, "row_id": row["id"]}


def boundary_checks(peer_domain: str) -> None:
    private_machines = http_fetch("127.0.0.1", 80, "localhost", "/api/machines")
    public_machines = http_fetch(POD_IP, 80, POD_IP, "/api/machines")
    private_ids = [machine.get("id") for machine in json.loads(private_machines["body"]).get("machines", [])] \
        if private_machines["status"] == 200 else []
    public_ids = [machine.get("id") for machine in json.loads(public_machines["body"]).get("machines", [])] \
        if public_machines["status"] == 200 else []
    check("boundary_private_lists_peer", peer_domain in private_ids and "local" in private_ids, private_ids)
    check("boundary_public_local_only", public_ids == ["local"], public_ids)
    private_conn = http_fetch("127.0.0.1", 80, "localhost", "/api/connections")
    public_conn = http_fetch(POD_IP, 80, POD_IP, "/api/connections")
    check("boundary_connections_200_private_404_public",
          private_conn["status"] == 200 and public_conn["status"] == 404,
          {"private": private_conn["status"], "public": public_conn["status"]})
    quoted = urllib.parse.quote(peer_domain)
    private_peer = http_fetch("127.0.0.1", 80, "localhost", f"/registry.json?machine={quoted}")
    public_peer = http_fetch(POD_IP, 80, POD_IP, f"/registry.json?machine={quoted}")
    check("boundary_peer_machine_200_private_404_public",
          private_peer["status"] == 200 and public_peer["status"] == 404,
          {"private": private_peer["status"], "public": public_peer["status"]})
    remote_host = f"route-isolation.{peer_domain}"
    private_route = http_fetch("127.0.0.1", 80, remote_host, "/registry.json")
    public_route = http_fetch(POD_IP, 80, remote_host, "/registry.json")
    private_domain = json.loads(private_route["body"]).get("dns_domain") if private_route["status"] == 200 else None
    public_domain = json.loads(public_route["body"]).get("dns_domain") if public_route["status"] == 200 else None
    check("boundary_remote_host_routes_private_only",
          private_route["status"] == 200 and private_domain == peer_domain
          and (public_route["status"] == 404 or (public_route["status"] == 200 and public_domain == DOMAIN)),
          {"host": remote_host, "private": [private_route["status"], private_domain],
           "public": [public_route["status"], public_domain]})


# --- phase 5: lifetime ------------------------------------------------------

def sigterm_checks(run_proc: subprocess.Popen, peer_domain: str, agent_pid: int) -> None:
    run_proc.send_signal(signal.SIGTERM)
    try:
        rc = run_proc.wait(90)
    except subprocess.TimeoutExpired:
        run_proc.kill()
        rc = "timeout"
    check("run_sigterm_clean_exit", rc == 0, rc)
    check("agent_worker_gone", not Path(f"/proc/{agent_pid}").exists(), agent_pid)
    check("ssh_masters_gone", not ssh_masters(), ssh_masters())
    listing = pm(["ps", "--format", "{{.Names}}"], timeout=120)
    names = [line for line in listing["out"].splitlines() if line.strip()]
    infra = infrastructure_names()
    retained = all(name in names for name in infra["exact"]) and all(
        any(name.startswith(prefix) for name in names) for prefix in infra["ingress_prefixes"])
    check("shared_infrastructure_retained", retained, {"expected": infra, "actual": names})
    page = http_fetch("127.0.0.1", 80, "localhost")
    check("private_entry_retained_after_sigterm", page["status"] == 200, page["status"])
    answer = raw_query(DOMAIN, dns_fixture.QTYPE_A)
    check("loopback_dns_retained_after_sigterm", answer["rcode"] == 0, answer)
    rows, profiles = saved_ssh_rows()
    check("saved_profiles_retained_after_sigterm",
          any(item.get("via") == "ssh" for item in rows)
          and any(profile.get("via") == "ssh" for profile in profiles.values()),
          {"targets": rows, "profile_domains": sorted(profiles)})
    check("peer_mapping_withdrawn_after_sigterm", "127.0.0.1" not in resolves(peer_domain), resolves(peer_domain))


def restart_checks(peer_domain: str) -> subprocess.Popen:
    proc = start_run("run-2.log")
    health = wait_for(lambda: run_ready(), 420, "restarted portmap run", process=proc)
    agent_pid = int((health["json"] or {}).get("pid"))
    check("restart_restores_peer_mapping", resolves(peer_domain) == ["127.0.0.1"], resolves(peer_domain))
    masters = [master for master in ssh_masters() if proc.pid in ancestors(master["pid"])]
    check("restart_revives_owned_master", bool(masters),
          {"runner_pid": proc.pid, "worker_pid": agent_pid, "masters": ssh_masters()})
    page = http_fetch("127.0.0.1", 80, peer_domain)
    check("restart_peer_endpoint_reachable", page["status"] == 200, page["status"])
    return proc


def demo_checks() -> None:
    result = portmap(["demo", "up"], timeout=900)
    try:
        payload = json.loads(result["out"])
    except ValueError:
        payload = {"ok": False, "parse_error": result["out"][-800:]}
    check("demo_one_shot_setup_ok", result["rc"] == 0 and payload.get("ok") is True, payload)
    endpoints = payload.get("endpoints") or {}
    served = None
    for endpoint in (endpoints.values() if isinstance(endpoints, dict) else []):
        host = urllib.parse.urlparse(endpoint.get("url") or "").hostname
        if host:
            served = wait_http_response("127.0.0.1", host, "Hostname")
            break
    check("demo_project_served", served is not None and served["status"] == 200 and "Hostname" in served["body"],
          served)
    down = portmap(["demo", "down"], timeout=300)
    check("demo_down_ok", down["rc"] == 0, down["out"][-500:] + down["err"][-500:])


def host_access_teardown_with_consumers(states: dict, peer_domain: str) -> None:
    """Host-access teardown with the local role and live project consumers.

    Final behavior contract: teardown restores the visible resolver
    exactly and removes the remote connection, but RETAINS the shared
    CoreDNS - the exact current instance - plus any already-live host port
    publications (recreating the OCI port binding would replace the
    instance). Projects keep resolving and serving; DNS health is proven
    from inside the shared network. Only the final teardown after down and
    project removal releases everything.
    """
    infrastructure = infrastructure_state()
    coredns = infrastructure["addresses"]["dns"]
    gateway = infrastructure["addresses"]["gateway"]
    host = states["dev"]["endpoints"]["frontend"]["host"]
    coredns_name = infrastructure.get("names", {}).get("dns", "")
    before = pm(["ps", "--format", "{{.ID}} {{.Names}}", "--filter", f"name={coredns_name}"], timeout=120)
    coredns_id_before = before["out"].split()[0] if before["out"].split() else ""
    teardown = portmap(["teardown"], timeout=600)
    check("host_access_teardown_ok", teardown["rc"] == 0,
          teardown["out"][-1500:] + teardown["err"][-1500:])
    identity = resolv_identity()
    check("host_access_resolver_restored_exactly",
          identity["content"] == ORIGINAL_RESOLV["content"] and identity["inode"] == ORIGINAL_RESOLV["inode"],
          {"content_match": identity["content"] == ORIGINAL_RESOLV["content"],
           "inode": [identity["inode"], ORIGINAL_RESOLV["inode"]]})
    check("host_os_no_longer_redirected", "127.0.0.1" not in resolves(DOMAIN), resolves(DOMAIN))
    check("teardown_removes_remote_connections",
          not ssh_masters() and "127.0.0.1" not in resolves(peer_domain),
          {"masters": ssh_masters(), "peer_domain": resolves(peer_domain)})
    after = pm(["ps", "--format", "{{.ID}} {{.Names}}", "--filter", f"name={coredns_name}"], timeout=120)
    parts = after["out"].split()
    check("consumer_coredns_same_instance_retained",
          bool(parts) and parts[0] == coredns_id_before,
          {"before": coredns_id_before, "after": after["out"].strip()})
    check("host_53_publication_retained_with_consumers",
          not port_free("127.0.0.1", 53, socket.SOCK_DGRAM) and not port_free("127.0.0.1", 53, socket.SOCK_STREAM),
          "the already-live 53 publication stays until the final teardown")
    probe = container_side_probe(coredns, host, gateway)
    check("container_dns_after_host_release",
          probe["rc"] == 0 and "container-side-ok" in probe["out"], (probe["out"] or probe["err"])[-800:])
    page = http_fetch("127.0.0.1", 80, host)
    check("project_serving_after_host_release", page["status"] == 200 and "portmap-http-ok" in page["body"], page)


def final_shutdown(states: dict) -> None:
    from_import = (
        "import json, sys\n"
        "from portmap.catalog import compose_down_project\n"
        "print(json.dumps(compose_down_project(sys.argv[1])))\n"
    )
    for label in ("dev", "feat"):
        result = sh(["python3", "-c", from_import, states[label]["compose_project"]],
                    env=portmap_env(), timeout=300, cwd=str(WORKSPACE))
        down = json.loads(result["out"]) if result["rc"] == 0 else {"ok": False, "error": result["err"]}
        check(f"project_{label}_compose_down", down.get("ok") is True, down)
        listing = pm(["ps", "-a", "--filter", f"label=com.docker.compose.project={states[label]['compose_project']}",
                      "--format", "{{.Names}}"], timeout=120)
        check(f"project_{label}_containers_gone",
              not [line for line in listing["out"].splitlines() if line.strip()], listing["out"])
    down = portmap(["down"], timeout=600)
    check("portmap_down_ok", down["rc"] == 0, down["out"][-800:] + down["err"][-800:])
    # Final teardown AFTER down and project removal releases the retained
    # CoreDNS and its host port publications.
    final = portmap(["teardown"], timeout=600)
    check("final_teardown_ok", final["rc"] == 0, final["out"][-800:] + final["err"][-800:])
    listing = pm(["ps", "--format", "{{.Names}}"], timeout=120)
    names = [line for line in listing["out"].splitlines() if line.strip()]
    check("public_ingress_released", not any(name.startswith("portmap-ingress-public-") for name in names), names)
    check("loopback_53_released_final",
          port_free("127.0.0.1", 53, socket.SOCK_DGRAM) and port_free("127.0.0.1", 53, socket.SOCK_STREAM), None)
    check("loopback_80_released_final", port_free("127.0.0.1", 80, socket.SOCK_STREAM), None)
    identity = resolv_identity()
    check("resolver_original_at_end",
          identity["content"] == ORIGINAL_RESOLV["content"] and identity["inode"] == ORIGINAL_RESOLV["inode"],
          {"inode": [identity["inode"], ORIGINAL_RESOLV["inode"]]})
    check("infrastructure_containers_gone", not any("portmap" in name for name in names), names)
    check("ssh_masters_gone_final", not ssh_masters(), ssh_masters())


def infrastructure_names() -> dict:
    infrastructure = infrastructure_state()
    exact = [name for name in infrastructure.get("names", {}).values() if name]
    return {"exact": exact,
            "ingress_prefixes": [f"portmap-ingress-{role}-" for role in ("private", "public")]}



def reinitialize_checks() -> subprocess.Popen | None:
    """Re-establish real state for the pod-recreation scenario: plain run,
    agent-API reconnect of the saved peer, then SIGTERM only. The resolver
    journal, saved profiles and shared infrastructure survive for the
    recreated pod to recover; nothing is torn down or fabricated."""
    proc = start_run("run-3.log")
    try:
        health = wait_for(lambda: run_ready(), 600, "reinitialized plain portmap run", process=proc)
    except BaseException:
        proc.terminate()
        raise
    agent_pid = int(health["json"]["pid"])
    identity = resolv_identity()
    check("reinit_resolver_taken_over", b"nameserver 127.0.0.1" in identity["content"],
          identity["content"].decode())
    saved = agent_request("GET", "/access/connections")
    row = next((item for item in (saved["json"] or {}).get("connections", [])
                if item.get("target") == f"workspace-user@{PEER_IP}" and item.get("via") == "ssh"), None)
    if row is not None:
        connect = agent_request("POST", "/access/connections", {"action": "connect", "id": row["id"]})
        check("reinit_connection_reestablished", connect["status"] == 200,
              {"status": connect["status"], "body": connect["json"]})
    else:
        check("reinit_connection_reestablished", False, saved)
    masters = [master for master in ssh_masters() if agent_pid in ancestors(master["pid"])]
    check("reinit_master_supervised", bool(masters), ssh_masters())
    proc.send_signal(signal.SIGTERM)
    try:
        rc = proc.wait(90)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = "timeout"
    check("reinit_sigterm_clean_exit", rc == 0, rc)
    check("reinit_workers_gone", not ssh_masters(), ssh_masters())
    rows, profiles = saved_ssh_rows()
    check("reinit_profiles_retained_no_teardown",
          any(item.get("via") == "ssh" for item in rows)
          and any(profile.get("via") == "ssh" for profile in profiles.values()),
          {"targets": rows, "profile_domains": sorted(profiles)})
    listing = pm(["ps", "--format", "{{.Names}}"], timeout=120)
    names = [line for line in listing["out"].splitlines() if line.strip()]
    check("reinit_infrastructure_retained_no_teardown",
          any("portmap" in name for name in names), names)
    return None


# --- workspace role ---------------------------------------------------------

def workspace_role() -> int:
    engine = run_proc = restart_proc = None
    try:
        assert_profile()
        (WORKSPACE / "incoming-marker").write_text("nested-workspace-reachable\n", encoding="utf-8")
        pre_portmap_dns_checks()
        engine = start_engine()
        # The driver binds the same generated, read-only OpenSSH config into
        # each generation's home. Credentials remain real, user-owned files
        # under the persistent workspace; no portmap state is fabricated.
        if not (HOME / ".ssh" / "config").is_file():
            raise AssertionError("the fixture's persistent SSH configuration is missing")
        staged = WORKSPACE / "ssh"
        if not (staged / "id_ed25519").exists() or not (staged / "known_hosts").exists():
            raise AssertionError("driver did not stage the uid-1000 SSH material into /workspace/ssh")
        rec("ssh_config_note", "disposable fixture home config over driver-staged uid-1000 keys; no real user SSH config exists or is edited")
        run_proc = start_run("run-1.log")
        health = wait_for(lambda: run_ready(), 600, "plain portmap run readiness", process=run_proc)
        agent_pid = int(health["json"]["pid"])
        rec("first_agent_pid", agent_pid)
        forward_resolver_checks()
        up = portmap(["up"], timeout=900)
        check("portmap_up_ok", up["rc"] == 0, up["out"][-1500:] + up["err"][-1500:])
        public = socket.create_connection((POD_IP, 80), timeout=5)
        public.close()
        check("public_ingress_exact_pod_ip", True, POD_IP)
        repo = prepare_repo()
        layout = repo["layout"]
        dev = agent_compose_up(layout.dev)
        feat = agent_compose_up(layout.feat)
        states = {"dev": dev["state"], "feat": feat["state"]}
        rec("project_states", states)
        frontend_host = states["dev"]["endpoints"]["frontend"]["host"]
        dns_data_plane_checks(frontend_host)
        project_checks(states)
        raw_port_checks(states["feat"])
        container_side_checks(states["dev"])
        peer = ssh_outbound_checks()
        peer_domain = peer.get("domain")
        if not peer_domain:
            raise AssertionError("peer connection was not established; cannot continue boundary checks")
        boundary_checks(peer_domain)
        sigterm_checks(run_proc, peer_domain, agent_pid)
        run_proc = None
        restart_proc = restart_checks(peer_domain)
        demo_checks()
        # Stop the restarted runner first: host-access removal happens with
        # projects still running and consuming CoreDNS.
        restart_proc.send_signal(signal.SIGTERM)
        try:
            rc = restart_proc.wait(90)
        except subprocess.TimeoutExpired:
            restart_proc.kill()
            rc = "timeout"
        check("restart_sigterm_clean_exit", rc == 0, rc)
        check("restart_workers_gone", not ssh_masters(), ssh_masters())
        restart_proc = None
        host_access_teardown_with_consumers(states, peer_domain)
        final_shutdown(states)
        # Orderly full-cycle done. Now deliberately REINITIALIZE real state
        # for the pod-recreation scenario: run (fresh host access over the
        # same original resolver), save+connect the peer through the agent
        # mutation API, then SIGTERM ONLY - the resolver journal, saved
        # profiles and shared infrastructure are preserved (no teardown), so
        # the recreated pod must recover them on a cold engine.
        run_proc = reinitialize_checks()
        return 0 if STEPS["checks"] and all(item["ok"] for item in STEPS["checks"].values()) else 1
    finally:
        for proc in (run_proc, restart_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(60)
                except subprocess.TimeoutExpired:
                    proc.kill()
        stop_engine(engine)
        rec("finished_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        write_receipt("exercise-workspace.json")


# --- peer role: fresh server-only default -----------------------------------

class Busy53:
    """Hold loopback UDP+TCP :53 so server-only up cannot depend on it."""

    def __init__(self) -> None:
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 53))
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", 53))
        self.tcp.listen(4)

    def close(self) -> None:
        self.udp.close()
        self.tcp.close()


def peer_role() -> int:
    engine = None
    busy = None
    ready = {"ready": False, "checks": STEPS["checks"]}
    try:
        # No sudo on the peer: a fresh server-only up must not need any.
        sudo_check = sh(["sudo", "-n", "true"], timeout=30)
        check("peer_has_no_sudo", sudo_check["rc"] != 0, sudo_check["err"].strip()[:200])
        assert_profile(include_sudo=False)
        busy = Busy53()
        rec("peer_53_held_busy", True)
        engine = start_engine()
        up = portmap(["up"], timeout=900)
        check("server_only_up_with_53_busy", up["rc"] == 0, up["out"][-1500:] + up["err"][-1500:])
        identity = resolv_identity()
        check("server_only_no_host_dns_takeover",
              identity["content"] == ORIGINAL_RESOLV["content"] and identity["inode"] == ORIGINAL_RESOLV["inode"],
              {"content": identity["content"].decode(), "inode": [identity["inode"], ORIGINAL_RESOLV["inode"]]})

        def peer_catalog_ready() -> dict:
            page = http_fetch("127.0.0.1", 80, "localhost")
            if page["status"] != 200:
                raise AssertionError(f"peer catalog status {page['status']}")
            # Server-only readiness: up returned rc 0 AND the authoritative
            # status reports the gateway actually running. No host DNS is
            # required or asserted here by design.
            status_reports_running(require_resolver=False)
            return page

        page = wait_for(peer_catalog_ready, 300, "peer server-only readiness")
        check("server_only_publishes_http", page["status"] == 200, page["status"])
        infrastructure = infrastructure_state()
        coredns = infrastructure["addresses"]["dns"]
        gateway = infrastructure["addresses"]["gateway"]
        dns_probe = (
            "import socket, sys\n"
            "coredns, domain, gateway, upstream = sys.argv[1:5]\n"
            "resolved = socket.getaddrinfo(domain, None, socket.AF_INET)[0][4][0]\n"
            "assert resolved == gateway, f'machine domain view: {resolved}'\n"
            "answer = socket.getaddrinfo(upstream, None, socket.AF_INET)[0][4][0]\n"
            "print('peer-container-dns-ok', resolved, answer)\n"
        )
        result = pm(["run", "--rm", "--network", GATEWAY_NETWORK, "--dns", coredns,
                     "docker.io/library/python:3.12-alpine", "python3", "-c", dns_probe,
                     coredns, DOMAIN, gateway, f"peer-fixture.{ZONE}"], timeout=300)
        check("server_only_container_dns_supplied",
              result["rc"] == 0 and "peer-container-dns-ok" in result["out"],
              (result["out"] or result["err"])[-800:])
        ready = {"ready": all(item["ok"] for item in STEPS["checks"].values()),
                 "domain": DOMAIN, "checks": STEPS["checks"]}
        write_receipt("peer-ready.json", ready)
        if not ready["ready"]:
            raise AssertionError("peer initialization checks failed")
        deadline = time.monotonic() + 5400
        while time.monotonic() < deadline and not (WORKSPACE / "peer-stop").exists():
            time.sleep(2)
        down = portmap(["down"], timeout=600)
        check("server_only_down_ok", down["rc"] == 0, down["out"][-800:] + down["err"][-800:])
        identity = resolv_identity()
        check("server_only_resolver_unchanged_at_end",
              identity["content"] == ORIGINAL_RESOLV["content"] and identity["inode"] == ORIGINAL_RESOLV["inode"],
              {"inode": [identity["inode"], ORIGINAL_RESOLV["inode"]]})
        return 0 if STEPS["checks"] and all(item["ok"] for item in STEPS["checks"].values()) else 1
    finally:
        if busy is not None:
            busy.close()
        stop_engine(engine)
        ready["checks"] = STEPS["checks"]
        write_receipt("peer-ready.json", ready)
        rc = 0 if STEPS["checks"] and all(item["ok"] for item in STEPS["checks"].values()) else 1
        write_receipt("peer-final.json", {"final": True, "returncode": rc, "checks": STEPS["checks"]})


# --- recreated pod role -----------------------------------------------------

def recreated_role() -> int:
    engine = run_proc = None
    try:
        assert_profile()
        new_sha = hashlib.sha256(ORIGINAL_RESOLV["content"]).hexdigest()
        check("recreated_resolver_identity_is_new",
              new_sha != PREVIOUS_RESOLV_SHA256,
              {"new": new_sha, "previous": PREVIOUS_RESOLV_SHA256})
        rows, profiles = saved_ssh_rows()
        check("recreated_saved_connection_retained",
              any(item.get("via") == "ssh" for item in rows)
              and any(profile.get("via") == "ssh" for profile in profiles.values()),
              {"targets": rows, "profile_domains": sorted(profiles)})
        engine = start_engine()
        run_proc = start_run("recreated-run.log")
        health = wait_for(lambda: run_ready(), 600, "recreated plain portmap run readiness", process=run_proc)
        # Cold engine + new pod: the saved SSH connection must be RESTORED
        # by the product itself (setup_client -> _restore_saved_ssh), with a
        # master supervised by the NEW agent worker on the new generation.
        agent_pid = int(health["json"]["pid"])
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not ssh_masters():
            time.sleep(2)
        masters = [master for master in ssh_masters() if run_proc.pid in ancestors(master["pid"])]
        check("recreated_connection_restored",
              resolves("peer.portmap") == ["127.0.0.1"] and bool(masters),
              {"peer_domain": resolves("peer.portmap"), "masters": ssh_masters(),
               "agent_pid": agent_pid})
        check("recreated_restored_master_supervised", bool(masters), ssh_masters())
        identity = resolv_identity()
        check("recreated_takes_over_current_file",
              b"nameserver 127.0.0.1" in identity["content"] and identity["inode"] == ORIGINAL_RESOLV["inode"],
              {"content": identity["content"].decode(), "inode": [identity["inode"], ORIGINAL_RESOLV["inode"]]})
        status = portmap_json(portmap(["status"], timeout=120))
        check("recreated_status_forward_installed",
              status.get("resolver", {}).get("installed") is True
              and status.get("resolver", {}).get("backend") == "forward", status.get("resolver"))
        run_proc.send_signal(signal.SIGTERM)
        try:
            rc = run_proc.wait(90)
        except subprocess.TimeoutExpired:
            run_proc.kill()
            rc = "timeout"
        run_proc = None
        check("recreated_sigterm_clean_exit", rc == 0, rc)
        check("recreated_owned_masters_gone", not ssh_masters(), ssh_masters())
        teardown = portmap(["teardown"], timeout=600)
        check("recreated_teardown_ok", teardown["rc"] == 0,
              teardown["out"][-1500:] + teardown["err"][-1500:])
        identity = resolv_identity()
        restored_new = identity["content"] == ORIGINAL_RESOLV["content"] and identity["inode"] == ORIGINAL_RESOLV["inode"]
        restored_old = hashlib.sha256(identity["content"]).hexdigest() == PREVIOUS_RESOLV_SHA256
        check("recreated_restores_fresh_backup",
              restored_new and not restored_old,
              {"restored_new_identity": restored_new, "restored_dead_identity": restored_old,
               "inode": [identity["inode"], ORIGINAL_RESOLV["inode"]]})
        check("recreated_ports_released",
              port_free("127.0.0.1", 53, socket.SOCK_DGRAM) and port_free("127.0.0.1", 80, socket.SOCK_STREAM), None)
        return 0 if STEPS["checks"] and all(item["ok"] for item in STEPS["checks"].values()) else 1
    finally:
        if run_proc is not None and run_proc.poll() is None:
            run_proc.terminate()
            try:
                run_proc.wait(60)
            except subprocess.TimeoutExpired:
                run_proc.kill()
        stop_engine(engine)
        rec("finished_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        write_receipt("exercise-recreated.json")


# --- receipts / entry point -------------------------------------------------

def receipt_detail(value):
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    raise TypeError(f"unsupported receipt detail type: {type(value).__name__}")


def write_receipt(name: str, payload: dict | None = None) -> None:
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = RECEIPT_DIR / name
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload or STEPS, indent=2, sort_keys=True, default=receipt_detail) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    roles = {"workspace": workspace_role, "peer": peer_role, "recreated": recreated_role}
    try:
        return roles[ROLE]()
    except BaseException as exc:
        STEPS["fatal"] = f"{type(exc).__name__}: {exc}"
        check("no_fatal_error", False, STEPS["fatal"])
        return 1
    finally:
        if ROLE == "workspace":
            write_receipt("exercise-workspace.json")
        elif ROLE == "recreated":
            write_receipt("exercise-recreated.json")


if __name__ == "__main__":
    sys.exit(main())
