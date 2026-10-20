"""Behavior of the public, read-only persistent-helper startup boundaries.

No fake Podman/helper executables: subprocess cases exercise no-op and failure
boundaries before engine operations. Native-info validation receives data at
its parsing boundary. Real CLI/API selection and built-package entry points
are verified independently with isolated storage after source freeze.
"""
import gzip
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portmap import runtime_startup as activation


class PodmanHelperActivationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.data = self.home / "custom data 'quoted'"
        self.helpers_root = self.data / "portmap-deploy" / "podman-helpers"
        self.helpers = self.helpers_root / "producer-selected-release"
        self.override = self.helpers_root / activation.OVERRIDE_NAME
        # No executable search path: no-op must not invoke an engine/helper.
        # Invalid unrelated settings prove the public command does not load them.
        self.environment = {
            "HOME": str(self.home), "XDG_DATA_HOME": str(self.data), "PATH": "",
            "PYTHONDONTWRITEBYTECODE": "1", "PORTMAP_RUNTIME": "invalid",
            "PORTMAP_HTTP_PORT": "not-an-integer",
        }

    def invoke(self, action="activate", extra=None):
        environment = self.environment | (extra or {})
        ledger = self.root / "subprocess-launches.json"

        def snapshot():
            result = {}
            for path in self.root.rglob("*"):
                if path == ledger:
                    continue
                st = path.lstat()
                content = os.readlink(path) if stat.S_ISLNK(st.st_mode) else (
                    path.read_bytes() if stat.S_ISREG(st.st_mode) else None
                )
                result[str(path)] = st.st_mode, st.st_uid, st.st_gid, st.st_mtime_ns, st.st_ctime_ns, content
            return result

        # Audit actual process/socket attempts; do not replace commands/results.
        runner = """
import json, sys
from pathlib import Path
source, action, ledger = sys.argv[1:]
sys.path.insert(0, source)
launches = []
def observe(event, args):
    if event in ("subprocess.Popen", "socket.connect"):
        launches.append(event)
sys.addaudithook(observe)
try:
    from portmap.cli import main
    raise SystemExit(main(["runtime-env", action]))
finally:
    Path(ledger).write_text(json.dumps(launches))
"""
        before = snapshot()
        result = subprocess.run(
            [sys.executable, "-c", runner, str(Path(activation.__file__).resolve().parents[1]), action, str(ledger)],
            env=environment, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(ledger.read_text()), [])
        self.assertEqual(snapshot(), before)
        return result

    def owned(self, entries=None, content=None):
        self.helpers.mkdir(parents=True)
        self.helpers.chmod(0o2775)
        text = content if content is not None else (
            activation.OWNED_HEADER + "\n[engine]\nhelper_binaries_dir = "
            + json.dumps(entries if entries is not None else [str(self.helpers), "/usr/lib/podman"]) + "\n"
        )
        self.override.write_text(text)
        self.override.chmod(0o664)

    def assert_failed_without_export(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_absent_override_leaves_both_starter_boundaries_unchanged(self):
        for action in ("activate", "verify-api"):
            with self.subTest(action=action):
                result = self.invoke(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(self.helpers_root.exists())

    def test_foreign_explicit_override_wins_even_when_owned_candidate_is_broken(self):
        self.owned(content=activation.OWNED_HEADER + "\ninvalid = [")
        before = self.override.read_bytes(), self.override.stat().st_ctime_ns
        for value in ("/foreign/containers.conf", ""):
            for action in ("activate", "verify-api"):
                with self.subTest(value=value, action=action):
                    result = self.invoke(action, {"CONTAINERS_CONF_OVERRIDE": value})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
        self.assertEqual((self.override.read_bytes(), self.override.stat().st_ctime_ns), before)

    def test_unclaimed_foreign_file_is_not_adopted(self):
        self.owned(content="[engine]\nhelper_binaries_dir = []\n")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_claimed_malformed_config_fails_instead_of_falling_back(self):
        self.owned(content=activation.OWNED_HEADER + "\n[engine]\nhelper_binaries_dir = [")
        self.assert_failed_without_export(self.invoke())

    def test_owned_symlink_is_refused_without_modification(self):
        self.owned()
        saved = self.override.with_suffix(".saved")
        self.override.rename(saved)
        self.override.symlink_to(saved.name)
        self.assert_failed_without_export(self.invoke())
        self.assertTrue(self.override.is_symlink())

    def test_missing_selected_owned_override_fails_clearly(self):
        self.assert_failed_without_export(self.invoke(extra={"CONTAINERS_CONF_OVERRIDE": str(self.override)}))

    def test_invalid_helper_list_and_foreign_leading_directory_fail(self):
        for entries in ([], [3], ["/foreign/helpers"]):
            with self.subTest(entries=entries):
                if self.helpers.exists():
                    self.override.unlink()
                    self.helpers.rmdir()
                self.owned(entries=entries)
                self.assert_failed_without_export(self.invoke())

    def test_broken_owned_helpers_fail_without_changing_base_or_override(self):
        self.owned()
        base = self.root / "boot-containers.conf"
        base.write_text('[engine]\nruntime = "crun"\n')
        before = base.read_bytes(), self.override.read_bytes()
        self.assert_failed_without_export(self.invoke(extra={"CONTAINERS_CONF": str(base)}))
        self.assertEqual((base.read_bytes(), self.override.read_bytes()), before)

    def test_producer_modes_and_xdg_paths_are_accepted_without_normalization(self):
        self.owned()
        for name in activation.HELPERS:
            path = self.helpers / name
            path.write_bytes(b"existing executable fixture, never invoked")
            path.chmod(0o775)
        before = {p: p.stat().st_mode for p in (self.helpers, self.override, *self.helpers.iterdir())}
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(activation.owned_override(), (self.override, self.helpers))
        self.assertEqual({p: p.stat().st_mode for p in before}, before)

    def test_api_boundary_refuses_lost_export_before_contacting_socket(self):
        self.owned()
        self.assert_failed_without_export(self.invoke("verify-api"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux dynamic-loader regression")
    def test_native_children_ignore_broken_bundle_library_for_both_environment_paths(self):
        native_gzip = shutil.which("gzip")
        if native_gzip is None:
            self.skipTest("native gzip is unavailable")
        payload = "persistent helper startup native-child payload\n" * 32
        archive = self.root / "payload.gz"
        archive.write_bytes(gzip.compress(payload.encode()))
        command = [native_gzip, "--decompress", "--stdout", str(archive)]
        bundle = self.root / "bundle"
        libraries = bundle / "_internal"
        libraries.mkdir(parents=True)
        (libraries / "libc.so.6").write_bytes(b"intentionally invalid native library\n")
        user_libraries = self.root / "user-libraries"
        user_libraries.mkdir()
        environment = dict(os.environ)
        environment["LD_LIBRARY_PATH"] = str(user_libraries)
        baseline = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        self.assertEqual(baseline.stdout, payload)

        poisoned = environment | {"LD_LIBRARY_PATH": os.pathsep.join((str(libraries), str(user_libraries)))}
        control = subprocess.run(command, env=poisoned, capture_output=True, text=True, timeout=30)
        if control.returncode == 0:
            self.skipTest("native gzip does not load libc.so.6 from LD_LIBRARY_PATH")
        # No command/results are mocked: this is the same real executable and
        # compressed input that the deliberately broken loader path prevents.
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(bundle / "portmap")),
            patch.object(sys, "_MEIPASS", str(libraries), create=True),
            patch.dict(os.environ, poisoned, clear=True),
        ):
            for selected_environment in (poisoned, None):
                with self.subTest(explicit_environment=selected_environment is not None):
                    self.assertEqual(activation.run(command, environment=selected_environment), payload)

    def test_native_info_validation_requires_both_effective_helpers_and_versions(self):
        versions = {"netavark": "42.3.1", "aardvark-dns": "42.3.2"}
        native = {"host": {
            "security": {"rootless": True}, "networkBackend": "netavark",
            "networkBackendInfo": {"path": str(self.helpers / "netavark"), "version": "netavark 42.3.1",
                                   "dns": {"path": str(self.helpers / "aardvark-dns"), "version": "aardvark-dns 42.3.2"}},
        }}
        activation.validate_selection(native, self.helpers, versions)
        for section, key, wrong in (
            (native["host"]["networkBackendInfo"], "path", "/usr/lib/podman/netavark"),
            (native["host"]["networkBackendInfo"]["dns"], "path", "/usr/lib/podman/aardvark-dns"),
            (native["host"]["networkBackendInfo"]["dns"], "version", "aardvark-dns 0.1"),
            (native["host"]["security"], "rootless", False),
        ):
            with self.subTest(key=key, wrong=wrong):
                previous = section[key]
                section[key] = wrong
                with self.assertRaises(activation.ActivationError):
                    activation.validate_selection(native, self.helpers, versions)
                section[key] = previous
        for invalid in ([], {"host": None}, {"host": {"security": None}}):
            with self.subTest(invalid=invalid), self.assertRaises(activation.ActivationError):
                activation.validate_selection(invalid, self.helpers, versions)


if __name__ == "__main__":
    unittest.main()
