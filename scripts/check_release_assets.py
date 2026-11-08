# SPDX-License-Identifier: GPL-2.0-only
"""Refuse publication without archive-bound macOS signing and runtime evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

CHECKS = ("developer_id", "hardened_runtime", "notarization_ticket", "gatekeeper", "runtime_checks")


def checked_assets(downloads, reports, version):
    for target, suffix in (("macos-arm64", ".zip"), ("macos-x64", ".zip"),
                           ("windows-x64", ".zip"), ("linux-x64", ".tar.gz")):
        name = f"rockbox-library-manager-v{version}-{target}{suffix}"
        archive = downloads / name
        if archive.is_symlink() or not archive.is_file() or not archive.stat().st_size:
            raise ValueError(f"Missing regular release archive: {name}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        companion = downloads / (name + ".sha256")
        if companion.is_symlink() or companion.read_text(encoding="ascii").split() != [digest, name]:
            raise ValueError(f"Release checksum mismatch: {name}")
        if target.startswith("macos-"):
            data = json.loads((reports / f"smoke-reports-{target}/macos-signing.json").read_text(encoding="utf-8"))
            if any(data.get(key) is not True for key in CHECKS) or data.get("archive") != name or data.get("sha256") != digest:
                raise ValueError(f"Missing matching signing evidence: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    args = parser.parse_args()
    from rockbox_manager import __version__
    checked_assets(args.downloads, args.reports, __version__)
    print("All native archives and both extracted macOS signing/runtime reports verified")


if __name__ == "__main__":
    # Executing a script normally puts scripts/, rather than the checkout, on sys.path.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
