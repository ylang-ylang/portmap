#!/usr/bin/env python3
"""Bounded prepare/exercise/cleanup driver for the nested-workspace fixture.

Runs as an UNPRIVILEGED outer test operator on a dedicated verification VM
(a normal login user with an existing subuid range; never root, never host
root maps). The outer engine is ROOTLESS Podman with its own private
storage/runroot/config namespace and userspace uplink (slirp4netns or
pasta), so container networking never touches the VM's global forwarding
sysctls (ip_forward stays whatever the host policy says). Everything the
driver creates carries the run label; exact IDs are recorded in
``manifest.json`` immediately after creation, and cleanup removes only
those exact IDs after re-inspecting their ownership labels.

The driver never runs a sweep, prune, ``--all`` or wildcard form; it never
changes VM-global DNS, NSS, sysctls, services, subuid/subgid files or SSH
configuration; it never touches containers, networks or images outside its
private store. The only files it creates are inside its own scratch state
directory plus the receipts directory.

Commands (each independently bounded, each writing one JSON receipt):

    python -m tests.fixtures.nested_workspace.driver baseline  --state-dir D
    python -m tests.fixtures.nested_workspace.driver prepare   --state-dir D
    python -m tests.fixtures.nested_workspace.driver exercise  --state-dir D
    python -m tests.fixtures.nested_workspace.driver cleanup   --state-dir D
    python -m tests.fixtures.nested_workspace.driver status    --state-dir D

``--state-dir`` is disposable scratch (removed after a successful cleanup);
receipts and the manifest survive in ``--out-dir`` (default: sibling
``<state-dir>-out``). Source-mode proof: the current worktree's ``src/``,
``pyproject.toml`` and the two fixture modules are staged into the
workspace containers; no packaged binary is claimed.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import pwd
import random
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

from . import dns_fixture

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[2]

LABEL_RUN = "portmap.nested-fixture"
LABEL_ROLE = "portmap.nested.role"
# Observed target profile: CapBnd 0x802001db even under sudo.
CAP_PROFILE = (
    "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID",
    "SETGID", "SETUID", "SETPCAP", "SYS_ADMIN", "SETFCAP",
)
DNS_ZONE = "portmap-nest.internal"
DNS_RECORD_DUAL_A = "198.51.100.10"      # TEST-NET-2: fixture DNS data only
DNS_RECORD_DUAL_AAAA = "2001:db8::10"    # documentation range, never routed
DNS_RECORD_V6ONLY = "2001:db8::11"
GATEWAY_NETWORK = "portmap_nested_gw"    # inner (in-workspace) network name

TIMEOUTS = {
    "build": 1800, "network": 60, "run": 240, "cp": 180,
    "exec_setup": 240, "keygen": 30, "probe": 90, "peer_ready": 2100,
    "workspace_exercise": 2700, "recreated_exercise": 2100,
    "peer_final": 600, "recreate": 300, "remove": 120,
}


class DriverError(RuntimeError):
    pass


def run_command(argv: list[str], *, timeout: int, cwd: Path | None = None,
                env: dict[str, str] | None = None) -> dict:
    argv = [str(item) for item in argv]
    try:
        process = subprocess.Popen(
            argv, cwd=str(cwd) if cwd else None, env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return {"argv": argv, "returncode": 127, "timed_out": False, "stdout": "", "stderr": str(exc)}
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass
        stdout, stderr = process.communicate()
        stderr = (stderr or "") + f"\n[driver] deadline of {timeout}s expired"
    return {
        "argv": argv, "returncode": process.returncode, "timed_out": timed_out,
        "stdout": stdout or "", "stderr": stderr or "",
    }


def checked(result: dict, what: str) -> dict:
    if result["returncode"] != 0 or result["timed_out"]:
        raise DriverError(
            f"{what} failed (rc={result['returncode']}, timed_out={result['timed_out']})\n"
            f"argv: {result['argv']}\nstdout:\n{result['stdout']}\nstderr:\n{result['stderr']}"
        )
    return result


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class Podman:
    """Every call scoped to the fixture's private rootless namespace.

    Scope per podman(1) docs: ``--root``/``--runroot`` for the store,
    ``--tmpdir`` inside our owned XDG runtime dir (which itself must sit
    under the operator's real /run/user/<uid> because Debian's pasta
    AppArmor profile only permits paths there), and
    ``--network-config-dir`` for netavark. ``--config`` is NOT used: it is
    the Docker-auth config path, not containers.conf; the engine config is
    selected with the ``CONTAINERS_CONF`` environment variable and storage
    with ``CONTAINERS_STORAGE_CONF``. ``CONTAINER_HOST`` /
    ``CONTAINER_CONNECTION`` are unset so no accidental remote engine can
    be selected. USER/LOGNAME/HOME are pinned to the real pwd entry
    because an inherited root identity makes rootless Podman choose a
    broken single-id map.
    """

    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / "storage"
        self.conf_dir = state_dir / "containers-conf"
        self.network_conf = state_dir / "netavark"
        operator = pwd.getpwuid(os.getuid())
        runtime_base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{operator.pw_uid}")
        if not runtime_base.is_dir():
            # Never create or chmod the operator's real runtime base; a
            # missing base means no usable login context for rootless podman.
            raise DriverError(
                f"operator runtime base {runtime_base} does not exist; run the driver "
                "from a login context with a valid XDG_RUNTIME_DIR"
            )
        # Hash-named private runtime dir: no cross-run suffix collisions.
        self.runtime = runtime_base / ("portmap-nested-" + sha256_bytes(str(state_dir).encode())[:16])
        self.runroot = self.runtime / "runroot"
        for directory in (self.root, self.runroot, self.runtime,
                          self.conf_dir / "containers.conf.d", self.network_conf):
            directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.runtime, 0o700)
        os.chmod(self.network_conf, 0o700)
        (self.conf_dir / "containers.conf").write_text(
            "[engine]\ncgroup_manager = \"cgroupfs\"\nevents_logger = \"file\"\n",
            encoding="utf-8",
        )
        # Explicit private storage config: kernel overlay, no mount program.
        # With --root, storage options from system defaults are not merged,
        # so the driver (not an implicit default) owns this choice; a kernel
        # that cannot do userns overlay fails loudly here rather than
        # silently switching drivers.
        (self.conf_dir / "storage.conf").write_text(
            "[storage]\ndriver = \"overlay\"\n", encoding="utf-8"
        )
        self.environment = dict(os.environ)
        for key in ("CONTAINER_HOST", "CONTAINER_CONNECTION"):
            self.environment.pop(key, None)
        self.environment.update({
            "XDG_RUNTIME_DIR": str(self.runtime),
            "CONTAINERS_CONF": str(self.conf_dir / "containers.conf"),
            "CONTAINERS_STORAGE_CONF": str(self.conf_dir / "storage.conf"),
            "USER": operator.pw_name,
            "LOGNAME": operator.pw_name,
            "HOME": operator.pw_dir,
        })

    def argv(self, *args: object) -> list[str]:
        return [
            "podman", "--root", str(self.root), "--runroot", str(self.runroot),
            "--tmpdir", str(self.runtime / "tmp"),
            "--network-config-dir", str(self.network_conf),
            "--events-backend", "file", *(str(a) for a in args),
        ]

    def run(self, *args: object, timeout: int = 120) -> dict:
        return run_command(self.argv(*args), timeout=timeout, env=self.environment)

    def raw(self, *args: object, timeout: int = 120, cwd: Path | None = None) -> dict:
        """Raw argv form (build/cp with custom cwd) sharing the same env."""
        return run_command(self.argv(*args), timeout=timeout, cwd=cwd, env=self.environment)


# --- host baseline -----------------------------------------------------------


def fingerprint(path: Path) -> dict:
    info = path.lstat()
    payload = {
        "mode": oct(info.st_mode & 0o7777), "uid": info.st_uid, "gid": info.st_gid,
        "inode": info.st_ino, "symlink": os.readlink(path) if path.is_symlink() else None,
    }
    if path.is_file():
        payload["sha256"] = sha256_file(path)
    return payload


def live_userns_maps() -> list[list[int]]:
    ranges: set[tuple[int, int]] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        for name in ("uid_map", "gid_map"):
            try:
                for line in (entry / name).read_text().splitlines():
                    parts = line.split()
                    if len(parts) == 3:
                        ranges.add((int(parts[1]), int(parts[2])))
            except OSError:
                continue
    return sorted([base, base + length] for base, length in ranges)


def route_networks() -> list[str]:
    networks: list[str] = []
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) < 8:
                continue
            try:
                destination = int(parts[1], 16)
                mask = int(parts[7], 16)
            except ValueError:
                continue
            if mask == 0:
                continue
            address = ipaddress.IPv4Address(destination.to_bytes(4, sys.byteorder))
            netmask = ipaddress.IPv4Address(mask.to_bytes(4, sys.byteorder))
            networks.append(str(ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)))
    except OSError:
        pass
    return networks


def host_baseline() -> dict:
    files = {}
    for name in ("/etc/resolv.conf", "/etc/nsswitch.conf", "/etc/subuid", "/etc/subgid"):
        path = Path(name)
        files[name] = fingerprint(path) if path.exists() else None
    sysctls = {
        "/proc/sys/net/ipv4/ip_unprivileged_port_start": "lowport",
        "/proc/sys/net/ipv4/ip_forward": "ip_forward",
        "/proc/sys/net/ipv6/conf/all/forwarding": "ipv6_all_forwarding",
        "/proc/sys/net/ipv6/conf/default/forwarding": "ipv6_default_forwarding",
        "/proc/sys/user/max_user_namespaces": "userns_max",
    }
    readings = {}
    for path, key in sysctls.items():
        try:
            readings[key] = Path(path).read_text().strip()
        except OSError:
            readings[key] = None
    # Read-only inventory of other engines: never invoke the podman CLI
    # against unrelated stores (that can initialize a store or spawn a
    # pause process); observe the on-disk databases directly instead.
    operator = pwd.getpwuid(os.getuid())
    store_databases = {
        "rootful_store_root": "/var/lib/containers/storage",
        "rootful_containers": "/var/lib/containers/storage/overlay-containers/containers.json",
        "rootful_networks": "/var/lib/containers/networks/database",
        "rootless_store_root": f"{operator.pw_dir}/.local/share/containers/storage",
        "rootless_containers": f"{operator.pw_dir}/.local/share/containers/storage/overlay-containers/containers.json",
        "rootless_network_config_root": f"{operator.pw_dir}/.config/containers/networks",
        "rootless_networks": f"{operator.pw_dir}/.config/containers/networks/database",
    }
    default_podman = {}
    for key, path in store_databases.items():
        entry = Path(path)
        try:
            default_podman[key] = fingerprint(entry) if entry.exists() else None
        except OSError as exc:
            default_podman[key] = {"unavailable_errno": exc.errno}
    docker = run_command(["docker", "--host", "unix:///var/run/docker.sock", "ps", "-aq"], timeout=30)
    return {
        "files": files,
        "sysctls": readings,
        "live_userns_maps": live_userns_maps(),
        "routes": route_networks(),
        "default_podman_store": default_podman,
        "default_docker_store": sorted({line for line in docker["stdout"].splitlines() if line}) if docker["returncode"] == 0 else "unavailable",
        "kernel": Path("/proc/sys/kernel/osrelease").read_text().strip(),
    }


def choose_subnet(baseline: dict) -> str:
    routed = [ipaddress.ip_network(net) for net in baseline["routes"]]
    pools = ["10.213", "10.214", "10.215", "10.216"]
    for pool in pools:
        for third in random.Random().sample(range(1, 255), 254):
            candidate = ipaddress.ip_network(f"{pool}.{third}.0/24")
            if not any(candidate.overlaps(net) for net in routed):
                return str(candidate)
    raise DriverError("no free /24 found for the fixture network")


# --- prerequisites ----------------------------------------------------------

COMPOSE_PLUGIN_PATHS = (
    "/usr/libexec/docker/cli-plugins/docker-compose",
    "/usr/lib/docker/cli-plugins/docker-compose",
    "/usr/local/lib/docker/cli-plugins/docker-compose",
    "/usr/local/libexec/docker/cli-plugins/docker-compose",
)


def check_prerequisites() -> dict:
    problems: list[str] = []
    facts: dict = {}
    if os.geteuid() == 0:
        problems.append("driver must run as an unprivileged operator (rootless outer Podman), not root")
    entry = pwd.getpwuid(os.getuid())
    facts["operator"] = {"name": entry.pw_name, "uid": entry.pw_uid, "gid": entry.pw_gid, "home": entry.pw_dir}
    subuid_total = 0
    try:
        for line in Path("/etc/subuid").read_text().splitlines():
            fields = line.split(":")
            if fields[0] == entry.pw_name and len(fields) == 3 and fields[2].isdigit():
                subuid_total += int(fields[2])
    except OSError:
        pass
    facts["operator_subuid_ids"] = subuid_total
    if subuid_total < 65536:
        problems.append(f"operator {entry.pw_name} needs at least 65536 subuid ids for rootless nesting")
    version = run_command(["podman", "--version"], timeout=30)
    if version["returncode"] != 0:
        problems.append(f"outer Podman not usable: {version['stderr'].strip()}")
    else:
        facts["podman"] = version["stdout"].strip()
    uplink = next((name for name in ("pasta", "slirp4netns") if shutil.which(name)), None)
    facts["userspace_uplink"] = uplink
    if uplink is None:
        problems.append("neither pasta nor slirp4netns is installed; rootless outer networking needs one")
    fuse = Path("/dev/fuse")
    if fuse.exists():
        info = fuse.stat()
        facts["dev_fuse"] = {"major": os.major(info.st_rdev), "minor": os.minor(info.st_rdev), "mode": oct(info.st_mode & 0o777)}
    else:
        facts["dev_fuse"] = None
        problems.append("/dev/fuse is absent; this fixture requires it for the target profile")
    facts["dev_tun"] = Path("/dev/net/tun").is_char_device()
    if not facts["dev_tun"]:
        problems.append("/dev/net/tun is absent; the inner rootless engine needs its userspace network device")
    userns = Path("/proc/sys/user/max_user_namespaces").read_text().strip()
    if int(userns or "0") <= 0:
        problems.append("user namespaces are disabled on the host")
    facts["userns_max"] = userns
    facts["lowport"] = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start").read_text().strip()
    facts["ssh_client"] = shutil.which("ssh") or ""
    if not facts["ssh_client"]:
        problems.append("an ssh client is required on the operator host")
    docker_cli = shutil.which("docker")
    facts["docker_cli"] = docker_cli or ""
    if not docker_cli:
        problems.append("a docker CLI is required to stage into the fixture image")
    compose = next((p for p in COMPOSE_PLUGIN_PATHS if Path(p).exists()), None)
    if compose is None:
        problems.append("docker compose plugin binary not found in known locations")
    facts["docker_compose"] = compose or ""
    upstreams = original_upstreams()
    if not upstreams:
        problems.append("/etc/resolv.conf exposes no non-loopback upstream for the fixture DNS forwarder")
    facts["vm_upstreams"] = upstreams
    if problems:
        raise DriverError("prerequisites failed: " + "; ".join(problems))
    return facts


def original_upstreams() -> list[str]:
    upstreams: list[str] = []
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                address = ipaddress.ip_address(fields[1])
                if not address.is_loopback:
                    upstreams.append(fields[1])
    except OSError:
        return []
    return upstreams


# --- SSH material -----------------------------------------------------------

def generate_ssh_material(keys: Path, workspace_ip: str, peer_ip: str) -> dict:
    keys.mkdir(parents=True, exist_ok=True)
    os.chmod(keys, 0o700)
    material = {"dir": str(keys)}

    def keygen(path: Path, comment: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        checked(run_command(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
            timeout=TIMEOUTS["keygen"],
        ), f"ssh-keygen {path.name}")
        os.chmod(path, 0o600)
        return path.with_suffix(".pub").read_text().strip()

    material["client_public"] = keygen(keys / "id_ed25519", "portmap-nested-fixture")
    ws_pub = keygen(keys / "ws-hostkeys" / "ssh_host_ed25519_key", "nested-workspace-host")
    peer_pub = keygen(keys / "peer-hostkeys" / "ssh_host_ed25519_key", "nested-peer-host")
    material["ws_host_public"] = ws_pub
    material["peer_host_public"] = peer_pub
    (keys / "authorized_keys").write_text(material["client_public"] + "\n", encoding="utf-8")
    os.chmod(keys / "authorized_keys", 0o644)
    (keys / "known_hosts").write_text(
        f"{workspace_ip} {ws_pub}\n{peer_ip} {peer_pub}\n", encoding="utf-8"
    )
    os.chmod(keys / "known_hosts", 0o644)
    pinned = (
        "Host *\n"
        "  User workspace-user\n"
        "  IdentityFile /workspace/ssh/id_ed25519\n"
        "  IdentitiesOnly yes\n"
        "  UserKnownHostsFile /workspace/ssh/known_hosts\n"
        "  StrictHostKeyChecking yes\n"
        "  ConnectTimeout 10\n"
    )
    (keys / "ssh_config").write_text(pinned, encoding="utf-8")
    (keys / "ssh_config_incoming").write_text(pinned, encoding="utf-8")
    # sshd runs as the ordinary uid 1000 (the observed cap profile has no
    # SYS_CHROOT, so root privilege separation cannot chroot). The driver
    # copies the pinned host key and authorized_keys into /workspace/ssh
    # owned by uid 1000 before starting it; sshd never reads /fixture-keys.
    for role in ("workspace", "peer"):
        (keys / f"{role}-sshd.conf").write_text(
            "Port 22\n"
            "ListenAddress 0.0.0.0\n"
            "HostKey /workspace/ssh/host_ed25519_key\n"
            "AuthorizedKeysFile /workspace/ssh/authorized_keys\n"
            "PermitRootLogin no\n"
            "PubkeyAuthentication yes\n"
            "PasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\n"
            "UsePAM no\n"
            "PidFile /workspace/ssh/sshd.pid\n"
            "LogLevel VERBOSE\n",
            encoding="utf-8",
        )
    return material


# --- source staging ---------------------------------------------------------

def build_source_archive(destination: Path) -> dict:
    members: list[tuple[Path, str]] = []

    def add_tree(root: Path, arc_root: str) -> None:
        for path in sorted(root.rglob("*")):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_file():
                members.append((path, f"{arc_root}/{path.relative_to(root)}"))

    add_tree(REPO_ROOT / "src", "src")
    members.append((REPO_ROOT / "pyproject.toml", "pyproject.toml"))
    members.append((REPO_ROOT / "README.md", "README.md"))
    members.append((REPO_ROOT / "tests" / "fixtures" / "test_repo_manager.py", "tests/fixtures/test_repo_manager.py"))
    members.append((PACKAGE_DIR / "exercise.py", "tests/fixtures/nested_workspace/exercise.py"))
    members.append((PACKAGE_DIR / "dns_fixture.py", "tests/fixtures/nested_workspace/dns_fixture.py"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        for path, arcname in members:
            archive.add(path, arcname=arcname)
    return {"archive": str(destination), "members": len(members), "sha256": sha256_file(destination)}


def stage_source_into(podman: Podman, container_id: str, archive: Path) -> dict:
    checked(podman.raw("cp", str(archive), f"{container_id}:/workspace/staged.tar",
                       timeout=TIMEOUTS["cp"]), "podman cp source archive")
    checked(podman.run(
        "exec", "-u", "0", container_id, "/bin/sh", "-c",
        "mkdir -p /workspace/source /workspace/logs && tar -xzf /workspace/staged.tar -C /workspace/source "
        "&& rm -f /workspace/staged.tar && chown 1000:1000 /workspace "
        "&& chown -R 1000:1000 /workspace/source /workspace/logs",
        timeout=TIMEOUTS["exec_setup"],
    ), "extract staged source")
    return {"destination": "/workspace/source"}


# --- prepare ----------------------------------------------------------------

def workspace_run_flags(manifest: dict, role: str, ip: str, *, name: str | None = None,
                        resolver: str | None = None, role_label: str | None = None,
                        scratch_role: str | None = None, publish_ssh: bool = False) -> list[str]:
    """Exact outer run flags for one workspace pod. The recreated pod passes
    its own role label and resolver file while reusing the workspace scratch
    (persistent portmap state) and the same pod IP. ``publish_ssh`` adds a
    loopback publication for the driver's host-side probe; rootless bridge
    IPs are unreachable from the host, so probes go through rootlessport."""
    scratch_role = scratch_role or role
    flags = [
        "run", "-d", "--name", name or manifest["names"][role],
        "--label", f"{LABEL_RUN}={manifest['run_id']}",
        "--label", f"{LABEL_ROLE}={role_label or role}",
        # Rootless outer engine: the workspace maps the full 65536-ID range of
        # its own user namespace (never host uid 0; host side is the operator's
        # dedicated subuid range). Explicit for evidence.
        "--uidmap", "0:0:65536",
        "--gidmap", "0:0:65536",
        "--cap-drop", "ALL",
        *(arg for cap in CAP_PROFILE for arg in ("--cap-add", cap)),
        "--security-opt", "seccomp=unconfined",
        "--security-opt", "apparmor=unconfined",
        "--security-opt", "unmask=/proc/*",
        "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
        "--user", "1000:1000",
        "--network", manifest["network"]["name"], "--ip", ip,
        "--device", "/dev/fuse",
        "--device", "/dev/net/tun",
        "-v", f"{manifest['scratch'][scratch_role]}:/workspace",
        "-v", f"{manifest['out_dir']}:/receipts",
        "-v", f"{manifest['ssh']['dir']}:/fixture-keys:ro",
        "-v", f"{manifest['ssh']['dir']}/ssh_config:/home/workspace-user/.ssh/config:ro",
        "-v", f"{resolver or manifest['resolver'][scratch_role]}:/etc/resolv.conf",
    ]
    if publish_ssh:
        flags += ["--publish", "127.0.0.1::22/tcp"]
    flags.append(manifest["image"]["tag"])
    return flags


def prepare(state_dir: Path, out_dir: Path) -> int:
    if state_dir.exists() and any(state_dir.iterdir()):
        raise DriverError(f"state dir {state_dir} is not empty; refuse to reuse")
    state_dir.mkdir(parents=True)
    run_id = "nested-" + uuid.uuid4().hex[:10]
    receipt: dict = {"command": "prepare", "run_id": run_id, "steps": {}, "ok": False}
    try:
        facts = check_prerequisites()
        receipt["prerequisites"] = facts
        baseline = host_baseline()
        write_json(out_dir / "baseline-before.json", baseline)

        subnet = choose_subnet(baseline)
        receipt["steps"]["selection"] = {"subnet": subnet, "outer_mode": "rootless-userspace-uplink"}

        network_address = ipaddress.ip_network(subnet).network_address
        ips = {name: str(ipaddress.ip_address(int(network_address) + offset))
               for name, offset in (("dns", 10), ("workspace", 11), ("peer", 12))}

        podman = Podman(state_dir)
        keys = state_dir / "keys"
        material = generate_ssh_material(keys, ips["workspace"], ips["peer"])

        resolver_dir = state_dir / "resolv"
        resolver_dir.mkdir(parents=True)
        resolv_content = (
            "# portmap nested-workspace fixture resolver (original upstream)\n"
            f"nameserver {ips['dns']}\n"
            f"search svc.{DNS_ZONE} {DNS_ZONE}\n"
            "options ndots:5\n"
        )
        resolver: dict[str, str] = {}
        for role in ("workspace", "peer"):
            path = resolver_dir / f"{role}-resolv.conf"
            path.write_text(resolv_content, encoding="utf-8")
            os.chmod(path, 0o644)  # owned by the operator == container uid 0
            resolver[role] = str(path)

        build_context = state_dir / "build-context"
        build_context.mkdir()
        for name in ("Containerfile", "dns_fixture.py"):
            shutil.copy2(PACKAGE_DIR / name, build_context / name)
        shutil.copy2(facts["docker_cli"], build_context / "docker-cli")
        shutil.copy2(facts["docker_compose"], build_context / "docker-compose")
        binaries = {}
        for source, staged in ((facts["docker_cli"], "docker-cli"), (facts["docker_compose"], "docker-compose")):
            version = run_command([str(build_context / staged), "--version"], timeout=30)
            binaries[staged] = {"source": source, "sha256": sha256_file(build_context / staged),
                                "version": version["stdout"].strip() or version["stderr"].strip()}
        receipt["steps"]["staged_binaries"] = binaries

        image_tag = f"portmap-nested-workspace:{run_id}"
        checked(podman.raw("build", "--isolation=chroot", "-t", image_tag, "--label", f"{LABEL_RUN}={run_id}",
                           "-f", "Containerfile", ".",
                           cwd=build_context, timeout=TIMEOUTS["build"]),
                "build fixture image")
        image_id = json.loads(checked(podman.run("inspect", "--type", "image", image_tag, timeout=60),
                                      "inspect image")["stdout"])[0]["Id"]

        network_name = f"portmap-nested-net-{run_id}"
        net = checked(podman.run("network", "create", "--disable-dns", "--subnet", subnet,
                                 "--label", f"{LABEL_RUN}={run_id}", network_name,
                                 timeout=TIMEOUTS["network"]), "create fixture network")
        network_id = net["stdout"].strip()

        records = {
            f"peer-fixture.{DNS_ZONE}": {"A": ips["peer"]},
            f"dual.{DNS_ZONE}": {"A": DNS_RECORD_DUAL_A, "AAAA": DNS_RECORD_DUAL_AAAA},
            f"v6only.{DNS_ZONE}": {"AAAA": DNS_RECORD_V6ONLY},
        }
        dns_args = ["run", "-d", "--name", f"portmap-nested-dns-{run_id}",
                    "--label", f"{LABEL_RUN}={run_id}", "--label", f"{LABEL_ROLE}=dns",
                    "--network", network_name, "--ip", ips["dns"],
                    # Rootless bridge IPs are unreachable from the host; the
                    # readiness probe uses loopback publications instead.
                    "--publish", "127.0.0.1::53/udp", "--publish", "127.0.0.1::53/tcp",
                    "-v", f"{PACKAGE_DIR / 'dns_fixture.py'}:/dns/dns_fixture.py:ro",
                    "python:3.12-alpine",
                    "python3", "/dns/dns_fixture.py", "--listen", "0.0.0.0", "--port", "53",
                    "--zone", DNS_ZONE]
        for name, types in records.items():
            for rtype, value in types.items():
                dns_args += ["--record", f"{name}={rtype}:{value}"]
        for upstream in facts["vm_upstreams"]:
            dns_args += ["--forward", upstream]
        dns_container = checked(podman.run(*dns_args, timeout=TIMEOUTS["run"]), "run dns fixture")["stdout"].strip()
        dns_udp_port = _published_port(podman, dns_container, "53/udp")
        dns_tcp_port = _published_port(podman, dns_container, "53/tcp")

        def wait_dns() -> None:
            answer = dns_fixture.query("127.0.0.1", f"peer-fixture.{DNS_ZONE}",
                                       dns_fixture.QTYPE_A, port=dns_udp_port, timeout=3)
            if answer["rcode"] != 0 or not answer["answers"]:
                raise DriverError(f"fixture DNS not answering on published port: {answer}")
        wait_until(wait_dns, TIMEOUTS["probe"], "fixture DNS readiness")

        manifest = {
            "version": 1, "run_id": run_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "fixture_root": str(PACKAGE_DIR), "repo_root": str(REPO_ROOT),
            "state_dir": str(state_dir), "out_dir": str(out_dir),
            "labels": {LABEL_RUN: run_id},
            "outer": {"podman": facts["podman"], "kernel": baseline["kernel"],
                      "mode": "rootless", "uplink": facts["userspace_uplink"],
                      "operator": facts["operator"],
                      "storage": str(podman.root), "runroot": str(podman.runroot),
                      "containers_conf": str(podman.conf_dir / "containers.conf"),
                      "storage_conf": str(podman.conf_dir / "storage.conf"),
                      "network_config_dir": str(podman.network_conf)},
            "userns": {
                "mode": "outer-rootless: container uid 0 maps to the intermediate namespace uid 0 "
                        "(the unprivileged operator's own host uid), and container uids 1-65535 map "
                        "onto the operator's contiguous subordinate host range - a two-segment map "
                        "that never includes host uid 0 and never overlaps other host identities",
                "container_visible_map": [[0, 0, 65536]],
                "mapping_verification": (
                    "Verified rootless Podman semantics: --uidmap values index the intermediate "
                    "namespace, so 0:1:65536 would map container uid 0 onto the operator's first "
                    "SUBORDINATE host uid (100000). That variant was rejected for this fixture "
                    "because the operator-owned resolver bind and /workspace scratch would fall "
                    "outside the container's host-id range (visible as overflow uid 65534) and a "
                    "non-root operator cannot chown them into the subordinate range, breaking the "
                    "target-observed root-owned 0644 resolver profile. 0:0:65536 keeps a contiguous "
                    "isolated 65536-ID map that never includes host uid 0: container uid 0 is the "
                    "unprivileged operator and container uid 1000 maps onto a subordinate host id."
                ),
            },
            "network": {"name": network_name, "id": network_id, "subnet": subnet, **ips},
            "image": {"tag": image_tag, "id": image_id},
            "names": {"dns": f"portmap-nested-dns-{run_id}",
                      "workspace": f"portmap-nested-ws-{run_id}",
                      "workspace_recreated": f"portmap-nested-ws-{run_id}-r2",
                      "peer": f"portmap-nested-peer-{run_id}"},
            "containers": {}, "binaries": binaries, "ssh": material,
            "resolver": resolver, "resolv_content": resolv_content,
            "resolv_sha256": sha256_bytes(resolv_content.encode()),
            "dns_fixture": {"zone": DNS_ZONE, "records": records,
                            "upstreams": facts["vm_upstreams"]},
            "gateway_network": GATEWAY_NETWORK,
            "scratch": {role: str(state_dir / "workspaces" / role) for role in ("workspace", "peer")},
            "cap_profile": list(CAP_PROFILE), "expected_cap_bnd": "00000000802001db",
        }
        for role in ("workspace", "peer"):
            (state_dir / "workspaces" / role).mkdir(parents=True)
        container_ids: dict[str, str] = {}
        run_flags: dict[str, list[str]] = {}
        ssh_ports: dict[str, int] = {}
        for role in ("workspace", "peer"):
            flags = workspace_run_flags(manifest, role, ips[role], publish_ssh=True)
            run_flags[role] = flags
            container_ids[role] = checked(podman.run(*flags, timeout=TIMEOUTS["run"]),
                                          f"run {role} container")["stdout"].strip()
            ssh_ports[role] = _published_port(podman, container_ids[role], "22/tcp")
            manifest["containers"][role] = {
                "id": container_ids[role], "name": manifest["names"][role], "ip": ips[role],
                "ssh_port": ssh_ports[role],
            }
        manifest["containers"]["dns"] = {
            "id": dns_container, "name": manifest["names"]["dns"], "ip": ips["dns"],
            "udp_port": dns_udp_port, "tcp_port": dns_tcp_port,
        }
        # Pinned host keys cover both in-network IPs (container-to-container)
        # and the published loopback ports the driver itself probes through.
        keys_dir = Path(manifest["ssh"]["dir"])
        (keys_dir / "known_hosts").write_text(
            f"{ips['workspace']} {material['ws_host_public']}\n"
            f"{ips['peer']} {material['peer_host_public']}\n"
            f"[127.0.0.1]:{ssh_ports['workspace']} {material['ws_host_public']}\n"
            f"[127.0.0.1]:{ssh_ports['peer']} {material['peer_host_public']}\n",
            encoding="utf-8",
        )
        os.chmod(keys_dir / "known_hosts", 0o644)
        manifest["run_flags"] = run_flags
        for role, entry in manifest["containers"].items():
            labels = json.loads(checked(podman.run("inspect", entry["id"], timeout=60),
                                        f"inspect {role}")["stdout"])[0]["Config"]["Labels"]
            if labels.get(LABEL_RUN) != run_id:
                raise DriverError(f"{role} container {entry['id']} is missing the run label")
        receipt["steps"]["objects_created"] = {role: entry["id"] for role, entry in manifest["containers"].items()}

        archive_info = build_source_archive(state_dir / "source.tar.gz")
        receipt["steps"]["source_archive"] = archive_info
        for role in ("workspace", "peer"):
            stage_source_into(podman, container_ids[role], state_dir / "source.tar.gz")

        write_json(state_dir / "manifest.json", manifest)
        write_json(out_dir / "manifest.json", manifest)
        receipt["ok"] = True
        receipt["manifest"] = str(state_dir / "manifest.json")
        return 0
    except BaseException as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        # A failed prepare never leaves fixture objects behind: remove the
        # exact labeled objects created so far, preserving diagnostics.
        receipt["rollback"] = _rollback_prepared(state_dir, run_id)
        return 1
    finally:
        write_json(out_dir / "prepare.json", receipt)


def _rollback_prepared(state_dir: Path, run_id: str) -> dict:
    """Best-effort exact-label removal after a failed prepare.

    Discovery uses ``--filter label=`` only (no output-shape parsing, so
    version-specific JSON key spellings cannot silently disable ownership
    checks); removal still goes through the typed exact-ID path.
    """
    result: dict = {}
    selector = f"{LABEL_RUN}={run_id}"
    try:
        podman = Podman(state_dir)
        containers = podman.run("ps", "-aq", "--filter", f"label={selector}", timeout=60)
        for container_id in [line.strip() for line in containers["stdout"].splitlines() if line.strip()]:
            result[f"container:{container_id[:12]}"] = remove_exact(
                podman, "container", container_id, {LABEL_RUN: run_id})
        networks = podman.run("network", "ls", "-q", "--filter", f"label={selector}", timeout=60)
        for network_id in [line.strip() for line in networks["stdout"].splitlines() if line.strip()]:
            result[f"network:{network_id[:12]}"] = remove_exact(
                podman, "network", network_id, {LABEL_RUN: run_id})
        images = podman.run("images", "-q", "--filter", f"label={selector}", timeout=60)
        for image_id in [line.strip() for line in images["stdout"].splitlines() if line.strip()]:
            result[f"image:{image_id[:12]}"] = remove_exact(podman, "image", image_id, {LABEL_RUN: run_id})
    except BaseException as exc:  # noqa: BLE001 - rollback must never mask the original failure
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def wait_until(action, timeout: int, what: str, interval: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            action()
            return
        except Exception as exc:  # noqa: BLE001 - poll loop reports the last error
            last = exc
            time.sleep(interval)
    raise DriverError(f"timed out after {timeout}s waiting for {what}: {last}")

def _published_port(podman: Podman, container_id: str, spec: str) -> int:
    """Read back one exact container's published loopback port; fail fast."""
    result = checked(podman.run("port", container_id, spec, timeout=60), f"podman port {spec}")
    for line in result["stdout"].splitlines():
        tail = line.rsplit(":", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    raise DriverError(f"no published host port found for {container_id} {spec}: {result['stdout']!r}")


# --- exercise ---------------------------------------------------------------

def podman_exec(podman: Podman, container_id: str, *args: object, user: str = "1000:1000",
                detach: bool = False, env: dict[str, str] | None = None, timeout: int = 120) -> dict:
    argv = ["exec"]
    if detach:
        argv.append("-d")
    argv += ["-u", user]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    if user == "1000:1000":
        # OCI exec grants the configured caps to this user; ordinary
        # workspace sessions must retain only the bounding set.
        args = ("setpriv", "--inh-caps=-all", "--ambient-caps=-all", "--", *args)
    return run_command(podman.argv(*argv, container_id, *args), timeout=timeout, env=podman.environment)


def nested_env(manifest: dict, role: str) -> dict[str, str]:
    domain = "peer.portmap" if role == "peer" else "workspace.portmap"
    env = {
        "NESTED_ROLE": role,
        "NESTED_RECEIPT_DIR": "/receipts",
        "NESTED_SOURCE": "/workspace/source",
        "NESTED_POD_IP": manifest["containers"]["workspace_recreated"]["ip"] if role == "recreated"
                         else manifest["containers"][role]["ip"],
        "NESTED_DNS_IP": manifest["network"]["dns"],
        "NESTED_WS_IP": manifest["containers"]["workspace"]["ip"],
        "NESTED_PEER_IP": manifest["containers"]["peer"]["ip"],
        "NESTED_ZONE": manifest["dns_fixture"]["zone"],
        "NESTED_DOMAIN": domain,
        "NESTED_GATEWAY_NETWORK": manifest["gateway_network"],
        "NESTED_DUAL_A": DNS_RECORD_DUAL_A,
        "NESTED_DUAL_AAAA": DNS_RECORD_DUAL_AAAA,
        "NESTED_V6ONLY": DNS_RECORD_V6ONLY,
    }
    if role == "recreated":
        env["NESTED_PREVIOUS_RESOLV_SHA256"] = manifest["resolv_sha256"]
    return env


def verify_objects(podman: Podman, manifest: dict) -> dict:
    status = {}
    for role, entry in manifest["containers"].items():
        result = podman.run("inspect", entry["id"], timeout=60)
        if result["returncode"] != 0:
            raise DriverError(f"{role} container {entry['id']} is gone before exercise")
        labels = json.loads(result["stdout"])[0]["Config"]["Labels"]
        if labels.get(LABEL_RUN) != manifest["run_id"] or labels.get(LABEL_ROLE) != role:
            raise DriverError(f"{role} container {entry['id']} fails ownership verification")
        status[role] = "ok"
    return status


def start_sshd(podman: Podman, manifest: dict, role: str, container_id: str | None = None) -> dict:
    """Run sshd as the ordinary uid 1000, not root.

    The observed target cap profile omits CAP_SYS_CHROOT even under sudo,
    so a root sshd could not perform privilege separation. Container root
    only stages uid-1000-owned material into /workspace/ssh (0700): the
    pinned host key + authorized_keys for sshd, and the client key,
    known_hosts and pinned ssh configs for the ordinary user (the
    /fixture-keys mount is readable by container uid 0 only). The private
    client key is staged 0600 uid 1000 - never world-readable. sshd then
    runs unprivileged (port 22 is bindable because the pod namespace sets
    the unprivileged port start to 0).
    """
    entry_id = container_id or manifest["containers"][role]["id"]
    conf = "workspace-sshd.conf" if role in ("workspace", "recreated") else "peer-sshd.conf"
    prefix = "ws" if role in ("workspace", "recreated") else "peer"
    checked(podman_exec(
        podman, entry_id, "/bin/sh", "-c",
        "install -d -o 1000 -g 1000 -m 0700 /workspace/ssh /workspace/logs "
        f"&& cp /fixture-keys/{prefix}-hostkeys/ssh_host_ed25519_key /workspace/ssh/host_ed25519_key "
        "&& cp /fixture-keys/authorized_keys /workspace/ssh/authorized_keys "
        "&& cp /fixture-keys/" + conf + " /workspace/ssh/sshd.conf "
        "&& cp /fixture-keys/id_ed25519 /workspace/ssh/id_ed25519 "
        "&& cp /fixture-keys/id_ed25519.pub /workspace/ssh/id_ed25519.pub "
        "&& cp /fixture-keys/known_hosts /workspace/ssh/known_hosts "
        "&& cp /fixture-keys/ssh_config /workspace/ssh/ssh_config "
        "&& cp /fixture-keys/ssh_config_incoming /workspace/ssh/ssh_config_incoming "
        "&& chown -R 1000:1000 /workspace/ssh "
        "&& chmod 0600 /workspace/ssh/host_ed25519_key /workspace/ssh/id_ed25519 "
        "&& chmod 0644 /workspace/ssh/authorized_keys /workspace/ssh/sshd.conf "
        "/workspace/ssh/id_ed25519.pub /workspace/ssh/known_hosts "
        "/workspace/ssh/ssh_config /workspace/ssh/ssh_config_incoming",
        user="0:0", timeout=60,
    ), f"stage {role} ssh material")
    return checked(podman_exec(
        podman, entry_id, "/bin/sh", "-c",
        "exec /usr/sbin/sshd -D -e -f /workspace/ssh/sshd.conf 2>>/workspace/logs/sshd.log",
        detach=True, timeout=60,
    ), f"start {role} sshd")


def ssh_probe(manifest: dict, role: str) -> dict:
    """Probe a pod's sshd through its published loopback port (rootless
    bridge IPs are not host-reachable), with the pinned host key."""
    keys = Path(manifest["ssh"]["dir"])
    port = manifest["containers"][role]["ssh_port"]
    return run_command([
        "ssh", "-p", str(port), "-i", str(keys / "id_ed25519"), "-o", "IdentitiesOnly=yes",
        "-o", f"UserKnownHostsFile={keys / 'known_hosts'}", "-o", "StrictHostKeyChecking=yes",
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "workspace-user@127.0.0.1", "id -u",
    ], timeout=60)


def recreate_workspace(podman: Podman, manifest: dict, state_dir: Path) -> dict:
    """Pod recreation: exact-ID removal of the old workspace, fresh container
    with the SAME persistent /workspace (portmap state survives) and a NEW
    resolver bind file (fresh inode, variant config)."""
    old = manifest["containers"]["workspace"]
    removal = remove_exact(podman, "container", old["id"],
                           {LABEL_RUN: manifest["run_id"], LABEL_ROLE: "workspace"})
    new_resolv = state_dir / "resolv" / "workspace-resolv-recreated.conf"
    # Same upstream and semantics, deliberately different byte content: the
    # recreated pod must take a FRESH backup of the current file, never reuse
    # the dead pod's saved identity.
    new_content = (
        "# portmap nested-workspace fixture resolver (recreated pod; original upstream)\n"
        f"nameserver {manifest['network']['dns']}\n"
        f"search {manifest['dns_fixture']['zone']} svc.{manifest['dns_fixture']['zone']}\n"
        "options ndots:5 attempts:2\n"
    )
    new_resolv.write_text(new_content, encoding="utf-8")
    os.chmod(new_resolv, 0o644)
    flags = workspace_run_flags(
        manifest, "workspace_recreated", old["ip"],
        name=manifest["names"]["workspace_recreated"], resolver=str(new_resolv),
        role_label="workspace_recreated", scratch_role="workspace",
    )
    new_id = checked(podman.run(*flags, timeout=TIMEOUTS["run"]), "run recreated workspace")["stdout"].strip()
    manifest["containers"]["workspace_recreated"] = {
        "id": new_id, "name": manifest["names"]["workspace_recreated"], "ip": old["ip"],
    }
    manifest["resolver"]["recreated"] = str(new_resolv)
    manifest["resolv_recreated_sha256"] = sha256_bytes(new_content.encode())
    checked(podman.run("exec", "-u", "0", new_id, "/bin/sh", "-c",
                       "mkdir -p /workspace/logs && chown 1000:1000 /workspace /workspace/logs",
                       timeout=TIMEOUTS["exec_setup"]), "prepare recreated workspace dirs")
    stage_source_into(podman, new_id, state_dir / "source.tar.gz")
    write_json(state_dir / "manifest.json", manifest)
    write_json(Path(manifest["out_dir"]) / "manifest.json", manifest)
    return {"old_removed": removal, "new_id": new_id, "resolv": str(new_resolv)}


def exercise(state_dir: Path, out_dir: Path) -> int:
    manifest = read_json(state_dir / "manifest.json")
    podman = Podman(state_dir)
    receipt: dict = {"command": "exercise", "run_id": manifest["run_id"], "phases": {}, "ok": False}
    peer_entry = manifest["containers"]["peer"]
    workspace_entry = manifest["containers"]["workspace"]
    exercise_path = "/workspace/source/tests/fixtures/nested_workspace/exercise.py"
    try:
        receipt["objects"] = verify_objects(podman, manifest)
        for role in ("workspace", "peer"):
            start_sshd(podman, manifest, role)
            probe = ssh_probe(manifest, role)
            receipt["phases"][f"sshd_probe_{role}"] = {
                "returncode": probe["returncode"], "stdout": probe["stdout"].strip(),
            }
            if probe["returncode"] != 0 or probe["stdout"].strip() != "1000":
                raise DriverError(f"sshd probe failed for {role}: {probe['stderr']}")

        # The peer is the fresh server-only scenario: plain `portmap up` must
        # not need host DNS at all, so loopback :53 is held busy and the
        # workspace user's sudo elevation is removed for the whole peer life.
        revoke = podman_exec(podman, peer_entry["id"], "/bin/sh", "-c",
                             "mv /etc/sudoers.d/workspace-user /etc/sudoers.d/.revoked",
                             user="0:0", timeout=60)
        receipt["phases"]["peer_sudo_revoked"] = {"returncode": revoke["returncode"]}

        ready_file = out_dir / "peer-ready.json"
        ready_file.unlink(missing_ok=True)
        (out_dir / "peer-final.json").unlink(missing_ok=True)
        launch = podman_exec(podman, peer_entry["id"], "python3", exercise_path,
                             env=nested_env(manifest, "peer"), detach=True, timeout=60)
        receipt["phases"]["peer_launch"] = {"returncode": launch["returncode"]}
        if launch["returncode"] != 0:
            raise DriverError(f"peer exercise launch failed: {launch['stderr']}")

        def peer_ready() -> None:
            if not ready_file.exists():
                raise DriverError("peer-ready.json not written yet")
        wait_until(peer_ready, TIMEOUTS["peer_ready"], "peer readiness")
        ready = read_json(ready_file)
        if not ready.get("ready"):
            raise DriverError(f"peer initialization failed: {ready}")

        run = podman_exec(podman, workspace_entry["id"], "python3", exercise_path,
                          env=nested_env(manifest, "workspace"),
                          timeout=TIMEOUTS["workspace_exercise"])
        receipt["phases"]["workspace_exercise"] = {
            "returncode": run["returncode"], "timed_out": run["timed_out"],
            "stdout_tail": run["stdout"][-4000:], "stderr_tail": run["stderr"][-4000:],
        }
        if run["returncode"] != 0:
            raise DriverError("workspace acceptance failed; retaining the original pod and its receipt for diagnosis")

        incoming = podman_exec(podman, peer_entry["id"], "ssh", "-F", "/workspace/ssh/ssh_config_incoming",
                               manifest["containers"]["workspace"]["ip"],
                               "sh -c 'id -u && cat /workspace/incoming-marker'",
                               timeout=120)
        receipt["phases"]["incoming_ssh"] = {
            "returncode": incoming["returncode"], "stdout": incoming["stdout"].strip(),
            "stderr": incoming["stderr"][-2000:],
        }

        # Pod recreation runs while the peer is STILL alive: the recreated
        # pod must restore the saved SSH connection against the real peer.
        receipt["phases"]["pod_recreation"] = recreate_workspace(podman, manifest, state_dir)
        recreated_id = manifest["containers"]["workspace_recreated"]["id"]
        recreated = podman_exec(podman, recreated_id, "python3", exercise_path,
                                env=nested_env(manifest, "recreated"),
                                timeout=TIMEOUTS["recreated_exercise"])
        receipt["phases"]["recreated_exercise"] = {
            "returncode": recreated["returncode"], "timed_out": recreated["timed_out"],
            "stdout_tail": recreated["stdout"][-4000:], "stderr_tail": recreated["stderr"][-4000:],
        }

        stop = podman_exec(podman, peer_entry["id"], "touch", "/workspace/peer-stop", timeout=60)
        receipt["phases"]["peer_stop_signal"] = {"returncode": stop["returncode"]}

        def peer_finished() -> None:
            path = out_dir / "peer-final.json"
            if not path.exists() or not read_json(path).get("final"):
                raise DriverError("peer-final.json not completed yet")

        try:
            wait_until(peer_finished, TIMEOUTS["peer_final"], "peer final receipt")
        except DriverError as exc:
            receipt["phases"]["peer_final_timeout"] = str(exc)

        checks = {}
        for name in ("exercise-workspace.json", "exercise-recreated.json"):
            if (out_dir / name).exists():
                for key, value in read_json(out_dir / name).get("checks", {}).items():
                    checks[f"{name.split('-')[1]}:{key}"] = value
        peer_final = read_json(out_dir / "peer-final.json") if (out_dir / "peer-final.json").exists() else {}
        for key, value in peer_final.get("checks", {}).items():
            checks[f"peer:{key}"] = value
        failed = sorted(name for name, result in checks.items() if not result.get("ok"))
        receipt["checks_summary"] = {"total": len(checks), "failed": failed}
        receipt["ok"] = (
            bool(checks) and not failed
            and run["returncode"] == 0 and recreated["returncode"] == 0
            and incoming["returncode"] == 0 and incoming["stdout"].splitlines()[0:1] == ["1000"]
            and bool(peer_final) and bool(peer_final.get("checks"))
        )
        return 0 if receipt["ok"] else 1
    except BaseException as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        write_json(out_dir / "exercise.json", receipt)


# --- cleanup ----------------------------------------------------------------

def _exists(podman: Podman, kind: str, object_id: str) -> bool | None:
    """Typed existence probe: True/False, or None on an actual error."""
    subcommand = {"container": "container", "network": "network", "image": "image"}[kind]
    result = podman.run(subcommand, "exists", object_id, timeout=60)
    if result["returncode"] == 0:
        return True
    if result["returncode"] == 1:
        return False
    raise DriverError(
        f"{subcommand} exists {object_id} failed with rc={result['returncode']} "
        f"(an error is not proof of absence): {result['stderr'][-1000:]}"
    )


def remove_exact(podman: Podman, kind: str, object_id: str, label: dict[str, str]) -> dict:
    """Typed exact-ID removal: per-kind inspect shape, fail-closed errors."""
    exists = _exists(podman, kind, object_id)
    if exists is False:
        return {"id": object_id, "action": "absent"}
    if kind == "network":
        inspect = checked(podman.run("network", "inspect", object_id, timeout=60), f"inspect owned network {object_id}")
        entry = json.loads(inspect["stdout"])[0]
        # containers/common marshals network labels as "labels" (libnetwork
        # JSON); accept both spellings, never an empty-read false pass.
        labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else None
        labels = labels or (entry.get("Labels") if isinstance(entry.get("Labels"), dict) else None) or {}
    elif kind == "container":
        inspect = checked(podman.run("container", "inspect", object_id, timeout=60), f"inspect owned container {object_id}")
        labels = json.loads(inspect["stdout"])[0]["Config"].get("Labels") or {}
    else:
        inspect = checked(podman.run("image", "inspect", object_id, timeout=60), f"inspect owned image {object_id}")
        labels = json.loads(inspect["stdout"])[0].get("Labels") or {}
    if any(labels.get(key) != value for key, value in label.items()):
        return {"id": object_id, "action": "refused", "labels": labels}
    if kind == "container":
        removal = podman.run("rm", "-f", "-t", "45", object_id, timeout=TIMEOUTS["remove"])
    elif kind == "network":
        removal = podman.run("network", "rm", object_id, timeout=TIMEOUTS["remove"])
    else:
        removal = podman.run("rmi", "-f", object_id, timeout=TIMEOUTS["remove"])
    return {"id": object_id, "action": "removed" if removal["returncode"] == 0 else "failed",
            "detail": removal["stderr"][-2000:] if removal["returncode"] else ""}


def cleanup(state_dir: Path, out_dir: Path) -> int:
    manifest_path = state_dir / "manifest.json"
    receipt: dict = {"command": "cleanup", "removed": {}, "ok": False}
    try:
        podman = Podman(state_dir)
        if not manifest_path.exists():
            # A prepare that failed before writing its manifest already
            # rolled its exact objects back; the scratch tree itself is
            # still removed honestly (processes, unshare fallback, no
            # fabricated success).
            receipt["note"] = "no manifest: scratch-only cleanup with exact process ownership"
            receipt["scratch"] = _cleanup_scratch_sequence(podman, state_dir, receipt)
            receipt["ok"] = receipt["scratch"]["removed"]
            return 0 if receipt["ok"] else 1
        manifest = read_json(manifest_path)
        receipt["run_id"] = manifest["run_id"]
        for role in ("workspace_recreated", "workspace", "peer", "dns"):
            entry = manifest["containers"].get(role)
            if not entry:
                continue
            receipt["removed"][f"container:{role}"] = remove_exact(
                podman, "container", entry["id"],
                {LABEL_RUN: manifest["run_id"], LABEL_ROLE: role},
            )
        receipt["removed"]["network"] = remove_exact(
            podman, "network", manifest["network"]["id"], {LABEL_RUN: manifest["run_id"]},
        )
        receipt["removed"]["image"] = remove_exact(podman, "image", manifest["image"]["id"], {})

        after = host_baseline()
        write_json(out_dir / "baseline-after.json", after)
        before = read_json(out_dir / "baseline-before.json")
        receipt["baseline_comparison"] = {
            "files_preserved": before["files"] == after["files"],
            "sysctls_preserved": before["sysctls"] == after["sysctls"],
            "forwarding_policy_preserved": (
                before["sysctls"].get("ip_forward") == after["sysctls"].get("ip_forward")
                and before["sysctls"].get("ipv6_all_forwarding") == after["sysctls"].get("ipv6_all_forwarding")
            ),
            "default_stores_preserved": (
                before["default_podman_store"] == after["default_podman_store"]
                and before["default_docker_store"] == after["default_docker_store"]
            ),
            "preexisting_userns_preserved": (
                {tuple(item) for item in before["live_userns_maps"]}
                <= {tuple(item) for item in after["live_userns_maps"]}
            ),
        }
        removal_ok = all(entry.get("action") in {"removed", "absent"}
                         for entry in receipt["removed"].values())
        store_empty = _private_store_settled(podman, manifest, receipt)
        receipt["scratch"] = _cleanup_scratch_sequence(podman, state_dir, receipt)
        receipt["ok"] = (
            removal_ok and store_empty
            and all(receipt["baseline_comparison"].values())
            and receipt["scratch"]["removed"]
        )
        return 0 if receipt["ok"] else 1
    except BaseException as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        write_json(out_dir / "cleanup.json", receipt)

def _private_store_settled(podman: Podman, manifest: dict, receipt: dict) -> bool:
    """The private store must be empty of OUR objects before deleting it."""
    listing = podman.run("ps", "-aq", timeout=60)
    if listing["returncode"] != 0:
        receipt["store_settled_error"] = listing["stderr"][-500:]
        return False
    remaining = [line for line in listing["stdout"].splitlines() if line.strip()]
    if _exists(podman, "network", manifest["network"]["id"]) is not False:
        remaining.append(f"network:{manifest['network']['id']}")
    receipt["store_remaining"] = remaining
    return not remaining


def _proc_starttime(pid: int) -> str | None:
    try:
        # /proc/<pid>/stat field 22 (starttime), after the parenthesized comm.
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw[raw.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _own_ancestors() -> set[int]:
    chain = set()
    current = os.getpid()
    for _ in range(64):
        chain.add(current)
        try:
            parent = int(process_ppid(current))
        except (OSError, ValueError):
            break
        if parent <= 1:
            break
        current = parent
    return chain


def process_ppid(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_text()
    return raw[raw.rindex(")") + 2:].split()[1]


def _path_boundary_match(value: str, marker: str) -> bool:
    """Exact token match or a marker/ child-path prefix; never raw substring."""
    return value == marker or value.startswith(marker + "/")


def _match_private_process(entry: Path, markers: tuple[str, ...], my_uid: int) -> dict | None:
    try:
        status = (entry / "status").read_text()
        uid_line = status.split("Uid:")[1].split("\n")[0].split()
        if not uid_line or uid_line[0] != str(my_uid):
            return None
        argv = [part.decode("utf-8", "replace") for part in (entry / "cmdline").read_bytes().split(b"\x00") if part]
        try:
            env_values = [item.split("=", 1)[-1] for item in
                          (entry / "environ").read_bytes().decode("utf-8", "replace").split("\x00") if "=" in item]
        except OSError:
            env_values = []
    except OSError:
        return None
    tokens = argv + env_values
    if not any(_path_boundary_match(token, marker) for token in tokens for marker in markers):
        return None
    return {"pid": int(entry.name), "starttime": _proc_starttime(int(entry.name)), "argv": " ".join(argv)[:300]}


def _signal_exact(pid: int, starttime: str, sig: int) -> bool:
    """Signal a pid only while it is provably still the same process."""
    if hasattr(os, "pidfd_open"):
        try:
            pidfd = os.pidfd_open(pid)
            try:
                if _proc_starttime(pid) != starttime:
                    return False
                import signal as _signal
                if hasattr(_signal, "pidfd_send_signal"):
                    _signal.pidfd_send_signal(pidfd, sig)
                    return True
            finally:
                os.close(pidfd)
        except (OSError, AttributeError):
            pass
    if _proc_starttime(pid) == starttime:  # immediate re-verify fallback
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False
    return False


def _scan_private_processes(state_dir: Path, podman: Podman, protected: set[int]) -> list[dict]:
    markers = (str(state_dir), str(podman.runtime), str(podman.network_conf))
    my_uid = os.getuid()
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in protected:
            continue
        record = _match_private_process(entry, markers, my_uid)
        if record is not None:
            matches.append(record)
    return matches


def _terminate_private_runtime_processes(state_dir: Path, podman: Podman, receipt: dict) -> list[dict]:
    """Terminate exactly the fixture's private runtime/store processes.

    Protected set: this driver process and its whole ancestor chain (an
    invoking shell/pytest whose argv carries --state-dir must never be
    matched). Each target is identified by uid + boundary-matched private
    path tokens and by its /proc starttime; every signal re-validates the
    identity (pidfd when available), with a bounded TERM wait before KILL
    and a final gone-check. Stragglers are reported, never guessed away.
    """
    terminated: list[dict] = []
    protected = _own_ancestors()
    matches = _scan_private_processes(state_dir, podman, protected)
    for record in matches:
        pid, starttime = record["pid"], record["starttime"]
        if starttime is None:
            record["action"] = "unverifiable (no starttime); skipped"
            terminated.append(record)
            continue
        _signal_exact(pid, starttime, 15)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _proc_starttime(pid) == starttime:
            time.sleep(0.1)
        if _proc_starttime(pid) == starttime:
            _signal_exact(pid, starttime, 9)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _proc_starttime(pid) == starttime:
                time.sleep(0.1)
        record["action"] = "terminated" if _proc_starttime(pid) != starttime else "straggler"
        terminated.append(record)
    receipt["private_process_scan"] = [item["argv"] for item in matches]
    return terminated


def _cleanup_scratch_sequence(podman: Podman, state_dir: Path, receipt: dict) -> dict:
    """Ordered, honest scratch teardown; no podman invocation after the
    mapped-owner deletion pass (which must run while the private runtime
    dir still exists so it cannot be recreated afterwards).

    (1) enter the private user/mount namespace, unmount only mounts inside
    the owned state tree, then delete it with mapped-owner authority;
    (2) validated, bounded process-termination pass looped until the
    matched set is empty;
    (3) remove the private runtime dir; verify state dir + runtime dir are
    absent and no matching process remains; report failures truthfully.
    """
    outcome: dict = {"removed": False}
    remove_script = r'''
import pathlib, re, shutil, subprocess, sys
root, runtime = (pathlib.Path(value) for value in sys.argv[1:])
for owned in (root, runtime):
    assert owned.is_absolute() and owned.resolve() == owned and not owned.is_symlink()
mounts = []
for line in pathlib.Path("/proc/self/mountinfo").read_text().splitlines():
    encoded = line.split()[4]
    mount = pathlib.Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), encoded))
    if any(mount == owned or owned in mount.parents for owned in (root, runtime)):
        mounts.append(mount)
for mount in sorted(mounts, key=lambda p: len(p.parts), reverse=True):
    subprocess.run(["umount", "--", str(mount)], check=True)
shutil.rmtree(root)
'''
    unshare = podman.raw("unshare", sys.executable, "-c", remove_script,
                        str(state_dir), str(podman.runtime), timeout=300)
    outcome["unshare"] = unshare
    if unshare["returncode"] == 0:
        outcome["method"] = "private unshare unmount and rmtree"
    else:
        outcome["method"] = "retained"
        outcome["unshare_error"] = unshare["stderr"][-2000:]
        outcome["note"] = f"state retained for diagnosis at {state_dir}"
        return outcome
    attempts = []
    for _ in range(4):
        remaining = _scan_private_processes(state_dir, podman, _own_ancestors())
        if not remaining:
            break
        attempts.append(_terminate_private_runtime_processes(state_dir, podman, receipt))
    outcome["process_passes"] = len(attempts)
    stragglers = _scan_private_processes(state_dir, podman, _own_ancestors())
    try:
        # Podman finalization can recreate its own lock/cache directories
        # after the mapped removal. No engine command may follow this pass.
        if state_dir.exists():
            shutil.rmtree(state_dir)
    except OSError as state_error:
        outcome["state_note"] = f"state dir {state_dir} retained: {state_error}"
    try:
        if podman.runtime.exists():
            shutil.rmtree(podman.runtime)
        runtime_removed = not podman.runtime.exists()
    except OSError as runtime_error:
        runtime_removed = False
        outcome["runtime_note"] = f"runtime dir {podman.runtime} retained: {runtime_error}"
    outcome["removed"] = (
        not state_dir.exists() and runtime_removed and not stragglers
    )
    outcome["state_dir_absent"] = not state_dir.exists()
    outcome["runtime_dir_absent"] = runtime_removed
    outcome["straggler_processes"] = stragglers
    return outcome


def status(state_dir: Path, out_dir: Path) -> int:
    manifest = read_json(state_dir / "manifest.json")
    podman = Podman(state_dir)
    report = {"run_id": manifest["run_id"], "objects": {}}
    for role, entry in manifest["containers"].items():
        result = podman.run("ps", "-a", "--filter", f"id={entry['id']}", "--format", "{{.Status}}", timeout=60)
        report["objects"][f"container:{role}"] = result["stdout"].strip() or "absent"
    result = podman.run("network", "ls", "--filter", f"id={manifest['network']['id']}", "--format", "{{.Name}}", timeout=60)
    report["objects"]["network"] = result["stdout"].strip() or "absent"
    print(json.dumps(report, indent=2))
    del out_dir
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="nested-workspace fixture driver")
    parser.add_argument("command", choices=["baseline", "prepare", "exercise", "cleanup", "status"])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    state_dir = args.state_dir.expanduser().resolve()
    out_dir = (args.out_dir or Path(str(state_dir) + "-out")).expanduser().resolve()
    # Receipts are written by both the operator-side driver and the in-container
    # exercise (container uid 1000 maps to a different operator-side uid).
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o777)
    if args.command == "baseline":
        write_json(out_dir / "baseline-before.json", host_baseline())
        print(json.dumps({"ok": True, "out_dir": str(out_dir)}))
        return 0
    functions = {"prepare": prepare, "exercise": exercise, "cleanup": cleanup, "status": status}
    rc = functions[args.command](state_dir, out_dir)
    os.chmod(out_dir, 0o777)
    print(json.dumps({"command": args.command, "returncode": rc, "out_dir": str(out_dir), "state_dir": str(state_dir)}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
