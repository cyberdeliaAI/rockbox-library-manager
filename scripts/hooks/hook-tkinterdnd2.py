# SPDX-License-Identifier: GPL-2.0-only
"""Bundle the same native tkdnd directory the installed loader selects."""
import platform
import sys
from pathlib import Path

import _tkinter
from PyInstaller.utils.hooks import get_package_paths

_, package = get_package_paths("tkinterdnd2")
machine = platform.machine().lower()
platform_dir = {("darwin", "arm64"): "osx-arm64", ("darwin", "x86_64"): "osx-x64",
                ("linux", "x86_64"): "linux-x64", ("win32", "amd64"): "win-x64"}.get((sys.platform, machine))
if not platform_dir:
    raise RuntimeError(f"Unsupported native build target: {sys.platform}/{machine}")
base = Path(package) / "tkdnd"
if int(_tkinter.TCL_VERSION.split(".")[0]) >= 9 and (base / (platform_dir + "-tcl9")).is_dir():
    platform_dir += "-tcl9"
datas = [(str(base / platform_dir), f"tkinterdnd2/tkdnd/{platform_dir}")]
