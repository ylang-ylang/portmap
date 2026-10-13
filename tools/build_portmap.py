#!/usr/bin/env python3
"""Build the unified self-contained frozen portmap archive.

Freezes the Python CLI, host agent and the unified container catalog
entrypoint into one PyInstaller bundle. CoreDNS and Traefik are no longer
bundled: the gateway, DNS and catalog containers come from images supplied by
the host Docker/Podman runtime at setup time.

Use packaging/portmap.Dockerfile on Linux for a glibc-2.31 build baseline. This
script never publishes artifacts or modifies the source release manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        raise ValueError(f"unsupported build architecture: {machine}")
    return f"{sys.platform}-{arch}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--release-tag", help="release asset tag, defaults to V<major>.<minor>")
    args = parser.parse_args()
    out = args.out_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    target = platform_key()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("release version must be numeric major.minor.patch")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(out / "frozen"), "--workpath", str(out / "work"), str(ROOT / "packaging" / "portmap.spec")],
        cwd=ROOT,
        check=True,
    )
    package = out / "frozen" / "portmap"
    executable = package / "portmap"
    if not executable.is_file():
        raise ValueError("freezer produced no portmap executable")
    filename = f"portmap-{version}-{target}.tar.gz"
    archive = out / filename
    # Dereference bundle-internal library symlinks, simplifying safe installer
    # extraction: published archives contain only regular files/directories.
    with tarfile.open(archive, "w:gz", dereference=True) as output:
        for member in sorted(package.iterdir()):
            if member.name == "release.json":
                continue
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
    manifest_document = json.dumps(manifest, indent=2) + "\n"
    (out / "release.json").write_text(manifest_document, encoding="utf-8")
    # The archive above was already hashed, so it deliberately contains no
    # manifest (embedding one would change its own checksum). Write the real
    # manifest next to the unpacked executable instead: the frozen catalog
    # resolves this sidecar first, letting the freshly built bundle serve its
    # own release for smoke tests without any network bootstrap.
    bundle_manifest = package / "release.json"
    bundle_manifest.write_text(manifest_document, encoding="utf-8")
    (out / (filename + ".sha256")).write_text(f"{record['sha256']}  {filename}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "archive": str(archive),
                "manifest": str(out / "release.json"),
                "bundle_manifest": str(bundle_manifest),
                **record,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
