"""Consumer-facing packaging and immutable Firefox release boundaries."""
import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("firefox_packaging", ROOT / "tools/build_firefox_extension.py")
packaging = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = packaging
_spec.loader.exec_module(packaging)
_pipeline_spec = importlib.util.spec_from_file_location("firefox_release_pipeline", ROOT / "tools/release_pipeline.py")
pipeline = importlib.util.module_from_spec(_pipeline_spec)
sys.modules[_pipeline_spec.name] = pipeline
_pipeline_spec.loader.exec_module(pipeline)


def test_development_package_is_reproducible_and_cannot_be_published(tmp_path):
    first, second = tmp_path / "first.xpi", tmp_path / "second.xpi"
    packaging.build(ROOT, first)
    packaging.build(ROOT, second)
    assert first.read_bytes() == second.read_bytes()
    assert packaging.check_payload(ROOT, first)["experiment_apis"]
    with pytest.raises(ValueError, match="stable signed attach/update feed"):
        packaging.check_signed_payload(ROOT, first)
    before = first.read_bytes()
    with pytest.raises(FileExistsError):
        packaging.build(ROOT, first)
    assert first.read_bytes() == before


def test_changed_runtime_bytes_are_rejected(tmp_path):
    _, files = packaging.runtime_files(ROOT)
    path = tmp_path / "changed.xpi"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content + b"\n" if name == "core.js" else content)
    with pytest.raises(ValueError, match="differs from source: core.js"):
        packaging.check_payload(ROOT, path)


@pytest.mark.parametrize("mutation", ["format", "permissions", "persistent_type", "duplicate_key"])
def test_manifest_formatting_preserves_meaning_not_changed_permissions(tmp_path, mutation):
    original, files = packaging.runtime_files(ROOT)
    changed = json.loads(files["manifest.json"])
    if mutation == "permissions":
        changed["permissions"].append("privacy")
    elif mutation == "persistent_type":
        changed["background"]["persistent"] = 1
    content = json.dumps(changed, indent=2, sort_keys=True).encode()
    if mutation == "duplicate_key":
        content = b'{"version":"0.21.0",' + content[1:]
    path = tmp_path / "signed-payload-fixture.xpi"
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, content if name == "manifest.json" else value)
    if mutation == "format":
        result = packaging.check_payload(ROOT, path)
        assert result == original
    else:
        with pytest.raises(ValueError):
            packaging.check_payload(ROOT, path)


def test_symlinked_runtime_file_is_not_packaged(tmp_path):
    source = tmp_path / "extension/firefox"
    source.mkdir(parents=True)
    (source / "manifest.json").symlink_to(ROOT / "extension/firefox/manifest.json")
    with pytest.raises(ValueError, match="symlinked"):
        packaging.build(tmp_path, tmp_path / "output.xpi")
    assert not (tmp_path / "output.xpi").exists()


@pytest.mark.parametrize("member", ["endpoints.json", ".git/config", "meta/client.json",
                                   "ssh/provider.js", "META-INF/cose.sig"])
def test_developer_payload_rejects_unlisted_or_misleading_members(member, tmp_path):
    _, files = packaging.runtime_files(ROOT)
    path = tmp_path / "extra.xpi"
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        archive.writestr(member, b"not part of the developer payload")
    with pytest.raises(ValueError):
        packaging.check_payload(ROOT, path)


def test_duplicate_payload_member_is_rejected(tmp_path):
    _, files = packaging.runtime_files(ROOT)
    path = tmp_path / "duplicate.xpi"
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("core.js", files["core.js"])
    with pytest.raises(ValueError, match="duplicate"):
        packaging.check_payload(ROOT, path)


def source_fixture(tmp_path):
    _, files = packaging.runtime_files(ROOT)
    for name, value in files.items():
        path = tmp_path / "extension/firefox" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    (tmp_path / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    return tmp_path / "extension/firefox"


@pytest.mark.parametrize("member", ["ssh/bridge.c", "ssh/libssh2.js", "ssh/libssh2.wasm",
                                   "ssh/libssh2-browser-callback.patch"])
def test_source_payload_integrity_rejects_mismatched_crypto_build_inputs(member, tmp_path):
    source = source_fixture(tmp_path)
    path = source / member
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="integrity mismatch"):
        packaging.build(tmp_path, tmp_path / "output.xpi")
    assert not (tmp_path / "output.xpi").exists()


@pytest.mark.parametrize("platform,field,value", [
    ("gecko", "strict_min_version", "140.0"),
    ("gecko_android", "strict_min_version", "142.0"),
    ("gecko", "strict_max_version", "999.0"),
    ("gecko", "update_url", packaging.UPDATE_URL),
])
def test_experimental_build_rejects_version_admission_gates_and_stable_feed(platform, field, value, tmp_path):
    source = source_fixture(tmp_path)
    path = source / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["browser_specific_settings"][platform][field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        packaging.build(tmp_path, tmp_path / "output.xpi")


def test_signed_attach_refuses_experiments_before_touching_release_bytes(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    release = assets / "release.json"
    release.write_bytes(b"existing immutable release")
    old_feed = assets / "firefox-updates.json"
    old_feed.write_bytes(b"existing stable update feed")
    with pytest.raises(ValueError, match="stable signed attach/update feed"):
        packaging.attach(ROOT, tmp_path / "does-not-need-to-exist.xpi", assets,
                         "v0.23.0", "a" * 40, tmp_path / "desktop.json", tmp_path / "android.json")
    assert release.read_bytes() == b"existing immutable release"
    assert old_feed.read_bytes() == b"existing stable update feed"
    assert set(path.name for path in assets.iterdir()) == {"release.json", "firefox-updates.json"}


@pytest.mark.parametrize("change", [
    {"signature_required": False}, {"platform": "desktop-firefox"},
    {"extension_sha256": "b" * 64}, {"source_commit": "c" * 40},
    {"status": "skipped"}, {"cases": []},
    {"cases": [{"name": "two nodes", "status": "skipped"}]},
])
def test_android_release_proof_cannot_be_replaced_by_unsigned_or_other_build(change, tmp_path):
    evidence = {
        "status": "passed", "platform": "android-firefox", "signature_required": True,
        "extension_sha256": "a" * 64, "extension_version": "0.21.0",
        "source_commit": "d" * 40, "browser_version": "128.0",
        "cases": [{"name": "two nodes", "status": "passed"}],
        **change,
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError):
        packaging.check_evidence(path, platform="android-firefox", digest="a" * 64,
                                 version="0.21.0", commit="d" * 40)


def release_fixture(tmp_path):
    version, tag = "0.21.0", "v0.21.0"
    records = []
    for name, body in (
        ("portmap-0.21.0-linux-amd64.tar.gz", b"runtime archive fixture"),
        ("portmap-firefox-0.21.0.xpi", b"signed XPI bytes fixture"),
        ("firefox-updates.json", b'{"addons":{}}\n'),
    ):
        path = tmp_path / name
        path.write_bytes(body)
        digest = packaging.sha256(path)
        (tmp_path / (name + ".sha256")).write_text(f"{digest}  {name}\n")
        records.append({"filename": name, "size": len(body), "sha256": digest,
                        "source_url": pipeline.canonical_source_url(tag, name)})
    records[0].update(os="linux", arch="amd64")
    manifest = {"version": version, "assets": [records[0]],
                "firefox": {"xpi": records[1], "updates": records[2]}}
    (tmp_path / "release.json").write_text(json.dumps(manifest))
    return manifest, records


def test_firefox_assets_participate_in_immutable_digest_validation(tmp_path):
    manifest, records = release_fixture(tmp_path)
    assert pipeline.validate_manifest(manifest, "0.21.0", tmp_path, tag="v0.21.0") == []
    names = pipeline.release_file_names(manifest)
    published = {record["filename"]: {"digest": "sha256:" + record["sha256"]}
                 for record in records}
    missing = pipeline._verify_published(published, manifest, json.dumps(manifest), names)
    assert "portmap-firefox-0.21.0.xpi" not in missing
    assert "firefox-updates.json.sha256" in missing
    published["portmap-firefox-0.21.0.xpi"]["digest"] = "sha256:" + "f" * 64
    with pytest.raises(pipeline.ReleaseError, match="immutable"):
        pipeline._verify_published(published, manifest, json.dumps(manifest), names)
    (tmp_path / "firefox-updates.json").write_text("tampered")
    errors = pipeline.validate_manifest(manifest, "0.21.0", tmp_path, tag="v0.21.0")
    assert "firefox asset content mismatch: firefox-updates.json" in errors


@pytest.mark.parametrize("filename", ["../outside.xpi", "other.xpi", "portmap-firefox-0.20.10.xpi"])
def test_release_manifest_cannot_redirect_firefox_asset_path(filename, tmp_path):
    manifest, _ = release_fixture(tmp_path)
    manifest["firefox"]["xpi"]["filename"] = filename
    assert pipeline.validate_manifest(manifest, "0.21.0", tmp_path, tag="v0.21.0")


def complete_receipt_fixture(platform, built, source):
    """Synthetic validator contract fixture; never actual browser verification."""
    state = {"active": True, "appDisabled": False, "temporary": False}
    def native(process):
        return {"addon_state": state.copy(), "signature_required": False,
                "browser_version": "synthetic-browser", "buildID": "synthetic-build", "processID": process}
    def receiver(node, ssh):
        return {"node": node, "path": "/synthetic", "peerLoopback": ssh,
                "authorization": False, "proxyAuthorization": False}
    def application(node, ssh):
        return {"page": receiver(node, ssh),
                "post": {**receiver(node, ssh), "body": "portmap-body-✓", "originAuthorizationCorrect": True},
                "websocket": {**receiver(node, ssh), "message": "portmap-real-websocket-✓"},
                "ssh_only_status": 200 if ssh else 403}
    observations = {"open-node-from-real-options-control": receiver("node-a", False),
                    "active-ssh-loss-no-direct-fallback": {"terminated": 1}}
    for prefix, node, ssh in (("a.portmap", "node-a", False), ("b.portmap", "node-b", True)):
        for suffix in ("http-app", "https-wss", "descendant"):
            observations[prefix + "-" + suffix] = application(node, ssh)
    for name in ("ssh-key-http-app", "ssh-key-https-wss", "restart-ssh-key-config-and-page"):
        observations[name] = application("node-b", True)
    observations["restart-direct-config-and-page"] = application("node-a", False)
    for name in ("a.portmap-binary-concurrency", "b.portmap-binary-concurrency", "ssh-key-binary-concurrency"):
        observations[name] = {"status": 200, "binary": True, "binaryWs": True,
                              "rows": [{"path": "/echo?parallel=" + str(index)} for index in range(4)]}
    targets = {letter: {"host": host, "sshPort": 2222, "httpPort": 8080, "httpsPort": 8443}
               for letter, host in (("a", "172.30.0.10"), ("b", "172.30.0.11"))}
    fixtures, qr_routes = {}, {}
    for index, label in enumerate(("direct", "ssh", "camera"), 1):
        mode, target = ("ssh", targets["b"]) if label == "ssh" else ("direct", targets["a"])
        domain = "qr-" + label + ".portmap"
        command = [sys.executable, "-c", "from portmap.cli import main; raise SystemExit(main())",
                   "connection-qr", "--name", "Owned QR " + label, "--domain", domain,
                   "--mode", mode, "--host", target["host"], "--http-port", "8080", "--https-port", "8443",
                   "--png", "/artifacts/qr/" + label + ".png", "--wire", "--no-terminal"]
        if mode == "ssh":
            command += ["--ssh-port", "2222", "--ssh-username", "probe", "--ssh-target-host", "127.0.0.1",
                        "--ssh-host-key-sha256", "a" * 64]
        fixtures[label] = {
            "command": command, "domain": domain,
            "png_sha256": hashlib.sha256(("synthetic-png-" + label).encode()).hexdigest(),
            "wire_sha256": hashlib.sha256(("synthetic-wire-" + label).encode()).hexdigest(),
        }
        qr_routes[label] = {**receiver("node-b" if mode == "ssh" else "node-a", mode == "ssh"),
                            "host": domain, "method": "GET", "path": "/qr-" + label + "-" + str(index) * 32}
    fixtures["camera"]["synthetic_padded_scene_sha256"] = hashlib.sha256(b"synthetic-padded-scene").hexdigest()
    observations.update({
        "qr-cli-produces-owned-fixtures": json.loads(json.dumps(fixtures)),
        "qr-image-direct-confirm-route": qr_routes["direct"],
        "qr-ssh-public-fill-clears-auth-and-routes": qr_routes["ssh"],
        "qr-camera-track-cancel-lifecycle": {
            "media_api_overridden": False, "cancelled": True, "synthetic_scene": True,
            "before": [{"kind": "video", "state": "live"}], "after": [{"kind": "video", "state": "ended"}],
        },
    })
    for label in ("malformed", "foreign-url", "secret", "version", "duplicate", "unknown-field", "overlap"):
        observations["qr-reject-" + label] = {
            "config_unchanged": True, "receiver_counts_unchanged": True, "remote_fetch": False,
        }
    camera_case, = packaging.REQUIRED_EXPERIMENTAL_PLATFORM_CASES[platform]
    observations[camera_case] = {
        "tracks": [{"kind": "video", "state": "ended"}], "synthetic_scene": True,
        "physical_camera": False, "media_api_overridden": False,
        "source": ("emulator-imagefile CLI PNG" if platform == "android-firefox" else
                   "Firefox native fake source; no QR scene"),
    }
    direct_routes = [qr_routes["direct"].copy()]
    if platform == "android-firefox":
        observations[camera_case]["route"] = qr_routes["camera"]
        direct_routes.append(qr_routes["camera"].copy())
    network = ({"recorder": "strace-connect-execve", "events": 1, "admission_address": "127.0.0.1",
                "admission_port": 9, "admission_attempts": 0} if platform == "desktop-firefox" else
               {"recorder": "android-emulator-native-pcap", "packets": 1, "ip_packets": 1,
                "scope": "emulator-external-interface", "loopback_admission_observable": False})
    return {
        "evidence_schema": packaging.EVIDENCE_SCHEMA, "source_provenance": source,
        "status": "passed", "platform": platform, "signature_required": False,
        "experimental": True, "capability_available": True, "addon_state": state,
        "extension_sha256": built["sha256"], "extension_version": built["version"],
        "source_commit": source["commit"], "browser_version": "synthetic-browser",
        "cases": [{"name": name, "status": "passed", "started_at": 1, "finished_at": 2}
                  for name in sorted(packaging.REQUIRED_EXPERIMENTAL_CASES |
                                     packaging.REQUIRED_EXPERIMENTAL_PLATFORM_CASES[platform])],
        "unsupported_install": native(1), "supported_install": native(2),
        "restart": {"before": native(2), "after": native(3), "reinstalled": False},
        "origin_requests": {"node-a": [receiver("node-a", False), *direct_routes],
                            "node-b": [receiver("node-b", True), qr_routes["ssh"].copy()],
                            "unrelated": [receiver("unrelated", False)], "trap": []},
        "network_audit": {**network, "observed_direct_http": True, "observed_ssh": True},
        "targets": targets, "qr_fixtures": fixtures,
        "qr_camera_boundary": {
            "physical_camera": False, "media_api_overridden": False,
            "android_source": "supported emulator -camera-back imagefile:<CLI PNG>",
            "desktop_source": "Firefox native fake camera; lifecycle only, not QR-scene proof",
        },
        "observations": observations,
    }


@pytest.fixture(params=tuple(packaging.REQUIRED_EXPERIMENTAL_PLATFORM_CASES))
def qr_receipt(request):
    source = {"commit": "d" * 40, "dirty": False, "payload_sha256": {"manifest.json": "b" * 64},
              "harness_sha256": {"tests/browser/qr.py": "c" * 64}}
    return complete_receipt_fixture(request.param, {"sha256": "a" * 64, "version": "0.25.0"}, source)


def check_receipt_fixture(proof):
    packaging.check_experimental_observations(proof, platform=proof["platform"], source=proof["source_provenance"])


def test_qr_receipt_fixture_matches_observed_platform_shapes(qr_receipt):
    # Preview/cancel asserts inside the harness but returns no observation.
    assert "qr-image-preview-cancel-inert" not in qr_receipt["observations"]
    check_receipt_fixture(qr_receipt)


def test_qr_receipt_requires_its_platform_camera_case(qr_receipt):
    required, = packaging.REQUIRED_EXPERIMENTAL_PLATFORM_CASES[qr_receipt["platform"]]
    qr_receipt["cases"] = [case for case in qr_receipt["cases"] if case["name"] != required]
    with pytest.raises(ValueError, match="required exercised cases"):
        check_receipt_fixture(qr_receipt)


def test_qr_receipt_cannot_claim_the_other_platform_camera(qr_receipt):
    other, = next(cases for platform, cases in packaging.REQUIRED_EXPERIMENTAL_PLATFORM_CASES.items()
                  if platform != qr_receipt["platform"])
    qr_receipt["cases"].append({"name": other, "status": "passed", "started_at": 1, "finished_at": 2})
    with pytest.raises(ValueError, match="camera cases for a different platform"):
        check_receipt_fixture(qr_receipt)


@pytest.mark.parametrize("name", [
    "qr-cli-produces-owned-fixtures", "qr-image-direct-confirm-route",
    "qr-ssh-public-fill-clears-auth-and-routes", "qr-camera-track-cancel-lifecycle",
    "qr-reject-malformed", "qr-reject-foreign-url", "qr-reject-secret", "qr-reject-version",
    "qr-reject-duplicate", "qr-reject-unknown-field", "qr-reject-overlap", "native-camera",
])
def test_qr_case_name_without_its_observation_is_not_evidence(qr_receipt, name):
    if name == "native-camera":
        name, = packaging.REQUIRED_EXPERIMENTAL_PLATFORM_CASES[qr_receipt["platform"]]
    del qr_receipt["observations"][name]
    with pytest.raises(ValueError):
        check_receipt_fixture(qr_receipt)


@pytest.mark.parametrize("label,field,value", [
    ("direct", "png_sha256", None), ("direct", "png_sha256", "a" * 63),
    ("ssh", "wire_sha256", "A" * 64), ("camera", "wire_sha256", True),
    ("camera", "synthetic_padded_scene_sha256", ""),
    ("direct", "domain", "unrelated.portmap"), ("ssh", "password", "not-public-metadata"),
])
def test_qr_fixture_metadata_requires_public_fields_and_sha256(qr_receipt, label, field, value):
    for fixtures in (qr_receipt["qr_fixtures"], qr_receipt["observations"]["qr-cli-produces-owned-fixtures"]):
        fixtures[label][field] = value
    with pytest.raises(ValueError, match="QR fixture metadata/digests|camera-scene digest"):
        check_receipt_fixture(qr_receipt)


@pytest.mark.parametrize("change", ["missing-fixture", "unobserved-fixtures", "unbound-host", "unpadded-camera"])
def test_qr_fixtures_must_match_cli_execution_and_owned_targets(qr_receipt, change):
    if change == "missing-fixture":
        del qr_receipt["qr_fixtures"]["ssh"]
    elif change == "unobserved-fixtures":
        qr_receipt["observations"]["qr-cli-produces-owned-fixtures"]["direct"]["png_sha256"] = "f" * 64
    elif change == "unbound-host":
        qr_receipt["targets"]["a"]["host"] = "192.0.2.1"
    else:
        fixture = qr_receipt["qr_fixtures"]["camera"]
        fixture["synthetic_padded_scene_sha256"] = fixture["png_sha256"]
    with pytest.raises(ValueError):
        check_receipt_fixture(qr_receipt)


@pytest.mark.parametrize("label,change", [
    ("direct", "entrypoint"), ("direct", "missing-wire"), ("direct", "missing-no-terminal"),
    ("direct", "duplicate-host"), ("direct", "wrong-mode"), ("camera", "wrong-png"),
    ("ssh", "password"), ("ssh", "private-key"), ("ssh", "empty-user"), ("ssh", "bad-fingerprint"),
])
def test_qr_fixture_requires_actual_public_only_cli_invocation(qr_receipt, label, change):
    command = qr_receipt["qr_fixtures"][label]["command"]
    if change == "entrypoint":
        command[2] = "print('pretend QR')"
    elif change in ("missing-wire", "missing-no-terminal"):
        command.remove("--" + change.removeprefix("missing-"))
    elif change == "duplicate-host":
        command += ["--host", "192.0.2.1"]
    elif change == "wrong-mode":
        command[command.index("--mode") + 1] = "ssh"
    elif change == "wrong-png":
        command[command.index("--png") + 1] = "/artifacts/qr/direct.png"
    elif change in ("password", "private-key"):
        command[-2:] = ["--" + change, "b" * 64]
    elif change == "empty-user":
        command[command.index("--ssh-username") + 1] = ""
    else:
        command[-1] = "not-a-host-key-fingerprint"
    qr_receipt["observations"]["qr-cli-produces-owned-fixtures"][label]["command"] = command.copy()
    with pytest.raises(ValueError, match="CLI invocation|CLI QR arguments|QR host-key fingerprint"):
        check_receipt_fixture(qr_receipt)


@pytest.mark.parametrize("field,value", [
    ("physical_camera", True), ("physical_camera", 0), ("media_api_overridden", True),
    ("media_api_overridden", 0), ("android_source", "physical phone camera"),
    ("desktop_source", "QR scene decoded through fake media API"),
])
def test_qr_camera_boundary_cannot_overclaim_or_override_capture(qr_receipt, field, value):
    qr_receipt["qr_camera_boundary"][field] = value
    with pytest.raises(ValueError, match="QR camera boundary"):
        check_receipt_fixture(qr_receipt)


@pytest.mark.parametrize("label", ["malformed", "foreign-url", "secret", "version",
                                  "duplicate", "unknown-field", "overlap"])
@pytest.mark.parametrize("field,value", [
    ("config_unchanged", False), ("config_unchanged", 1),
    ("receiver_counts_unchanged", False), ("receiver_counts_unchanged", 1),
    ("remote_fetch", True), ("remote_fetch", 0),
])
def test_qr_rejection_must_observe_no_config_mutation_or_receiver_fetch(qr_receipt, label, field, value):
    qr_receipt["observations"]["qr-reject-" + label][field] = value
    with pytest.raises(ValueError, match="inert .* QR rejection observations"):
        check_receipt_fixture(qr_receipt)


@pytest.mark.parametrize("label", ["direct", "ssh"])
@pytest.mark.parametrize("field,value", [
    ("node", "unrelated"), ("peerLoopback", None), ("authorization", True),
    ("proxyAuthorization", True), ("host", "unrelated.portmap"), ("method", "POST"),
    ("path", "/synthetic"), ("path", "/qr-direct-not-an-observed-request"),
])
def test_qr_confirm_requires_its_actual_node_and_transport_receiver(qr_receipt, label, field, value):
    name = "qr-image-direct-confirm-route" if label == "direct" else "qr-ssh-public-fill-clears-auth-and-routes"
    qr_receipt["observations"][name][field] = value
    with pytest.raises(ValueError, match="QR .*receiver"):
        check_receipt_fixture(qr_receipt)


@pytest.mark.parametrize("label", ["direct", "ssh"])
def test_qr_confirm_receiver_must_appear_in_origin_records(qr_receipt, label):
    name = "qr-image-direct-confirm-route" if label == "direct" else "qr-ssh-public-fill-clears-auth-and-routes"
    route = qr_receipt["observations"][name]
    rows = qr_receipt["origin_requests"][route["node"]]
    rows.remove(route)
    with pytest.raises(ValueError, match="QR route receiver observation"):
        check_receipt_fixture(qr_receipt)


@pytest.mark.parametrize("field,value", [
    ("media_api_overridden", True), ("cancelled", False), ("cancelled", 1), ("synthetic_scene", False),
    ("before", []), ("after", []), ("before", [{"kind": "video", "state": "ended"}]),
    ("after", [{"kind": "video", "state": "live"}]), ("before", [{"kind": "audio", "state": "live"}]),
    ("after", [{"kind": "audio", "state": "ended"}]),
    ("after", [{"kind": "video", "state": "ended"}, {"kind": "video", "state": "ended"}]),
])
def test_qr_cancel_requires_live_native_video_then_same_ended_tracks(qr_receipt, field, value):
    qr_receipt["observations"]["qr-camera-track-cancel-lifecycle"][field] = value
    with pytest.raises(ValueError, match="QR camera cancel|native QR video tracks"):
        check_receipt_fixture(qr_receipt)


@pytest.mark.parametrize("field,value", [
    ("media_api_overridden", True), ("physical_camera", True), ("physical_camera", 0),
    ("synthetic_scene", False), ("source", "mock decoder output"), ("tracks", []),
    ("tracks", [{"kind": "video", "state": "live"}]), ("tracks", [{"kind": "audio", "state": "ended"}]),
])
def test_qr_native_camera_observation_requires_stopped_tracks_and_honest_source(qr_receipt, field, value):
    name, = packaging.REQUIRED_EXPERIMENTAL_PLATFORM_CASES[qr_receipt["platform"]]
    qr_receipt["observations"][name][field] = value
    with pytest.raises(ValueError):
        check_receipt_fixture(qr_receipt)


def test_qr_native_camera_route_claim_matches_platform(qr_receipt):
    name, = packaging.REQUIRED_EXPERIMENTAL_PLATFORM_CASES[qr_receipt["platform"]]
    qr_receipt["observations"][name]["route"] = qr_receipt["observations"]["qr-image-direct-confirm-route"].copy()
    with pytest.raises(ValueError, match="QR route receiver observation|overclaims desktop"):
        check_receipt_fixture(qr_receipt)


def experimental_attachment_fixture(tmp_path):
    root = tmp_path / "source"
    source_fixture(root)
    for name in packaging.HARNESS_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()
    git("init", "--quiet")
    git("config", "user.name", "Packaging contract tests")
    git("config", "user.email", "packaging-tests@portmap.invalid")
    git("add", "-A")
    git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "synthetic packaging contract source")
    source = packaging.source_provenance(root)
    xpi = tmp_path / "experimental.xpi"
    built = packaging.build(root, xpi)
    version = built["version"]
    commit, tag = source["commit"], f"v{version}"
    assets = tmp_path / "assets"
    assets.mkdir()
    release = {"version": version, "source": {
        "repository": "ylang-ylang/portmap", "tag": tag, "commit": commit,
    }, "assets": []}
    (assets / "release.json").write_text(json.dumps(release))
    (assets / "firefox-updates.json").write_bytes(b"existing stable feed")
    evidence = {}
    for platform in ("desktop-firefox", "android-firefox"):
        path = tmp_path / (platform + ".json")
        path.write_text(json.dumps(complete_receipt_fixture(platform, built, source)))
        evidence[platform] = path
    return root, xpi, assets, tag, commit, evidence


def test_experimental_attach_generates_distinct_source_bound_feed_and_preserves_stable(tmp_path):
    root, xpi, assets, tag, commit, evidence = experimental_attachment_fixture(tmp_path)
    result = packaging.attach_experimental(root, xpi, assets, tag, commit,
                                           evidence["desktop-firefox"], evidence["android-firefox"])
    version = result["version"]
    name = f"portmap-firefox-experimental-{version}.xpi"
    assert (assets / name).read_bytes() == xpi.read_bytes()
    assert (assets / "firefox-updates.json").read_bytes() == b"existing stable feed"
    feed = json.loads((assets / "firefox-experimental-updates.json").read_text())
    entry, = feed["addons"][packaging.EXTENSION_ID]["updates"]
    assert entry == {
        "version": version,
        "update_link": f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/{name}",
        "update_hash": "sha256:" + packaging.sha256(xpi),
        "distribution": "experimental-capability-only", "signature_required": False,
        "capability_required": True,
    }
    release = json.loads((assets / "release.json").read_text())
    assert release["firefox_experimental"] == result["firefox_experimental"]
    assert "firefox" not in release
    for record in result["firefox_experimental"].values():
        path = assets / record["filename"]
        assert record["sha256"] == packaging.sha256(path)
        assert record["size"] == path.stat().st_size
        assert (assets / (path.name + ".sha256")).read_text() == f"{record['sha256']}  {path.name}\n"
    before = {path.name: path.read_bytes() for path in assets.iterdir()}
    with pytest.raises(ValueError, match="already attached"):
        packaging.attach_experimental(root, xpi, assets, tag, commit,
                                       evidence["desktop-firefox"], evidence["android-firefox"])
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == before


@pytest.mark.parametrize("change", [
    {"signature_required": True}, {"signature_required": 0}, {"experimental": False},
    {"capability_available": False}, {"capability_available": 1},
    {"addon_state": {"active": True, "appDisabled": False, "temporary": True}},
    {"addon_state": {"active": False, "appDisabled": False, "temporary": False}},
    {"addon_state": {}}, {"platform": "desktop-firefox"},
    {"extension_sha256": "f" * 64}, {"source_commit": "f" * 40},
    {"extension_version": "99.0.0"}, {"status": "skipped"}, {"cases": []},
    {"cases": [{"name": "routing", "status": "skipped"}]},
    {"cases": [{"name": "routing", "status": "passed"}, {"name": "routing", "status": "passed"}]},
])
def test_experimental_attach_requires_exact_actual_dual_platform_evidence(change, tmp_path):
    root, xpi, assets, tag, commit, evidence = experimental_attachment_fixture(tmp_path)
    android = evidence["android-firefox"]
    proof = json.loads(android.read_text())
    proof.update(change)
    android.write_text(json.dumps(proof))
    before = {path.name: path.read_bytes() for path in assets.iterdir()}
    with pytest.raises(ValueError):
        packaging.attach_experimental(root, xpi, assets, tag, commit,
                                       evidence["desktop-firefox"], android)
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == before


def test_experimental_attach_never_replaces_preexisting_output(tmp_path):
    root, xpi, assets, tag, commit, evidence = experimental_attachment_fixture(tmp_path)
    (assets / "firefox-experimental-updates.json").write_bytes(b"existing experimental feed")
    before = {path.name: path.read_bytes() for path in assets.iterdir()}
    with pytest.raises(ValueError, match="already exists"):
        packaging.attach_experimental(root, xpi, assets, tag, commit,
                                       evidence["desktop-firefox"], evidence["android-firefox"])
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == before


def experimental_release_fixture(tmp_path):
    manifest, records = release_fixture(tmp_path)
    experimental = {}
    for key, record in manifest["firefox"].items():
        name = (f"portmap-firefox-experimental-{manifest['version']}.xpi" if key == "xpi"
                else "firefox-experimental-updates.json")
        content = (tmp_path / record["filename"]).read_bytes()
        (tmp_path / name).write_bytes(content)
        (tmp_path / (name + ".sha256")).write_text(f"{record['sha256']}  {name}\n")
        experimental[key] = {**record, "filename": name,
                             "source_url": pipeline.canonical_source_url("v" + manifest["version"], name),
                             "distribution": "experimental-capability-only",
                             "signature_required": False, "capability_required": True}
    manifest["firefox_experimental"] = experimental
    (tmp_path / "release.json").write_text(json.dumps(manifest))
    return manifest


def test_experimental_and_historical_signed_assets_have_separate_immutable_identity(tmp_path):
    manifest = experimental_release_fixture(tmp_path)
    assert pipeline.validate_manifest(manifest, "0.21.0", tmp_path, tag="v0.21.0") == []
    names = pipeline.release_file_names(manifest)
    assert {"portmap-firefox-0.21.0.xpi", "portmap-firefox-experimental-0.21.0.xpi",
            "firefox-updates.json", "firefox-experimental-updates.json",
            "firefox-experimental-updates.json.sha256"} <= set(names)
    (tmp_path / "firefox-experimental-updates.json").write_bytes(b"tampered feed")
    errors = pipeline.validate_manifest(manifest, "0.21.0", tmp_path, tag="v0.21.0")
    assert "firefox asset content mismatch: firefox-experimental-updates.json" in errors


@pytest.mark.parametrize("change", [
    {"signature_required": True}, {"signature_required": 0},
    {"capability_required": False}, {"capability_required": 1},
    {"distribution": "signed"}, {"filename": "firefox-updates.json"},
    {"filename": "../portmap-firefox-experimental-0.21.0.xpi"},
    {"source_url": "https://unrelated.invalid/addon.xpi"},
])
def test_experimental_release_records_cannot_be_signed_mislabeled_or_redirected(change, tmp_path):
    manifest = experimental_release_fixture(tmp_path)
    manifest["firefox_experimental"]["xpi"].update(change)
    assert pipeline.validate_manifest(manifest, "0.21.0", tmp_path, tag="v0.21.0")


@pytest.mark.parametrize("missing_case", sorted(packaging.REQUIRED_EXPERIMENTAL_CASES))
def test_experimental_receipt_cannot_omit_any_required_boundary(missing_case, tmp_path):
    root, xpi, assets, tag, commit, evidence = experimental_attachment_fixture(tmp_path)
    android = evidence["android-firefox"]
    proof = json.loads(android.read_text())
    proof["cases"] = [case for case in proof["cases"] if case["name"] != missing_case]
    android.write_text(json.dumps(proof))
    before = {path.name: path.read_bytes() for path in assets.iterdir()}
    with pytest.raises(ValueError, match="required exercised cases"):
        packaging.attach_experimental(root, xpi, assets, tag, commit,
                                       evidence["desktop-firefox"], android)
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == before


@pytest.mark.parametrize("path,value", [
    (("evidence_schema",), "unknown-schema"),
    (("cases",), [{"name": "invented generic success", "status": "passed", "started_at": 1, "finished_at": 2}]),
    (("cases", 0, "started_at"), float("nan")),
    (("cases", 0, "finished_at"), 0),
    (("source_provenance",), None),
    (("source_provenance", "commit"), "f" * 40),
    (("source_provenance", "dirty"), True),
    (("source_provenance", "dirty"), 0),
    (("source_provenance", "payload_sha256"), {}),
    (("source_provenance", "harness_sha256"), {}),
    (("supported_install",), None),
    (("unsupported_install",), None),
    (("supported_install", "processID"), 0),
    (("supported_install", "buildID"), ""),
    (("restart", "after", "processID"), 2),
    (("restart", "reinstalled"), True),
    (("restart", "after", "buildID"), "different-build"),
    (("network_audit",), None),
    (("network_audit", "sentinel_attempts"), 1),
    (("network_audit", "recorder"), "unobserved"),
    (("network_audit", "ip_packets"), 0),
    (("network_audit", "observed_direct_http"), False),
    (("network_audit", "observed_ssh"), False),
    (("network_audit", "scope"), "guest-loopback"),
    (("network_audit", "loopback_admission_observable"), True),
    (("network_audit", "admission_attempts"), 0),
    (("origin_requests", "node-b"), []),
    (("origin_requests", "trap"), [{"path": "/leaked"}]),
    (("origin_requests", "node-b", 0, "proxyAuthorization"), True),
    (("observations",), {}),
    (("observations", "b.portmap-http-app", "page", "peerLoopback"), False),
    (("observations", "b.portmap-http-app", "post", "originAuthorizationCorrect"), False),
    (("observations", "b.portmap-http-app", "websocket", "message"), "corrupted"),
    (("observations", "ssh-key-https-wss", "ssh_only_status"), 403),
    (("observations", "ssh-key-binary-concurrency", "binaryWs"), False),
    (("observations", "b.portmap-binary-concurrency", "rows"), []),
    (("observations", "active-ssh-loss-no-direct-fallback", "terminated"), 0),
    (("qr_fixtures",), None),
    (("qr_camera_boundary", "media_api_overridden"), True),
    (("observations", "qr-cli-produces-owned-fixtures"), {}),
    (("observations", "qr-reject-foreign-url", "remote_fetch"), True),
    (("observations", "qr-ssh-public-fill-clears-auth-and-routes", "peerLoopback"), False),
    (("observations", "qr-camera-track-cancel-lifecycle", "before"), [{"kind": "video", "state": "ended"}]),
    (("observations", "qr-camera-native-synthetic-scene-route", "source"), "unobserved"),
])
def test_experimental_receipt_rejects_truncated_inconsistent_or_unbound_observations(path, value, tmp_path):
    root, xpi, assets, tag, commit, evidence = experimental_attachment_fixture(tmp_path)
    android = evidence["android-firefox"]
    proof = json.loads(android.read_text())
    target = proof
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value
    android.write_text(json.dumps(proof))
    before = {path.name: path.read_bytes() for path in assets.iterdir()}
    with pytest.raises(ValueError):
        packaging.attach_experimental(root, xpi, assets, tag, commit,
                                       evidence["desktop-firefox"], android)
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == before


@pytest.mark.parametrize("change", ["untracked", "tracked", "different-commit"])
def test_experimental_publication_requires_actual_clean_exact_commit(change, tmp_path):
    root, xpi, assets, tag, commit, evidence = experimental_attachment_fixture(tmp_path)
    if change == "untracked":
        (root / "uncommitted-source").write_text("not in the receipt commit")
    elif change == "tracked":
        (root / "tests/browser/runner.py").write_text("changed evidence producer")
    else:
        commit = "f" * 40
    before = {path.name: path.read_bytes() for path in assets.iterdir()}
    with pytest.raises(ValueError, match="clean checkout at the exact source commit"):
        packaging.attach_experimental(root, xpi, assets, tag, commit,
                                       evidence["desktop-firefox"], evidence["android-firefox"])
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == before


@pytest.mark.parametrize("field,value", [
    ("admission_address", "192.0.2.1"), ("admission_port", 80),
    ("admission_attempts", 1), ("admission_attempts", False),
    ("observed_direct_http", False), ("observed_ssh", False), ("events", 0),
])
def test_desktop_receipt_requires_actual_loopback_admission_syscall_audit(field, value, tmp_path):
    root, xpi, assets, tag, commit, evidence = experimental_attachment_fixture(tmp_path)
    desktop = evidence["desktop-firefox"]
    proof = json.loads(desktop.read_text())
    proof["network_audit"][field] = value
    desktop.write_text(json.dumps(proof))
    before = {path.name: path.read_bytes() for path in assets.iterdir()}
    with pytest.raises(ValueError):
        packaging.attach_experimental(root, xpi, assets, tag, commit,
                                       desktop, evidence["android-firefox"])
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == before


@pytest.mark.parametrize("name", ["tests/browser/runner.py", "tools/build_firefox_extension.py",
                                 "tools/build_firefox_ssh.py", "pyproject.toml"])
def test_publication_binds_git_blobs_even_when_index_hides_a_local_edit(name, tmp_path):
    root, xpi, assets, tag, commit, evidence = experimental_attachment_fixture(tmp_path)
    subprocess.run(["git", "-C", str(root), "update-index", "--assume-unchanged", name], check=True)
    path = root / name
    path.write_text(path.read_text() + "\n# uncommitted source input hidden from ordinary status\n")
    assert packaging.source_provenance(root)["dirty"] is False
    before = {path.name: path.read_bytes() for path in assets.iterdir()}
    with pytest.raises(ValueError, match="committed source differs"):
        packaging.attach_experimental(root, xpi, assets, tag, commit,
                                       evidence["desktop-firefox"], evidence["android-firefox"])
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == before


def test_developer_package_identity_does_not_depend_on_zlib(tmp_path, monkeypatch):
    first, second = tmp_path / "first.xpi", tmp_path / "second.xpi"
    packaging.build(ROOT, first)
    def unavailable_compressor(*args, **kwargs):
        raise AssertionError("developer artifact must not depend on a zlib compressor")
    monkeypatch.setattr(zipfile.zlib, "compressobj", unavailable_compressor)
    packaging.build(ROOT, second)
    assert first.read_bytes() == second.read_bytes()
    assert packaging.check_payload(ROOT, second)["version"] == packaging.check_payload(ROOT, first)["version"]
