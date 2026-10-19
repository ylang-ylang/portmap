#!/usr/bin/env python3
"""Build the unified self-contained frozen portmap archive.

Freezes the Python CLI, host agent and the unified container catalog
entrypoint into one PyInstaller bundle. CoreDNS and Traefik are no longer
bundled: the gateway, DNS and catalog containers come from images supplied by
the host Docker/Podman runtime at setup time.

Use packaging/portmap.Dockerfile on Linux for a glibc-2.31 build baseline. This
script never publishes artifacts; the generated manifest is a build output
only and is never committed to the source tree.

Release identity: pass the paired ``--release-tag v<major>.<minor>.<patch>``
and ``--source-commit <40-hex>`` flags for a release build. The builder then
verifies the tag matches the exact version and that the repository HEAD is
exactly the requested source commit, and records the
identity as a top-level manifest ``source`` block. Both flags may be omitted
for a local candidate build, which emits no ``source`` block.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REPOSITORY = "ylang-ylang/portmap"
TAG_RE = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")


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


def resolve_head() -> str:
    """Read source HEAD as its owner; the container uses the host runner UID."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "rev-parse",
            "HEAD",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot resolve repository HEAD: {result.stderr.strip()}")
    return result.stdout.strip()


def resolve_release_identity(
    version: str, release_tag: str | None, source_commit: str | None
) -> tuple[str, dict | None]:
    """Return (asset tag, manifest source block) for the build.

    ``--release-tag`` and ``--source-commit`` are paired: a release build
    proves the tag matches the exact version and that HEAD is the exact
    source commit being released, then records the
    identity in the generated manifest. A local candidate build omits both
    flags and records nothing.
    """
    match = re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
    )
    if match is None:
        raise ValueError("release version must be numeric major.minor.patch")
    default_tag = f"v{version}"
    if (release_tag is None) != (source_commit is None):
        raise ValueError(
            "--release-tag and --source-commit are paired; pass both for a "
            "release build or neither for a local candidate"
        )
    if release_tag is None or source_commit is None:
        return default_tag, None
    tag_match = TAG_RE.fullmatch(release_tag)
    if tag_match is None:
        raise ValueError("release tag must match repository v<major>.<minor>.<patch> policy")
    if release_tag != default_tag:
        raise ValueError(f"release tag {release_tag} does not match version {version}")
    if tuple(map(int, version.split("."))) <= (0, 19, 0):
        raise ValueError("new release versions must be greater than historical 0.19.0")
    if COMMIT_RE.fullmatch(source_commit) is None:
        raise ValueError("--source-commit must be a full 40-hex commit id")
    head = resolve_head()
    if head != source_commit:
        raise ValueError(
            f"HEAD {head} is not the requested release source {source_commit}"
        )
    return release_tag, {
        "repository": SOURCE_REPOSITORY,
        "tag": release_tag,
        "commit": source_commit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--release-tag",
        help="release asset tag v<major>.<minor>.<patch>; pairs with --source-commit",
    )
    parser.add_argument(
        "--source-commit",
        help="full 40-hex source commit being released; pairs with --release-tag",
    )
    args = parser.parse_args()
    out = args.out_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    target = platform_key()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    tag, source = resolve_release_identity(version, args.release_tag, args.source_commit)
    with tempfile.TemporaryDirectory(prefix="portmap-static-", dir=out) as temporary:
        static_root = Path(temporary)
        shutil.copytree(ROOT / "src/portmap/catalog_static", static_root, dirs_exist_ok=True)
        skill_asset = static_root / "skills/project-integration/SKILL.md"
        skill_asset.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "skills/project-integration/SKILL.md", skill_asset)
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(out / "frozen"), "--workpath", str(out / "work"), str(ROOT / "packaging" / "portmap.spec")],
            cwd=ROOT,
            env={**os.environ, "PORTMAP_BUILD_STATIC_ROOT": str(static_root)},
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
    record = {
        "os": os_name,
        "arch": arch,
        "filename": filename,
        "sha256": digest(archive),
        "size": archive.stat().st_size,
        "source_url": f"https://github.com/{SOURCE_REPOSITORY}/releases/download/{tag}/{filename}",
    }
    manifest: dict = {"version": version}
    if source is not None:
        manifest["source"] = source
    manifest["assets"] = [record]
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
                **({"source": source} if source else {}),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
