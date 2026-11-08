# SPDX-License-Identifier: GPL-2.0-only
# Verification approach adapted from Tagcast 1.6.5; upstream MIT notice follows.
# MIT License
# 
# Copyright (c) 2026 Cyberdelia
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Explicit Developer ID signing and notarization for the existing PyInstaller app."""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

SIGNING_SECRETS = ("CSC_LINK", "CSC_KEY_PASSWORD", "APPLE_ID", "APPLE_APP_SPECIFIC_PASSWORD", "APPLE_TEAM_ID")
SIGNING_CHECKS = ("developer_id", "hardened_runtime", "notarization_ticket", "gatekeeper", "runtime_checks")


def plain_environment():
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("CSC_", "WIN_CSC_", "APPLE_", "RLM_CODESIGN_"))}


def configuration(enabled):
    if not enabled:
        return None
    if sys.platform != "darwin":
        raise ValueError("--sign-macos requires macOS")
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event and event not in ("release", "workflow_dispatch"):
        raise ValueError("Signing is restricted to release or explicit manual builds")
    missing = [name for name in SIGNING_SECRETS if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing macOS signing secrets: " + ", ".join(missing))
    config = {name: os.environ[name] for name in SIGNING_SECRETS}
    if not re.fullmatch(r"[A-Z0-9]{10}", config["APPLE_TEAM_ID"]):
        raise ValueError("APPLE_TEAM_ID must contain ten uppercase letters/digits")
    try:
        certificate = base64.b64decode("".join(config["CSC_LINK"].split()), validate=True)
    except ValueError:
        raise ValueError("CSC_LINK must be the base64-encoded Developer ID .p12") from None
    if not certificate or len(certificate) > 1024 * 1024:
        raise ValueError("Unexpected signing certificate size")
    config["certificate"] = certificate
    return config


def command(args, label, *, check=True, timeout=120):
    """Never include command arguments or Apple/security output in an exception."""
    try:
        result = subprocess.run(args, env=plain_environment(), capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f"{label} could not complete") from None
    if check and result.returncode:
        raise RuntimeError(f"{label} failed (exit {result.returncode})")
    return result


@contextmanager
def signing_environment(config):
    """Import only for the build; restore the keychain search list even after failure."""
    if config is None:
        yield plain_environment()
        return
    original = shlex.split(command(["security", "list-keychains", "-d", "user"], "Read keychains").stdout)
    with tempfile.TemporaryDirectory(prefix="rlm-signing-") as name:
        root = Path(name)
        certificate, keychain = root / "developer.p12", root / "build.keychain-db"
        certificate.write_bytes(config["certificate"])
        certificate.chmod(0o600)
        password = secrets.token_urlsafe(32)
        try:
            command(["security", "create-keychain", "-p", password, str(keychain)], "Create temporary keychain")
            command(["security", "set-keychain-settings", "-lut", "3600", str(keychain)], "Configure keychain")
            command(["security", "unlock-keychain", "-p", password, str(keychain)], "Unlock temporary keychain")
            command(["security", "import", str(certificate), "-k", str(keychain), "-P", config["CSC_KEY_PASSWORD"],
                     "-T", "/usr/bin/codesign"], "Import Developer ID certificate")
            command(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k",
                     password, str(keychain)], "Permit noninteractive signing")
            command(["security", "list-keychains", "-d", "user", "-s", str(keychain), *original], "Select keychain")
            output = command(["security", "find-identity", "-v", "-p", "codesigning", str(keychain)],
                             "Find Developer ID identity").stdout
            identities = re.findall(r'\b([A-F0-9]{40}) "Developer ID Application: [^"\n]+ \(([A-Z0-9]{10})\)"', output)
            matches = [digest for digest, team in identities if team == config["APPLE_TEAM_ID"]]
            if len(matches) != 1:
                raise RuntimeError("Expected one valid Developer ID Application certificate for APPLE_TEAM_ID")
            env = plain_environment()
            env["RLM_CODESIGN_IDENTITY"] = matches[0]
            yield env
        finally:
            command(["security", "list-keychains", "-d", "user", "-s", *original], "Restore keychains", check=False)
            command(["security", "delete-keychain", str(keychain)], "Delete temporary keychain", check=False)


def verify_app(app, team=None):
    command(["codesign", "--verify", "--deep", "--strict", str(app)], "Verify application signature")
    if team is None:
        return
    for path in (app, app / "Contents/MacOS/Rockbox Library Manager", app / "Contents/MacOS/rockbox-library-manager"):
        result = command(["codesign", "--display", "--verbose=4", str(path)], "Read signing identity")
        detail = result.stdout + result.stderr
        if ("Authority=Developer ID Application:" not in detail or f"TeamIdentifier={team}\n" not in detail
                or not re.search(r"^CodeDirectory .*flags=.*\bruntime\b", detail, re.MULTILINE)):
            raise RuntimeError("App and CLI require hardened Developer ID signatures from APPLE_TEAM_ID")
    command(["xcrun", "stapler", "validate", str(app)], "Validate notarization ticket")
    command(["spctl", "--assess", "--type", "execute", "--verbose=2", str(app)], "Check Gatekeeper")


def notarize(app, config):
    with tempfile.TemporaryDirectory(prefix="rlm-notarize-") as name:
        archive = Path(name) / "submission.zip"
        command(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)],
                "Prepare notarization archive")
        result = command(["xcrun", "notarytool", "submit", str(archive), "--apple-id", config["APPLE_ID"],
                          "--password", config["APPLE_APP_SPECIFIC_PASSWORD"], "--team-id", config["APPLE_TEAM_ID"],
                          "--wait", "--timeout", "20m", "--output-format", "json"],
                         "Apple notarization", timeout=1260)
        try:
            accepted = json.loads(result.stdout).get("status") == "Accepted"
        except (ValueError, AttributeError):
            accepted = False
        if not accepted:
            raise RuntimeError("Apple has not accepted this notarization; no signed download will be published")
    command(["xcrun", "stapler", "staple", str(app)], "Staple notarization ticket")
    verify_app(app, config["APPLE_TEAM_ID"])
