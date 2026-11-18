"""Real-node command boundaries; network/Docker are replaceable external edges."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import os
import sys
from types import SimpleNamespace
import zipfile

import pytest

from tests.test_real_node_evidence import runtime_archive_fixture
from tests.browser.real_node import evidence, runtime as runtime_module

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('real_node_cli_contract', ROOT / 'tools/test_real_portmap.py')
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


@pytest.fixture(autouse=True)
def private_cache_creation():
    """Mirror CLI umask; permission-rejection cases explicitly chmod their inputs."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def write_addon(path, version):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({'version': version, 'experiment_apis': {'portmap': {}},
            'browser_specific_settings': {'gecko': {'id': gate.EXTENSION_ID}}}))


@pytest.fixture
def candidate(tmp_path):
    release, _ = runtime_archive_fixture(tmp_path / 'assets', '0.26.1', '1' * 40)
    manifest = tmp_path / 'assets/release.json'
    manifest.write_text(json.dumps(release))
    xpi = tmp_path / 'addon.xpi'
    write_addon(xpi, release['version'])
    args = SimpleNamespace(release=None, runtime_manifest=manifest,
        runtime_archive=manifest.parent / release['assets'][0]['filename'], xpi=xpi,
        source_commit=release['source']['commit'], platform='desktop',
        cache_dir=tmp_path / 'cache', artifacts=tmp_path / 'out')
    return args, release


def test_candidate_inputs_retain_exact_manifest_and_xpi_and_return_bound_archive(candidate, tmp_path):
    args, release = candidate
    out = tmp_path / 'proof'
    out.mkdir()
    inputs, archive, observed = gate.prepare_inputs(args, args.cache_dir, out)
    assert archive == out / 'inputs/runtime.tar.gz'
    assert archive.read_bytes() == args.runtime_archive.read_bytes()
    assert observed == release
    assert inputs == {'mode': 'candidate', 'version': release['version'], 'source_commit': args.source_commit,
                      'xpi_sha256': gate.sha256(args.xpi), 'runtime_archive_sha256': release['assets'][0]['sha256'],
                      'runtime_manifest_sha256': gate.sha256(args.runtime_manifest)}
    assert (out / 'inputs/release.json').read_bytes() == args.runtime_manifest.read_bytes()
    assert (out / 'inputs/addon.xpi').read_bytes() == args.xpi.read_bytes()
    retained = archive.read_bytes()
    args.runtime_archive.write_bytes(b'original candidate mutated after snapshot')
    assert archive.read_bytes() == retained


@pytest.mark.parametrize('change', ['archive-bytes', 'source', 'xpi-version'])
def test_candidate_identity_failure_occurs_before_any_runtime_or_browser(candidate, monkeypatch, change):
    args, _ = candidate
    if change == 'archive-bytes':
        args.runtime_archive.write_bytes(args.runtime_archive.read_bytes() + b'changed')
    elif change == 'source':
        args.source_commit = 'f' * 40
    else:
        write_addon(args.xpi, '99.0.0')
    allow_kvm(monkeypatch)
    for boundary in ('Runtime', 'browser_image', 'run_browser'):
        monkeypatch.setattr(gate, boundary, lambda *a, **k: pytest.fail('invalid inputs must not start runtime/browser work'))
    assert gate.run(args) == 1
    proof = json.loads((args.artifacts / 'result.json').read_text())
    assert proof['status'] == 'failed' and proof['error']
    assert [failure['stage'] for failure in proof['failures']] == ['inputs']
    assert proof['inputs'] == {} and proof['runtime'] == {} and proof['platforms'] == {}


def test_cache_key_tracks_toolchain_inputs_but_not_release_results_or_payload(tmp_path, monkeypatch):
    for name in gate.BROWSER_INPUTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    monkeypatch.setattr(gate, 'ROOT', tmp_path)
    monkeypatch.setattr(gate, 'runtime_cache_key', lambda: 'a' * 64)
    first = gate.cache_key()
    browser_first = gate.browser_cache_key()
    (tmp_path / 'result.json').write_text('{"status":"passed"}')
    (tmp_path / 'addon.xpi').write_bytes(b'unrelated new version')
    assert gate.cache_key() == first
    assert gate.browser_cache_key() == browser_first
    (tmp_path / gate.BROWSER_INPUTS[0]).write_text('new pinned browser toolchain')
    assert gate.cache_key() != first
    browser_second = gate.browser_cache_key()
    assert browser_second != browser_first
    second = gate.cache_key()
    monkeypatch.setattr(gate, 'runtime_cache_key', lambda: 'b' * 64)
    assert gate.cache_key() != second
    assert gate.browser_cache_key() == browser_second


@pytest.mark.parametrize('name', gate.BROWSER_INPUTS)
def test_browser_toolchain_changes_do_not_invalidate_the_runtime_base(tmp_path, monkeypatch, name):
    for filename in gate.BROWSER_INPUTS:
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('unit browser toolchain: ' + filename)
    monkeypatch.setattr(gate, 'ROOT', tmp_path)
    runtime_before = gate.runtime_cache_key()
    browser_before = gate.browser_cache_key()
    overall_before = gate.cache_key()
    (tmp_path / name).write_text('changed browser toolchain: ' + name)
    assert gate.runtime_cache_key() == runtime_before
    assert gate.browser_cache_key() != browser_before
    assert gate.cache_key() != overall_before


@pytest.mark.parametrize('name', runtime_module.SOURCE_FILES)
def test_runtime_source_changes_invalidate_only_the_dependency_construction_component(tmp_path, monkeypatch, name):
    for filename in runtime_module.SOURCE_FILES:
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('unit source: ' + filename)
    monkeypatch.setattr(runtime_module, 'SOURCE_DIR', tmp_path)
    monkeypatch.setattr(runtime_module.base, 'SOURCE_DIR', tmp_path)
    source_before = runtime_module._source_identities()
    runtime_before = runtime_module.cache_key()
    browser_before = gate.browser_cache_key()
    overall_before = gate.cache_key()
    (tmp_path / name).write_text('changed unit source: ' + name)
    source_after = runtime_module._source_identities()
    source_name = 'tests/browser/real_node/' + name
    construction_input = source_name in evidence.CONSTRUCTION_SOURCE_INPUTS
    assert source_after[source_name] != source_before[source_name]
    assert (runtime_module.cache_key() != runtime_before) is construction_input
    assert gate.browser_cache_key() == browser_before
    assert (gate.cache_key() != overall_before) is construction_input


def asset_record(payload=b'unit-test immutable download'):
    name = 'portmap-0.26.1-linux-amd64.tar.gz'
    return {'filename': name, 'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload),
            'source_url': gate.RELEASES + 'v0.26.1/' + name}


def test_verified_download_cache_reuses_only_identical_immutable_bytes(tmp_path, monkeypatch):
    payload, calls = b'unit-test immutable download', []
    record = asset_record(payload)
    def download(url, path, **requirements):
        calls.append((url, requirements))
        path.write_bytes(payload)
    monkeypatch.setattr(gate, 'download', download)
    first = gate.cached_asset(tmp_path, 'v0.26.1', record)
    assert first.read_bytes() == payload
    assert gate.cached_asset(tmp_path, 'v0.26.1', record) == first
    assert calls == [(record['source_url'], {'expected': record['sha256'], 'size': record['size']})]
    first.write_bytes(b'corrupted cached bytes')
    with pytest.raises(ValueError, match='integrity'):
        gate.cached_asset(tmp_path, 'v0.26.1', record)
    assert len(calls) == 1


def test_release_asset_redirect_or_noncanonical_url_is_rejected_before_download(tmp_path, monkeypatch):
    record = {**asset_record(), 'source_url': 'https://untrusted.invalid/runtime.tar.gz'}
    monkeypatch.setattr(gate, 'download', lambda *a, **k: pytest.fail('must not fetch a foreign release URL'))
    with pytest.raises(ValueError, match='URL'):
        gate.cached_asset(tmp_path, 'v0.26.1', record)
    redirect = gate.ReleaseRedirect()
    with pytest.raises(ValueError, match='official asset hosts'):
        redirect.redirect_request(None, None, 302, 'redirect', {}, 'https://untrusted.invalid/payload')


def test_symlinked_download_cache_directory_cannot_escape_immutable_root(tmp_path, monkeypatch):
    cache, outside = tmp_path / 'cache', tmp_path / 'outside'
    cache.mkdir()
    outside.mkdir()
    (cache / 'downloads').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(gate, 'download', lambda *a, **k: pytest.fail('escaped cache must not download'))
    with pytest.raises(ValueError):
        gate.cached_asset(cache, 'v0.26.1', asset_record())
    assert list(outside.iterdir()) == []


def test_corrupt_browser_image_archive_is_rejected_before_docker_load(tmp_path, monkeypatch):
    key = 'a' * 64
    directory = tmp_path / 'browsers' / key / 'desktop'
    directory.mkdir(parents=True)
    (directory / 'manifest.json').write_text(json.dumps({'cache_key': key, 'platform': 'desktop',
        'image_id': 'sha256:' + 'b' * 64, 'archive_sha256': 'c' * 64}))
    (directory / 'image.tar').write_bytes(b'corrupted cached OCI bytes')
    monkeypatch.setattr(gate.subprocess, 'run', lambda *a, **k: pytest.fail('must reject before Docker'))
    with pytest.raises(ValueError, match='checksum'):
        gate.browser_image(tmp_path, key, 'desktop', tmp_path / 'build.log')


def test_symlinked_browser_cache_entry_is_rejected_before_docker(tmp_path, monkeypatch):
    cache, outside, key = tmp_path / 'cache', tmp_path / 'outside', 'a' * 64
    directory = cache / 'browsers' / key
    directory.mkdir(parents=True)
    outside.mkdir()
    archive = outside / 'image.tar'
    archive.write_bytes(b'immutable archive bytes')
    (outside / 'manifest.json').write_text(json.dumps({'cache_key': key, 'platform': 'desktop',
        'image_id': 'sha256:' + 'b' * 64, 'archive_sha256': gate.sha256(archive)}))
    (directory / 'desktop').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(gate.subprocess, 'run', lambda *a, **k: pytest.fail('must reject escaped cache before Docker'))
    with pytest.raises(ValueError):
        gate.browser_image(cache, key, 'desktop', tmp_path / 'build.log')


def allow_kvm(monkeypatch):
    exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda path: True if str(path) == '/dev/kvm' else exists(path))
    monkeypatch.setattr(gate, 'require_local_docker', lambda: None)


@pytest.mark.parametrize('nesting', ['artifacts-in-cache', 'cache-in-artifacts'])
def test_public_results_and_immutable_cache_must_not_overlap(candidate, tmp_path, monkeypatch, nesting):
    args, _ = candidate
    allow_kvm(monkeypatch)
    if nesting == 'artifacts-in-cache':
        args.artifacts = args.cache_dir / 'results'
    else:
        args.cache_dir = args.artifacts / 'cache'
    with pytest.raises(ValueError, match='overlap|outside|separate'):
        gate.run(args)
    assert not (args.artifacts / 'result.json').exists()


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_partial_runtime_start_preserves_failure_stops_and_discards_private_state(candidate, monkeypatch, cleanup_fails):
    args, release = candidate
    allow_kvm(monkeypatch)
    lifecycle, state, checkpoints = [], [], []
    class Runtime:
        def __init__(self, **kwargs):
            state.append(kwargs['state_dir'])
            assert kwargs['addon_sha256'] == gate.sha256(args.xpi)
            assert not kwargs['state_dir'].is_relative_to(args.cache_dir)
            assert not kwargs['state_dir'].is_relative_to(args.artifacts)
        def prepare(self):
            lifecycle.append('prepare')
            return {'cache_hit': False}
        def start(self):
            lifecycle.append('start')
            raise RuntimeError('real boundary start failed')
        def collect(self):
            pytest.fail('collection requires a successful runtime start')
        def stop(self):
            lifecycle.append('stop')
            checkpoints.append(json.loads((args.artifacts / 'result.json').read_text()))
            if cleanup_fails:
                raise RuntimeError('secondary cleanup failed')
            return {'status': 'passed'}
    monkeypatch.setattr(gate, 'Runtime', Runtime)
    monkeypatch.setattr(gate, 'browser_image', lambda *a, **k: {'image_id': 'sha256:' + 'a' * 64})
    monkeypatch.setattr(gate, 'browser_cache_key', lambda: 'a' * 64)
    monkeypatch.setitem(sys.modules, 'build_firefox_extension', SimpleNamespace(source_provenance=lambda root: {'dirty': True}))
    assert gate.run(args) == 1
    proof = json.loads((args.artifacts / 'result.json').read_text())
    original = 'nodes_start: real boundary start failed'
    assert proof['status'] == 'failed'
    assert proof['error'] == original
    assert 'evidence' not in proof['runtime']
    expected = [{'stage': 'nodes_start', 'message': 'real boundary start failed'}]
    if cleanup_fails:
        expected.append({'stage': 'runtime_cleanup', 'message': 'secondary cleanup failed'})
        assert 'cleanup' not in proof['runtime']
    else:
        assert proof['runtime']['cleanup']['status'] == 'passed'
    assert proof['failures'] == expected
    assert checkpoints[0]['error'] == original
    assert checkpoints[0]['failures'] == expected[:1]
    assert checkpoints[0]['timings_seconds']['nodes_start'] >= 0
    assert proof['timings_seconds']['collect_cleanup'] >= 0
    assert lifecycle == ['prepare', 'start', 'stop']
    assert state and all(not path.exists() for path in state)


def test_started_runtime_preserves_browser_failure_and_records_both_secondary_failures(candidate, monkeypatch):
    args, _ = candidate
    allow_kvm(monkeypatch)
    lifecycle, state = [], []
    class Runtime:
        def __init__(self, **kwargs):
            state.append(kwargs['state_dir'])
        def prepare(self):
            lifecycle.append('prepare')
            return {'cache_hit': True}
        def start(self):
            lifecycle.append('start')
            return {'network': 'owned-unit-network', 'cache_hit': True}
        def collect(self):
            lifecycle.append('collect')
            raise RuntimeError('secondary collection failed')
        def stop(self):
            lifecycle.append('stop')
            raise RuntimeError('secondary cleanup failed')
    def browser(*args, **kwargs):
        lifecycle.append('desktop')
        raise RuntimeError('original browser failed')
    monkeypatch.setattr(gate, 'Runtime', Runtime)
    monkeypatch.setattr(gate, 'run_browser', browser)
    monkeypatch.setattr(gate, 'browser_image', lambda *a, **k: {'image_id': 'sha256:' + 'a' * 64})
    monkeypatch.setattr(gate, 'browser_cache_key', lambda: 'a' * 64)
    monkeypatch.setitem(sys.modules, 'build_firefox_extension', SimpleNamespace(source_provenance=lambda root: {'dirty': True}))
    assert gate.run(args) == 1
    proof = json.loads((args.artifacts / 'result.json').read_text())
    assert proof['error'] == 'desktop: original browser failed'
    assert proof['failures'] == [
        {'stage': 'desktop', 'message': 'original browser failed'},
        {'stage': 'runtime_collect', 'message': 'secondary collection failed'},
        {'stage': 'runtime_cleanup', 'message': 'secondary cleanup failed'},
    ]
    assert proof['timings_seconds']['desktop'] >= 0
    assert lifecycle == ['prepare', 'start', 'desktop', 'collect', 'stop']
    assert state and all(not path.exists() for path in state)


@pytest.mark.parametrize('arguments', [[], ['--release', 'v0.26.1', '--xpi', 'addon.xpi'],
                                      ['--xpi', 'addon.xpi', '--source-commit', '1' * 40]])
def test_cli_refuses_mixed_or_partial_input_modes_before_runtime(monkeypatch, arguments):
    monkeypatch.setattr(sys, 'argv', ['test_real_portmap.py', 'run', *arguments])
    monkeypatch.setattr(gate, 'run', lambda args: pytest.fail('invalid CLI must not run any artifacts'))
    assert gate.main() == 1


@pytest.mark.parametrize("platform,prebuilt", [("desktop", False), ("desktop", True), ("android", True)])
def test_existing_browser_launcher_reuses_toolchain_but_keeps_fixture_and_runner_boundaries(tmp_path, platform, prebuilt):
    executables = tmp_path / "bin"
    executables.mkdir()
    calls = tmp_path / "calls.jsonl"
    docker = executables / "docker"
    docker.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
with open(os.environ["CALLS"], "a") as out:
    out.write(json.dumps({"program":"docker","args":sys.argv[1:],
                         "browser_image":os.environ.get("BROWSER_IMAGE"),
                         "android_image":os.environ.get("BROWSER_ANDROID_IMAGE")}) + "\\n")
args = sys.argv[1:]
if args[:2] == ["image", "inspect"]:
    print(args[-1] if args[-1].startswith("sha256:") else "sha256:" + "b" * 64)
elif args and args[0] == "inspect":
    print("172.22.0.2")
elif "compose" in args and "ps" in args and args[-1] == "dns":
    print("owned-dns")
elif "compose" in args and "images" in args:
    print("[]")
''')
    python = executables / "python3"
    python.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
with open(os.environ["CALLS"], "a") as out:
    out.write(json.dumps({"program":"python3","args":sys.argv[1:]}) + "\\n")
''')
    stat = executables / "stat"
    stat.write_text("#!/bin/sh\nprintf '123\\n'\n")
    for executable in (docker, python, stat):
        executable.chmod(0o755)
    xpi = tmp_path / "addon.xpi"
    xpi.write_bytes(b"opaque launcher boundary test input")
    artifacts = tmp_path / "proof"
    environment = {**os.environ, "PATH": str(executables) + os.pathsep + os.environ.get("PATH", os.defpath),
                   "CALLS": str(calls), "BROWSER_SOURCE_COMMIT": "1" * 40, "ACCEPT_ANDROID_SDK_LICENSE": "1"}
    environment.pop("BROWSER_DESKTOP_IMAGE", None)
    environment.pop("BROWSER_ANDROID_IMAGE", None)
    if prebuilt:
        environment["BROWSER_DESKTOP_IMAGE"] = "sha256:" + "a" * 64
        environment["BROWSER_ANDROID_IMAGE"] = "sha256:" + "c" * 64
    command = ["sh", str(ROOT / "tests/browser/run.sh"), "--development", "--xpi", str(xpi),
               "--project", "portmap-browser-cache-contract", "--artifacts", str(artifacts)]
    if platform == "android":
        command.append("--android")
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    builds = [record for record in records if record["program"] == "docker" and
              "compose" in record["args"] and "build" in record["args"]]
    assert len(builds) == 1
    services = builds[0]["args"][builds[0]["args"].index("build") + 1:]
    assert set(services) == {"node-a", "node-b", "unrelated", "trap", "dns"} | (set() if prebuilt else {"browser"})
    controls = [record["args"] for record in records if record["program"] == "python3"]
    assert len(controls) == 1
    assert controls[0] == [str(ROOT / "tests/browser/control.py"), str(artifacts),
                           "portmap-browser-cache-contract", str(ROOT),
                           "android" if platform == "android" else "browser", "--development", "--xpi", "/artifacts/input.xpi"]
    assert (artifacts / "input.xpi").read_bytes() == xpi.read_bytes()
    assert any(record["program"] == "docker" and "up" in record["args"] and "--wait" in record["args"]
               for record in records)
    assert any(record["program"] == "docker" and "down" in record["args"] and "--volumes" in record["args"]
               for record in records)
    if prebuilt:
        assert builds[0]["browser_image"] == environment["BROWSER_DESKTOP_IMAGE"]
        assert not any(record["program"] == "docker" and record["args"][0] == "build" for record in records)


def test_browser_prewarm_prepares_both_immutable_images_without_starting_nodes(tmp_path, monkeypatch):
    calls = []
    key, browser_key = "a" * 64, "d" * 64
    args = SimpleNamespace(platform="both", cache_dir=tmp_path / "cache", artifacts=tmp_path / "prepare")
    monkeypatch.setenv("ACCEPT_ANDROID_SDK_LICENSE", "1")
    monkeypatch.setattr(gate, "require_local_docker", lambda: None)
    monkeypatch.setattr(gate, "cache_key", lambda: key)
    monkeypatch.setattr(gate, "browser_cache_key", lambda: browser_key)
    monkeypatch.setattr(gate, "Runtime", lambda **kwargs: pytest.fail("prewarm must not instantiate nodes/state"))
    def image(cache, identity, platform, out, desktop=None):
        calls.append((cache, identity, platform, desktop))
        return {"cache_key": identity, "platform": platform, "image_id": "sha256:" + "b" * 64,
                "archive_sha256": "c" * 64, "cache_hit": True}
    monkeypatch.setattr(gate, "browser_image", image)
    assert gate.prepare_browsers(args) == 0
    receipt = json.loads((args.artifacts / "images.json").read_text())
    assert receipt["schemaVersion"] == 1
    assert receipt["status"] == "ready"
    assert receipt["cache_key"] == key
    assert receipt["browser_cache_key"] == browser_key
    assert set(receipt["images"]) == {"desktop", "android"}
    assert [row[2] for row in calls] == ["desktop", "android"]
    assert all(row[0] == args.cache_dir and row[1] == browser_key for row in calls)
    assert calls[1][3] == receipt["images"]["desktop"]
    assert not (args.artifacts / "result.json").exists()


def test_browser_prewarm_requires_explicit_sdk_license_before_docker_or_cache(tmp_path, monkeypatch):
    args = SimpleNamespace(platform="both", cache_dir=tmp_path / "cache", artifacts=tmp_path / "prepare")
    monkeypatch.delenv("ACCEPT_ANDROID_SDK_LICENSE", raising=False)
    monkeypatch.setattr(gate, "require_local_docker", lambda: pytest.fail("SDK gate must run first"))
    with pytest.raises(ValueError, match="ACCEPT_ANDROID_SDK_LICENSE"):
        gate.prepare_browsers(args)
    assert not args.cache_dir.exists()
    assert not args.artifacts.exists()


@pytest.mark.parametrize("context,endpoint", [(None, "tcp://127.0.0.1:2375"), ("remote-test", "ssh://remote")])
def test_remote_docker_endpoint_is_rejected_before_runtime_mutation(monkeypatch, context, endpoint):
    calls = []
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    if context:
        monkeypatch.setenv("DOCKER_CONTEXT", context)
    else:
        monkeypatch.setenv("DOCKER_HOST", endpoint)
    def execute(command, **kwargs):
        calls.append(command)
        assert command[1:3] == ["context", "inspect"]
        return endpoint
    monkeypatch.setattr(gate, "execute", execute)
    with pytest.raises(ValueError, match="local Unix-socket"):
        gate.require_local_docker()
    assert len(calls) == (1 if context else 0)


@pytest.mark.parametrize("target", ["directory", "manifest", "archive"])
def test_shared_writable_browser_cache_is_rejected_before_executing_cached_image(tmp_path, monkeypatch, target):
    cache, key = tmp_path / "cache", "a" * 64
    directory = cache / "browsers" / key / "desktop"
    directory.mkdir(parents=True)
    archive, manifest = directory / "image.tar", directory / "manifest.json"
    archive.write_bytes(b"self-consistent but externally writable cached image")
    manifest.write_text(json.dumps({"cache_key": key, "platform": "desktop",
        "image_id": "sha256:" + "b" * 64, "archive_sha256": gate.sha256(archive)}))
    selected = {"directory": directory, "manifest": manifest, "archive": archive}[target]
    selected.chmod(0o777 if target == "directory" else 0o666)
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: pytest.fail("untrusted cache must not reach Docker"))
    with pytest.raises(ValueError, match="writ|owner|trust|cache"):
        gate.browser_image(cache, key, "desktop", tmp_path / "build.log")


def test_foreign_owned_cache_root_is_rejected_without_adopting_it(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    original = os.stat
    def foreign_owner(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == cache or path == str(cache):
            values = list(result)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return result
    monkeypatch.setattr(os, "stat", foreign_owner)
    monkeypatch.setattr(gate, "download", lambda *a, **k: pytest.fail("foreign-owned cache must not download"))
    with pytest.raises(ValueError):
        gate.cached_asset(cache, "v0.26.1", asset_record())


@pytest.mark.parametrize('private', [False, True])
def test_runtime_atomic_json_writes_complete_receipt_and_replaces_previous_document(tmp_path, private):
    path = tmp_path / 'nested/receipt.json'
    runtime_module.base._atomic_json(path, {'state': 'initial'}, private=private)
    assert json.loads(path.read_text()) == {'state': 'initial'}
    replacement = {'state': 'complete', 'observations': [{'ok': True, 'count': 2}]}
    runtime_module.base._atomic_json(path, replacement, private=private)
    assert json.loads(path.read_text()) == replacement
    assert path.stat().st_mode & 0o777 == (0o600 if private else 0o644)
    assert set(path.parent.iterdir()) == {path}


def test_real_node_declares_real_frontend_after_actual_empty_init_scaffold(tmp_path):
    from portmap.config import load_endpoint_declarations
    from portmap.errors import ConfigError
    from portmap.scaffold import init_portmap
    from tests.browser.real_node import node_bootstrap

    project = tmp_path / 'vite-app'
    project.mkdir()
    compose = project / 'compose.yaml'
    compose.write_bytes((ROOT / 'tests/browser/real_node/app/compose.yaml').read_bytes())
    init_portmap(project_directory=project, compose_file=compose, out_dir=project / '.portmap')
    declaration = project / '.portmap/endpoints.toml'
    with pytest.raises(ConfigError, match=r'config must contain \[endpoints'):
        load_endpoint_declarations(declaration)

    assert node_bootstrap.declare_project_endpoint(project) == declaration
    endpoint, = load_endpoint_declarations(declaration)
    assert (endpoint.name, endpoint.kind.value, endpoint.service, endpoint.container_port) == ('frontend', 'http', 'frontend', 5173)
    assert endpoint.preserve_host is False and endpoint.upstream_host is None
    assert endpoint.host is None and endpoint.host_port is None
    existing = declaration.read_text() + '\n# Keep an already declared project contract intact.\n'
    declaration.write_text(existing)
    node_bootstrap.declare_project_endpoint(project)
    assert declaration.read_text() == existing
