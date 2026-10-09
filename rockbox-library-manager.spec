# SPDX-License-Identifier: GPL-2.0-only
"""Native onedir GUI and CLI builds sharing one bundled Python runtime."""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

from rockbox_manager import __version__

root = Path(SPECPATH)
assets = root / "build" / "assets"
data = [(str(root / name), ".") for name in ("LICENSE", "CREDITS.md", "README.md")]
data += [(str(root / "docs"), "docs"), (str(assets / "licenses"), "licenses")]
data += [(str(root / "tests" / "fixtures" / "silence.flac"), "build-check")]
a = Analysis(
    [str(root / "scripts" / "frozen_entry.py")],
    pathex=[str(root)],
    datas=data,
    # ImageTk loads its Tk library finder dynamically on Linux.
    hiddenimports=collect_submodules("mutagen") + ["PIL._tkinter_finder"],
    hookspath=[str(root / "scripts" / "hooks")],
)
pyz = PYZ(a.pure)
suffix = ".exe" if sys.platform == "win32" else ""
icon = str(assets / ("app.icns" if sys.platform == "darwin" else "app.ico"))
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Rockbox Library Manager" + suffix,
          console=False, icon=icon, upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="rockbox-library-manager" + suffix,
          console=True, icon=icon, upx=False)
# Passing the CLI TOC keeps the GUI's bundle settings and main executable.
collection = COLLECT(gui, [(Path(cli.name).name, cli.name, "EXECUTABLE")],
                     a.binaries, a.datas, name="Rockbox Library Manager", upx=False)
if sys.platform == "darwin":
    app = BUNDLE(collection, name="Rockbox Library Manager.app", icon=icon,
                 bundle_identifier="nl.cyberdelia.rockbox-library-manager", version=__version__,
                 info_plist={"CFBundleShortVersionString": __version__, "NSHighResolutionCapable": True})
