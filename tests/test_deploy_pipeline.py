"""Consumer-visible behavior of the one-command deployment pipeline.

Covers tools/deploy_portmap.py (snapshot staging) and tools/deploy_apply.py
(remote application) at their real boundaries: the staged source tar, the
generated tap formula, staged-artifact verification, tap ownership, install
reuse decisions, download-cache preseeding and
the readiness checks. No build, SSH, Brew or container engine is executed.
"""
from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import platform
import tarfile
import threading
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller = _load("deploy_portmap")
deploy_apply = _load("deploy_apply")

BUILT_MANIFEST = (
    json.dumps(
        {
            "version": "0.14.0",
            "assets": [
                {
                    "os": "linux",
                    "arch": "amd64",
                    "filename": "portmap-0.14.0-linux-amd64.tar.gz",
                    "sha256": "a" * 64,
                    "size": 8020291,
                    "source_url": "https://github.com/ylang-ylang/portmap/releases/download/V0.14/portmap-0.14.0-linux-amd64.tar.gz",
                }
            ],
        },
        indent=2,
    )
    + "\n"
)

# Mirrors the feature tap's Formula/portmap.rb field layout (tap checkout at
# /home/ylang/homebrew-tap-portable-runtime): one source url/version/sha256
# plus two helper resource url/sha256 pairs.
TEMPLATE = '''class Portmap < Formula
  include Language::Python::Virtualenv

  desc "Unified local development gateway and split-DNS remote access"
  homepage "https://github.com/ylang-ylang/portmap"
  url "https://github.com/ylang-ylang/portmap/archive/refs/tags/V0.13.tar.gz"
  version "0.13.0"
  sha256 "9d14fa2ae8f8db2f0d35869773d8a1ca78490aad128e93d4f7f7f447d2d258c3"
  license "MIT"

  depends_on :linux
  depends_on "docker"
  depends_on "docker-compose"
  depends_on "python@3.12"

  allow_network_access! [:build, :postinstall]

  resource "brew-setup" do
    url "https://raw.githubusercontent.com/ylang-ylang/homebrew-tap/3488ca8a13aef7250f65b6d2d31386c13efd50f9/libexec/portmap_brew_setup.py"
    sha256 "b8cc345efd08c4ff1ac945e091f1a9f2c7f6922ed061aa3746832e8e230dfadd"
  end

  resource "brew-postinstall" do
    url "https://raw.githubusercontent.com/ylang-ylang/homebrew-tap/3488ca8a13aef7250f65b6d2d31386c13efd50f9/libexec/portmap_brew_postinstall.py"
    sha256 "909b2a593059a316c68b722bcc05e36ae1160b2d6baeddcf8593c762b493d45e"
  end

  def install
    virtualenv_install_with_resources without: ["brew-setup", "brew-postinstall"]
    resource("brew-setup").stage { libexec.install "portmap_brew_setup.py" }
    resource("brew-postinstall").stage { libexec.install "portmap_brew_postinstall.py" }
  end
end
'''


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "portmap"
    (root / "src/portmap").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)
    (root / "src/portmap/__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "portmap"\nversion = "0.14.0"\n', encoding="utf-8")
    helper = root / "tools/helper.sh"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    (root / "node_modules/pkg").mkdir(parents=True)
    (root / "node_modules/pkg/index.js").write_text("junk", encoding="utf-8")
    (root / "dist").mkdir()
    (root / "dist/stale.txt").write_text("stale", encoding="utf-8")
    (root / ".env.production").write_text("PRIVATE_TOKEN=not-for-deployment\n", encoding="utf-8")
    (root / ".git").write_text("gitdir: /private/worktree/metadata\n", encoding="utf-8")
    return root


def test_source_tar_is_pristine_and_excludes_local_build_state(source_tree, tmp_path):
    records = controller.source_records(source_tree)
    tar_path = tmp_path / "snapshot.tar.gz"
    controller.create_source_tar(source_tree, records, "0.14.0", tar_path)

    with tarfile.open(tar_path) as tar:
        names = tar.getnames()
        assert names and {name.split("/")[0] for name in names} == {"portmap-0.14.0"}
        # No generated manifest travels inside the source: the repository
        # carries no packaged release.json for the tar to replace.
        assert not any(name.endswith("/release.json") for name in names)
        assert "portmap-0.14.0/pyproject.toml" in names
        script = tar.getmember("portmap-0.14.0/tools/helper.sh")
        assert script.mode & 0o111, "staged scripts must stay executable"
        assert not any("node_modules" in name for name in names)
        assert not any(name.startswith("portmap-0.14.0/dist") for name in names)
        assert not any(".env.production" in name or name.endswith("/.git") for name in names)



def test_snapshot_identity_ignores_build_state_but_tracks_source(source_tree):
    def identity():
        return controller.snapshot_identity(
            "0.14.0",
            "linux-amd64",
            controller.source_records(source_tree),
            {"portmap_brew_setup.py": "b" * 64, "portmap_brew_postinstall.py": "c" * 64},
            "d" * 64,
        )

    before = identity()
    (source_tree / "node_modules/more.js").write_text("more", encoding="utf-8")
    (source_tree / "dist/newer.txt").write_text("newer", encoding="utf-8")
    assert identity() == before, "rebuild-local state must not change the snapshot identity"

    (source_tree / "src/portmap/new_module.py").write_text("", encoding="utf-8")
    assert identity() != before


def test_generate_formula_substitutes_only_the_pinned_fields():
    formula = deploy_apply.generate_formula(
        TEMPLATE,
        version="0.14.0",
        source_url="file:///stage/source/portmap-0.14.0-src-ab.tar.gz",
        source_sha256="1" * 64,
        brew_setup_url="file:///stage/helpers/portmap_brew_setup.py",
        brew_setup_sha256="2" * 64,
        brew_postinstall_url="file:///stage/helpers/portmap_brew_postinstall.py",
        brew_postinstall_sha256="3" * 64,
    )
    assert formula.endswith("\n")
    changed = [
        (before, after)
        for before, after in zip(TEMPLATE.splitlines(), formula.splitlines())
        if before != after
    ]
    assert len(changed) == 7, f"exactly the 7 pinned fields may change, got {changed}"
    lines = formula.splitlines()
    assert lines[5] == '  url "file:///stage/source/portmap-0.14.0-src-ab.tar.gz"'
    assert lines[6] == '  version "0.14.0"'
    assert lines[7] == '  sha256 "%s"' % ("1" * 64)
    assert '    url "file:///stage/helpers/portmap_brew_setup.py"' in lines
    assert '    sha256 "%s"' % ("2" * 64) in lines
    assert '    url "file:///stage/helpers/portmap_brew_postinstall.py"' in lines
    assert '    sha256 "%s"' % ("3" * 64) in lines


@pytest.mark.parametrize(
    "mutate",
    [
        "drop-helper-sha",
        "extra-url",
        "no-version",
    ],
)
def test_generate_formula_refuses_template_drift(mutate):
    template = TEMPLATE
    if mutate == "drop-helper-sha":
        template = template.replace(
            '    sha256 "909b2a593059a316c68b722bcc05e36ae1160b2d6baeddcf8593c762b493d45e"\n', ""
        )
    elif mutate == "extra-url":
        template = template.replace(
            '  license "MIT"',
            '  license "MIT"\n  url "https://unexpected.example/x.tar.gz"',
        )
    else:
        template = template.replace('  version "0.13.0"\n', "")
    with pytest.raises(deploy_apply.DeployError):
        deploy_apply.generate_formula(
            template,
            version="0.14.0",
            source_url="file:///s.tar.gz",
            source_sha256="1" * 64,
            brew_setup_url="file:///setup.py",
            brew_setup_sha256="2" * 64,
            brew_postinstall_url="file:///post.py",
            brew_postinstall_sha256="3" * 64,
        )


def build_stage(tmp_path):
    stage = tmp_path / "stage"
    contents = {
        "release.json": BUILT_MANIFEST,
        "archive/portmap-0.14.0-linux-amd64.tar.gz": "archive-bytes",
        "helpers/portmap_brew_setup.py": "setup-helper",
        "helpers/portmap_brew_postinstall.py": "postinstall-helper",
        "formula/template.rb": TEMPLATE,
        "source/portmap-0.14.0-src-ab12.tar.gz": "source-tar",
        "deploy_apply.py": "# apply\n",
    }
    files = {}
    for name, content in contents.items():
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        files[name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    asset = dict(json.loads(BUILT_MANIFEST)["assets"][0])
    asset["sha256"] = files["archive/portmap-0.14.0-linux-amd64.tar.gz"]
    asset["size"] = len("archive-bytes")
    snapshot = {
        "snapshot_id": "e" * 64,
        "version": "0.14.0",
        "platform": "linux-amd64",
        "asset": asset,
        "files": files,
    }
    (stage / "snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return stage, snapshot


def test_verify_stage_accepts_intact_stage_and_rejects_tampering(tmp_path):
    stage, _ = build_stage(tmp_path)
    snapshot = deploy_apply.verify_stage(stage)
    assert snapshot["version"] == "0.14.0"

    tampered, _ = build_stage(tmp_path / "second")
    (tampered / "helpers/portmap_brew_setup.py").write_text("tampered", encoding="utf-8")
    with pytest.raises(deploy_apply.DeployError, match="checksum mismatch"):
        deploy_apply.verify_stage(tampered)

    missing, _ = build_stage(tmp_path / "third")
    (missing / "formula/template.rb").unlink()
    with pytest.raises(deploy_apply.DeployError, match="missing"):
        deploy_apply.verify_stage(missing)

    mismatched, snapshot = build_stage(tmp_path / "fourth")
    snapshot["asset"]["sha256"] = "0" * 64
    (mismatched / "snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    with pytest.raises(deploy_apply.DeployError, match="archive does not match"):
        deploy_apply.verify_stage(mismatched)


def test_check_platform_requires_matching_target():
    machine = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
    system = platform.system().lower()
    matching = {"platform": f"{system}-{machine}", "asset": {"os": system, "arch": machine}}
    deploy_apply.check_platform(matching)  # must accept the actual host platform

    other = "arm64" if machine == "amd64" else "amd64"
    with pytest.raises(deploy_apply.DeployError, match="targets"):
        deploy_apply.check_platform({"platform": f"linux-{other}", "asset": {"os": "linux", "arch": other}})


def test_ensure_tap_preserves_foreign_taps_and_updates_owned_content(tmp_path):
    foreign = tmp_path / "foreign-tap"
    (foreign / "Formula").mkdir(parents=True)
    (foreign / "Formula/portmap.rb").write_text("# someone else's formula\n", encoding="utf-8")
    with pytest.raises(deploy_apply.DeployError, match="foreign"):
        deploy_apply.ensure_tap(foreign, "class Portmap\nend\n", "f" * 64, "a" * 64)
    assert (foreign / "Formula/portmap.rb").read_text(encoding="utf-8") == "# someone else's formula\n"
    assert list(foreign.iterdir()) == [foreign / "Formula"], "refusal must not add files"

    owned = tmp_path / "owned-tap"
    assert deploy_apply.ensure_tap(owned, "first-formula\n", "1" * 64, "a" * 64) == "created"
    assert (owned / "Formula/portmap.rb").read_text(encoding="utf-8") == "first-formula\n"
    unrelated = owned / "notes.txt"
    unrelated.write_text("keep me\n", encoding="utf-8")
    assert deploy_apply.ensure_tap(owned, "second-formula\n", "2" * 64, "b" * 64) == "updated"
    assert deploy_apply.ensure_tap(owned, "second-formula\n", "2" * 64, "b" * 64) == "unchanged"
    assert unrelated.read_text(encoding="utf-8") == "keep me\n"


def test_decide_install_action_reuses_identical_snapshot():
    applied = {"snapshot_id": "s" * 64, "formula_sha256": "f" * 64}
    assert deploy_apply.decide_install_action(None, "0.14.0", None, "f" * 64, "s" * 64) == "install"
    # An upgrade may also change taps; install rejects it before any work.
    assert deploy_apply.decide_install_action("0.13.0", "0.14.0", None, "f" * 64, "s" * 64) == "reinstall"
    assert deploy_apply.decide_install_action("0.14.0", "0.14.0", applied, "f" * 64, "s" * 64) == "skip"
    assert deploy_apply.decide_install_action("0.14.0", "0.14.0", None, "f" * 64, "s" * 64) == "reinstall"
    assert deploy_apply.decide_install_action("0.14.0", "0.14.0", applied, "9" * 64, "s" * 64) == "reinstall"
    assert deploy_apply.decide_install_action("0.15.0", "0.14.0", applied, "f" * 64, "s" * 64) == "refuse"


def test_preseed_download_cache_is_atomic_and_preserves_neighbors(tmp_path):
    cache = tmp_path / "downloads"
    cache.mkdir()
    neighbor = cache / "foreign-archive.tar.gz"
    neighbor.write_text("foreign bytes", encoding="utf-8")
    archive = tmp_path / "staged-portmap.tar.gz"
    payload = b"actual built archive"
    archive.write_bytes(payload)
    manifest = tmp_path / "release.json"
    manifest.write_text(BUILT_MANIFEST, encoding="utf-8")
    asset = {
        "filename": "portmap-0.14.0-linux-amd64.tar.gz",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }

    first = deploy_apply.preseed_download_cache(cache, archive, asset, manifest)
    assert first["archive"] == "seeded"
    assert first["manifest"] == "seeded"
    assert (cache / asset["filename"]).read_bytes() == payload
    assert (cache / "release.json").read_bytes() == BUILT_MANIFEST.encode("utf-8")
    again = deploy_apply.preseed_download_cache(cache, archive, asset, manifest)
    assert again["archive"] == "present"
    assert again["manifest"] == "present"

    (cache / asset["filename"]).write_bytes(b"corrupt")
    (cache / "release.json").write_bytes(b"stale manifest")
    repaired = deploy_apply.preseed_download_cache(cache, archive, asset, manifest)
    assert repaired["archive"] == "seeded"
    assert repaired["manifest"] == "seeded"
    assert (cache / asset["filename"]).read_bytes() == payload
    assert (cache / "release.json").read_bytes() == BUILT_MANIFEST.encode("utf-8")
    assert neighbor.read_text(encoding="utf-8") == "foreign bytes"
    assert {path.name for path in cache.iterdir() if path.is_file()} == {
        asset["filename"], "release.json", "foreign-archive.tar.gz",
    }


def test_unpack_source_flattens_and_reuses_identical_tar(source_tree, tmp_path):
    stage = tmp_path / "stage"
    (stage / "source").mkdir(parents=True)
    records = controller.source_records(source_tree)
    tar_path = stage / "source/portmap-0.14.0-src-ab12.tar.gz"
    controller.create_source_tar(source_tree, records, "0.14.0", tar_path)
    snapshot = {"files": {"source/portmap-0.14.0-src-ab12.tar.gz": controller.digest(tar_path)}}

    tree = deploy_apply.unpack_source(stage, snapshot)
    assert (tree / "pyproject.toml").is_file()
    assert (tree / "tools/helper.sh").exists()
    assert (tree / "tools/helper.sh").stat().st_mode & 0o111, "executables survive staging"
    assert not (tree / "src/portmap/release.json").exists()

    tar_path.unlink()  # identical unpacked copy is reused without re-extraction
    assert deploy_apply.unpack_source(stage, snapshot) == tree

    records.append({"path": "src/portmap/extra.py", "executable": False, "sha256": hashlib.sha256(b"").hexdigest()})
    (source_tree / "src/portmap/extra.py").write_text("", encoding="utf-8")
    controller.create_source_tar(source_tree, records, "0.14.0", tar_path)
    snapshot["files"]["source/portmap-0.14.0-src-ab12.tar.gz"] = controller.digest(tar_path)
    refreshed = deploy_apply.unpack_source(stage, snapshot)
    assert (refreshed / "src/portmap/extra.py").is_file()




class _Catalog(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = b'<html><script src="/assets/catalog.js"></script></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/assets/catalog.js":
            body = b"console.log('catalog')"
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
        elif self.path == "/registry.json":
            body = json.dumps({"dns_domain": "portmap.local", "services": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"broken"
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def test_check_http_catalog_verifies_real_catalog_serving():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Catalog)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = deploy_apply.check_http_catalog("127.0.0.1", port)
        assert result["ok"], result
        assert result["scripts"] == ["/assets/catalog.js"]
        assert result["services"] == 0
        assert result["url"] == f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()

    dead = deploy_apply.check_http_catalog("127.0.0.1", 1)
    assert not dead["ok"] and dead["error"]
