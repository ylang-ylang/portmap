#!/usr/bin/env python3
"""Build a client-only self-contained archive, with pinned native dependencies.

Use packaging/client.Dockerfile on Linux for a glibc-2.31 build baseline. This
script never publishes artifacts or modifies the source release manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_NATIVE_ARCHIVE = 256 * 1024 * 1024


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def download(url: str, path: Path, *, sha256: str | None, limit: int) -> None:
    if not url.startswith("https://"):
        raise ValueError("build dependencies must use verified HTTPS URLs")
    if path.is_file() and (sha256 is None or digest(path) == sha256):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=path.parent)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "portmap-client-builder", "Accept-Encoding": "identity"})
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(request, timeout=60) as response:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"build dependency exceeds {limit} bytes")
                output.write(chunk)
        if sha256 is not None and digest(Path(temporary)) != sha256:
            raise ValueError(f"checksum mismatch for {url}")
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def native_dependencies(out: Path, target: str) -> Path:
    manifest = json.loads((ROOT / "packaging" / "client-natives.json").read_text())
    if target not in manifest:
        raise ValueError(f"no pinned native dependencies for {target}")
    native = out / "native" / target
    native.mkdir(parents=True, exist_ok=True)
    (native / "licenses").mkdir(exist_ok=True)
    for name, entry in manifest[target].items():
        source = out / "cache" / f"{name}-{entry['version']}-{target}.tar.gz"
        download(entry["url"], source, sha256=entry["sha256"], limit=MAX_NATIVE_ARCHIVE)
        with tarfile.open(source, "r:gz") as archive:
            candidates = [member for member in archive.getmembers() if member.isfile() and Path(member.name).name == name]
            if len(candidates) != 1 or candidates[0].size > 512 * 1024 * 1024:
                raise ValueError(f"native archive does not contain exactly one bounded {name} binary")
            stream = archive.extractfile(candidates[0])
            if stream is None:
                raise ValueError(f"cannot read {name} binary")
            with stream, (native / name).open("wb") as output:
                shutil.copyfileobj(stream, output, 1024 * 1024)
        (native / name).chmod(0o755)
        download(
            entry["license_url"],
            native / "licenses" / f"{name}-{entry['version']}.LICENSE",
            sha256=None,
            limit=1024 * 1024,
        )
    return native


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        raise ValueError(f"unsupported build architecture: {machine}")
    return f"{sys.platform}-{arch}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true", help="download and verify native build inputs only")
    parser.add_argument("--release-tag", help="release asset tag, defaults to V<major>.<minor>")
    args = parser.parse_args()
    out = args.out_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    target = platform_key()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("release version must be numeric major.minor.patch")
    native = native_dependencies(out, target)
    if args.prepare_only:
        print(json.dumps({"native_dir": str(native), "platform": target, "version": version}))
        return 0
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(out / "frozen"), "--workpath", str(out / "work"), str(ROOT / "packaging" / "client.spec")],
        env={**os.environ, "PORTMAP_CLIENT_NATIVE_DIR": str(native)},
        cwd=ROOT,
        check=True,
    )
    package = out / "frozen" / "portmap-client"
    executable = package / "portmap-client"
    if not executable.is_file():
        raise ValueError("freezer produced no client executable")
    filename = f"portmap-client-{version}-{target}.tar.gz"
    archive = out / filename
    # Dereference bundle-internal library symlinks, simplifying safe installer
    # extraction: published archives contain only regular files/directories.
    with tarfile.open(archive, "w:gz", dereference=True) as output:
        for member in sorted(package.iterdir()):
            output.add(member, arcname=member.name)
    os_name, arch = target.split("-", 1)
    tag = args.release_tag or "V" + ".".join(version.split(".")[:2])
    if not re.fullmatch(r"V[0-9]+\.[0-9]+", tag):
        raise ValueError("release tag must match repository V<major>.<minor> policy")
    record = {
        "os": os_name,
        "arch": arch,
        "filename": filename,
        "sha256": digest(archive),
        "size": archive.stat().st_size,
        "source_url": f"https://github.com/ylang-ylang/portmap/releases/download/{tag}/{filename}",
    }
    manifest = {"version": version, "assets": [record]}
    (out / "client_release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (out / (filename + ".sha256")).write_text(f"{record['sha256']}  {filename}\n", encoding="utf-8")
    print(json.dumps({"archive": str(archive), "manifest": str(out / "client_release.json"), **record}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
