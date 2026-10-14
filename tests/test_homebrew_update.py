import copy
import hashlib
import io
import json
import plistlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import update_homebrew as updater


def cask(version="1.3.0", arm="a" * 64, intel="b" * 64):
    return (updater.MANAGED + 'cask "rockbox-library-manager" do\n'
            f'  version "{version}"\n'
            f'  sha256 arm:   "{arm}",\n         intel: "{intel}"\n'
            '  app "Rockbox Library Manager.app"\nend\n')


def release_fixture(version="1.4.0", bundle_version=None):
    release = {"tag_name": "v" + version, "draft": False, "prerelease": False, "assets": []}
    downloads = {}
    for arch in ("arm64", "x64"):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zipped:
            zipped.writestr("Rockbox Library Manager.app/Contents/Info.plist", plistlib.dumps({
                "CFBundleShortVersionString": bundle_version or version,
                "CFBundleIdentifier": "nl.cyberdelia.rockbox-library-manager",
            }))
            for executable in ("Rockbox Library Manager", "rockbox-library-manager"):
                zipped.writestr("Rockbox Library Manager.app/Contents/MacOS/" + executable, arch)
        name = f"rockbox-library-manager-v{version}-macos-{arch}.zip"
        archive = stream.getvalue()
        digest = hashlib.sha256(archive).hexdigest()
        companion = f"{digest}  {name}\n".encode("ascii")
        for filename, content in ((name, archive), (name + ".sha256", companion)):
            url = f"https://github.com/{updater.REPOSITORY}/releases/download/v{version}/{filename}"
            release["assets"].append({"name": filename, "state": "uploaded", "browser_download_url": url,
                                      "digest": "sha256:" + hashlib.sha256(content).hexdigest()})
            downloads[url] = content
    return release, downloads


class HomebrewUpdateTests(unittest.TestCase):
    def setUp(self):
        self.release, self.downloads = release_fixture()

    def fetch(self, url, limit):
        content = self.downloads[url]
        self.assertLessEqual(len(content), limit)
        return content

    def test_update_preserves_unrelated_cask_content_and_uses_actual_hashes(self):
        before = cask() + "# Keep this manual note\n"
        after, _ = updater.prepare(before, self.release, self.fetch)
        self.assertIn('version "1.4.0"', after)
        self.assertTrue(after.endswith('# Keep this manual note\n'))
        for asset in self.release["assets"][::2]:
            self.assertIn(asset["digest"].split(":")[1], after)
        self.assertEqual(after.count(updater.MANAGED), 1)

    def test_identical_version_and_hashes_are_idempotent(self):
        after, _ = updater.prepare(cask(), self.release, self.fetch)
        repeated, _ = updater.prepare(after, self.release, self.fetch)
        self.assertEqual(repeated, after)

    def test_same_version_rebuilt_archives_refresh_checksums(self):
        after, _ = updater.prepare(cask("1.4.0"), self.release, self.fetch)
        self.assertNotEqual(after, cask("1.4.0"))
        self.assertIn('version "1.4.0"', after)

    def test_initial_update_adds_automation_attribution(self):
        after, _ = updater.prepare(cask().removeprefix(updater.MANAGED), self.release, self.fetch)
        self.assertTrue(after.startswith(updater.MANAGED))

    def test_older_prerelease_and_draft_do_not_download_or_downgrade(self):
        for overrides in ({"tag_name": "v1.2.2"}, {"tag_name": "v1.4.0-beta.1"},
                          {"prerelease": True}, {"draft": True}, {"tag_name": "v1.04.0"}):
            with self.subTest(overrides=overrides):
                release = dict(self.release, **overrides)
                fetch = unittest.mock.Mock(side_effect=AssertionError("Must not download"))
                self.assertEqual(updater.prepare(cask(), release, fetch)[0], cask())
                fetch.assert_not_called()

    def test_versions_are_compared_numerically(self):
        release, downloads = release_fixture("1.10.0")
        after, _ = updater.prepare(cask("1.9.0"), release, lambda url, limit: downloads[url])
        self.assertIn('version "1.10.0"', after)

    def test_missing_architecture_or_companion_refused(self):
        for index in (2, 3):
            with self.subTest(index=index):
                release = copy.deepcopy(self.release)
                del release["assets"][index]
                with self.assertRaisesRegex(ValueError, "Missing"):
                    updater.prepare(cask(), release, self.fetch)

    def test_unfinished_unexpected_or_duplicate_assets_refused(self):
        for field, value in (("state", "new"), ("browser_download_url", "https://example.org/app.zip")):
            release = copy.deepcopy(self.release)
            release["assets"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                updater.prepare(cask(), release, self.fetch)
        release = copy.deepcopy(self.release)
        release["assets"].append(release["assets"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            updater.prepare(cask(), release, self.fetch)

    def test_archive_and_companion_asset_digests_are_verified(self):
        for index in (0, 1):
            release = copy.deepcopy(self.release)
            release["assets"][index]["digest"] = "sha256:" + "0" * 64
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "digest mismatch"):
                updater.prepare(cask(), release, self.fetch)

    def test_companion_hash_and_filename_are_verified(self):
        for content in (b"0" * 64 + b"  wrong.zip\n", b"invalid\n"):
            release = copy.deepcopy(self.release)
            asset = release["assets"][1]
            asset["digest"] = "sha256:" + hashlib.sha256(content).hexdigest()
            downloads = dict(self.downloads, **{asset["browser_download_url"]: content})
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "Companion checksum"):
                updater.prepare(cask(), release, lambda url, limit: downloads[url])

    def test_archive_version_is_verified(self):
        release, downloads = release_fixture(bundle_version="1.3.0")
        with self.assertRaisesRegex(ValueError, "identity/version"):
            updater.prepare(cask(), release, lambda url, limit: downloads[url])

    def test_unexpected_cask_layout_refused(self):
        for before in (cask().replace('version "1.3.0"', 'version :latest'),
                       cask().replace('sha256 arm:', 'sha256 arm64:')):
            with self.subTest(before=before), self.assertRaisesRegex(ValueError, "layout"):
                updater.prepare(before, self.release, self.fetch)

    def test_download_size_is_bounded_and_has_no_authorization_header(self):
        with patch.object(updater.urllib.request, "urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = b"12345"
            with self.assertRaisesRegex(ValueError, "size limit"):
                updater.download("https://example.org/public.zip", 4)
            request = opener.call_args.args[0]
            self.assertIsNone(request.get_header("Authorization"))
            opener.return_value.__enter__.return_value.read.assert_called_once_with(5)

    def test_apply_checks_candidate_and_stale_base_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Casks").mkdir()
            target = root / "Casks" / updater.CASK
            target.write_text(cask())
            candidate = root / updater.CASK
            candidate.write_text(cask("1.4.0"))
            metadata = {"base_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                        "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}
            target.write_text(cask("1.5.0"))
            with self.assertRaisesRegex(ValueError, "changed during validation"):
                updater.apply(root, candidate, metadata)
            self.assertIn('version "1.5.0"', target.read_text())
            target.write_text(cask())
            candidate.write_text(cask("9.9.9"))
            with self.assertRaisesRegex(ValueError, "candidate changed"):
                updater.apply(root, candidate, metadata)
            self.assertEqual(target.read_text(), cask())
            candidate.write_text(cask("1.4.0"))
            self.assertTrue(updater.apply(root, candidate, metadata))
            self.assertFalse(updater.apply(root, candidate, metadata))

    def test_cli_failure_leaves_tap_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Casks").mkdir()
            target = root / "Casks" / updater.CASK
            target.write_text(cask())
            args = ["update_homebrew.py", "--tap", str(root), "--output", str(root / "candidate")]
            with patch("sys.argv", args), patch.object(updater.subprocess, "check_output", return_value=json.dumps({
                    **self.release, "assets": []})):
                with self.assertRaises(ValueError):
                    updater.main()
            self.assertEqual(target.read_text(), cask())
            self.assertFalse((root / "candidate").exists())


if __name__ == "__main__":
    unittest.main()
