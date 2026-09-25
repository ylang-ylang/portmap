# PyInstaller one-directory build. Runtime data stays relative to the executable.
import os
import sysconfig
from importlib.metadata import distribution
from pathlib import Path

root = Path(SPECPATH).parent
native = Path(os.environ["PORTMAP_CLIENT_NATIVE_DIR"])
freezer = distribution("pyinstaller")
freezer_license = next(item for item in freezer.files if item.name == "COPYING.txt")

analysis = Analysis(
    [str(root / "packaging" / "client_entry.py")],
    pathex=[str(root / "src")],
    binaries=[
        (str(native / "coredns"), "portmap/client_bin"),
        (str(native / "traefik"), "portmap/client_bin"),
    ],
    datas=[
        (str(root / "src" / "portmap" / "catalog_static"), "portmap/catalog_static"),
        (str(root / "LICENSE"), "licenses/portmap"),
        (str(native / "licenses"), "licenses/native"),
        (str(Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"), "licenses/python"),
        (str(freezer.locate_file(freezer_license)), "licenses/pyinstaller"),
        # Notices for the shared libraries from the pinned Debian build image.
        *[
            (f"/usr/share/doc/{package}/copyright", f"licenses/system/{package}")
            for package in ("libssl1.1", "liblzma5", "libbz2-1.0", "zlib1g")
        ],
        ("/usr/share/common-licenses", "licenses/system/common"),
    ],
    hiddenimports=["portmap.client_cli", "portmap.client_view", "portmap.client_runtime", "portmap.web_static"],
    excludes=[
        "portmap.cli", "portmap.catalog", "portmap.agent", "portmap.agent_client",
        "portmap.broker", "portmap.broker_shim", "portmap.compose",
        "portmap.compose_takeover", "portmap.config", "portmap.demo",
        "portmap.host_dns", "portmap.model", "portmap.planner", "portmap.ports",
        "portmap.registry", "portmap.repo_identity", "portmap.scaffold",
        "portmap.settings", "portmap.client_downloads",
    ],
    noarchive=False,
)
python_archive = PYZ(analysis.pure)
executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="portmap-client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=False, name="portmap-client")
