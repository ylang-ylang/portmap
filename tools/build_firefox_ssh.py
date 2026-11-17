#!/usr/bin/env python3
"""Fetch exact SSH sources, then rebuild packaged JS/WASM under Emscripten 3.1.74.

Separate fetch/build permits a network-disabled compiler container. Never signs or
publishes, never overwrites shipped assets, and never runs dependency tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SSH = ROOT / "extension/firefox/ssh"
SOURCES = (
    ("libssh2", "https://github.com/libssh2/libssh2.git", "a312b43325e3383c865a87bb1d26cb52e3292641"),
    ("mbedtls", "https://github.com/Mbed-TLS/mbedtls.git", "e185d7fd85499c8ce5ca2a54f5cf8fe7dbe3f8df"),
    ("mbedtls/framework", "https://github.com/Mbed-TLS/mbedtls-framework.git", "457996474728cb8e968ed21953b72f74d2f536b2"),
)
FLAGS = "-O2 -DMBEDTLS_NO_PLATFORM_ENTROPY -DMBEDTLS_ENTROPY_HARDWARE_ALT -DLIBSSH2_BROWSER_CALLBACK_IO"


def run(*args, cwd=None, capture=False):
    return subprocess.run([str(arg) for arg in args], cwd=cwd, check=True,
                          text=True, stdout=subprocess.PIPE if capture else None).stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(work):
    work.mkdir(parents=True, exist_ok=False)
    for name, url, commit in SOURCES:
        target = work / "source" / name
        target.mkdir(parents=True, exist_ok=True)
        run("git", "init", "--quiet", target)
        run("git", "-C", target, "fetch", "--depth=1", url, commit)
        run("git", "-C", target, "checkout", "--quiet", "--detach", "FETCH_HEAD")
        if run("git", "-C", target, "rev-parse", "HEAD", capture=True).strip() != commit:
            raise ValueError(f"source commit mismatch: {name}")
    run("git", "-C", work / "source/libssh2", "apply", "--whitespace=error",
        SSH / "libssh2-browser-callback.patch")
    provenance = json.loads((SSH / "PROVENANCE.json").read_text())
    for name, expected in provenance["patchedSources"].items():
        if digest(work / "source" / name) != expected:
            raise ValueError(f"patched source mismatch: {name}")


def build(work, check):
    compiler = run("emcc", "--version", capture=True)
    if not re.search(r"\b3\.1\.74\b", compiler.splitlines()[0]):
        raise ValueError("Require exactly Emscripten 3.1.74")
    provenance = json.loads((SSH / "PROVENANCE.json").read_text())
    for name in ("ssh/bridge.c", "ssh/libssh2-browser-callback.patch"):
        if digest(SSH.parent / name) != provenance["integrity"][name]:
            raise ValueError(f"build input differs from reviewed provenance: {name}")
    # Verify source identity and the complete tracked diff before compilation.
    for name, _, commit in SOURCES:
        source = work / "source" / name
        if run("git", "-C", source, "rev-parse", "HEAD", capture=True).strip() != commit:
            raise ValueError(f"source commit mismatch: {name}")
        changed = run("git", "-C", source, "diff", "--name-only", "HEAD", capture=True).splitlines()
        allowed = ["src/misc.c", "src/session.c"] if name == "libssh2" else []
        if sorted(changed) != allowed:
            raise ValueError(f"unexpected source modification: {name}: {changed}")
    for name, expected in provenance["patchedSources"].items():
        if digest(work / "source" / name) != expected:
            raise ValueError(f"patched source mismatch: {name}")
    output = work / "generated"
    output.mkdir(exist_ok=False)
    prefix = work / "build/prefix"
    common = ["-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_C_FLAGS={FLAGS}",
              f"-DCMAKE_INSTALL_PREFIX={prefix}", "-DCMAKE_INSTALL_LIBDIR=lib"]
    run("emcmake", "cmake", "-S", work / "source/mbedtls", "-B", work / "build/mbedtls",
        *common, "-DENABLE_TESTING=OFF", "-DENABLE_PROGRAMS=OFF", "-DMBEDTLS_FATAL_WARNINGS=OFF")
    run("cmake", "--build", work / "build/mbedtls", "--target", "install", "--parallel", "2")
    run("emcmake", "cmake", "-S", work / "source/libssh2", "-B", work / "build/libssh2",
        *common, "-DBUILD_STATIC_LIBS=ON", "-DBUILD_SHARED_LIBS=OFF", "-DBUILD_TESTING=OFF",
        "-DBUILD_EXAMPLES=OFF", "-DCRYPTO_BACKEND=mbedTLS", "-DENABLE_ZLIB_COMPRESSION=OFF",
        "-DENABLE_DEBUG_LOGGING=OFF", f"-DMBEDTLS_INCLUDE_DIR={prefix}/include",
        f"-DMBEDCRYPTO_LIBRARY={prefix}/lib/libmbedcrypto.a")
    run("cmake", "--build", work / "build/libssh2", "--target", "install", "--parallel", "2")
    run("emcc", SSH / "bridge.c", "-O2", f"-I{prefix}/include", prefix / "lib/libssh2.a",
        prefix / "lib/libmbedcrypto.a", "-sMODULARIZE=1", "-sEXPORT_NAME=NightlySshWasm",
        "-sENVIRONMENT=web", "-sDYNAMIC_EXECUTION=0", "-sALLOW_MEMORY_GROWTH=1",
        "-sMAXIMUM_MEMORY=134217728", "-sNO_EXIT_RUNTIME=1", "--no-entry", "-sFILESYSTEM=0",
        '-sEXPORTED_FUNCTIONS=["_malloc","_free"]', "-o", output / "libssh2.js")
    results = {}
    for name in ("libssh2.js", "libssh2.wasm"):
        results[name] = digest(output / name)
        if check and (output / name).read_bytes() != (SSH / name).read_bytes():
            raise ValueError(f"rebuilt payload differs from shipped bytes: {name}; output retained at {output}")
    return {"output": str(output), "sha256": results, "matches_shipped": True if check else None,
            "compiler": compiler.splitlines()[0], "publication_allowed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "build"))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="require exact shipped JS/WASM bytes")
    args = parser.parse_args()
    work = args.work_dir.resolve()
    os.environ.setdefault("SOURCE_DATE_EPOCH", "0")
    os.environ["LC_ALL"] = "C"
    os.environ["TZ"] = "UTC"
    try:
        if args.command == "fetch":
            fetch(work)
            result = {"source_directory": str(work / "source"), "patched": True}
        else:
            result = build(work, args.check)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Firefox SSH build: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
