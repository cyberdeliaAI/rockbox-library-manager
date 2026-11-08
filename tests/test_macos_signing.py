"""Signing tests prove control flow; actual signing is checked separately on Mac CI."""
import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_release_assets as release
from scripts import macos_signing as signing


ENV = {"CSC_LINK": base64.b64encode(b"test-certificate").decode(), "CSC_KEY_PASSWORD": "certificate-secret",
       "APPLE_ID": "developer@example.invalid", "APPLE_APP_SPECIFIC_PASSWORD": "notary-secret",
       "APPLE_TEAM_ID": "ABCDEFGHIJ"}


class MacSigningTests(unittest.TestCase):
    def config(self):
        with patch.dict(signing.os.environ, ENV, clear=True), patch.object(signing.sys, "platform", "darwin"):
            return signing.configuration(True)

    def test_missing_credentials_wrong_platform_and_pr_events_fail_before_commands(self):
        with patch.dict(signing.os.environ, {}, clear=True), patch.object(signing.sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "Missing"):
                signing.configuration(True)
        for event in ("pull_request", "pull_request_target", "push"):
            with patch.dict(signing.os.environ, {**ENV, "GITHUB_EVENT_NAME": event}, clear=True), \
                    patch.object(signing.sys, "platform", "darwin"):
                with self.assertRaisesRegex(ValueError, "restricted"):
                    signing.configuration(True)
        with patch.object(signing.sys, "platform", "linux"):
            with self.assertRaisesRegex(ValueError, "requires macOS"):
                signing.configuration(True)

    def test_ordinary_builds_strip_credentials_and_ambient_signing_identity(self):
        with patch.dict(signing.os.environ, {**ENV, "RLM_CODESIGN_IDENTITY": "ambient", "KEEP": "value"}, clear=True):
            self.assertIsNone(signing.configuration(False))
            with signing.signing_environment(None) as env:
                self.assertEqual(env, {"KEEP": "value"})

    def test_secret_command_failure_and_timeouts_are_redacted(self):
        for result in (subprocess.CompletedProcess([], 1, "notary-secret", "certificate-secret"),
                       subprocess.TimeoutExpired(["tool", "notary-secret"], 1)):
            with patch.object(signing.subprocess, "run") as run:
                if isinstance(result, Exception):
                    run.side_effect = result
                else:
                    run.return_value = result
                with self.assertRaises(RuntimeError) as caught:
                    signing.command(["tool", "notary-secret"], "Signing step")
                self.assertNotIn("secret", str(caught.exception))

    def test_temporary_keychain_restored_on_build_failure(self):
        calls = []
        fingerprint = "A" * 40

        def command(args, label, **kwargs):
            calls.append(args)
            out = '"/original/keychain-db"' if label == "Read keychains" else ""
            if label == "Find Developer ID identity":
                out = f'1) {fingerprint} "Developer ID Application: Test (ABCDEFGHIJ)"'
            return subprocess.CompletedProcess(args, 0, out, "")

        with patch.object(signing, "command", side_effect=command), patch.dict(signing.os.environ, ENV, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Build failed"):
                with signing.signing_environment(self.config()) as env:
                    self.assertEqual(env["RLM_CODESIGN_IDENTITY"], fingerprint)
                    self.assertFalse(any(name in env for name in ENV))
                    raise RuntimeError("Build failed")
        self.assertEqual(calls[-2], ["security", "list-keychains", "-d", "user", "-s", "/original/keychain-db"])
        self.assertEqual(calls[-1][1], "delete-keychain")
        self.assertFalse(Path(calls[-1][-1]).parent.exists())

    def test_verification_checks_app_cli_team_runtime_staple_and_gatekeeper(self):
        signature = "Authority=Developer ID Application: Test\nTeamIdentifier=ABCDEFGHIJ\nCodeDirectory v=20500 flags=0x10000(runtime)\n"
        with patch.object(signing, "command", return_value=subprocess.CompletedProcess([], 0, "", signature)) as run:
            signing.verify_app(Path("Test.app"), "ABCDEFGHIJ")
            self.assertEqual(sum(call.args[0][1] == "--display" for call in run.call_args_list), 3)
            self.assertTrue(any(call.args[0][:3] == ["xcrun", "stapler", "validate"] for call in run.call_args_list))
            self.assertTrue(any(call.args[0][0] == "spctl" for call in run.call_args_list))
        with patch.object(signing, "command", return_value=subprocess.CompletedProcess([], 0, "", "Signature=adhoc\n")):
            with self.assertRaisesRegex(RuntimeError, "hardened Developer ID"):
                signing.verify_app(Path("Test.app"), "ABCDEFGHIJ")

    def test_apple_must_accept_before_stapling(self):
        for status in ("Invalid", "In Progress", "Accepted"):
            with self.subTest(status=status), patch.object(signing, "command") as run, \
                    patch.object(signing, "verify_app") as verify:
                run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"status": status}), "")
                if status == "Accepted":
                    signing.notarize(Path("Test.app"), self.config())
                    verify.assert_called_once()
                else:
                    with self.assertRaisesRegex(RuntimeError, "not accepted"):
                        signing.notarize(Path("Test.app"), self.config())
                    verify.assert_not_called()
                    self.assertFalse(any(call.args[0][:3] == ["xcrun", "stapler", "staple"] for call in run.call_args_list))

    def test_ticket_service_retry_is_bounded_and_does_not_accept_invalid_tickets(self):
        results = [subprocess.CompletedProcess([], code, "", "") for code in (68, 68, 0)]
        with patch.object(signing, "command", side_effect=results) as run, patch.object(signing.time, "sleep") as sleep:
            signing.stapler("validate", Path("Test.app"))
            self.assertEqual(run.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15])
            self.assertTrue(all(call.args[0][:3] == ["xcrun", "stapler", "validate"] for call in run.call_args_list))
        for code, attempts in ((68, 3), (65, 1), (66, 1), (77, 1)):
            with self.subTest(code=code), patch.object(signing.time, "sleep"), \
                    patch.object(signing, "command", return_value=subprocess.CompletedProcess([], code, "", "")) as run:
                with self.assertRaises(RuntimeError):
                    signing.stapler("validate", Path("Test.app"))
                self.assertEqual(run.call_count, attempts)


class ReleaseEvidenceTests(unittest.TestCase):
    def test_both_archives_require_exact_hash_bound_signing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            downloads, reports = base / "downloads", base / "reports"
            downloads.mkdir()
            for target, suffix in (("macos-arm64", ".zip"), ("macos-x64", ".zip"),
                                   ("windows-x64", ".zip"), ("linux-x64", ".tar.gz")):
                name = f"rockbox-library-manager-v1.4.0-{target}{suffix}"
                archive = downloads / name
                archive.write_bytes(target.encode())
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                (downloads / (name + ".sha256")).write_text(f"{digest}  {name}\n")
                if target.startswith("macos-"):
                    report = reports / f"smoke-reports-{target}/macos-signing.json"
                    report.parent.mkdir(parents=True)
                    report.write_text(json.dumps({**dict.fromkeys(release.CHECKS, True), "archive": name, "sha256": digest}))
            release.checked_assets(downloads, reports, "1.4.0")
            report = reports / "smoke-reports-macos-x64/macos-signing.json"
            data = json.loads(report.read_text())
            for field in ("developer_id", "notarization_ticket", "sha256", "archive", "runtime_checks"):
                report.write_text(json.dumps({**data, field: False}))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    release.checked_assets(downloads, reports, "1.4.0")
