# SPDX-License-Identifier: GPL-2.0-only
"""Native onedir GUI and CLI builds sharing one bundled Python runtime."""
import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

from rockbox_manager import __version__

version_match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(?:b|-beta\.)(\d+))?", __version__)
if version_match is None:
    raise RuntimeError("Unsupported native application version")
release_version, beta = version_match.groups()
bundle_version = release_version + ("b" + beta if beta else "")

root = Path(SPECPATH)
assets = root / "build" / "assets"
data = [(str(root / name), ".") for name in ("LICENSE", "CREDITS.md", "README.md")]
data += [(str(root / "docs"), "docs"), (str(assets / "licenses"), "licenses")]
data += [(str(root / "tests" / "fixtures" / "silence.flac"), "build-check")]
native = root / "build/podbox"
if not (native / "engine.json").is_file() or not (native / "source.tar.gz").is_file():
    raise RuntimeError("Build the pinned PodBox engines with scripts/build_podbox.py before freezing.")
data += [(str(native / "engine.json"), "podbox"), (str(native / "source.tar.gz"), "licenses/PodBox")]
binaries = [(str(path), "podbox/codecs") for path in (native / "codecs").glob("*.codec")]
binaries += [(str(native / (name + (".exe" if sys.platform == "win32" else ""))), "podbox")
             for name in ("database_pb", "artcache_pb", "soundscan")]
a = Analysis(
    [str(root / "scripts" / "frozen_entry.py")],
    pathex=[str(root)],
    datas=data,
    binaries=binaries,
    # ImageTk loads its Tk library finder dynamically on Linux.
    hiddenimports=collect_submodules("mutagen") + ["PIL._tkinter_finder"],
    hookspath=[str(root / "scripts" / "hooks")],
)
pyz = PYZ(a.pure)
suffix = ".exe" if sys.platform == "win32" else ""
identity = os.environ.get("RLM_CODESIGN_IDENTITY") if sys.platform == "darwin" else None
icon = str(assets / ("app.icns" if sys.platform == "darwin" else "app.ico"))
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Rockbox Library Manager" + suffix,
          console=False, icon=icon, upx=False, codesign_identity=identity)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="rockbox-library-manager" + suffix,
          console=True, icon=icon, upx=False, codesign_identity=identity)
# Passing the CLI TOC keeps the GUI's bundle settings and main executable.
collection = COLLECT(gui, [(Path(cli.name).name, cli.name, "EXECUTABLE")],
                     a.binaries, a.datas, name="Rockbox Library Manager", upx=False)
if sys.platform == "darwin":
    app = BUNDLE(collection, name="Rockbox Library Manager.app", icon=icon,
                 bundle_identifier="nl.cyberdelia.rockbox-library-manager", version=bundle_version,
                 info_plist={"CFBundleShortVersionString": release_version,
                             "CFBundleVersion": bundle_version,
                             "CFBundleGetInfoString": "Rockbox Library Manager " + __version__,
                             "NSHighResolutionCapable": True})
