#!/usr/bin/env python3
"""Apply a staged portmap deployment snapshot on the SSH target.

Invoked by tools/deploy_portmap.py on the deployment target as the ordinary
user. Verifies every staged artifact checksum, unpacks the source snapshot,
creates or updates only the tool-owned portmap-local/snapshot tap (substituting
source URL/version/SHA and the two helper resource URL/SHA values in the
staged Formula template), installs through the existing Homebrew, preseeds
the portmap download cache with the verified archive, invokes the installed
Brew setup helper outside any Homebrew sandbox via the installed libexec
Python -- the installed ``portmap setup`` itself verifies and, when needed,
repairs the container runtime before creating any portmap container -- and
finally checks the real ``portmap status --check`` plus
actual system DNS resolution and HTTP catalog access. Foreign formulas, taps,
containers and networks are never modified and nothing is pruned. Re-applying
an identical snapshot skips rebuild/reinstall and only restores readiness.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

TAP_NAME = "portmap-local/snapshot"
TAP_MARKER = "portmap_deploy.json"
HELPERS = ("portmap_brew_setup.py", "portmap_brew_postinstall.py")
ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class DeployError(Exception):
    pass


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    with os.fdopen(handle, "w", encoding="utf-8") as document:
        document.write(text)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- staged artifact verification -------------------------------------------------


def verify_stage(stage_dir: Path) -> dict:
    """Fail honestly unless every staged file matches the snapshot checksums."""
    snapshot = load_json(stage_dir / "snapshot.json")
    if not isinstance(snapshot, dict):
        raise DeployError(f"stage snapshot metadata is missing or invalid: {stage_dir}")
    missing = {"snapshot_id", "version", "platform", "asset", "files"} - snapshot.keys()
    if missing:
        raise DeployError(f"stage snapshot metadata lacks {sorted(missing)}")
    if not isinstance(snapshot["files"], dict) or not isinstance(snapshot["asset"], dict):
        raise DeployError("stage snapshot metadata has an invalid files or asset section")
    for name, expected in snapshot["files"].items():
        path = stage_dir / name
        if not path.is_file():
            raise DeployError(f"staged file is missing: {name}")
        actual = digest(path)
        if actual != expected:
            raise DeployError(f"staged file checksum mismatch for {name}: expected {expected}, got {actual}")
    asset = snapshot["asset"]
    if snapshot["files"].get(f"archive/{asset['filename']}") != asset["sha256"]:
        raise DeployError("staged archive does not match the staged release manifest")
    return snapshot


def check_platform(snapshot: dict) -> None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = ARCHITECTURES.get(machine)
    if arch is None:
        raise DeployError(f"unsupported target architecture: {machine}")
    asset = snapshot["asset"]
    if asset["os"] != system or asset["arch"] != arch or snapshot["platform"] != f"{system}-{arch}":
        raise DeployError(
            f"staged build targets {snapshot['platform']} ({asset['os']}/{asset['arch']}), "
            f"this target is {system}-{arch}"
        )


def unpack_source(stage_dir: Path, snapshot: dict) -> Path:
    """Unpack the staged source snapshot; reuse an identical unpacked copy."""
    tar_names = [name for name in snapshot["files"] if name.startswith("source/")]
    if len(tar_names) != 1:
        raise DeployError(f"stage must carry exactly one source snapshot, found {tar_names}")
    tar_name = tar_names[0]
    expected = snapshot["files"][tar_name]
    source_dir = stage_dir / "source-tree"
    marker = stage_dir / "source-tree.sha256"
    if source_dir.is_dir() and marker.is_file() and marker.read_text().strip() == expected:
        return source_dir
    if source_dir.exists():
        shutil.rmtree(source_dir)
    unpacking = stage_dir / "source-tree.tmp"
    if unpacking.exists():
        shutil.rmtree(unpacking)
    unpacking.mkdir(parents=True)
    with tarfile.open(stage_dir / tar_name, "r:gz") as tar:
        members = tar.getmembers()
        tops = {member.name.split("/", 1)[0] for member in members}
        for member in members:
            if member.name.startswith("/") or ".." in member.name.split("/"):
                raise DeployError(f"unsafe source snapshot member: {member.name}")
        if len(tops) != 1 or any("/" not in member.name and not member.isdir() for member in members):
            raise DeployError("source snapshot tar layout is unexpected")
        try:
            tar.extractall(unpacking, filter="data")
        except TypeError:  # Python < 3.12 has no extraction filters
            tar.extractall(unpacking)
    roots = list(unpacking.iterdir())
    if len(roots) == 1 and roots[0].is_dir():
        roots[0].rename(source_dir)
    else:
        unpacking.rename(source_dir)
    shutil.rmtree(unpacking, ignore_errors=True)
    marker.write_text(expected + "\n", encoding="utf-8")
    return source_dir


# --- Brew formula templating ------------------------------------------------------


def file_url(path: Path) -> str:
    return "file://" + urllib.parse.quote(str(path))


def generate_formula(
    template: str,
    *,
    version: str,
    source_url: str,
    source_sha256: str,
    brew_setup_url: str,
    brew_setup_sha256: str,
    brew_postinstall_url: str,
    brew_postinstall_sha256: str,
) -> str:
    """Substitute ONLY the pinned source URL/version/SHA and the two helper
    resource URL/SHA fields of the staged Formula template."""
    urls = [source_url, brew_setup_url, brew_postinstall_url]
    sha256s = [source_sha256, brew_setup_sha256, brew_postinstall_sha256]
    fields = (
        (re.compile(r'^(\s*)url\s+"[^"]*"\s*$'), "url", urls),
        (re.compile(r'^(\s*)version\s+"[^"]*"\s*$'), "version", [version]),
        (re.compile(r'^(\s*)sha256\s+"[^"]*"\s*$'), "sha256", sha256s),
    )
    lines = []
    seen = {"url": 0, "version": 0, "sha256": 0}
    for line in template.splitlines():
        for pattern, kind, values in fields:
            match = pattern.match(line)
            if not match:
                continue
            index = seen[kind]
            if index >= len(values):
                raise DeployError(f"formula template carries more {kind} fields than substitutable values")
            seen[kind] += 1
            lines.append(f'{match.group(1)}{kind} "{values[index]}"')
            break
        else:
            lines.append(line)
    if seen != {"url": 3, "version": 1, "sha256": 3}:
        raise DeployError(
            f"formula template drift: expected 3 url, 1 version and 3 sha256 fields, found {seen}"
        )
    return "\n".join(lines) + ("\n" if template.endswith("\n") else "")


def tap_directory(brew_repository: Path) -> Path:
    return brew_repository / "Library" / "Taps" / "portmap-local" / "homebrew-snapshot"


def ensure_tap(tap_dir: Path, formula_text: str, formula_sha256: str, snapshot_id: str) -> str:
    """Create or update ONLY the tool-owned tap; refuse foreign content."""
    formula_path = tap_dir / "Formula" / "portmap.rb"
    marker_path = tap_dir / TAP_MARKER
    if tap_dir.is_dir() and any(tap_dir.iterdir()) and not marker_path.is_file():
        raise DeployError(f"refusing to alter an existing foreign tap directory: {tap_dir}")
    if not formula_path.is_file():
        action = "created"
    elif formula_path.read_text(encoding="utf-8") == formula_text:
        action = "unchanged"
    else:
        action = "updated"
    if action != "unchanged":
        atomic_write(formula_path, formula_text)
    atomic_write(
        marker_path,
        json.dumps({"tap": TAP_NAME, "snapshot_id": snapshot_id, "formula_sha256": formula_sha256}, indent=2) + "\n",
    )
    return action


# --- Brew installation ------------------------------------------------------------


def brew_environment() -> dict:
    # Process-scoped flags for our invocations only: no global Homebrew
    # configuration is written, nothing self-updates and no foreign package
    # or cache is ever cleaned up.
    environment = dict(os.environ)
    environment["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    environment["HOMEBREW_NO_INSTALL_CLEANUP"] = "1"
    return environment


def find_brew(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
        raise DeployError(f"brew not found at {path}")
    for candidate in ("/home/linuxbrew/.linuxbrew/bin/brew", "/opt/homebrew/bin/brew"):
        if Path(candidate).is_file():
            return Path(candidate)
    found = shutil.which("brew")
    if found:
        return Path(found)
    raise DeployError("no Homebrew installation found on the target")


def brew(brew_path: Path, *arguments: str, capture: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        [str(brew_path), *arguments],
        capture_output=capture,
        text=True,
        env=brew_environment(),
    )
    if capture and completed.returncode != 0:
        raise DeployError(f"brew {' '.join(arguments[:2])} failed: {(completed.stderr or completed.stdout).strip()}")
    return completed


def installed_version(brew_path: Path) -> str | None:
    result = subprocess.run(
        [str(brew_path), "list", "--versions", "portmap"],
        capture_output=True, text=True, env=brew_environment(),
    )
    if result.returncode and (result.stdout.strip() or result.stderr.strip()):
        raise DeployError(f"cannot inspect installed portmap: {result.stderr.strip() or result.stdout.strip()}")
    output = result.stdout.strip()
    if not output:
        return None
    fields = output.split()
    return fields[-1] if len(fields) >= 2 else None


def version_tuple(value: str) -> tuple:
    return tuple(int(part) for part in value.split("."))


def decide_install_action(installed: str | None, snapshot_version: str, applied, formula_sha256: str, snapshot_id: str) -> str:
    """Install a fresh package; reinstall existing packages transactionally.

    Brew's reinstall preserves the old keg for rollback and permits switching
    from a published tap to our snapshot tap; install rejects that transition.
    Identical applied snapshots are skipped and downgrades are refused.
    """
    if installed is None:
        return "install"
    marker_matches = (
        isinstance(applied, dict)
        and applied.get("snapshot_id") == snapshot_id
        and applied.get("formula_sha256") == formula_sha256
    )
    if installed == snapshot_version:
        return "skip" if marker_matches else "reinstall"
    if version_tuple(installed) < version_tuple(snapshot_version):
        return "reinstall"
    return "refuse"


def verify_installed_cli(formula_prefix: Path, version: str) -> Path:
    cli = formula_prefix / "bin" / "portmap"
    if not cli.is_file():
        raise DeployError(f"installed formula is missing its entrypoint: {cli}")
    completed = subprocess.run([str(cli), "--version"], capture_output=True, text=True)
    if completed.returncode != 0 or completed.stdout.strip() != f"portmap {version}":
        raise DeployError(
            f"installed portmap reports {completed.stdout.strip()!r}, expected portmap {version}"
        )
    return cli


# --- download preseed --------------------------------------------------------------


def download_cache_dir_for(portmap_state_dir: Path, environment: dict) -> Path:
    explicit = environment.get("PORTMAP_DOWNLOAD_DIR")
    return Path(explicit).expanduser() if explicit else portmap_state_dir / "downloads"


def preseed_download_cache(cache_dir: Path, archive_path: Path, asset: dict) -> dict:
    """Place the verified archive at its manifest-pinned cache name; never
    remove or rewrite any other cache entry."""
    target = cache_dir / asset["filename"]
    if target.is_file() and target.stat().st_size == asset["size"] and digest(target) == asset["sha256"]:
        return {"cache_dir": str(cache_dir), "action": "present", "archive": str(target)}
    cache_dir.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(cache_dir), prefix=f".{asset['filename']}.", suffix=".part")
    with os.fdopen(handle, "wb") as output, archive_path.open("rb") as source:
        shutil.copyfileobj(source, output)
    os.replace(temporary, target)
    return {"cache_dir": str(cache_dir), "action": "seeded", "archive": str(target)}


# --- setup invocation and readiness checks ---------------------------------------


def run_setup_helper(formula_prefix: Path, brew_prefix: Path) -> subprocess.CompletedProcess:
    """Run the installed Brew setup helper directly, outside any Homebrew
    sandbox, with the ordinary session environment."""
    helper = formula_prefix / "libexec" / "portmap_brew_setup.py"
    python = formula_prefix / "libexec" / "bin" / "python"
    if not helper.is_file() or not python.is_file():
        raise DeployError(f"installed formula is missing the setup helper venv under {formula_prefix}/libexec")
    return subprocess.run(
        [str(python), str(helper), "--brew-prefix", str(brew_prefix), "--formula-prefix", str(formula_prefix)]
    )


def portmap_status(cli: Path, *, check: bool) -> dict:
    completed = subprocess.run(
        [str(cli), "status"] + (["--check"] if check else []), capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise DeployError(f"portmap status failed ({completed.returncode}): {completed.stderr.strip()}")
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise DeployError(f"portmap status did not print JSON: {exc}") from exc


def check_os_dns(domain: str) -> dict:
    """Resolve the catalog domain through the actual system resolver."""
    try:
        infos = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return {"domain": domain, "resolved": False, "error": str(exc)}
    return {"domain": domain, "resolved": True, "addresses": sorted({info[4][0] for info in infos})}


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            source = dict(attrs).get("src")
            if source:
                self.scripts.append(source)


def _fetch(host: str, port: int, path: str, *, timeout: int = 15):
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Host": host, "Connection": "close"})
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type", ""), response.read()
    finally:
        connection.close()


def check_http_catalog(domain: str, port: int) -> dict:
    """Fetch the catalog through actual system DNS + HTTP like a user browser."""
    url = f"http://{domain}" + ("" if port == 80 else f":{port}") + "/"
    result = {"url": url}
    try:
        status, content_type, body = _fetch(domain, port, "/")
        if status != 200 or "text/html" not in content_type:
            raise ValueError(f"GET / returned HTTP {status} ({content_type})")
        page = _Scripts()
        page.feed(body.decode("utf-8"))
        if not page.scripts:
            raise ValueError("homepage has no application script")
        for script in page.scripts:
            script_status, script_type, _ = _fetch(domain, port, script)
            if script_status != 200 or "javascript" not in script_type:
                raise ValueError(f"application asset {script} is not served as JavaScript")
        registry_status, _, registry_body = _fetch(domain, port, "/registry.json")
        registry = json.loads(registry_body.decode("utf-8"))
        if (
            registry_status != 200
            or not isinstance(registry, dict)
            or not isinstance(registry.get("dns_domain"), str)
            or not isinstance(registry.get("services"), list)
        ):
            raise ValueError("catalog API did not return a portmap catalog")
    except (OSError, ValueError) as exc:
        result.update(ok=False, error=str(exc))
        return result
    result.update(ok=True, scripts=page.scripts, services=len(registry["services"]))
    return result


def record_applied(state_dir: Path, snapshot: dict, formula_sha256: str) -> None:
    atomic_write(
        state_dir / "applied.json",
        json.dumps(
            {
                "snapshot_id": snapshot["snapshot_id"],
                "version": snapshot["version"],
                "formula_sha256": formula_sha256,
                "archive_sha256": snapshot["asset"]["sha256"],
                "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        )
        + "\n",
    )


# --- orchestration ----------------------------------------------------------------


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage-dir", type=Path, required=True, help="verified staged snapshot directory")
    parser.add_argument("--state-dir", type=Path, required=True, help="tool-owned deployment state directory")
    parser.add_argument("--brew", default=None, help="brew executable path (default: auto-detected)")
    args = parser.parse_args(argv)
    stage_dir = args.stage_dir.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    report = {"stage_dir": str(stage_dir), "state_dir": str(state_dir)}
    try:
        snapshot = verify_stage(stage_dir)
        report.update(snapshot_id=snapshot["snapshot_id"], version=snapshot["version"], platform=snapshot["platform"])
        check_platform(snapshot)
        brew_path = find_brew(args.brew)
        brew_prefix = Path(brew(brew_path, "--prefix").stdout.strip())
        report["brew_prefix"] = str(brew_prefix)
        brew_repository = Path(brew(brew_path, "--repository").stdout.strip())
        report["brew_repository"] = str(brew_repository)

        source_dir = unpack_source(stage_dir, snapshot)
        report["source_dir"] = str(source_dir)
        source_tar = next(name for name in snapshot["files"] if name.startswith("source/"))
        formula_text = generate_formula(
            (stage_dir / "formula" / "template.rb").read_text(encoding="utf-8"),
            version=snapshot["version"],
            source_url=file_url(stage_dir / source_tar),
            source_sha256=snapshot["files"][source_tar],
            brew_setup_url=file_url(stage_dir / "helpers" / HELPERS[0]),
            brew_setup_sha256=snapshot["files"][f"helpers/{HELPERS[0]}"],
            brew_postinstall_url=file_url(stage_dir / "helpers" / HELPERS[1]),
            brew_postinstall_sha256=snapshot["files"][f"helpers/{HELPERS[1]}"],
        )
        formula_sha256 = hashlib.sha256(formula_text.encode("utf-8")).hexdigest()
        tap_dir = tap_directory(brew_repository)
        report["tap"] = {"path": str(tap_dir), "action": ensure_tap(tap_dir, formula_text, formula_sha256, snapshot["snapshot_id"])}

        installed = installed_version(brew_path)
        action = decide_install_action(
            installed, snapshot["version"], load_json(state_dir / "applied.json"),
            formula_sha256, snapshot["snapshot_id"],
        )
        if action == "refuse":
            raise DeployError(
                f"portmap {installed} is already installed; downgrading to {snapshot['version']} "
                "is not supported by this deployment"
            )
        if action != "skip":
            completed = brew(brew_path, action, "--formula", f"{TAP_NAME}/portmap", capture=False)
            if completed.returncode != 0:
                raise DeployError(f"brew {action} failed with exit {completed.returncode}")
        report["install"] = action

        formula_prefix = Path(brew(brew_path, "--prefix", f"{TAP_NAME}/portmap").stdout.strip())
        report["formula_prefix"] = str(formula_prefix)
        cli = verify_installed_cli(formula_prefix, snapshot["version"])

        server_state = (portmap_status(cli, check=False).get("server") or {}).get("state_dir")
        if not server_state:
            raise DeployError("portmap status did not report a state directory")
        report["preseed"] = preseed_download_cache(
            download_cache_dir_for(Path(server_state), os.environ),
            stage_dir / "archive" / snapshot["asset"]["filename"],
            snapshot["asset"],
        )

        completed = run_setup_helper(formula_prefix, brew_prefix)
        if completed.returncode != 0:
            raise DeployError(f"portmap Brew setup failed with exit {completed.returncode}")
        report["setup"] = {"exit": 0}

        status = portmap_status(cli, check=True)
        gateway = status.get("gateway") or {}
        if not gateway.get("running") or not (status.get("resolver") or {}).get("installed"):
            raise DeployError(f"portmap status --check reports host access is not ready: {status}")
        http_port = gateway.get("http_port")
        if type(http_port) is not int or not 1 <= http_port <= 65535:
            raise DeployError(f"status did not report a usable HTTP port: {gateway!r}")
        registry_status, _, registry_body = _fetch("127.0.0.1", http_port, "/registry.json")
        registry = json.loads(registry_body)
        domain = registry.get("dns_domain")
        if registry_status != 200 or not isinstance(domain, str) or not domain.endswith(".portmap"):
            raise DeployError("the local catalog did not report its managed .portmap domain")
        dns = check_os_dns(domain)
        http = check_http_catalog(domain, http_port)
        report["checks"] = {"status": {"running": True, "domain": domain, "http_port": http_port}, "dns": dns, "http": http}
        if not dns["resolved"] or dns.get("addresses") != ["127.0.0.1"]:
            raise DeployError(f"system DNS did not resolve {domain} to the private loopback entry: {dns}")
        if not http["ok"]:
            raise DeployError(f"system HTTP catalog check failed: {http.get('error')}")

        state_dir.mkdir(parents=True, exist_ok=True)
        record_applied(state_dir, snapshot, formula_sha256)
        print(json.dumps(report, indent=2))
        return 0
    except DeployError as exc:
        print(f"deploy failed: {exc}", file=sys.stderr)
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 1
    except Exception as exc:  # unexpected failures stay explicit, never silent
        print(f"deploy failed unexpectedly: {exc!r}", file=sys.stderr)
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
