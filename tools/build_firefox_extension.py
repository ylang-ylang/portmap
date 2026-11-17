#!/usr/bin/env python3
"""Package capability-only experimental Firefox builds; preserve stable signing boundaries."""
from __future__ import annotations

import argparse
import hashlib
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
RUNTIME_FILES = (
    "manifest.json", "core.js", "controller.js", "background.js",
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
EVIDENCE_SCHEMA = "portmap-browser-capability-qr-v2"
HARNESS_FILES = tuple("tests/browser/" + name for name in (
    "runner.py", "android.py", "autofill.py", "network_evidence.py", "run.sh",
    "traced-geckodriver.sh", "control.py", "compose.yaml", "compose.android.yaml",
    "Dockerfile", "Android.Dockerfile", "fixture.py", "target.py", "dns.py", "requirements.txt",
    "qr.py", "camera_scene.py", "camera_probe.py", "CameraProbe.Dockerfile",
)) + (
    "tools/build_firefox_extension.py", "tools/build_firefox_ssh.py", "pyproject.toml",
    "src/portmap/cli.py", "src/portmap/connection_qr.py",
    "src/portmap/_qr_encoder.py", "src/portmap/_qr_encoder.LICENSE", "packaging/portmap.spec",
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
        for name in ("ssh/bridge.c", "ssh/libssh2.js", "ssh/libssh2.wasm",
                     "ssh/libssh2-browser-callback.patch"):
            if hashlib.sha256(files[name]).hexdigest() != integrity.get(name):
                raise ValueError(f"SSH source/payload integrity mismatch: {name}")
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
    require(isinstance(fixtures, dict) and set(fixtures) == {"direct", "ssh", "camera"},
            "lacks actual CLI QR fixtures")
    targets = proof.get("targets")
    require(isinstance(targets, dict), "lacks QR fixture receiver targets")
    for label, mode, target_name in (("direct", "direct", "a"), ("ssh", "ssh", "b"), ("camera", "direct", "a")):
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
        require(isinstance(command, list) and len(command) == (28 if mode == "ssh" else 20) and
                all(isinstance(arg, str) and bool(arg) for arg in command) and
                Path(command[0]).is_absolute() and
                command[1:4] == ["-c", "from portmap.cli import main; raise SystemExit(main())", "connection-qr"],
                f"lacks actual {label} connection-qr CLI invocation")
        expected = [
            "--name", "Owned QR " + label, "--domain", fixture["domain"], "--mode", mode,
            "--host", target["host"], "--http-port", str(target["httpPort"]),
            "--https-port", str(target["httpsPort"]), "--png", "/artifacts/qr/" + label + ".png",
            "--wire", "--no-terminal",
        ]
        if mode == "ssh":
            require(digest(command[-1]), "has invalid public SSH QR host-key fingerprint")
            expected += ["--ssh-port", str(target["sshPort"]), "--ssh-username", "probe",
                         "--ssh-target-host", "127.0.0.1", "--ssh-host-key-sha256", command[-1]]
        require(command[4:] == expected, f"has inconsistent or non-public {label} CLI QR arguments")
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


def attach_experimental(root: Path, xpi: Path, assets: Path, tag: str, commit: str,
                        desktop_evidence: Path, android_evidence: Path) -> dict:
    """Attach explicitly experimental bytes, never modifying the stable Firefox feed."""
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
    if "firefox_experimental" in release:
        raise ValueError("experimental Firefox assets already attached; never replace release bytes")
    filename = f"portmap-firefox-experimental-{version}.xpi"
    feedname = "firefox-experimental-updates.json"
    base_url = f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/"
    label = {"distribution": EXPERIMENTAL_DISTRIBUTION, "signature_required": False,
             "capability_required": True}
    updates = {"addons": {EXTENSION_ID: {"updates": [{
        "version": version, "update_link": base_url + filename,
        "update_hash": "sha256:" + digest, **label,
    }]}}}
    payloads = {filename: xpi.read_bytes(), feedname: (json.dumps(updates, indent=2) + "\n").encode()}
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
    release_path.write_text(json.dumps(release, indent=2) + "\n", encoding="utf-8")
    return {"version": version, "source_commit": commit, "firefox_experimental": records, **label}


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
                                         args.commit, args.desktop_evidence, args.android_evidence)
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
