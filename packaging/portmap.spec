# PyInstaller one-directory build of the unified portmap executable.
import os
# Runtime data stays relative to the executable.
import sysconfig
from importlib.metadata import distribution
from pathlib import Path

root = Path(SPECPATH).parent
package_root = root / "src" / "portmap"
static_root = Path(os.environ.get("PORTMAP_BUILD_STATIC_ROOT", package_root / "catalog_static"))
freezer = distribution("pyinstaller")
freezer_license = next(item for item in freezer.files if item.name == "COPYING.txt")

# The unified bundle carries every role module: CLI, server catalog/agent
# workers and the client transport are all spawned from this executable
# (hidden subcommands re-exec the frozen binary), so nothing may be excluded.
# The frozen container entrypoint (`_serve-catalog` -> portmap.catalog.main)
# and any newly added module are picked up by the same directory scan.
hiddenimports = sorted(
    f"portmap.{path.stem}" for path in package_root.glob("*.py") if path.stem != "__init__"
)

analysis = Analysis(
    [str(root / "packaging" / "portmap_entry.py")],
    pathex=[str(root / "src")],
    datas=[
        (str(static_root), "portmap/catalog_static"),
        (str(package_root / "demo_assets"), "portmap/demo_assets"),
        (str(package_root / "scaffold_templates"), "portmap/scaffold_templates"),
        # No release.json here: the frozen archive cannot embed the manifest
        # that pins the archive's own checksum. The installer writes the
        # canonical sidecar next to the installed executable; raw-archive
        # bundles bootstrap the pinned published manifest remotely.
        (str(package_root / "download_assets"), "portmap/download_assets"),
        (str(root / "LICENSE"), "licenses/portmap"),
        (str(Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"), "licenses/python"),
        (str(freezer.locate_file(freezer_license)), "licenses/pyinstaller"),
        # Notices for the shared libraries from the pinned Debian build image.
        *[
            (f"/usr/share/doc/{package}/copyright", f"licenses/system/{package}")
            for package in ("libssl1.1", "liblzma5", "libbz2-1.0", "zlib1g")
        ],
        ("/usr/share/common-licenses", "licenses/system/common"),
    ],
    hiddenimports=hiddenimports,
    noarchive=False,
)
python_archive = PYZ(analysis.pure)
executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="portmap",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=False, name="portmap")
