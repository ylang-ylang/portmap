#!/usr/bin/env python3
"""Package Firefox development builds and attach verified signed release assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ID = "firefox@portmap.ylang-ylang.github.io"
UPDATE_URL = "https://github.com/ylang-ylang/portmap/releases/latest/download/firefox-updates.json"
RUNTIME_FILES = (
    "manifest.json", "core.js", "controller.js", "background.js",
    "options.html", "options.js", "options.css", "icon.svg",
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
    if gecko.get("id") != EXTENSION_ID or gecko.get("update_url") != UPDATE_URL:
        raise ValueError("extension ID or self-distribution update URL does not match release contract")
    if "gecko_android" not in settings:
        raise ValueError("extension must explicitly support Firefox Android")
    return manifest, files


def build(root: Path, output: Path) -> dict:
    manifest, files = runtime_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids silently replacing an artifact someone is testing.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return {"artifact": str(output), "version": manifest["version"], "sha256": sha256(output), "signed": False}


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


def check_signed_payload(root: Path, signed_xpi: Path) -> dict:
    """Bind source payload; only manifest JSON formatting may differ after signing."""
    manifest, expected = runtime_files(root)
    if signed_xpi.is_symlink() or not signed_xpi.is_file():
        raise ValueError("signed XPI must be a regular file")
    with zipfile.ZipFile(signed_xpi) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("XPI contains duplicate entries")
        payload = {name for name in names if not name.startswith("META-INF/") and not name.endswith("/")}
        if payload != set(expected):
            raise ValueError("signed XPI runtime members differ from the source allowlist")
        for name, content in expected.items():
            actual = archive.read(name)
            matches = canonical_manifest(actual) == canonical_manifest(content) if name == "manifest.json" else actual == content
            if not matches:
                raise ValueError(f"signed XPI differs from source: {name}")
        if not any(name in names for name in ("META-INF/mozilla.rsa", "META-INF/cose.sig")):
            raise ValueError("XPI has no Mozilla signature envelope; development packages are not publishable")
    return manifest


def check_evidence(path: Path, *, platform: str, digest: str, version: str, commit: str) -> None:
    proof = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "status": "passed", "platform": platform, "extension_sha256": digest,
        "extension_version": version, "source_commit": commit, "signature_required": True,
    }
    for key, value in required.items():
        if proof.get(key) != value:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    development = commands.add_parser("build", help="create an explicitly unsigned development XPI")
    development.add_argument("--output", type=Path, required=True)
    release = commands.add_parser("attach", help="attach signed, source-matched, browser-verified release assets")
    release.add_argument("--signed-xpi", type=Path, required=True)
    release.add_argument("--assets-dir", type=Path, required=True)
    release.add_argument("--tag", required=True)
    release.add_argument("--commit", required=True)
    release.add_argument("--desktop-evidence", type=Path, required=True)
    release.add_argument("--android-evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build(args.repo_root, args.output)
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
