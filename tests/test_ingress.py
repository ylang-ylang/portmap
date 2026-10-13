import json
import os
import re
import socket as socket_module
import stat as stat_module
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap import ingress
from portmap.errors import PortAllocationError, PortmapError

SYSTEMCTL = "/usr/bin/fake-systemctl"
LISTEN_STREAM = re.compile(r"^ListenStream=(.+):(\d+)$", re.MULTILINE)


def free_port():
    with socket_module.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class FakeSystemd:
    """Emulates the exact privileged surface portmap is allowed to use.

    ``install``/``rm`` operate on real files inside the fake unit directory;
    ``systemctl`` tracks unit states and opens real listening sockets when a
    socket unit starts, so activation probes behave like socket activation.
    """

    def __init__(self, unit_dir: Path, proxyd: Path):
        self.unit_dir = unit_dir
        self.proxyd = proxyd
        self.commands = []  # decoded argv (sudo prefix removed)
        self.raw_commands = []  # argv exactly as invoked
        self.envs = []
        self.active = {}
        self.results = {}
        self.listeners = {}
        self.start_errors = {}
        self.enabled = set()
        self.reloads = 0
        self.fail_service_always = False
        self.fail_install_content = None  # staged content containing this fails

    # -- helpers -------------------------------------------------------------

    def unit_content(self, unit):
        try:
            return (self.unit_dir / unit).read_text(encoding="utf-8")
        except OSError:
            return ""

    def socket_listening(self, socket_unit):
        match = LISTEN_STREAM.search(self.unit_content(socket_unit))
        return bool(match) and f"{match.group(1)}:{match.group(2)}" in self.listeners

    def open_listener(self, bind, port):
        key = f"{bind}:{port}"
        if key in self.listeners:
            return
        listener = socket_module.socket()
        listener.bind((bind, int(port)))
        listener.listen(16)
        self.listeners[key] = listener

    def close_listener(self, bind, port):
        listener = self.listeners.pop(f"{bind}:{port}", None)
        if listener is not None:
            listener.close()

    def service_state(self, unit):
        if self.active.get(unit) == "active":
            return "active"
        socket_unit = unit[: -len(".service")] + ".socket"
        if self.socket_listening(socket_unit):
            if self.fail_service_always:
                self.active[unit] = "failed"
                self.results[unit] = "resources"
            else:
                self.active[unit] = "active"
        return self.active.get(unit, "inactive")

    def systemctl_verbs(self):
        return [command[1] for command in self.commands if command and command[0] == SYSTEMCTL]

    def privileged(self):
        """Decoded commands that ran through sudo (empty when the suite is root)."""
        if os.geteuid() == 0:
            return []
        return [list(command[2:]) for command in self.raw_commands if command[:2] == ["sudo", "--"]]

    # -- subprocess.run replacement -------------------------------------------

    def run(self, command, **kwargs):
        self.raw_commands.append(list(command))
        self.envs.append(kwargs.get("env"))
        decoded = list(command[2:]) if command[:2] == ["sudo", "--"] else list(command)
        self.commands.append(decoded)
        if decoded[0].endswith("systemctl"):
            code, out, err = self.systemctl(decoded)
        elif decoded[0] == "install":
            code, out, err = self.install(decoded)
        elif decoded[0] == "rm":
            code, out, err = self.remove(decoded)
        else:
            raise AssertionError(f"unexpected privileged command: {command}")
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    def systemctl(self, argv):
        verb, units = argv[1], argv[2:]
        if verb == "show":
            unit = units[-1]
            properties = [argv[index + 1] for index, item in enumerate(argv[2:-1], start=2) if item == "-p"]
            lines = []
            for prop in properties:
                if prop == "ActiveState":
                    state = self.service_state(unit) if unit.endswith(".service") else self.active.get(unit, "inactive")
                    lines.append(f"ActiveState={state}")
                elif prop == "Result":
                    lines.append(f"Result={self.results.get(unit, '')}")
            return 0, "\n".join(lines) + "\n", ""
        if verb == "is-enabled":
            return (0, "enabled\n", "") if units[0] in self.enabled else (0, "static\n", "")
        if verb == "daemon-reload":
            self.reloads += 1
            return 0, "", ""
        if verb == "start":
            unit = units[0]
            if unit in self.start_errors:
                return 1, "", self.start_errors[unit]
            match = LISTEN_STREAM.search(self.unit_content(unit))
            if match:
                self.open_listener(match.group(1), match.group(2))
            self.active[unit] = "active"
            return 0, "", ""
        if verb == "stop":
            for unit in units:
                self.active[unit] = "inactive"
                self.results.pop(unit, None)
                match = LISTEN_STREAM.search(self.unit_content(unit))
                if match:
                    self.close_listener(match.group(1), match.group(2))
            return 0, "", ""
        if verb == "reset-failed":
            for unit in units:
                self.active[unit] = "inactive"
                self.results.pop(unit, None)
            return 0, "", ""
        raise AssertionError(f"unexpected systemctl verb: {verb}")

    def install(self, argv):
        source, destination = argv[argv.index("--") + 1 :]
        content = Path(source).read_text(encoding="utf-8")
        if self.fail_install_content and self.fail_install_content in content:
            return 1, "", f"install: simulated failure for {destination}"
        Path(destination).write_text(content, encoding="utf-8")
        os.chmod(destination, 0o644)
        return 0, "", ""

    def remove(self, argv):
        for path in argv[argv.index("--") + 1 :]:
            Path(path).unlink(missing_ok=True)
        return 0, "", ""


@pytest.fixture
def fake_systemd(tmp_path, monkeypatch):
    unit_dir = tmp_path / "units"
    run_dir = tmp_path / "run"
    unit_dir.mkdir()
    run_dir.mkdir()
    proxyd = tmp_path / "systemd-socket-proxyd"
    proxyd.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    proxyd.chmod(0o755)
    fake = FakeSystemd(unit_dir, proxyd)
    monkeypatch.setattr(ingress, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(ingress, "SYSTEMD_RUN_DIR", run_dir)
    monkeypatch.setattr(ingress, "PROXYD_CANDIDATES", (proxyd,))
    monkeypatch.setattr(ingress, "ROOT_UID", os.getuid())
    monkeypatch.setattr(ingress, "_systemctl_path", lambda: SYSTEMCTL)
    monkeypatch.setattr(ingress.subprocess, "run", fake.run)
    return fake


def ensure_private(tmp_path, *, port=None, target_port=18080, **kwargs):
    return ingress.ensure_ingress(
        tmp_path,
        name="private",
        bind_address="127.0.0.1",
        port=port if port is not None else free_port(),
        target_port=target_port,
        **kwargs,
    )


# --- naming and unit content ------------------------------------------------------


def test_unit_names_are_deterministic_uid_and_state_hash(tmp_path):
    first = ingress.unit_names(tmp_path, "private")
    assert first == ingress.unit_names(tmp_path, "private")
    socket_unit, service_unit = first
    pattern = rf"portmap-ingress-private-{os.getuid()}-[0-9a-f]{{8}}"
    assert re.fullmatch(pattern + r"\.socket", socket_unit)
    assert re.fullmatch(pattern + r"\.service", service_unit)
    assert ingress.unit_names(tmp_path / "other-state", "private") != first
    assert ingress.unit_names(tmp_path, "public") != first




# --- input validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "edge"},
        {"name": "private", "bind_address": "127.0.0.2"},
        {"name": "private", "bind_address": "0.0.0.0"},
        {"name": "private", "bind_address": "::1"},
        {"name": "public", "bind_address": "0.0.0.0"},
        {"name": "public", "bind_address": "::"},
        {"name": "public", "bind_address": "127.0.0.1"},
        {"name": "public", "bind_address": "::1"},
        {"name": "public", "bind_address": "::ffff:127.0.0.1"},
        {"name": "public", "bind_address": "224.0.0.1"},
        {"name": "public", "bind_address": "ff02::1"},
        {"name": "public", "bind_address": "fe80::1%eth0"},
        {"name": "public", "bind_address": "example.test"},
        {"name": "private", "bind_address": "127.0.0.1", "port": 0},
        {"name": "private", "bind_address": "127.0.0.1", "port": True},
        {"name": "private", "bind_address": "127.0.0.1", "port": "80"},
        {"name": "private", "bind_address": "127.0.0.1", "target_port": 65536},
    ],
)
def test_ensure_rejects_invalid_parameters(tmp_path, fake_systemd, kwargs):
    arguments = {"name": "private", "bind_address": "127.0.0.1", "port": 80, "target_port": 18080}
    arguments.update(kwargs)
    with pytest.raises(PortmapError):
        ingress.ensure_ingress(tmp_path, **arguments)
    assert fake_systemd.commands == []


def test_ensure_requires_linux_systemd_and_systemctl(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(PortmapError, match="unsupported on darwin"):
        ingress.ensure_ingress(tmp_path, name="private", bind_address="127.0.0.1", port=80, target_port=18080)

    monkeypatch.undo()
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    monkeypatch.setattr(ingress, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(ingress, "SYSTEMD_RUN_DIR", tmp_path / "absent")
    with pytest.raises(PortmapError, match="systemd is not managing this system"):
        ingress.ensure_ingress(tmp_path, name="private", bind_address="127.0.0.1", port=80, target_port=18080)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(ingress, "SYSTEMD_RUN_DIR", run_dir)
    monkeypatch.setattr(ingress.shutil, "which", lambda name: None)
    with pytest.raises(PortmapError, match="systemctl was not found"):
        ingress.ensure_ingress(tmp_path, name="private", bind_address="127.0.0.1", port=80, target_port=18080)


def test_missing_or_unsafe_proxyd_is_an_actionable_error(tmp_path, fake_systemd, monkeypatch):
    monkeypatch.setattr(ingress, "PROXYD_CANDIDATES", ())
    with pytest.raises(PortmapError, match="systemd-socket-proxyd was not found.*apt install systemd"):
        ensure_private(tmp_path)

    monkeypatch.setattr(ingress, "PROXYD_CANDIDATES", (fake_systemd.proxyd,))
    fake_systemd.proxyd.chmod(0o777)
    with pytest.raises(PortmapError, match="rejected as unsafe"):
        ensure_private(tmp_path)


def test_proxyd_symlink_to_writable_target_is_rejected(tmp_path, fake_systemd, monkeypatch):
    target = tmp_path / "evil-proxyd"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o766)
    link = tmp_path / "proxyd-link"
    link.symlink_to(target)
    monkeypatch.setattr(ingress, "PROXYD_CANDIDATES", (link,))
    with pytest.raises(PortmapError, match="systemd-socket-proxyd was not found"):
        ensure_private(tmp_path)


# --- ensure lifecycle -----------------------------------------------------------------


def test_ensure_installs_root_owned_units_and_activates(tmp_path, fake_systemd):
    port = free_port()
    result = ensure_private(tmp_path, port=port, target_port=18080)

    assert result["running"] is True
    assert result["owned"] is True
    assert result["changed"] is True
    assert result["bind_address"] == "127.0.0.1"
    assert result["port"] == port
    assert result["target_port"] == 18080
    assert result["enabled"] is False

    socket_unit, service_unit = ingress.unit_names(tmp_path, "private")
    socket_file = fake_systemd.unit_dir / socket_unit
    service_file = fake_systemd.unit_dir / service_unit
    marker = ingress._marker(tmp_path, "private")
    assert socket_file.read_text(encoding="utf-8").startswith(marker + "\n")
    assert service_file.read_text(encoding="utf-8").startswith(marker + "\n")
    for unit_file in (socket_file, service_file):
        assert stat_module.S_IMODE(unit_file.stat().st_mode) == 0o644
    assert f"ListenStream=127.0.0.1:{port}\n" in socket_file.read_text(encoding="utf-8")
    assert f"ExecStart={fake_systemd.proxyd} 127.0.0.1:18080\n" in service_file.read_text(encoding="utf-8")

    verbs = fake_systemd.systemctl_verbs()
    assert "daemon-reload" in verbs
    assert "start" in verbs
    assert "enable" not in verbs  # no boot enablement, ever
    privileged = fake_systemd.privileged()
    if os.geteuid() != 0:
        assert privileged, "unit installation must run through sudo when unprivileged"
    for command in privileged:
        assert command[0] in {"install", "rm", SYSTEMCTL}
        assert command[0] not in {"sh", "bash", "tee", "env"}

    saved = json.loads((tmp_path / "ingress" / "private.json").read_text(encoding="utf-8"))
    assert saved["target_port"] == 18080
    assert saved["proxyd"] == str(Path(os.path.realpath(fake_systemd.proxyd)))


def test_ensure_is_idempotent_without_privileged_commands(tmp_path, fake_systemd):
    port = free_port()
    ensure_private(tmp_path, port=port)
    fake_systemd.commands.clear()
    fake_systemd.raw_commands.clear()

    result = ensure_private(tmp_path, port=port)

    assert result["changed"] is False
    assert result["running"] is True
    assert fake_systemd.privileged() == []
    assert set(fake_systemd.systemctl_verbs()) <= {"show", "is-enabled"}


def test_ensure_updates_units_when_parameters_change(tmp_path, fake_systemd):
    port = free_port()
    ensure_private(tmp_path, port=port, target_port=18080)
    fake_systemd.commands.clear()

    result = ensure_private(tmp_path, port=port, target_port=19090)

    assert result["running"] is True
    assert result["changed"] is True
    assert result["target_port"] == 19090
    verbs = fake_systemd.systemctl_verbs()
    assert "stop" in verbs and "start" in verbs and "daemon-reload" in verbs
    _, service_unit = ingress.unit_names(tmp_path, "private")
    content = (fake_systemd.unit_dir / service_unit).read_text(encoding="utf-8")
    assert "127.0.0.1:19090" in content
    assert "127.0.0.1:18080" not in content


def test_ensure_reports_port_conflict_without_touching_system(tmp_path, fake_systemd):
    port = free_port()
    blocker = socket_module.socket()
    blocker.bind(("127.0.0.1", port))
    blocker.listen(8)
    try:
        with pytest.raises(PortAllocationError, match=rf"127\.0\.0\.1:{port} is already served"):
            ensure_private(tmp_path, port=port)
    finally:
        blocker.close()
    assert fake_systemd.commands == []
    assert list(fake_systemd.unit_dir.iterdir()) == []


def test_ensure_translates_socket_start_address_in_use_to_conflict(tmp_path, fake_systemd):
    socket_unit, _ = ingress.unit_names(tmp_path, "private")
    fake_systemd.start_errors[socket_unit] = f"{socket_unit}: Failed to listen on sockets: Address already in use"
    with pytest.raises(PortAllocationError, match="never stops unrelated services"):
        ensure_private(tmp_path)
    assert list(fake_systemd.unit_dir.iterdir()) == []
    assert not (tmp_path / "ingress" / "private.json").exists()


def test_service_units_carry_no_ip_policy_but_keep_hardening(tmp_path, fake_systemd):
    ensure_private(tmp_path, target_port=18080)

    _, service_unit = ingress.unit_names(tmp_path, "private")
    content = (fake_systemd.unit_dir / service_unit).read_text(encoding="utf-8")
    # Backends are container-published loopback ports: rootless Podman may DNAT
    # to the container bridge address, which a loopback-only allowlist would drop.
    assert "IPAddressAllow" not in content and "IPAddressDeny" not in content
    for directive in (
        "DynamicUser=yes",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=\n",
        "AmbientCapabilities=\n",
        "RestrictAddressFamilies=AF_INET AF_UNIX",
        "ProtectSystem=strict",
    ):
        assert directive in content
    installs = [c for c in fake_systemd.commands if c[0] == "install" and c[-1].endswith(service_unit)]
    assert len(installs) == 1  # one fixed unit content, no fallback variant


def test_ensure_activation_failure_rolls_back_installed_units(tmp_path, fake_systemd):
    fake_systemd.fail_service_always = True
    with pytest.raises(PortmapError, match="did not activate"):
        ensure_private(tmp_path)
    assert list(fake_systemd.unit_dir.iterdir()) == []
    assert not (tmp_path / "ingress" / "private.json").exists()
    verbs = fake_systemd.systemctl_verbs()
    assert "stop" in verbs  # rollback stopped the units it had started


def test_failed_reactivation_restores_previous_units(tmp_path, fake_systemd):
    port = free_port()
    ensure_private(tmp_path, port=port)
    socket_unit, service_unit = ingress.unit_names(tmp_path, "private")
    service_path = fake_systemd.unit_dir / service_unit
    original_service = service_path.read_bytes()
    original_state = (tmp_path / "ingress" / "private.json").read_bytes()
    fake_systemd.active[service_unit] = "failed"
    fake_systemd.fail_service_always = True

    with pytest.raises(PortmapError, match="did not activate"):
        ensure_private(tmp_path, port=port)

    assert service_path.read_bytes() == original_service
    assert (tmp_path / "ingress" / "private.json").read_bytes() == original_state
    assert fake_systemd.active[socket_unit] == "active"


@pytest.mark.parametrize("failure", [OSError("state disk full"), KeyboardInterrupt()])
def test_state_commit_failure_releases_new_listener(tmp_path, fake_systemd, monkeypatch, failure):
    port = free_port()

    def fail_commit(*args, **kwargs):
        raise failure

    monkeypatch.setattr(ingress, "_save_state", fail_commit)
    with pytest.raises(type(failure)) as caught:
        ensure_private(tmp_path, port=port)
    assert caught.value is failure

    assert not list(fake_systemd.unit_dir.iterdir())
    with socket_module.socket() as replacement:
        replacement.bind(("127.0.0.1", port))


def test_interrupted_update_restores_previous_listener_and_target(tmp_path, fake_systemd, monkeypatch):
    port = free_port()
    ensure_private(tmp_path, port=port, target_port=18080)
    socket_unit, service_unit = ingress.unit_names(tmp_path, "private")
    before = {unit: (fake_systemd.unit_dir / unit).read_bytes() for unit in (socket_unit, service_unit)}
    original_state = (tmp_path / "ingress" / "private.json").read_bytes()
    interruption = KeyboardInterrupt()

    def interrupt(*args, **kwargs):
        raise interruption

    monkeypatch.setattr(ingress, "_save_state", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        ensure_private(tmp_path, port=port, target_port=18081)

    assert caught.value is interruption
    assert {unit: (fake_systemd.unit_dir / unit).read_bytes() for unit in before} == before
    assert (tmp_path / "ingress" / "private.json").read_bytes() == original_state
    assert fake_systemd.socket_listening(socket_unit)
    assert ingress.ingress_status(tmp_path, name="private")["running"]


def test_failed_update_restores_previous_working_units(tmp_path, fake_systemd):
    port = free_port()
    ensure_private(tmp_path, port=port, target_port=18080)
    socket_unit, service_unit = ingress.unit_names(tmp_path, "private")
    before = {
        unit: (fake_systemd.unit_dir / unit).read_text(encoding="utf-8")
        for unit in (socket_unit, service_unit)
    }
    fake_systemd.fail_install_content = "19090"

    with pytest.raises(PortmapError, match="installing system unit"):
        ensure_private(tmp_path, port=port, target_port=19090)

    for unit, content in before.items():
        assert (fake_systemd.unit_dir / unit).read_text(encoding="utf-8") == content
    assert fake_systemd.active[socket_unit] == "active"
    assert json.loads((tmp_path / "ingress" / "private.json").read_text(encoding="utf-8"))["target_port"] == 18080


def test_ensure_without_sudo_refuses_before_any_mutation(tmp_path, fake_systemd):
    with pytest.raises(PortmapError, match="use_sudo=True"):
        ensure_private(tmp_path, use_sudo=False)
    assert fake_systemd.commands == []
    assert list(fake_systemd.unit_dir.iterdir()) == []


# --- ownership and tampering -----------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        "foreign-marker",
        "role-swap",
        "symlink",
        "world-writable",
    ],
)
def test_ensure_refuses_to_touch_foreign_units(tmp_path, fake_systemd, monkeypatch, tamper):
    socket_unit, _ = ingress.unit_names(tmp_path, "private")
    path = fake_systemd.unit_dir / socket_unit
    if tamper == "foreign-marker":
        path.write_text("# someone else's unit\n[Socket]\nListenStream=0.0.0.0:80\n", encoding="utf-8")
        path.chmod(0o644)
        expected = "not owned by this portmap state"
    elif tamper == "role-swap":
        content = ingress._socket_unit_content(ingress._marker(tmp_path, "public"), "public", "192.0.2.10", 80)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o644)
        expected = "not owned by this portmap state"
    elif tamper == "symlink":
        target = tmp_path / "elsewhere.socket"
        target.write_text("[Socket]\n", encoding="utf-8")
        path.symlink_to(target)
        expected = "symlink"
    elif tamper == "world-writable":
        content = ingress._socket_unit_content(ingress._marker(tmp_path, "private"), "private", "127.0.0.1", 80)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o666)
        expected = "group- or world-writable"
    else:  # pragma: no cover - all cases are enumerated above
        raise AssertionError(f"unknown tamper case: {tamper}")
    original = path.read_text(encoding="utf-8") if path.is_file() else None
    with pytest.raises(PortmapError, match=expected):
        ensure_private(tmp_path)
    assert fake_systemd.commands == []
    if original is not None:
        assert path.read_text(encoding="utf-8") == original


def test_ensure_refuses_foreign_service_unit_alone(tmp_path, fake_systemd):
    _, service_unit = ingress.unit_names(tmp_path, "private")
    path = fake_systemd.unit_dir / service_unit
    path.write_text("# not portmap\n[Service]\nExecStart=/bin/sh -c reboot\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(PortmapError, match="refusing to touch"):
        ensure_private(tmp_path)
    assert path.read_text(encoding="utf-8").startswith("# not portmap")


def test_inspection_rejects_units_owned_by_another_user(tmp_path, fake_systemd, monkeypatch):
    socket_unit, _ = ingress.unit_names(tmp_path, "private")
    path = fake_systemd.unit_dir / socket_unit
    content = ingress._socket_unit_content(ingress._marker(tmp_path, "private"), "private", "127.0.0.1", 80)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(ingress, "ROOT_UID", os.getuid() + 4242)
    state, reason, captured = ingress._inspect_unit(path, ingress._marker(tmp_path, "private"), None)
    assert state == "foreign"
    assert "not root" in reason
    assert captured is None


# --- status ------------------------------------------------------------------------------


def test_status_is_read_only_and_reports_running(tmp_path, fake_systemd):
    port = free_port()
    ensure_private(tmp_path, port=port, target_port=18080)
    fake_systemd.commands.clear()
    fake_systemd.raw_commands.clear()

    status = ingress.ingress_status(tmp_path, name="private")

    assert status["running"] is True
    assert status["owned"] is True
    assert status["installed"] is True
    assert status["bind_address"] == "127.0.0.1"
    assert status["port"] == port
    assert status["target_port"] == 18080
    assert status["socket_state"] == "active"
    assert status["service_state"] == "active"
    socket_unit, service_unit = ingress.unit_names(tmp_path, "private")
    assert status["socket_unit"] == socket_unit
    assert status["service_unit"] == service_unit
    for command in fake_systemd.raw_commands:
        assert command[0] == SYSTEMCTL
        assert command[1] in {"show", "is-enabled"}


def test_status_prefers_installed_ipv6_socket_over_stale_state(tmp_path, fake_systemd):
    socket_unit, service_unit = ingress.unit_names(tmp_path, "public")
    marker = ingress._marker(tmp_path, "public")
    for unit, content in (
        (socket_unit, f"{marker}\n[Socket]\nListenStream=[fd12:3456::1]:80\n"),
        (service_unit, f"{marker}\n[Service]\nExecStart={fake_systemd.proxyd} 127.0.0.1:18081\n"),
    ):
        path = fake_systemd.unit_dir / unit
        path.write_text(content, encoding="utf-8")
        path.chmod(0o644)
    ingress._save_state(
        tmp_path, "public", {"bind_address": "192.0.2.42", "port": 81, "target_port": 19090}
    )

    status = ingress.ingress_status(tmp_path, name="public")

    assert status["owned"] is True
    assert status["bind_address"] == "fd12:3456::1"
    assert status["port"] == 80
    assert status["target_port"] == 18081


def test_status_reports_absent_ingress(tmp_path, fake_systemd):
    status = ingress.ingress_status(tmp_path, name="public")
    assert status["installed"] is False
    assert status["owned"] is False
    assert status["running"] is False
    assert status["bind_address"] is None
    assert status["error"] is None
    assert status["socket_unit"].startswith("portmap-ingress-public-")


def test_status_reports_foreign_and_partial_installations(tmp_path, fake_systemd):
    socket_unit, _ = ingress.unit_names(tmp_path, "private")
    path = fake_systemd.unit_dir / socket_unit
    path.write_text("# foreign unit\n", encoding="utf-8")
    path.chmod(0o644)
    status = ingress.ingress_status(tmp_path, name="private")
    assert status["installed"] is True
    assert status["owned"] is False
    assert status["running"] is False
    assert "no portmap ownership marker" in status["error"]

    path.write_text(ingress._socket_unit_content(ingress._marker(tmp_path, "private"), "private", "127.0.0.1", 80))
    path.chmod(0o644)
    status = ingress.ingress_status(tmp_path, name="private")
    assert status["error"] == "incomplete ingress installation: only the socket unit is present"


def test_status_reports_params_from_saved_state_when_units_absent(tmp_path, fake_systemd):
    ensure_private(tmp_path, target_port=18081)
    for unit in fake_systemd.unit_dir.iterdir():
        unit.unlink()
    status = ingress.ingress_status(tmp_path, name="private")
    assert status["installed"] is False
    assert status["target_port"] == 18081


def test_status_on_unsupported_platform_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    status = ingress.ingress_status(tmp_path, name="private")
    assert status["running"] is False
    assert "unsupported on darwin" in status["error"]


# --- stop ---------------------------------------------------------------------------------


def test_stop_stops_and_removes_only_owned_units(tmp_path, fake_systemd):
    ensure_private(tmp_path)
    socket_unit, service_unit = ingress.unit_names(tmp_path, "private")
    fake_systemd.commands.clear()

    result = ingress.stop_ingress(tmp_path, name="private")

    assert result["stopped"] is True
    assert result["removed"] is True
    assert result["installed"] is False
    assert list(fake_systemd.unit_dir.iterdir()) == []
    assert not (tmp_path / "ingress" / "private.json").exists()
    verbs = fake_systemd.systemctl_verbs()
    assert "stop" in verbs and "reset-failed" in verbs and "daemon-reload" in verbs
    assert "enable" not in verbs
    for command in fake_systemd.privileged():
        assert command[0] in {"install", "rm", SYSTEMCTL}


def test_stop_is_idempotent(tmp_path, fake_systemd):
    ensure_private(tmp_path)
    ingress.stop_ingress(tmp_path, name="private")
    fake_systemd.commands.clear()
    fake_systemd.raw_commands.clear()

    result = ingress.stop_ingress(tmp_path, name="private")

    assert result["removed"] is False
    assert result["installed"] is False
    assert fake_systemd.privileged() == []
    assert set(fake_systemd.systemctl_verbs()) <= {"show"}


def test_stop_refuses_foreign_units(tmp_path, fake_systemd):
    socket_unit, _ = ingress.unit_names(tmp_path, "private")
    path = fake_systemd.unit_dir / socket_unit
    path.write_text("# foreign\n[Socket]\nListenStream=0.0.0.0:80\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(PortmapError, match="refusing to touch"):
        ingress.stop_ingress(tmp_path, name="private")
    assert path.exists()
    assert fake_systemd.commands == []


def test_stop_without_sudo_refuses_before_stopping(tmp_path, fake_systemd):
    ensure_private(tmp_path)
    socket_unit, _ = ingress.unit_names(tmp_path, "private")
    fake_systemd.commands.clear()
    fake_systemd.raw_commands.clear()

    with pytest.raises(PortmapError, match="use_sudo=True"):
        ingress.stop_ingress(tmp_path, name="private", use_sudo=False)

    assert fake_systemd.privileged() == []
    assert fake_systemd.active[socket_unit] == "active"
    assert (fake_systemd.unit_dir / socket_unit).exists()


def test_stop_on_unsupported_platform_is_actionable(tmp_path):
    try:
        original = sys.platform
        sys.platform = "darwin"
        with pytest.raises(PortmapError, match="unsupported on darwin"):
            ingress.stop_ingress(tmp_path, name="private")
    finally:
        sys.platform = original


