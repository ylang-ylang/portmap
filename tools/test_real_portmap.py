#!/usr/bin/env python3
"""Run real Portmap/Vite browser acceptance with cached tools and fresh state."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.browser.real_node.runtime import Runtime, cache_key as runtime_cache_key

REPOSITORY = "ylang-ylang/portmap"
RELEASES = "https://github.com/" + REPOSITORY + "/releases/download/"
EXTENSION_ID = "firefox@portmap.ylang-ylang.github.io"
HEX = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
TAG = re.compile(r"v(\d+\.\d+\.\d+)\Z")
BROWSER_INPUTS = ("tests/browser/Dockerfile", "tests/browser/Android.Dockerfile",
                  "tests/browser/requirements.txt")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def dump(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def execute(command: list[str], *, log: Path | None = None, timeout: int = 1800) -> str:
    if log is None:
        result = subprocess.run(command, check=True, text=True, capture_output=True, timeout=timeout)
        return result.stdout.strip()
    with log.open("ab") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{command[0]} exited {result.returncode}; see {log.name}")
    return ""


def browser_cache_key() -> str:
    inputs = {name: sha256(ROOT / name) for name in BROWSER_INPUTS}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def cache_key() -> str:
    inputs = {"browser": browser_cache_key(), "runtime": runtime_cache_key()}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def trusted_cache_path(path: Path, *, directory: bool) -> None:
    value = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    require(expected_type(value.st_mode) and value.st_uid == os.getuid() and
            value.st_mode & 0o022 == 0,
            "Cache entries must be real, owned by this user and not writable by other users")


def cache_directory(root: Path, *parts: str) -> Path:
    require(root.is_dir() and not root.is_symlink(), "Cache root must be a real directory")
    trusted_cache_path(root, directory=True)
    for ancestor in root.resolve().parents:
        value = ancestor.stat()
        require(value.st_uid in (0, os.getuid()) and
                (value.st_mode & 0o022 == 0 or bool(value.st_mode & stat.S_ISVTX)),
                "Cache ancestors must not allow another user to replace cache paths")
    current = root
    for part in parts:
        require(isinstance(part, str) and part not in ("", ".", "..") and
                Path(part).name == part, "Invalid cache path component")
        current = current / part
        require(not current.is_symlink(), "Cache directories must not be symlinks")
        current.mkdir(exist_ok=True, mode=0o700)
        require(current.is_dir() and not current.is_symlink() and
                current.resolve().is_relative_to(root.resolve()), "Cache directory escaped its root")
        trusted_cache_path(current, directory=True)
    return current


def separate_trees(first: Path, second: Path) -> None:
    first, second = first.resolve(), second.resolve()
    require(not first.is_relative_to(second) and not second.is_relative_to(first),
            "Cache, public evidence and private state directories must not overlap")


def prepare_directories(args) -> tuple[Path, Path]:
    cache_argument = args.cache_dir.expanduser().absolute()
    require(not cache_argument.is_symlink(), "Cache root must not be a symlink")
    cache = cache_argument.resolve()
    out = args.artifacts.expanduser().absolute() if args.artifacts else Path(tempfile.mkdtemp(prefix="portmap-real-node-proof-"))
    try:
        separate_trees(cache, out)
    except ValueError:
        if not args.artifacts:
            out.rmdir()
        raise
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_directory(cache)
    if args.artifacts:
        out.mkdir(parents=True, exist_ok=False)
    require(not out.is_symlink(), "Evidence directory must not be a symlink")
    return cache, out


def require_local_docker() -> None:
    context = os.environ.get("DOCKER_CONTEXT")
    endpoint = (execute(["docker", "context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"])
                if context else os.environ.get("DOCKER_HOST") or
                execute(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]))
    require(endpoint.startswith("unix:///"), "Use a local Unix-socket Docker engine, not a remote context")
    require(execute(["docker", "info", "--format", "{{.OSType}}"], timeout=30) == "linux",
            "Real-node tools require a Linux Docker engine")


def selected_platforms(args) -> tuple[str, ...]:
    platforms = ("desktop", "android") if args.platform == "both" else (args.platform,)
    if "android" in platforms:
        require(os.environ.get("ACCEPT_ANDROID_SDK_LICENSE") == "1",
                "Read the Android SDK terms and explicitly set ACCEPT_ANDROID_SDK_LICENSE=1")
    return platforms


@contextlib.contextmanager
def cache_lock(path: Path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        value = os.fstat(descriptor)
        require(stat.S_ISREG(value.st_mode) and value.st_uid == os.getuid() and value.st_mode & 0o022 == 0,
                "Cache lock must be a private regular file owned by this user")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        from urllib.parse import urlsplit
        destination = urlsplit(url)
        require(destination.scheme == "https" and destination.hostname in {
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"},
            "Release download redirected outside the official asset hosts")
        return super().redirect_request(request, response, code, message, headers, url)


def download(url: str, path: Path, *, expected: str | None = None, size: int | None = None) -> None:
    require(not path.exists() and not path.is_symlink(), "Download destination already exists")
    opener = urllib.request.build_opener(ReleaseRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "portmap-real-node-test"})
    limit = size if size is not None else 1024 * 1024
    try:
        with opener.open(request, timeout=120) as response, path.open("xb") as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                require(total <= limit, "Release download exceeded its declared size")
                output.write(chunk)
        if size is not None:
            require(total == size, "Release download size mismatch")
        if expected is not None:
            require(sha256(path) == expected, "Release download checksum mismatch")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def asset_record(release: dict, version: str) -> dict:
    candidates = [value for value in release.get("assets", [])
                  if isinstance(value, dict) and value.get("os") == "linux" and value.get("arch") == "amd64"]
    require(len(candidates) == 1, "Runtime manifest requires one linux-amd64 asset")
    record = candidates[0]
    require(record.get("filename") == f"portmap-{version}-linux-amd64.tar.gz" and
            isinstance(record.get("sha256"), str) and bool(HEX.fullmatch(record["sha256"])) and
            type(record.get("size")) is int and record["size"] > 0, "Invalid runtime asset identity")
    return record


def cached_asset(cache: Path, tag: str, record: dict) -> Path:
    expected_url = RELEASES + tag + "/" + record["filename"]
    require(record.get("source_url") == expected_url, "Release asset URL does not match its identity")
    directory = cache_directory(cache, "downloads", record["sha256"])
    path = directory / record["filename"]
    with cache_lock(directory / ".lock"):
        require(not path.is_symlink(), "Cached release asset must not be a symlink")
        if path.exists():
            trusted_cache_path(path, directory=False)
            require(path.is_file() and path.stat().st_size == record["size"] and
                    sha256(path) == record["sha256"], "Cached release asset failed integrity verification")
        else:
            temporary = directory / ("download-" + uuid.uuid4().hex)
            download(expected_url, temporary, expected=record["sha256"], size=record["size"])
            temporary.replace(path)
    return path


def prepare_inputs(args, cache: Path, out: Path) -> tuple[dict, Path, dict]:
    directory = out / "inputs"
    directory.mkdir()
    manifest_path = directory / "release.json"
    if args.release:
        match = TAG.fullmatch(args.release)
        require(match is not None, "Use an exact published vX.Y.Z tag")
        download(RELEASES + args.release + "/release.json", manifest_path)
    else:
        require(args.runtime_manifest.is_file() and not args.runtime_manifest.is_symlink(),
                "Runtime manifest must be a regular file")
        shutil.copyfile(args.runtime_manifest, manifest_path)
    release = json.loads(manifest_path.read_text())
    require(isinstance(release, dict), "Runtime manifest must be an object")
    version = release.get("version")
    require(isinstance(version, str) and TAG.fullmatch("v" + version) is not None,
            "Runtime manifest version is invalid")
    source = release.get("source")
    require(isinstance(source, dict) and source.get("repository") == REPOSITORY and
            source.get("tag") == "v" + version and isinstance(source.get("commit"), str) and
            COMMIT.fullmatch(source["commit"]) is not None, "Runtime manifest source identity is invalid")
    record = asset_record(release, version)
    if args.release:
        require(args.release == source["tag"], "Published tag/version mismatch")
        archive = cached_asset(cache, args.release, record)
        xpi_record = release.get("firefox_experimental", {}).get("xpi", {})
        require(xpi_record.get("filename") == f"portmap-firefox-experimental-{version}.xpi" and
                isinstance(xpi_record.get("sha256"), str) and HEX.fullmatch(xpi_record["sha256"]) and
                type(xpi_record.get("size")) is int and xpi_record["size"] > 0,
                "Published manifest lacks an experimental XPI identity")
        xpi = cached_asset(cache, args.release, xpi_record)
    else:
        require(args.source_commit == source["commit"], "Candidate source commit differs from runtime manifest")
        archive, xpi = args.runtime_archive.resolve(), args.xpi.resolve()
        require(archive.is_file() and sha256(archive) == record["sha256"] and
                archive.stat().st_size == record["size"], "Candidate runtime archive differs from manifest")
    require(xpi.is_file(), "Missing tested XPI")
    expected_xpi = sha256(xpi)
    shutil.copyfile(xpi, directory / "addon.xpi")
    xpi = directory / "addon.xpi"
    require(sha256(xpi) == expected_xpi, "XPI changed while taking its input snapshot")
    shutil.copyfile(archive, directory / "runtime.tar.gz")
    archive = directory / "runtime.tar.gz"
    require(archive.stat().st_size == record["size"] and sha256(archive) == record["sha256"],
            "Runtime archive changed while taking its input snapshot")
    with zipfile.ZipFile(xpi) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
    require(manifest.get("version") == version and
            manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id") == EXTENSION_ID and
            bool(manifest.get("experiment_apis")), "XPI/runtime version or extension identity mismatch")
    return ({"mode": "release" if args.release else "candidate", "version": version,
             "source_commit": source["commit"], "xpi_sha256": sha256(xpi),
             "runtime_archive_sha256": record["sha256"], "runtime_manifest_sha256": sha256(manifest_path)},
            archive, release)


def browser_image(cache: Path, key: str, platform: str, out: Path, desktop: dict | None = None) -> dict:
    require(platform in ("desktop", "android") and HEX.fullmatch(key) is not None,
            "Invalid browser cache identity")
    directory = cache_directory(cache, "browsers", key) / platform
    require(not directory.is_symlink(), "Browser cache directories must not be symlinks")
    if directory.exists():
        trusted_cache_path(directory, directory=True)
    with cache_lock(directory.parent / (platform + ".lock")):
        metadata = directory / "manifest.json"
        if metadata.exists():
            require(not metadata.is_symlink(), "Browser cache manifest must not be a symlink")
            trusted_cache_path(metadata, directory=False)
            record = json.loads(metadata.read_text())
            require(record.get("cache_key") == key and record.get("platform") == platform and
                    isinstance(record.get("image_id"), str) and
                    re.fullmatch(r"sha256:[0-9a-f]{64}", record["image_id"]) is not None,
                    "Browser cache identity mismatch")
            archive = directory / "image.tar"
            trusted_cache_path(archive, directory=False)
            require(not archive.is_symlink() and archive.is_file() and
                    sha256(archive) == record.get("archive_sha256"), "Browser image cache checksum mismatch")
            probe = subprocess.run(["docker", "image", "inspect", record["image_id"]],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if probe.returncode:
                execute(["docker", "image", "load", "--input", str(archive)], log=out)
            require(execute(["docker", "image", "inspect", "--format", "{{.Id}}", record["image_id"]]) ==
                    record["image_id"], "Loaded browser image differs from cache")
            return {**record, "cache_hit": True}
        require(not directory.exists(), "Incomplete browser cache entry; remove it before retrying")
        tag = f"portmap-real-node-browser:{key[:20]}-{platform}"
        with tempfile.TemporaryDirectory(prefix=platform + "-", dir=directory.parent) as temporary:
            stage = Path(temporary)
            if platform == "desktop":
                command = ["docker", "build", "--target", "browser-tools", "--file",
                           str(ROOT / "tests/browser/Dockerfile"), "--tag", tag, str(ROOT)]
            else:
                require(desktop is not None, "Android requires the prepared desktop toolchain base")
                base_tag = f"portmap-real-node-browser:{key[:20]}-desktop"
                execute(["docker", "tag", desktop["image_id"], base_tag])
                command = ["docker", "build", "--file", str(ROOT / "tests/browser/Android.Dockerfile"),
                           "--build-arg", "BROWSER_BASE=" + base_tag, "--build-arg", "ACCEPT_ANDROID_SDK_LICENSE=1",
                           "--build-arg", "FIREFOX_ANDROID_CHANNEL=nightly", "--tag", tag, str(ROOT)]
            execute(command, log=out, timeout=3600)
            identity = execute(["docker", "image", "inspect", "--format", "{{.Id}}", tag])
            require(re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is not None,
                    "Docker returned an invalid browser image identity")
            execute(["docker", "image", "save", "--output", str(stage / "image.tar"), identity], log=out)
            record = {"cache_key": key, "platform": platform, "image_id": identity,
                      "archive_sha256": sha256(stage / "image.tar")}
            dump(stage / "manifest.json", record)
            stage.rename(directory)
        return {**record, "cache_hit": False}


def prepare_browsers(args) -> int:
    platforms = selected_platforms(args)
    require_local_docker()
    cache, out = prepare_directories(args)
    started = time.monotonic()
    key = browser_cache_key()
    desktop = browser_image(cache, key, "desktop", out / "browser-prepare.log")
    images = {"desktop": desktop}
    if "android" in platforms:
        images["android"] = browser_image(cache, key, "android", out / "browser-prepare.log", desktop)
    receipt = {"schemaVersion": 1, "status": "ready", "cache_key": cache_key(),
               "browser_cache_key": key, "images": images, "elapsed_seconds": time.monotonic() - started}
    dump(out / "images.json", receipt)
    print(json.dumps({"receipt": str(out / "images.json"), **receipt}, indent=2))
    return 0


def run_browser(platform: str, image: dict, runtime: dict, out: Path, xpi: Path) -> dict:
    out.mkdir()
    name = "portmap-real-node-" + platform + "-" + uuid.uuid4().hex[:12]
    command = ["docker", "run", "--rm", "--name", name, "--network", runtime["network"],
               "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true", "--shm-size", "1g"]
    environment = {"HOME": "/tmp/realnode-home", "XDG_RUNTIME_DIR": "/tmp/realnode-runtime",
                   "MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1", "PYTHONPATH": "/repo/src"}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        environment[key] = environment[key.lower()] = ""
    if platform == "android":
        command += ["--device", "/dev/kvm:/dev/kvm", "--group-add", str(Path("/dev/kvm").stat().st_gid)]
        environment.update(ACCEPT_ANDROID_SDK_LICENSE="1", ANDROID_FIREFOX_CHANNEL="nightly",
                           ANDROID_AVD_HOME="/tmp/android-avd")
    for key, value in environment.items():
        command += ["--env", key + "=" + value]
    for source, target, readonly in ((ROOT, "/repo", True), (xpi, "/input/addon.xpi", True),
                                    (Path(runtime["public_manifest"]), "/input/public.json", True),
                                    (Path(runtime["private_manifest"]), "/input/private.json", True),
                                    (out, "/artifacts", False)):
        command += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
    command += ["--entrypoint", "python", image["image_id"], "/repo/tests/browser/real_node/browser.py",
                "--platform", platform, "--xpi", "/input/addon.xpi", "--public-manifest", "/input/public.json",
                "--private-manifest", "/input/private.json", "--output", "/artifacts"]
    try:
        execute(command, log=out / "launcher.log", timeout=1200)
    finally:
        subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
        remaining = execute(["docker", "ps", "--all", "--filter", "name=" + name,
                             "--format", "{{.Names}}"], timeout=30).splitlines()
        require(name not in remaining, "Owned browser container was not removed")
        dump(out / "container-cleanup.json", {"status": "passed", "name": name, "removed": True})
    result = json.loads((out / "receipt.json").read_text())
    require(result.get("status") == "passed", f"Actual {platform} real-node browser checks failed")
    return result


def artifact_hashes(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "Public evidence must not contain symlinks")
        if path.is_file() and path != root / "result.json":
            result[path.relative_to(root).as_posix()] = sha256(path)
    return result


def record_failure(proof: dict, stage: str, error: BaseException) -> None:
    message = "Interrupted" if isinstance(error, KeyboardInterrupt) else str(error)[:2000]
    proof["status"] = "failed"
    if proof["error"] is None:
        proof["error"] = stage + ": " + message
    proof["failures"].append({"stage": stage, "message": message})


def run(args) -> int:
    require(sys.platform == "linux", "Real-node acceptance requires a Linux KVM runner")
    require(Path("/dev/kvm").exists(), "Real-node acceptance requires /dev/kvm; no privileged fallback")
    platforms = selected_platforms(args)
    cache, out = prepare_directories(args)
    started = time.monotonic()
    proof = {"schemaVersion": 1, "kind": "portmap-real-node-browser", "status": "failed",
             "inputs": {}, "harness": {}, "runtime": {}, "platforms": {},
             "timings_seconds": {}, "artifacts": {}, "error": None, "failures": []}
    dump(out / "result.json", proof)
    runtime = None
    runtime_started = False
    phase, stage = "preflight", time.monotonic()
    private = tempfile.TemporaryDirectory(prefix="portmap-real-node-private-", dir=out.parent)
    separate_trees(cache, Path(private.name))
    separate_trees(out, Path(private.name))
    try:
        require_local_docker()
        phase = "inputs"
        stage = time.monotonic()
        inputs, archive, release = prepare_inputs(args, cache, out)
        proof["inputs"] = inputs
        from build_firefox_extension import source_provenance
        proof["harness"] = source_provenance(ROOT)
        proof["timings_seconds"]["inputs"] = time.monotonic() - stage
        phase, stage = "runtime_prepare", time.monotonic()
        runtime_out = out / "runtime"
        runtime_out.mkdir()
        runtime = Runtime(cache_dir=cache_directory(cache, "runtime"), state_dir=Path(private.name), out_dir=runtime_out,
                          cli_archive=archive, cli_sha256=inputs["runtime_archive_sha256"],
                          version=inputs["version"], source_commit=inputs["source_commit"],
                          addon_sha256=inputs["xpi_sha256"])
        proof["runtime"]["cache"] = runtime.prepare()
        proof["timings_seconds"]["runtime_prepare"] = time.monotonic() - stage
        phase, stage = "browser_prepare", time.monotonic()
        key = browser_cache_key()
        desktop = browser_image(cache, key, "desktop", out / "browser-prepare.log")
        images = {"desktop": desktop}
        if "android" in platforms:
            images["android"] = browser_image(cache, key, "android", out / "browser-prepare.log", desktop)
        proof["browser_images"] = images
        proof["timings_seconds"]["browser_prepare"] = time.monotonic() - stage
        phase, stage = "nodes_start", time.monotonic()
        active = runtime.start()
        runtime_started = True
        proof["runtime"]["start"] = {key: value for key, value in active.items() if key != "private_manifest"}
        proof["timings_seconds"]["nodes_start"] = time.monotonic() - stage
        for platform in platforms:
            phase, stage = platform, time.monotonic()
            proof["platforms"][platform] = run_browser(platform, images[platform], active, out / platform,
                                                       out / "inputs/addon.xpi")
            proof["timings_seconds"][platform] = time.monotonic() - stage
        proof["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        proof["timings_seconds"].setdefault(phase, time.monotonic() - stage)
        record_failure(proof, phase, error)
        dump(out / "result.json", proof)
    finally:
        if runtime is not None:
            stage = time.monotonic()
            if runtime_started:
                try:
                    proof["runtime"]["evidence"] = runtime.collect()
                except Exception as error:
                    record_failure(proof, "runtime_collect", error)
            try:
                proof["runtime"]["cleanup"] = runtime.stop()
                require(proof["runtime"]["cleanup"].get("status") == "passed", "Owned runtime cleanup failed")
            except Exception as error:
                record_failure(proof, "runtime_cleanup", error)
            proof["timings_seconds"]["collect_cleanup"] = time.monotonic() - stage
        try:
            private.cleanup()
        except Exception as error:
            record_failure(proof, "private_cleanup", error)
        proof["timings_seconds"]["total"] = time.monotonic() - started
        try:
            proof["artifacts"] = artifact_hashes(out)
        except Exception as error:
            record_failure(proof, "artifact_inventory", error)
        dump(out / "result.json", proof)
    if proof["status"] == "passed":
        try:
            from tests.browser.real_node.evidence import validate_aggregate
            validate_aggregate(out / "result.json", release=release,
                               xpi_sha256=proof["inputs"]["xpi_sha256"], version=proof["inputs"]["version"],
                               commit=proof["inputs"]["source_commit"], source=None, require_candidate=False,
                               required_platforms=platforms)
        except Exception as error:
            record_failure(proof, "evidence_validation", error)
            dump(out / "result.json", proof)
    print(json.dumps({"status": proof["status"], "result": str(out / "result.json"),
                      "timings_seconds": proof["timings_seconds"], "error": proof["error"]}, indent=2))
    return 0 if proof["status"] == "passed" else 1


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("cache-key", help="print the immutable environment cache input identity")
    prewarm_parser = commands.add_parser("prepare-browsers", help="load or build cached browser tools only")
    prewarm_parser.add_argument("--platform", choices=("both", "desktop", "android"), default="both")
    prewarm_parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/portmap-real-node")
    prewarm_parser.add_argument("--artifacts", type=Path)
    execute_parser = commands.add_parser("run", help="test full real nodes with fresh isolated state")
    execute_parser.add_argument("--release")
    execute_parser.add_argument("--xpi", type=Path)
    execute_parser.add_argument("--runtime-archive", type=Path)
    execute_parser.add_argument("--runtime-manifest", type=Path)
    execute_parser.add_argument("--source-commit")
    execute_parser.add_argument("--platform", choices=("both", "desktop", "android"), default="both")
    execute_parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/portmap-real-node")
    execute_parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "cache-key":
            print(cache_key())
            return 0
        if args.command == "prepare-browsers":
            return prepare_browsers(args)
        candidate = (args.xpi, args.runtime_archive, args.runtime_manifest, args.source_commit)
        require((bool(args.release) and not any(candidate)) or (not args.release and all(candidate)),
                "Choose --release vX.Y.Z OR all candidate artifact/source arguments")
        return run(args)
    except (Exception, KeyboardInterrupt) as error:
        print("real-node gate: " + ("Interrupted" if isinstance(error, KeyboardInterrupt) else str(error)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
