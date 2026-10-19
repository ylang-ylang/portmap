#!/usr/bin/env python3
"""One-command SSH deployment of the current portmap source snapshot.

python3 tools/deploy_portmap.py SSH_TARGET --tap-source PATH

Builds the real frozen Linux archive with the existing pinned builder
(packaging/portmap.Dockerfile + tools/build_portmap.py), stages a checksummed
pristine source snapshot together with the built release manifest, the
feature tap's exact Brew helpers and Formula template, streams the stage to
the target through safely quoted SSH commands, and applies it remotely via
tools/deploy_apply.py. Source installs resolve the official published
manifest for their exact version; the staged manifest is seeded into the
target's portmap download cache so the deployed snapshot serves its own
verified archive. Re-running with unchanged inputs reuses the staged build
and only re-verifies the remote installation. The controller owns only its
labeled build artifacts, its tagged builder image and its stage directory;
nothing is pruned and no foreign data is touched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAP_FORMULA = "Formula/portmap.rb"
APPLY_SCRIPT = "tools/deploy_apply.py"
BREW_HELPERS = ("portmap_brew_setup.py", "portmap_brew_postinstall.py")
SOURCE_ROOTS = ("src", "packaging", "tools")
SOURCE_FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "portmap.toml")
EXCLUDED_DIRS = {
    ".git", ".agents", ".codex", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "__pycache__", ".venv", "node_modules", "dist", "test_repo",
    ".portmap-state", ".portmap-agent-runtime",
}
EXCLUDED_NAMES = {".env"}
ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class DeployError(Exception):
    pass


def note(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _included(relative: Path) -> bool:
    parts = relative.parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return False
    return parts[-1] not in EXCLUDED_NAMES and not parts[-1].endswith(".pyc")


def source_records(root: Path) -> list[dict]:
    """Inventory only build/package inputs, never arbitrary worktree files."""
    records = []
    paths = [root / name for name in SOURCE_FILES if (root / name).is_file()]
    for name in SOURCE_ROOTS:
        paths.extend((root / name).rglob("*"))
    for path in sorted(paths):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = path.relative_to(root)
        if not _included(relative):
            continue
        if path.is_symlink():
            records.append({"path": relative.as_posix(), "symlink": os.readlink(path)})
        else:
            records.append(
                {
                    "path": relative.as_posix(),
                    "executable": bool(path.stat().st_mode & 0o111),
                    "sha256": digest(path),
                }
            )
    return records


def builder_platform() -> str:
    machine = platform.machine().lower()
    arch = ARCHITECTURES.get(machine)
    if arch is None:
        raise DeployError(f"unsupported controller architecture: {machine}")
    return f"linux-{arch}"


def snapshot_identity(
    version: str, platform_key: str, records: list[dict], helpers: dict[str, str], template_sha256: str
) -> str:
    document = json.dumps(
        {"version": version, "platform": platform_key, "records": records, "helpers": helpers, "formula": template_sha256},
        sort_keys=True,
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def engine_command(choice: str) -> str:
    failures = []
    for candidate in (("docker", "podman") if choice == "auto" else (choice,)):
        if shutil.which(candidate) is None:
            failures.append(f"{candidate}: executable unavailable")
            continue
        try:
            probe = subprocess.run([candidate, "info"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{candidate}: {exc}")
            continue
        if probe.returncode == 0:
            return candidate
        failures.append(f"{candidate}: {probe.stderr.strip()}")
    raise DeployError("no usable container engine for the pinned builder: " + "; ".join(failures))


def build_frozen_archive(engine: str, source: Path, out_dir: Path) -> None:
    """Run the existing pinned builder over the source; own only its outputs."""
    dockerfile = source / "packaging" / "portmap.Dockerfile"
    if not dockerfile.is_file():
        raise DeployError(f"missing pinned builder Dockerfile: {dockerfile}")
    tag = f"portmap-deploy-builder:{digest(dockerfile)[:12]}"
    probe = subprocess.run([engine, "image", "inspect", tag], capture_output=True)
    if probe.returncode != 0:
        note(f"building pinned builder image {tag}")
        subprocess.run([engine, "build", "-t", tag, "-f", str(dockerfile), str(dockerfile.parent)], check=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            engine, "run", "--rm",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp",
            "-v", f"{source}:/src:ro",
            "-v", f"{out_dir}:/out",
            tag, "--out-dir", "/out",
        ],
        check=True,
    )


def create_source_tar(source: Path, records: list[dict], version: str, destination: Path) -> None:
    """Pack the pristine snapshot source tree.

    No generated manifest is committed to the source: the repository carries
    no packaged release.json, and the installed source resolves the official
    published manifest (or the seeded cache entry) for its exact version.
    """

    def deterministic(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        return info

    top = f"portmap-{version}"
    with tarfile.open(destination, "w:gz") as tar:
        for record in records:
            relative = record["path"]
            tar.add(source / relative, arcname=f"{top}/{relative}", recursive=False, filter=deterministic)


def stage_is_valid(stage: Path) -> bool:
    if not snapshot_path.is_file():
        return False
    try:
        snapshot = json.loads(snapshot_path.read_text())
        for name, expected in snapshot["files"].items():
            path = stage / name
            if not path.is_file() or digest(path) != expected:
                return False
        return snapshot["files"].get(f"archive/{snapshot['asset']['filename']}") == snapshot["asset"]["sha256"]
    except (OSError, KeyError, TypeError, ValueError):
        return False


def restage(
    engine: str,
    source: Path,
    tap_source: Path,
    stage_root: Path,
    stage: Path,
    records: list[dict],
    version: str,
    identity: str,
    platform_key: str,
) -> dict:
    """Build the frozen archive and populate a fresh stage directory."""
    apply_script = source / APPLY_SCRIPT
    if not apply_script.is_file():
        raise DeployError(f"missing {apply_script}")
    stage_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=stage_root, prefix=".stage-") as temporary:
        work = Path(temporary)
        build_dir = work / "build"
        build_dir.mkdir()
        note(f"building frozen archive for {platform_key} with the pinned builder")
        build_frozen_archive(engine, source, build_dir)
        manifest_path = build_dir / "release.json"
        manifest = json.loads(manifest_path.read_text())
        asset = manifest["assets"][0]
        if (
            manifest["version"] != version
            or len(manifest["assets"]) != 1
            or asset["filename"] != f"portmap-{version}-{platform_key}.tar.gz"
        ):
            raise DeployError(f"builder produced an unexpected manifest: {manifest}")
        if digest(build_dir / asset["filename"]) != asset["sha256"]:
            raise DeployError("builder archive checksum does not match its manifest")
        manifest_document = manifest_path.read_bytes()
        for name in ("archive", "helpers", "formula", "source"):
            (work / name).mkdir()
        shutil.move(str(build_dir / asset["filename"]), str(work / "archive" / asset["filename"]))
        shutil.copy2(apply_script, work / "deploy_apply.py")
        for name in BREW_HELPERS:
            shutil.copy2(tap_source / "libexec" / name, work / "helpers" / name)
        shutil.copy2(tap_source / TAP_FORMULA, work / "formula" / "template.rb")
        source_tar = work / "source" / f"portmap-{version}-src-{identity[:12]}.tar.gz"
        create_source_tar(source, records, version, source_tar)
        (work / "release.json").write_bytes(manifest_document)
        shutil.rmtree(build_dir)
        files = {}
        for path in sorted(work.rglob("*")):
            if path.is_file():
                files[path.relative_to(work).as_posix()] = digest(path)
        snapshot = {
            "snapshot_id": identity,
            "version": version,
            "platform": platform_key,
            "asset": asset,
            "files": files,
        }
        (work / "snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        if stage.exists():
            shutil.rmtree(stage)
        work.rename(stage)
        return snapshot


def run_ssh(target: str, argv: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    """Run one remote command; every argument is shell-quoted, the target is
    caller-provided and never hardcoded."""
    command = " ".join(shlex.quote(part) for part in argv)
    kwargs = {"capture_output": True, "text": True} if capture else {}
    ssh = ["ssh"]
    if not capture and sys.stdin.isatty():
        ssh.append("-t")
    completed = subprocess.run([*ssh, target, command], **kwargs)
    if capture and completed.returncode != 0:
        raise DeployError(f"ssh {target} {argv[0]} failed: {(completed.stderr or completed.stdout).strip()}")
    return completed


def remote_layout(target: str, remote_root: str, identity: str) -> dict:
    home = run_ssh(target, ["printenv", "HOME"], capture=True).stdout.strip()
    if not home.startswith("/"):
        raise DeployError(f"could not resolve the home directory on {target}")
    root = remote_root if not remote_root.startswith("~") else home + remote_root[1:]
    system = run_ssh(target, ["uname", "-sm"], capture=True).stdout.split()
    if len(system) != 2 or system[0].lower() != "linux" or system[1].lower() not in ARCHITECTURES:
        raise DeployError(f"unsupported target platform: {system!r}")
    return {
        "root": root,
        "stage": f"{root}/stage-{identity[:12]}",
        "state": f"{root}/state",
        "platform": f"linux-{ARCHITECTURES[system[1].lower()]}",
    }


def transfer_stage(target: str, stage: Path, remote_stage: str) -> None:
    run_ssh(target, ["mkdir", "-p", remote_stage], capture=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as bundle:
        with tarfile.open(bundle.name, "w:gz") as tar:
            for path in sorted(p for p in stage.rglob("*") if p.is_file()):
                tar.add(path, arcname=path.relative_to(stage).as_posix())
        bundle.flush()
        with open(bundle.name, "rb") as handle:
            completed = subprocess.run(
                ["ssh", target, f"tar -xzf - -C {shlex.quote(remote_stage)}"], stdin=handle
            )
    if completed.returncode != 0:
        raise DeployError(f"stage transfer to {target}:{remote_stage} failed with exit {completed.returncode}")


def _require_stage_root_outside_snapshot(source: Path, stage_root: Path) -> None:
    try:
        relative = stage_root.relative_to(source)
    except ValueError:
        return
    if relative.parts and relative.parts[0] not in EXCLUDED_DIRS:
        raise DeployError(
            f"--stage-root must live outside the source tree or inside an excluded directory "
            f"(e.g. {source / 'dist'}), otherwise stages would invalidate their own snapshot"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="SSH target, passed to ssh as one argument (e.g. user@host)")
    parser.add_argument("--tap-source", type=Path, required=True,
                        help="feature tap checkout providing Formula/portmap.rb and libexec/ helpers")
    parser.add_argument("--source", type=Path, default=ROOT, help="portmap source worktree (default: this checkout)")
    parser.add_argument("--stage-root", type=Path, default=None,
                        help="local stage/cache root (default: <source>/dist/deploy)")
    parser.add_argument("--remote-root", default="~/.portmap-deploy", help="deployment root on the target")
    parser.add_argument("--engine", choices=("auto", "docker", "podman"), default="auto",
                        help="container engine used for the pinned builder")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        source = args.source.resolve()
        tap_source = args.tap_source.resolve()
        stage_root = args.stage_root.resolve() if args.stage_root else source / "dist" / "deploy"
        _require_stage_root_outside_snapshot(source, stage_root)
        version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
        if not (source / APPLY_SCRIPT).is_file():
            raise DeployError(f"missing {APPLY_SCRIPT} in {source}")
        helpers = {}
        for name in BREW_HELPERS:
            helper = tap_source / "libexec" / name
            if not helper.is_file():
                raise DeployError(f"missing Brew helper: {helper}")
            helpers[name] = digest(helper)
        template = tap_source / TAP_FORMULA
        if not template.is_file():
            raise DeployError(f"missing formula template: {template}")
        platform_key = builder_platform()
        records = source_records(source)
        identity = snapshot_identity(version, platform_key, records, helpers, digest(template))
        remote = remote_layout(args.target, args.remote_root, identity)
        if remote["platform"] != platform_key:
            raise DeployError(
                f"target {args.target} is {remote['platform']}; this snapshot targets {platform_key}"
            )
        stage = stage_root / identity[:16]
        if stage_is_valid(stage):
            snapshot = json.loads((stage / "snapshot.json").read_text())
            note(f"reusing staged snapshot {identity[:12]} (version {version}, {platform_key})")
        else:
            snapshot = restage(engine_command(args.engine), source, tap_source, stage_root, stage,
                               records, version, identity, platform_key)
            note(f"staged snapshot {identity[:12]} (version {version}, {platform_key})")
        note(f"transferring stage to {args.target}:{remote['stage']}")
        transfer_stage(args.target, stage, remote["stage"])
        note(f"applying snapshot on {args.target}")
        completed = run_ssh(
            args.target,
            [
                "python3",
                f"{remote['stage']}/deploy_apply.py",
                "--stage-dir", remote["stage"],
                "--state-dir", remote["state"],
            ],
        )
        if completed.returncode != 0:
            raise DeployError(f"remote apply failed with exit {completed.returncode}")
        note(f"deployed portmap {snapshot['version']} ({platform_key}) on {args.target}")
        return 0
    except (DeployError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"deploy failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
