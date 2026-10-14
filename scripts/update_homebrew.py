# SPDX-License-Identifier: GPL-2.0-only
"""Prepare a verified cask update; publishing is handled by the workflow."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import plistlib
import re
import subprocess
import urllib.request
import zipfile
from pathlib import Path

REPOSITORY = "cyberdeliaAI/rockbox-library-manager"
CASK = "rockbox-library-manager.rb"
MANAGED = "# Updated by Rockbox Library Manager's verified Homebrew release workflow.\n"
STABLE = r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
HASHES = re.compile(r'  sha256 arm:   "[0-9a-f]{64}",\n         intel: "[0-9a-f]{64}"')


def download(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "Rockbox-Library-Manager-Homebrew"})
    # Release downloads are public. Never forward GitHub API credentials to a CDN.
    with urllib.request.urlopen(request, timeout=60) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Release download exceeds its size limit")
    return content


def prepare(cask, release, fetch=download):
    """Return a candidate without modifying the input; reject partial/unverified releases."""
    tag = release["tag_name"]
    match = re.fullmatch("v" + STABLE, tag)
    if release.get("draft") or release.get("prerelease") or not match:
        return cask, "Skipping a draft or non-stable release"
    current = re.search(r'^  version "(' + STABLE + r')"$', cask, re.MULTILINE)
    if not current or len(HASHES.findall(cask)) != 1:
        raise ValueError("Unexpected cask version/checksum layout")
    version = tag[1:]
    if tuple(map(int, version.split("."))) < tuple(map(int, current.group(1).split("."))):
        return cask, "Skipping an older release"
    assets = {}
    for asset in release["assets"]:
        if asset["name"] in assets:
            raise ValueError("Duplicate release asset name")
        assets[asset["name"]] = asset
    hashes = []
    for arch in ("arm64", "x64"):
        name = f"rockbox-library-manager-{tag}-macos-{arch}.zip"
        data = []
        for filename, limit in ((name, 200 * 1024 * 1024), (name + ".sha256", 1024)):
            asset = assets.get(filename)
            expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}"
            if not asset or asset.get("state") != "uploaded" or asset.get("browser_download_url") != expected_url:
                raise ValueError(f"Missing or unexpected release asset: {filename}")
            content = fetch(expected_url, limit)
            digest = hashlib.sha256(content).hexdigest()
            if asset.get("digest") != "sha256:" + digest:
                raise ValueError(f"GitHub asset digest mismatch: {filename}")
            data.append(content)
        archive, companion = data
        digest = hashlib.sha256(archive).hexdigest()
        if companion.decode("ascii").split() != [digest, name]:
            raise ValueError(f"Companion checksum mismatch: {name}")
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            plist = zipped.getinfo("Rockbox Library Manager.app/Contents/Info.plist")
            if plist.file_size > 1024 * 1024:
                raise ValueError("Unexpected application plist size")
            info = plistlib.loads(zipped.read(plist))
            if (info.get("CFBundleShortVersionString") != version
                    or info.get("CFBundleIdentifier") != "nl.cyberdelia.rockbox-library-manager"):
                raise ValueError(f"Application identity/version mismatch: {name}")
            for executable in ("Rockbox Library Manager", "rockbox-library-manager"):
                zipped.getinfo("Rockbox Library Manager.app/Contents/MacOS/" + executable)
        hashes.append(digest)
    updated = cask[:current.start()] + f'  version "{version}"' + cask[current.end():]
    updated = HASHES.sub(f'  sha256 arm:   "{hashes[0]}",\n         intel: "{hashes[1]}"', updated)
    if not updated.startswith(MANAGED):
        updated = MANAGED + updated
    return updated, f"Verified both macOS downloads for {tag}"


def apply(tap, candidate, metadata):
    """Refuse to overwrite a cask changed since validation; leave other tap files alone."""
    target = tap / "Casks" / CASK
    current = target.read_bytes()
    updated = candidate.read_bytes()
    if hashlib.sha256(updated).hexdigest() != metadata["candidate_sha256"]:
        raise ValueError("Validated candidate changed")
    if current == updated:
        return False
    if hashlib.sha256(current).hexdigest() != metadata["base_sha256"]:
        raise ValueError("The tap cask changed during validation; retry the workflow")
    target.write_bytes(updated)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tap", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag", default="", help="Require the requested tag to be the latest stable release")
    parser.add_argument("--apply", action="store_true", help="Apply an already validated candidate")
    args = parser.parse_args()
    candidate = args.output / CASK
    metadata_file = args.output / "metadata.json"
    if args.apply:
        changed = apply(args.tap, candidate, json.loads(metadata_file.read_text(encoding="utf-8")))
    else:
        release = json.loads(subprocess.check_output(["gh", "api", f"repos/{REPOSITORY}/releases/latest"], text=True))
        if args.tag and args.tag != release["tag_name"]:
            raise ValueError("Requested tag is not the latest stable release")
        original = (args.tap / "Casks" / CASK).read_text(encoding="utf-8")
        updated, message = prepare(original, release)
        print(message)
        changed = updated != original
        args.output.mkdir(parents=True, exist_ok=True)
        candidate.write_text(updated, encoding="utf-8")
        metadata_file.write_text(json.dumps({
            "tag": release["tag_name"],
            "base_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "candidate_sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
        }), encoding="utf-8")
    print("Cask update prepared" if changed else "Cask is already up to date")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"changed={str(changed).lower()}\n")


if __name__ == "__main__":
    main()
