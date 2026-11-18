#!/usr/bin/env python3
"""Package capability-only experimental Firefox builds; preserve stable signing boundaries."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ID = "firefox@portmap.ylang-ylang.github.io"
UPDATE_URL = "https://github.com/ylang-ylang/portmap/releases/latest/download/firefox-updates.json"
EXPERIMENTAL_UPDATE_URL = "https://github.com/ylang-ylang/portmap/releases/latest/download/firefox-experimental-updates.json"
# Exact published v0.23.0 stable feed. Carry it unchanged when an experimental
# release becomes latest; its signed XPI remains at the original release URL.
STABLE_UPDATE_SHA256 = "f1fe43a61e21ef293d8dc51763136a6d5694cd3f948ae237b65e9a9affba0f6d"
STABLE_UPDATE_FEED = b'''{
  "addons": {
    "firefox@portmap.ylang-ylang.github.io": {
      "updates": [
        {
          "version": "0.23.0",
          "update_link": "https://github.com/ylang-ylang/portmap/releases/download/v0.23.0/portmap-firefox-0.23.0.xpi",
          "update_hash": "sha256:d98d92c44b410f6ddb1fa5c55d96d6e4b0ae3da3becbf5f79ab337b8dcd3b889",
          "applications": {
            "gecko": {
              "strict_min_version": "140.0"
            }
          }
        }
      ]
    }
  }
}
'''
RUNTIME_FILES = (
    "manifest.json", "core.js", "managed-identity.js", "controller.js", "background.js",
    "proxy.js", "transports.js", "transport/api.js", "transport/schema.json",
    "options.html", "options.js", "options.css", "icon.svg",
    "connection-code.js", "scan.js",
    "vendor/jsQR.js", "vendor/jsQR.LICENSE", "vendor/PROVENANCE.json",
    "ssh/ssh.js", "ssh/bridge.c", "ssh/libssh2.js", "ssh/libssh2.wasm",
    "ssh/libssh2-browser-callback.patch", "ssh/PROVENANCE.json",
    "ssh/licenses/libssh2-COPYING", "ssh/licenses/mbedtls-LICENSE",
    "ssh/licenses/mbedtls-framework-LICENSE", "ssh/licenses/emscripten-LICENSE",
    "ssh/licenses/musl-COPYRIGHT", "ssh/licenses/compiler-rt-LICENSE",
)

EXPERIMENTAL_DISTRIBUTION = "experimental-capability-only"
EVIDENCE_SCHEMA = "portmap-browser-managed-ssh-v3"
HARNESS_FILES = tuple("tests/browser/" + name for name in (
    "runner.py", "android.py", "autofill.py", "network_evidence.py", "run.sh",
    "traced-geckodriver.sh", "control.py", "compose.yaml", "compose.android.yaml",
    "Dockerfile", "Android.Dockerfile", "fixture.py", "target.py", "dns.py", "requirements.txt",
    "qr.py", "camera_scene.py", "camera_probe.py", "CameraProbe.Dockerfile",
    "managed.py", "managed_fixture.py",
)) + (
    "tools/build_firefox_extension.py", "tools/build_firefox_ssh.py", "pyproject.toml",
    "src/portmap/cli.py", "src/portmap/connection_qr.py",
    "src/portmap/_qr_encoder.py", "src/portmap/_qr_encoder.LICENSE", "packaging/portmap.spec",
    "tools/test_real_portmap.py", "tests/browser/real_node/evidence.py",
    "tests/browser/real_node/browser.py", "tests/browser/real_node/android_runtime.py",
    "tests/browser/real_node/traced-geckodriver.sh",
    "tests/__init__.py",
    "tests/browser/real_node/__init__.py", "tests/browser/real_node/runtime.py",
    "tests/browser/real_node/base_cache.py",
    "tests/browser/real_node/runtime-inputs.json", "tests/browser/real_node/Runtime.Dockerfile",
    "tests/browser/real_node/base_bootstrap.py", "tests/browser/real_node/node_bootstrap.py",
    "tests/browser/real_node/guest_evidence.py", "tests/browser/real_node/native_gateway_markers.py",
    "tests/browser/real_node/deps/Dockerfile", "tests/browser/real_node/deps/package.json",
    "tests/browser/real_node/deps/package-lock.json", "tests/browser/real_node/app/Dockerfile",
    "tests/browser/real_node/app/compose.yaml", "tests/browser/real_node/app/package.json",
    "tests/browser/real_node/app/vite.config.js", "tests/browser/real_node/app/index.html",
    "tests/browser/real_node/app/src/main.js", "tests/browser/real_node/app/src/style.css",
    "tests/browser/real_node/app/public/badge.svg", "tests/browser/real_node/app/public/proof.txt",
)
REQUIRED_EXPERIMENTAL_CASES = frozenset((
    "actual-missing-capability-blocks-both-modes",
    "actual-native-self-check-enables-both-modes", "unconfigured-fails-closed",
    "name-autofill-normalization-and-invalid-labels",
    "name-autofill-preserves-independent-manual-overrides",
    "name-autofill-save-and-actual-node-route",
    "name-autofill-edit-preserves-saved-endpoints-and-state",
    "save-independent-direct-and-ssh-nodes", "open-node-from-real-options-control",
    "a.portmap-http-app", "a.portmap-binary-concurrency", "a.portmap-https-wss", "a.portmap-descendant",
    "b.portmap-http-app", "b.portmap-binary-concurrency", "b.portmap-https-wss", "b.portmap-descendant",
    "ssh-key-configuration", "ssh-key-http-app", "ssh-key-binary-concurrency", "ssh-key-https-wss",
    "unknown-namespace-rejected", "ssh-reject-password", "ssh-reject-pin", "ssh-reject-ssh-port",
    "unreachable-direct-keeps-other-node", "active-ssh-loss-no-direct-fallback", "disabled-node-isolated",
    "update-feed-offline-preserves-state", "restart-direct-config-and-page", "restart-ssh-key-config-and-page",
    "delete-node-revokes-route", "actual-network-path-evidence",
    "qr-cli-produces-owned-fixtures", "qr-image-preview-cancel-inert",
    "qr-image-direct-confirm-route", "qr-ssh-public-fill-clears-auth-and-routes",
    "qr-reject-malformed", "qr-reject-foreign-url", "qr-reject-secret",
    "qr-reject-version", "qr-reject-duplicate", "qr-reject-unknown-field",
    "qr-reject-overlap", "qr-camera-track-cancel-lifecycle",
    "managed-browser-key-generate-copy-manual-login",
    "managed-password-bootstrap-fresh-key-login",
    "managed-duplicate-preserves-bytes-and-restricted-options",
    "managed-wrong-pin-rejected-before-authentication", "managed-wrong-password-no-save",
    "managed-password-disabled-no-save", "managed-password-disabled-manual-key-login",
    "managed-restricted-exec-no-save", "managed-partial-install-fresh-key-failure-no-save",
    "managed-reject-authorized-keys-symlink", "managed-reject-ssh-directory-symlink",
    "managed-reject-world-writable-authorized-keys", "managed-reject-world-writable-ssh-directory",
    "managed-reject-foreign-owned-authorized-keys", "managed-cancel-inflight-no-stale-save",
    "managed-closed-options-no-stale-save", "managed-password-not-persisted",
    "managed-browser-restart-key-route", "managed-reset-destroys-profile-key",
    "advanced-default-ports-save-closed-and-reedit", "advanced-custom-ports-save-reedit-and-route",
    "advanced-null-https-preserved-through-edit-and-route", "advanced-cancel-and-reset-restore-new-defaults",
    "advanced-invalid-hidden-ports-expand-block-and-allow-correction",
    "qr-imported-custom-ports-retained-through-edit",
    "qr-imported-null-https-retained-through-edit-and-route",
))
REQUIRED_EXPERIMENTAL_PLATFORM_CASES = {
    "desktop-firefox": frozenset(("qr-camera-native-fake-lifecycle",)),
    "android-firefox": frozenset(("qr-camera-native-synthetic-scene-route",)),
}


def is_experimental(manifest: dict) -> bool:
    return bool(manifest.get("experiment_apis"))


def refuse_experimental_stable_attach(manifest: dict) -> None:
    if is_experimental(manifest):
        raise ValueError(
            "Experimental networking XPI cannot use the stable signed attach/update feed. "
            "Build/validate a developer artifact or use attach-experimental with actual dual-platform capability evidence."
        )


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_files(root: Path) -> tuple[dict, dict[str, bytes]]:
    source = root / "extension/firefox"
    files = {}
    for name in RUNTIME_FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing or symlinked extension runtime file: {name}")
        files[name] = path.read_bytes()
    manifest = json.loads(files["manifest.json"])
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if manifest.get("version") != version:
        raise ValueError("extension version must equal Portmap's source version")
    settings = manifest.get("browser_specific_settings", {})
    gecko = settings.get("gecko", {})
    if gecko.get("id") != EXTENSION_ID:
        raise ValueError("extension ID does not match the source contract")
    if is_experimental(manifest):
        if gecko.get("update_url") != EXPERIMENTAL_UPDATE_URL:
            raise ValueError("experimental extension must use its separate experimental update feed")
        for platform in ("gecko", "gecko_android"):
            if any(key in settings.get(platform, {}) for key in ("strict_min_version", "strict_max_version")):
                raise ValueError("experimental compatibility must be capability-only, not version-gated")
        provenance = json.loads(files["ssh/PROVENANCE.json"])
        integrity = provenance["integrity"]
        for name in ("ssh/bridge.c", "ssh/ssh.js", "ssh/libssh2.js", "ssh/libssh2.wasm",
                     "ssh/libssh2-browser-callback.patch"):
            if hashlib.sha256(files[name]).hexdigest() != integrity.get(name):
                raise ValueError(f"SSH source/payload integrity mismatch: {name}")
        for name in (name for name in RUNTIME_FILES if name.startswith("ssh/licenses/")):
            if hashlib.sha256(files[name]).hexdigest() != provenance.get("licenseIntegrity", {}).get(name):
                raise ValueError(f"SSH license integrity mismatch: {name}")
        recipe = "tools/build_firefox_ssh.py"
        if sha256(root / recipe) != provenance.get("buildInputs", {}).get(recipe):
            raise ValueError("SSH build recipe integrity mismatch")
        qr_provenance = json.loads(files["vendor/PROVENANCE.json"])
        for name in ("jsQR.js", "jsQR.LICENSE"):
            actual = hashlib.sha256(files["vendor/" + name]).hexdigest()
            if actual != qr_provenance.get("files", {}).get(name, {}).get("sha256"):
                raise ValueError(f"QR decoder source/payload integrity mismatch: {name}")
    elif gecko.get("update_url") != UPDATE_URL:
        raise ValueError("stable self-distribution update URL does not match release contract")
    if "gecko_android" not in settings:
        raise ValueError("extension must explicitly support Firefox Android")
    return manifest, files


def build(root: Path, output: Path) -> dict:
    manifest, files = runtime_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids silently replacing an artifact someone is testing.
    # Stored members avoid environment-dependent zlib bytes in artifact identity.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return {"artifact": str(output), "version": manifest["version"], "sha256": sha256(output),
            "signed": False, "distribution": EXPERIMENTAL_DISTRIBUTION,
            "publication_allowed": False,
            "installation": "Requires experimental APIs to be admitted by Firefox; runtime capability self-check must pass."}


def canonical_manifest(content: bytes) -> str:
    """Allow signer JSON formatting, never duplicate keys or changed values."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate manifest key: {key}")
            result[key] = value
        return result

    value = json.loads(content, object_pairs_hook=unique_object)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def check_payload(root: Path, xpi: Path, *, signed: bool = False) -> dict:
    """Compare every packaged byte to the explicit source allowlist."""
    manifest, expected = runtime_files(root)
    if xpi.is_symlink() or not xpi.is_file():
        raise ValueError("XPI must be a regular file")
    with zipfile.ZipFile(xpi) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("XPI contains duplicate entries")
        payload = {name for name in names if not name.startswith("META-INF/") and not name.endswith("/")}
        if payload != set(expected):
            raise ValueError("XPI runtime members differ from the source allowlist")
        for name, content in expected.items():
            actual = archive.read(name)
            matches = canonical_manifest(actual) == canonical_manifest(content) if name == "manifest.json" else actual == content
            if not matches:
                raise ValueError(f"XPI differs from source: {name}")
        if signed:
            if not any(name in names for name in ("META-INF/mozilla.rsa", "META-INF/cose.sig")):
                raise ValueError("XPI has no Mozilla signature envelope; development packages are not publishable")
        elif any(name.startswith("META-INF/") for name in names):
            raise ValueError("developer artifact must not contain a signature envelope")
    return manifest


def check_signed_payload(root: Path, signed_xpi: Path) -> dict:
    """Historical stable signing path; never interpret an Experiment as signed release."""
    manifest, _ = runtime_files(root)
    refuse_experimental_stable_attach(manifest)
    return check_payload(root, signed_xpi, signed=True)


def source_provenance(root: Path) -> dict:
    """Capture actual Git state and exact payload/harness inputs, including dirty trees."""
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        if result.returncode:
            raise ValueError("source provenance requires a readable Git checkout")
        return result.stdout.strip()
    if Path(git("rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise ValueError("source provenance requires the repository root")
    _, files = runtime_files(root)
    harness = {}
    for name in HARNESS_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing or symlinked browser evidence harness: {name}")
        harness[name] = sha256(path)
    return {"commit": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=all")),
            "payload_sha256": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
            "harness_sha256": harness}


def require_committed_inputs(root: Path, commit: str) -> None:
    """Bind every shipped/evidence input to Git blobs, including hidden dirty files."""
    result = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", commit], capture_output=True)
    if result.returncode:
        raise ValueError("cannot read exact committed source inputs")
    tree = {}
    for entry in result.stdout.split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            mode, kind, digest = metadata.split()
            tree[name.decode()] = (mode, kind, digest.decode())
    for name in (*("extension/firefox/" + name for name in RUNTIME_FILES), *HARNESS_FILES):
        content = (root / name).read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        recorded = tree.get(name)
        if recorded not in ((b"100644", b"blob", blob), (b"100755", b"blob", blob)):
            raise ValueError(f"committed source differs from packaged or evidence input: {name}")


def check_experimental_observations(proof: dict, *, platform: str, source: dict) -> None:
    """Validate complete trusted-CI receipts, not authenticate arbitrary supplied JSON."""
    def require(condition, description):
        if not condition:
            raise ValueError(f"{platform} experimental evidence {description}")
    def positive_integer(value):
        return type(value) is int and value > 0
    def native(value, label):
        require(isinstance(value, dict), f"lacks native {label}")
        state = value.get("addon_state")
        require(isinstance(state, dict) and state.get("active") is True and
                state.get("appDisabled") is False and state.get("temporary") is False,
                f"has invalid permanent addon state in {label}")
        require(value.get("signature_required") is False and positive_integer(value.get("processID")) and
                isinstance(value.get("buildID"), str) and bool(value["buildID"]) and
                isinstance(value.get("browser_version"), str) and bool(value["browser_version"]),
                f"lacks observed browser identity in {label}")
    require(proof.get("evidence_schema") == EVIDENCE_SCHEMA, "has unsupported schema")
    recorded = proof.get("source_provenance")
    require(isinstance(recorded, dict), "lacks source provenance")
    for field in ("commit", "dirty", "payload_sha256", "harness_sha256"):
        require(recorded.get(field) == source[field] and
                (field != "dirty" or recorded[field] is False), f"has mismatched source {field}")
    names = {case["name"] for case in proof["cases"]}
    require(platform in REQUIRED_EXPERIMENTAL_PLATFORM_CASES, "has unsupported platform")
    required_cases = REQUIRED_EXPERIMENTAL_CASES | REQUIRED_EXPERIMENTAL_PLATFORM_CASES[platform]
    require(required_cases <= names,
            "lacks required exercised cases: " + ", ".join(sorted(required_cases - names)))
    other_camera_cases = set().union(*(cases for name, cases in REQUIRED_EXPERIMENTAL_PLATFORM_CASES.items()
                                      if name != platform))
    require(not names & other_camera_cases, "claims camera cases for a different platform")
    for case in proof["cases"]:
        start, finish = case.get("started_at"), case.get("finished_at")
        require(type(start) in (int, float) and type(finish) in (int, float) and
                math.isfinite(start) and math.isfinite(finish) and 0 < start <= finish,
                f"has invalid observed timestamps for {case['name']}")
    native(proof.get("unsupported_install"), "unsupported install")
    native(proof.get("supported_install"), "supported install")
    require(proof["supported_install"]["browser_version"] == proof["browser_version"],
            "has inconsistent observed browser version")
    require(proof["supported_install"]["addon_state"] == proof["addon_state"],
            "has inconsistent supported addon state")
    restart = proof.get("restart")
    require(isinstance(restart, dict) and restart.get("reinstalled") is False, "lacks actual no-reinstall restart")
    native(restart.get("before"), "before restart")
    native(restart.get("after"), "after restart")
    require(restart["before"]["processID"] != restart["after"]["processID"],
            "does not show a native process restart")
    require(all(value["browser_version"] == proof["browser_version"] and
                value["buildID"] == proof["supported_install"]["buildID"]
                for value in (proof["unsupported_install"], restart["before"], restart["after"])),
            "mixes different native browser builds across observations")
    origins = proof.get("origin_requests")
    require(isinstance(origins, dict) and origins.get("trap") == [], "lacks empty fallback-trap receiver evidence")
    for node in ("node-a", "node-b", "unrelated"):
        rows = origins.get(node)
        require(isinstance(rows, list) and bool(rows), f"lacks {node} receiver observations")
        require(all(isinstance(row, dict) and row.get("node") == node and
                    isinstance(row.get("path"), str) and row.get("proxyAuthorization") is False for row in rows),
                f"has invalid or credential-leaking {node} receiver observations")
    audit = proof.get("network_audit")
    require(isinstance(audit, dict) and audit.get("observed_direct_http") is True and
            audit.get("observed_ssh") is True, "lacks observed direct and SSH network paths")
    if platform == "desktop-firefox":
        require(audit.get("recorder") == "strace-connect-execve" and positive_integer(audit.get("events")) and
                audit.get("admission_address") == "127.0.0.1" and type(audit.get("admission_port")) is int and
                audit["admission_port"] == 9 and type(audit.get("admission_attempts")) is int and
                audit["admission_attempts"] == 0,
                "lacks actual desktop fail-closed admission syscall observations")
    else:
        require(audit.get("recorder") == "android-emulator-native-pcap" and
                positive_integer(audit.get("packets")) and positive_integer(audit.get("ip_packets")) and
                audit.get("scope") == "emulator-external-interface" and
                audit.get("loopback_admission_observable") is False and
                "sentinel_attempts" not in audit and "admission_attempts" not in audit,
                "lacks actual native Android external-interface observations or overclaims loopback visibility")
    observations = proof.get("observations")
    require(isinstance(observations, dict), "lacks application observations")
    def receiver(row, node, ssh, label):
        require(isinstance(row, dict) and row.get("node") == node and row.get("peerLoopback") is ssh and
                row.get("proxyAuthorization") is False, f"has invalid {label} receiver/mode observation")
    def application(name, node, ssh):
        value = observations.get(name)
        require(isinstance(value, dict), f"lacks {name} application observation")
        for part in ("page", "post", "websocket"):
            receiver(value.get(part), node, ssh, name + " " + part)
        require(value["page"].get("authorization") is False and
                value["post"].get("body") == "portmap-body-✓" and
                value["post"].get("originAuthorizationCorrect") is True and
                value["websocket"].get("message") == "portmap-real-websocket-✓" and
                value.get("ssh_only_status") == (200 if ssh else 403),
                f"has incomplete {name} HTTP/auth/WebSocket/SSH-gate observations")
    receiver(observations.get("open-node-from-real-options-control"), "node-a", False, "options Open")
    for prefix, node, ssh in (("a.portmap", "node-a", False), ("b.portmap", "node-b", True)):
        for suffix in ("http-app", "https-wss", "descendant"):
            application(prefix + "-" + suffix, node, ssh)
    for name in ("ssh-key-http-app", "ssh-key-https-wss", "restart-ssh-key-config-and-page"):
        application(name, "node-b", True)
    application("restart-direct-config-and-page", "node-a", False)
    for name in ("a.portmap-binary-concurrency", "b.portmap-binary-concurrency", "ssh-key-binary-concurrency"):
        value = observations.get(name)
        require(isinstance(value, dict) and value.get("status") == 200 and
                value.get("binary") is True and value.get("binaryWs") is True and
                isinstance(value.get("rows"), list) and
                [row.get("path") if isinstance(row, dict) else None for row in value["rows"]] ==
                ["/echo?parallel=" + str(index) for index in range(4)],
                f"lacks complete {name} binary/concurrent observations")
    lost = observations.get("active-ssh-loss-no-direct-fallback")
    require(isinstance(lost, dict) and positive_integer(lost.get("terminated")),
            "lacks observed termination of a live SSH process")

    def digest(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    fixtures = proof.get("qr_fixtures")
    require(isinstance(fixtures, dict) and set(fixtures) == {"direct", "http-only", "ssh", "camera"},
            "lacks actual CLI QR fixtures")
    targets = proof.get("targets")
    require(isinstance(targets, dict), "lacks QR fixture receiver targets")
    for label, mode, target_name in (("direct", "direct", "a"), ("http-only", "direct", "a"),
                                     ("ssh", "ssh", "b"), ("camera", "direct", "a")):
        fixture, target = fixtures[label], targets.get(target_name)
        fields = {"command", "png_sha256", "wire_sha256", "domain"}
        if label == "camera":
            fields.add("synthetic_padded_scene_sha256")
        require(isinstance(fixture, dict) and set(fixture) == fields and
                digest(fixture.get("png_sha256")) and digest(fixture.get("wire_sha256")) and
                fixture.get("domain") == "qr-" + label + ".portmap",
                f"has invalid {label} CLI QR fixture metadata/digests")
        require(isinstance(target, dict) and isinstance(target.get("host"), str) and bool(target["host"]) and
                target.get("httpPort") == 8080 and type(target.get("httpPort")) is int and
                target.get("httpsPort") == 8443 and type(target.get("httpsPort")) is int and
                target.get("sshPort") == 2222 and type(target.get("sshPort")) is int,
                f"lacks observed {label} CLI QR receiver metadata")
        command = fixture["command"]
        require(isinstance(command, list) and len(command) >= 4 and
                all(isinstance(arg, str) and bool(arg) for arg in command) and
                Path(command[0]).is_absolute() and
                command[1:4] == ["-c", "from portmap.cli import main; raise SystemExit(main())", "connection-qr"],
                f"lacks actual {label} connection-qr CLI invocation")
        # Flag order is not CLI semantics. Reject unknown/duplicate flags and
        # validate their public values rather than pinning an incidental order.
        expected = {
            "--name": "Owned QR " + label, "--domain": fixture["domain"], "--mode": mode,
            "--host": target["host"], "--http-port": str(target["httpPort"]),
            "--png": "/artifacts/qr/" + label + ".png", "--wire": True, "--no-terminal": True,
        }
        expected.update({"--no-https": True} if label == "http-only" else
                        {"--https-port": str(target["httpsPort"])})
        if mode == "ssh":
            expected.update({"--ssh-port": str(target["sshPort"]), "--ssh-username": "probe",
                             "--ssh-target-host": "127.0.0.1", "--ssh-host-key-sha256": None})
        flags = {}
        at = 4
        while at < len(command):
            flag = command[at]
            require(flag in expected and flag not in flags,
                    f"has inconsistent or non-public {label} CLI QR arguments")
            if expected[flag] is True:
                flags[flag] = True
                at += 1
            else:
                require(at + 1 < len(command), f"has incomplete {label} CLI QR arguments")
                flags[flag] = command[at + 1]
                at += 2
        if mode == "ssh":
            pin = flags.get("--ssh-host-key-sha256")
            require(digest(pin), "has invalid public SSH QR host-key fingerprint")
            expected["--ssh-host-key-sha256"] = pin
        require(flags == expected, f"has inconsistent or non-public {label} CLI QR arguments")
        if label == "camera":
            require(digest(fixture["synthetic_padded_scene_sha256"]) and
                    fixture["synthetic_padded_scene_sha256"] != fixture["png_sha256"],
                    "lacks distinct padded CLI camera-scene digest")
    require(observations.get("qr-cli-produces-owned-fixtures") == fixtures,
            "lacks matching exercised CLI QR fixture observations")
    boundary = proof.get("qr_camera_boundary")
    require(isinstance(boundary, dict) and boundary.get("physical_camera") is False and
            boundary.get("media_api_overridden") is False and
            boundary.get("android_source") == "supported emulator -camera-back imagefile:<CLI PNG>" and
            boundary.get("desktop_source") == "Firefox native fake camera; lifecycle only, not QR-scene proof",
            "lacks actual synthetic/native QR camera boundary")
    def qr_route(row, label, node, ssh):
        receiver(row, node, ssh, "QR " + label)
        require(row.get("authorization") is False and row.get("method") == "GET" and
                row.get("host") == fixtures[label]["domain"] and isinstance(row.get("path"), str) and
                re.fullmatch(r"/qr-" + label + r"-[0-9a-f]{32}", row["path"]) is not None and
                row in origins[node], f"lacks actual {label} QR route receiver observation")
    qr_route(observations.get("qr-image-direct-confirm-route"), "direct", "node-a", False)
    qr_route(observations.get("qr-ssh-public-fill-clears-auth-and-routes"), "ssh", "node-b", True)
    for label in ("malformed", "foreign-url", "secret", "version", "duplicate", "unknown-field", "overlap"):
        rejected = observations.get("qr-reject-" + label)
        require(isinstance(rejected, dict) and rejected.get("config_unchanged") is True and
                rejected.get("receiver_counts_unchanged") is True and rejected.get("remote_fetch") is False,
                f"lacks inert {label} QR rejection observations")
    def video_tracks(value, state, label):
        require(isinstance(value, list) and bool(value) and
                all(isinstance(track, dict) and track.get("kind") == "video" and
                    track.get("state") == state for track in value),
                f"lacks observed {state} native QR video tracks for {label}")
    cancelled = observations.get("qr-camera-track-cancel-lifecycle")
    require(isinstance(cancelled, dict) and cancelled.get("media_api_overridden") is False and
            cancelled.get("cancelled") is True and cancelled.get("synthetic_scene") is True,
            "lacks real native QR camera cancel observation")
    video_tracks(cancelled.get("before"), "live", "cancel")
    video_tracks(cancelled.get("after"), "ended", "cancel")
    require(len(cancelled["before"]) == len(cancelled["after"]),
            "has inconsistent QR camera cancel track observations")
    camera_case, = REQUIRED_EXPERIMENTAL_PLATFORM_CASES[platform]
    camera = observations.get(camera_case)
    require(isinstance(camera, dict) and camera.get("synthetic_scene") is True and
            camera.get("physical_camera") is False and camera.get("media_api_overridden") is False,
            "lacks actual native synthetic QR camera observations")
    video_tracks(camera.get("tracks"), "ended", camera_case)
    if platform == "android-firefox":
        require(camera.get("source") == "emulator-imagefile CLI PNG",
                "lacks actual emulator CLI QR camera source")
        qr_route(camera.get("route"), "camera", "node-a", False)
    else:
        require(camera.get("source") == "Firefox native fake source; no QR scene" and "route" not in camera,
                "overclaims desktop fake-camera QR scene/route observations")
    managed = proof.get("managed_ssh")
    require(isinstance(managed, dict) and managed.get("real_server") is True and
            managed.get("browser_webcrypto") is True and
            managed.get("private_material_exported_to_proof") is False and
            positive_integer(managed.get("disposable_accounts")),
            "lacks real browser-managed SSH boundary")
    for field in ("profile_key_fingerprint", "public_key_sha256"):
        require(isinstance(managed.get(field), str) and
                re.fullmatch(r"[0-9a-f]{64}", managed[field]) is not None,
                "lacks managed SSH public identity digest")

    def managed_case(suffix, true_fields=()):
        name = "managed-" + suffix
        value = observations.get(name)
        require(isinstance(value, dict), f"lacks {name} observation")
        require(all(value.get(field) is True for field in true_fields),
                f"lacks required {name} security observations")
        fixture = value.get("fixture")
        if fixture is not None:
            require(isinstance(fixture, dict), f"has invalid {name} fixture")
            events = fixture.get("events")
            require(isinstance(events, dict) and all(
                type(events.get(field)) is int and events[field] >= 0
                for field in ("passwordAccepted", "passwordFailed", "keyAccepted", "execStarted")),
                f"lacks real {name} SSH server event counters")
        return value

    generated = managed_case("browser-key-generate-copy-manual-login", (
        "fingerprint_confirmed", "inspection_no_auth", "endpoint_change_invalidates_confirmation",
        "public_key_copied", "fresh_key_login", "route_loopback"))
    require(isinstance(generated.get("identity_before"), dict) and
            generated["identity_before"].get("present") is False,
            "managed SSH identity was not generated in a fresh profile")
    identity = generated.get("identity_after")
    require(isinstance(identity, dict) and identity.get("present") is True and
            type(identity.get("rsaBits")) is int and identity["rsaBits"] >= 3072 and
            identity.get("privateFieldsPresent") is True and
            identity.get("publicReferenceMatches") is True and identity.get("localPrivateCopy") is False and
            identity.get("keyFingerprint") == managed["profile_key_fingerprint"],
            "lacks persisted isolated RSA managed SSH identity")
    saved = generated.get("saved")
    require(isinstance(saved, dict) and saved.get("authType") == "managed" and
            saved.get("credentialFree") is True, "managed SSH connection contains credential material")
    installed = managed_case("password-bootstrap-fresh-key-login", (
        "fresh_key_login", "route_loopback", "https_route_loopback",
        "password_cleared", "existing_bytes_preserved"))
    fixture = installed.get("fixture")
    require(isinstance(fixture, dict), "lacks SSH bootstrap server observations")
    events, keys = fixture.get("events", {}), fixture.get("authorizedKeys", {})
    require(events.get("passwordAccepted") == 1 and events.get("keyAccepted", 0) >= 1 and
            events.get("execStarted", 0) >= 1 and isinstance(keys, dict) and
            keys.get("managedOccurrences") == 1 and keys.get("baselinePreserved") is True and
            keys.get("fileMode") == "0o600" and keys.get("directoryMode") == "0o700",
            "lacks real append/fresh-key-login/bootstrap permissions evidence")
    managed_case("duplicate-preserves-bytes-and-restricted-options", (
        "bytes_unchanged", "restricted_options_preserved", "arbitrary_quoted_comment_preserved",
        "fresh_key_login"))
    wrong_pin = managed_case("wrong-pin-rejected-before-authentication", (
        "inspection_no_auth", "wrong_pin_before_auth", "bytes_unchanged"))
    require(wrong_pin.get("saved") is None and isinstance(wrong_pin.get("fixture"), dict) and
            all(wrong_pin["fixture"]["events"].get(field) == 0 for field in (
                "passwordAccepted", "passwordFailed", "keyAccepted", "execStarted")),
            "managed SSH wrong pin permitted authentication or saving")
    for suffix in ("wrong-password-no-save", "password-disabled-no-save", "restricted-exec-no-save",
                   "reject-authorized-keys-symlink", "reject-ssh-directory-symlink",
                   "reject-world-writable-authorized-keys", "reject-world-writable-ssh-directory",
                   "reject-foreign-owned-authorized-keys"):
        rejected = managed_case(suffix, ("bytes_unchanged", "password_cleared"))
        require(rejected.get("saved") is None and isinstance(rejected.get("ui_error"), str) and
                bool(rejected["ui_error"]), f"managed SSH {suffix} lacks honest rejection")
    managed_case("password-disabled-manual-key-login", ("fresh_key_login", "route_loopback"))
    partial = managed_case("partial-install-fresh-key-failure-no-save", (
        "remote_key_installed", "partial_change_disclosed", "password_cleared"))
    require(partial.get("saved") is None and partial.get("fresh_key_login") is False,
            "managed SSH partial installation was incorrectly saved")
    for suffix, closed in (("cancel-inflight-no-stale-save", False), ("closed-options-no-stale-save", True)):
        cancelled = managed_case(suffix, ("no_stale_save", "password_cleared"))
        require(cancelled.get("saved") is None and cancelled.get("options_closed") is closed and
                type(cancelled.get("remote_key_present")) is bool,
                "managed SSH cancellation conceals partial remote state or saved a stale node")
    managed_case("password-not-persisted", (
        "password_absent", "managed_auth_credential_free", "snapshot_has_no_private_material",
        "private_key_not_in_storage_local", "password_input_empty"))
    restarted = managed_case("browser-restart-key-route", (
        "same_profile_key", "native_process_changed", "first_route_loopback", "route_loopback"))
    require(restarted.get("reinstalled") is False, "managed SSH restart reinstalled the extension")
    managed_case("reset-destroys-profile-key", (
        "identity_removed", "reference_removed", "connections_removed", "new_key_differs",
        "old_route_blocked", "remote_key_retained"))


def check_evidence(path: Path, *, platform: str, digest: str, version: str, commit: str,
                   experimental: bool = False, source: dict | None = None) -> None:
    proof = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(proof, dict):
        raise ValueError("browser evidence must be an object")
    required = {
        "status": "passed", "platform": platform, "extension_sha256": digest,
        "extension_version": version, "source_commit": commit, "signature_required": not experimental,
    }
    if experimental:
        required.update(experimental=True, capability_available=True)
        state = proof.get("addon_state")
        if not isinstance(state, dict) or any(state.get(key) is not value for key, value in
                                              (("active", True), ("appDisabled", False), ("temporary", False))):
            raise ValueError(f"{platform} evidence has no actual permanent active addon state")
    for key, value in required.items():
        if proof.get(key) != value or (type(value) is bool and proof.get(key) is not value):
            raise ValueError(f"{platform} evidence does not match required {key}")
    if not isinstance(proof.get("browser_version"), str) or not proof["browser_version"]:
        raise ValueError(f"{platform} evidence has no real browser version")
    cases = proof.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{platform} evidence contains no exercised cases")
    names = set()
    for case in cases:
        if not isinstance(case, dict) or case.get("status") != "passed":
            raise ValueError(f"{platform} has failed or unexecuted cases")
        name = case.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"{platform} evidence has invalid or duplicate case names")
        names.add(name)
    if experimental:
        if source is None:
            raise ValueError("experimental publication requires source provenance")
        check_experimental_observations(proof, platform=platform, source=source)


def attach(root: Path, signed_xpi: Path, assets: Path, tag: str, commit: str,
           desktop_evidence: Path, android_evidence: Path) -> dict:
    manifest = check_signed_payload(root, signed_xpi)
    version = manifest["version"]
    if tag != f"v{version}" or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("tag/version or source commit mismatch")
    digest = sha256(signed_xpi)
    for platform, path in (("desktop-firefox", desktop_evidence), ("android-firefox", android_evidence)):
        check_evidence(path, platform=platform, digest=digest, version=version, commit=commit)
    release_path = assets / "release.json"
    if release_path.is_symlink():
        raise ValueError("release manifest must not be a symlink")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if release.get("version") != version or release.get("source") != {
        "repository": "ylang-ylang/portmap", "tag": tag, "commit": commit,
    }:
        raise ValueError("runtime release manifest has different source identity")
    if "firefox" in release:
        raise ValueError("Firefox assets are already attached; never replace release bytes")
    filename = f"portmap-firefox-{version}.xpi"
    base_url = f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/"
    updates = {"addons": {EXTENSION_ID: {"updates": [{
        "version": version, "update_link": base_url + filename,
        "update_hash": "sha256:" + digest,
        "applications": {"gecko": {
            "strict_min_version": manifest["browser_specific_settings"]["gecko"]["strict_min_version"],
        }},
    }]}}}
    destinations = [assets / name for name in (
        filename, filename + ".sha256", "firefox-updates.json", "firefox-updates.json.sha256",
    )]
    if any(path.exists() or path.is_symlink() for path in destinations):
        raise ValueError("Firefox output already exists; refusing replacement")
    with (assets / filename).open("xb") as output, signed_xpi.open("rb") as source:
        shutil.copyfileobj(source, output)
    with (assets / "firefox-updates.json").open("x", encoding="utf-8") as output:
        output.write(json.dumps(updates, indent=2) + "\n")
    records = {}
    for key, name in (("xpi", filename), ("updates", "firefox-updates.json")):
        path = assets / name
        value = sha256(path)
        records[key] = {"filename": name, "sha256": value, "size": path.stat().st_size,
                        "source_url": base_url + name}
        with (assets / (name + ".sha256")).open("x", encoding="utf-8") as output:
            output.write(f"{value}  {name}\n")
    release["firefox"] = records
    release_path.write_text(json.dumps(release, indent=2) + "\n", encoding="utf-8")
    return {"version": version, "source_commit": commit, "firefox": records}


def check_real_node_evidence(path: Path, *, archive: Path, release: dict, digest: str,
                             version: str, commit: str, source: dict) -> dict:
    """Run the trusted full-node validator, never code from supplied evidence."""
    specification = importlib.util.spec_from_file_location(
        "portmap_real_node_evidence", ROOT / "tests/browser/real_node/evidence.py")
    validator = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(validator)
    proof = validator.validate_aggregate(
        path, release=release, xpi_sha256=digest, version=version, commit=commit,
        source=source, require_candidate=True, required_platforms=("desktop", "android"))
    record = next(record for record in release["assets"]
                  if record["os"] == "linux" and record["arch"] == "amd64")
    if (archive.is_symlink() or not archive.is_file() or
            sha256(archive) != record["sha256"] or archive.stat().st_size != record["size"]):
        raise ValueError("real-node evidence: candidate CLI archive differs from tested bytes")
    return proof


def attach_experimental(root: Path, xpi: Path, assets: Path, tag: str, commit: str,
                        desktop_evidence: Path, android_evidence: Path,
                        real_node_evidence: Path) -> dict:
    """Attach experimental bytes and carry forward the exact historical stable feed."""
    manifest = check_payload(root, xpi)
    if not is_experimental(manifest):
        raise ValueError("attach-experimental requires an experimental manifest")
    version = manifest["version"]
    if tag != f"v{version}" or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("tag/version or source commit mismatch")
    source = source_provenance(root)
    if source["commit"] != commit or source["dirty"] is not False:
        raise ValueError("experimental publication requires a clean checkout at the exact source commit")
    require_committed_inputs(root, commit)
    digest = sha256(xpi)
    for platform, path in (("desktop-firefox", desktop_evidence), ("android-firefox", android_evidence)):
        check_evidence(path, platform=platform, digest=digest, version=version, commit=commit,
                       experimental=True, source=source)
    release_path = assets / "release.json"
    if release_path.is_symlink() or not release_path.is_file():
        raise ValueError("release manifest must be a regular file")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if release.get("version") != version or release.get("source") != {
        "repository": "ylang-ylang/portmap", "tag": tag, "commit": commit,
    }:
        raise ValueError("runtime release manifest has different source identity")
    if any(field in release for field in ("firefox", "firefox_experimental", "firefox_stable_updates")):
        raise ValueError("Firefox assets already attached; never replace release bytes")
    check_real_node_evidence(real_node_evidence, archive=assets / f"portmap-{version}-linux-amd64.tar.gz",
                             release=release, digest=digest, version=version, commit=commit, source=source)
    filename = f"portmap-firefox-experimental-{version}.xpi"
    feedname = "firefox-experimental-updates.json"
    base_url = f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/"
    label = {"distribution": EXPERIMENTAL_DISTRIBUTION, "signature_required": False,
             "capability_required": True}
    updates = {"addons": {EXTENSION_ID: {"updates": [{
        "version": version, "update_link": base_url + filename,
        "update_hash": "sha256:" + digest, **label,
    }]}}}
    if hashlib.sha256(STABLE_UPDATE_FEED).hexdigest() != STABLE_UPDATE_SHA256:
        raise ValueError("historical stable Firefox feed differs from its published digest")
    stable = {
        "filename": "firefox-updates.json", "sha256": STABLE_UPDATE_SHA256,
        "size": len(STABLE_UPDATE_FEED), "source_url": base_url + "firefox-updates.json",
        "distribution": "stable-compatibility-feed", "original_version": "0.23.0",
        "original_source_url": "https://github.com/ylang-ylang/portmap/releases/download/v0.23.0/firefox-updates.json",
    }
    payloads = {
        filename: xpi.read_bytes(), feedname: (json.dumps(updates, indent=2) + "\n").encode(),
        "firefox-updates.json": STABLE_UPDATE_FEED,
        "firefox-updates.json.sha256": f"{STABLE_UPDATE_SHA256}  firefox-updates.json\n".encode(),
    }
    records = {}
    for key, name in (("xpi", filename), ("updates", feedname)):
        content = payloads[name]
        value = hashlib.sha256(content).hexdigest()
        records[key] = {"filename": name, "sha256": value, "size": len(content),
                        "source_url": base_url + name, **label}
        payloads[name + ".sha256"] = f"{value}  {name}\n".encode()
    if any((assets / name).exists() or (assets / name).is_symlink() for name in payloads):
        raise ValueError("experimental Firefox output already exists; refusing replacement")
    # Identity/evidence/collision checks precede writes; exclusive creation
    # preserves existing assets if another writer races attachment.
    for name, content in payloads.items():
        with (assets / name).open("xb") as output:
            output.write(content)
    release["firefox_experimental"] = records
    release["firefox_stable_updates"] = stable
    release_path.write_text(json.dumps(release, indent=2) + "\n", encoding="utf-8")
    return {"version": version, "source_commit": commit, "firefox_experimental": records,
            "firefox_stable_updates": stable, **label}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    development = commands.add_parser("build", help="create an unsigned capability-only experimental developer XPI")
    development.add_argument("--output", type=Path, required=True)
    validation = commands.add_parser("validate", help="validate a developer XPI against all source payload bytes")
    validation.add_argument("--xpi", type=Path, required=True)
    release = commands.add_parser("attach", help="historical stable path; explicitly rejects experimental manifests")
    release.add_argument("--signed-xpi", type=Path, required=True)
    release.add_argument("--assets-dir", type=Path, required=True)
    release.add_argument("--tag", required=True)
    release.add_argument("--commit", required=True)
    release.add_argument("--desktop-evidence", type=Path, required=True)
    release.add_argument("--android-evidence", type=Path, required=True)
    experimental = commands.add_parser("attach-experimental", help="attach source-bound dual-platform verified experimental assets")
    experimental.add_argument("--xpi", type=Path, required=True)
    experimental.add_argument("--assets-dir", type=Path, required=True)
    experimental.add_argument("--tag", required=True)
    experimental.add_argument("--commit", required=True)
    experimental.add_argument("--desktop-evidence", type=Path, required=True)
    experimental.add_argument("--android-evidence", type=Path, required=True)
    experimental.add_argument("--real-node-evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build(args.repo_root, args.output)
        elif args.command == "validate":
            manifest = check_payload(args.repo_root, args.xpi)
            result = {"artifact": str(args.xpi), "version": manifest["version"],
                      "sha256": sha256(args.xpi), "distribution": EXPERIMENTAL_DISTRIBUTION,
                      "signed": False, "publication_allowed": False, "payload_matches_source": True}
        elif args.command == "attach-experimental":
            result = attach_experimental(args.repo_root, args.xpi, args.assets_dir, args.tag,
                                         args.commit, args.desktop_evidence, args.android_evidence,
                                         args.real_node_evidence)
        else:
            result = attach(args.repo_root, args.signed_xpi, args.assets_dir, args.tag,
                            args.commit, args.desktop_evidence, args.android_evidence)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"firefox packaging: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
