# SPDX-License-Identifier: GPL-2.0-only
"""Build, archive and verify native downloads; never read user settings."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rockbox_manager import __version__  # noqa: E402


def target_name():
    machine = platform.machine().lower()
    targets = {("darwin", "arm64"): "macos-arm64", ("darwin", "x86_64"): "macos-x64",
               ("win32", "amd64"): "windows-x64", ("linux", "x86_64"): "linux-x64"}
    return targets[(sys.platform, machine)]


def prepare_assets():
    from PIL import Image
    from rockbox_manager.ui_theme import C, app_icon_image

    assets = ROOT / "build" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    icon = app_icon_image(C["accent"]).resize((512, 512), Image.Resampling.LANCZOS)
    icon.save(assets / "app.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
    if sys.platform == "darwin":
        icon.save(assets / "app.icns")
    versions = {"Python": platform.python_version()}
    python_license = next((path for path in (Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
                                            Path(sys.base_prefix) / "LICENSE.txt") if path.is_file()), None)
    if python_license is None:
        raise RuntimeError("Cannot locate the bundled Python license")
    license_directory = assets / "licenses" / "Python"
    license_directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(python_license, license_directory / "LICENSE.txt")
    for name in ("PyInstaller", "mutagen", "Pillow", "requests", "certifi", "charset-normalizer",
                 "idna", "urllib3", "tkinterdnd2"):
        dist = metadata.distribution(name)
        versions[name] = dist.version
        for entry in dist.files or []:
            if ".dist-info/licenses/" not in str(entry):
                continue
            source = Path(dist.locate_file(entry))
            if source.is_file():
                destination = assets / "licenses" / name / Path(str(entry).split("/licenses/", 1)[1])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
    (assets / "licenses" / "versions.json").write_text(json.dumps(versions, indent=2), encoding="utf-8")


def executables(directory):
    if sys.platform == "darwin":
        base = directory / "Rockbox Library Manager.app" / "Contents" / "MacOS"
    else:
        base = directory / "Rockbox Library Manager"
    suffix = ".exe" if sys.platform == "win32" else ""
    return base / ("Rockbox Library Manager" + suffix), base / ("rockbox-library-manager" + suffix)


def smoke(directory, report_dir):
    gui, cli = executables(directory)
    for executable in (gui, cli):
        report = report_dir / (executable.stem + ".json")
        subprocess.run([str(executable), "--bundle-smoke-test", str(report)], cwd=directory,
                       check=True, timeout=90)
        data = json.loads(report.read_text(encoding="utf-8"))
        if not data.get("ok") or data.get("version") != __version__:
            raise RuntimeError(f"Frozen application check failed: {data}")
    for arguments, expected in ((["--version"], __version__), (["--help"], "Legacy artwork"),
                                (["artwork", "--help"], "usage:")):
        completed = subprocess.run([str(cli), *arguments], cwd=directory, check=True,
                                   text=True, capture_output=True, timeout=30)
        if expected not in completed.stdout:
            raise RuntimeError(f"Frozen CLI check failed: {arguments}: {completed.stdout}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", default="", help="Require vVERSION to match the bundled version")
    args = parser.parse_args()
    if args.release_tag and args.release_tag != "v" + __version__:
        parser.error(f"Release tag must be v{__version__}")
    prepare_assets()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
                    "--distpath", str(ROOT / "dist" / "app"), str(ROOT / "rockbox-library-manager.spec")],
                   cwd=ROOT, check=True)
    packages = ROOT / "dist" / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    stem = f"rockbox-library-manager-v{__version__}-{target_name()}"
    if sys.platform == "darwin":
        archive = packages / (stem + ".zip")
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(ROOT / "dist" / "app" / "Rockbox Library Manager.app"), str(archive)], check=True)
    elif sys.platform == "win32":
        archive = Path(shutil.make_archive(str(packages / stem), "zip", ROOT / "dist" / "app",
                                           "Rockbox Library Manager"))
    else:
        archive = packages / (stem + ".tar.gz")
        with tarfile.open(archive, "w:gz") as output:
            output.add(ROOT / "dist" / "app" / "Rockbox Library Manager", arcname="Rockbox Library Manager")
    # Test the distributed archive, from a path with spaces outside the checkout.
    with tempfile.TemporaryDirectory(prefix="rlm extracted bundle ") as directory:
        extracted = Path(directory)
        if sys.platform == "darwin":
            subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
            subprocess.run(["codesign", "--verify", "--deep", "--strict",
                            str(extracted / "Rockbox Library Manager.app")], check=True)
        elif sys.platform == "win32":
            with zipfile.ZipFile(archive) as zipped:
                zipped.extractall(extracted)
        else:
            with tarfile.open(archive) as tar:
                tar.extractall(extracted, filter="data")
        reports = ROOT / "build" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        smoke(extracted, reports)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Verified {archive.name}: SHA-256 {digest}")


if __name__ == "__main__":
    main()
