# SPDX-License-Identifier: GPL-2.0-only
"""Keep Mac release compatibility instead of pip's preferred macOS-14 wheel."""
import importlib.metadata
import platform
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    if sys.platform != "darwin":
        raise RuntimeError("This step is only for native macOS builds.")
    architecture = platform.machine()
    target = {"arm64": "macosx_11_0_arm64", "x86_64": "macosx_10_13_x86_64"}[architecture]
    version = importlib.metadata.version("numpy")
    python = f"{sys.version_info.major}{sys.version_info.minor}"
    with tempfile.TemporaryDirectory(prefix="rlm-numpy-wheel-") as folder:
        subprocess.run([sys.executable, "-m", "pip", "download", f"numpy=={version}",
                        "--only-binary=:all:", "--no-deps", "--platform", target,
                        "--python-version", python, "--implementation", "cp", "--abi", f"cp{python}",
                        "--dest", folder], check=True)
        wheels = list(Path(folder).glob("*.whl"))
        if len(wheels) != 1 or target not in wheels[0].name:
            raise RuntimeError("Expected exactly one NumPy wheel for the existing Mac baseline.")
        subprocess.run([sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheels[0])], check=True)


if __name__ == "__main__":
    main()
