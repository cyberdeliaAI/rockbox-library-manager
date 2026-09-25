"""Media scans and real conversions use only temporary test files."""
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen.flac import FLAC, Picture, MetadataBlock
from PIL import Image

from rockbox_manager import media_tools as media

FFMPEG = shutil.which("ffmpeg")


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-media-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "Music"
        self.root.mkdir()
        self.cancel = threading.Event()
        self.cache = self.base / "cache.sqlite3"

    def image(self, name, size):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, "#33AA88").save(path, "JPEG")
        return path

    def scan(self, size=300, **kwargs):
        return media.scan(self.root, size, self.cache, self.cancel, **kwargs)

    def apply(self, candidates, *, backup=True, size=300):
        return media.apply(self.root, candidates, size, self.base / "backups" if backup else None,
                           FFMPEG or "", self.cancel)

    def flac(self, *, rate=96000, bits=24, channels=2):
        path = self.root / "Björk & Friends" / "01 test.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency=997:sample_rate={rate}:duration=0.25", "-ac", str(channels),
                        "-sample_fmt", "s32" if bits == 24 else "s16", "-c:a", "flac", str(path)], check=True)
        return path

    def test_dimensions_and_cached_scan_reacts_to_new_target(self):
        self.image("Artist/folder.jpg", (600, 600))
        self.image("Artist/Small/cover.jpg", (200, 200))
        self.image("Artist/Correct/folder.jpg", (300, 300))
        self.image("Artist/Wide/cover.jpg", (700, 200))
        self.image("Artist/ignored.jpg", (800, 800))
        first = self.scan()
        self.assertEqual((first.checked, first.cached), (4, 0))
        self.assertEqual(sorted(c.kind for c in first.candidates), ["artwork", "small_artwork", "small_artwork"])
        with patch.object(media, "image_info", side_effect=AssertionError("unchanged headers should be cached")):
            second = self.scan(500)
        self.assertEqual(second.cached, 4)
        self.assertEqual(len(second.candidates), 4)
        self.assertFalse(second.errors)
        fresh = self.scan(fresh=True)
        self.assertEqual(fresh.cached, 0)

    def test_resize_creates_verified_recoverable_backup_and_invalidates_cache(self):
        source = self.image("Artist/Album/cover.jpg", (800, 500))
        original = source.read_bytes()
        result = self.apply(self.scan().candidates)
        self.assertEqual(result.completed, ["Artist/Album/cover.jpg"])
        self.assertFalse(result.errors)
        self.assertEqual((result.backup_folder / "originals/Artist/Album/cover.jpg").read_bytes(), original)
        manifest = json.loads((result.backup_folder / "manifest.json").read_text())
        self.assertEqual(manifest["files"][0]["sha256"], media.digest(result.backup_folder / "originals/Artist/Album/cover.jpg", self.cancel))
        with Image.open(source) as output:
            self.assertEqual(output.size, (300, 300))
            self.assertFalse(output.info.get("progressive"))
        second = self.scan()
        self.assertEqual((second.cached, second.candidates), (0, []))

    def test_backup_disabled_replaces_without_creating_backup_directory(self):
        source = self.image("folder.jpg", (600, 600))
        result = self.apply(self.scan().candidates, backup=False)
        self.assertEqual(len(result.completed), 1)
        self.assertIsNone(result.backup_folder)
        self.assertFalse((self.base / "backups").exists())
        with Image.open(source) as output:
            self.assertEqual(output.size, (300, 300))

    def test_small_images_never_upscaled(self):
        source = self.image("folder.jpg", (200, 200))
        before = source.read_bytes()
        with self.assertRaisesRegex(media.MediaError, "Small artwork"):
            self.apply(self.scan().candidates)
        self.assertEqual(source.read_bytes(), before)

    def test_stale_preview_and_changed_during_conversion_preserve_current_file(self):
        source = self.image("folder.jpg", (600, 600))
        plan = self.scan().candidates
        source.write_bytes(source.read_bytes() + b"changed")
        before = source.read_bytes()
        result = self.apply(plan)
        self.assertIn("changed since scan", result.errors[0])
        self.assertEqual(source.read_bytes(), before)
        plan = self.scan().candidates
        resize = media.resize_artwork
        def changed(src, dst, size):
            resize(src, dst, size)
            src.write_bytes(before + b"again")
        with patch.object(media, "resize_artwork", side_effect=changed):
            result = self.apply(plan)
        self.assertIn("changed during conversion", result.errors[0])
        self.assertEqual(source.read_bytes(), before + b"again")
        self.assertFalse(list(self.root.glob(".rlm-*")))

    def test_backup_failure_and_cancel_leave_original(self):
        source = self.image("folder.jpg", (600, 600))
        plan = self.scan().candidates
        before = source.read_bytes()
        with patch.object(media, "backup_file", side_effect=OSError("Disk full")):
            result = self.apply(plan)
        self.assertEqual(source.read_bytes(), before)
        self.assertIn("Disk full", result.errors[0])
        resize = media.resize_artwork
        def cancel(src, dst, size):
            resize(src, dst, size)
            self.cancel.set()
        with patch.object(media, "resize_artwork", side_effect=cancel):
            result = self.apply(plan)
        self.assertTrue(result.cancelled)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(list(self.root.glob(".rlm-*")))

    def test_bad_files_are_reported_and_other_files_remain_actionable(self):
        (self.root / "bad.flac").write_bytes(b"not a FLAC")
        (self.root / "folder.jpg").write_bytes(b"not a JPEG")
        self.image("Good/cover.jpg", (500, 500))
        result = self.scan()
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(len(result.candidates), 1)
        self.cancel.set()
        result = self.scan()
        self.assertTrue(result.cancelled)
        self.assertEqual(result.checked, 0)

    def test_links_outside_root_and_backup_inside_root_are_refused(self):
        source = self.image("folder.jpg", (600, 600))
        plan = self.scan().candidates
        with self.assertRaisesRegex(media.MediaError, "outside the music folder"):
            media.apply(self.root, plan, 300, self.root / "backups", "", self.cancel)
        with self.assertRaises(media.MediaError):
            media.safe_path(self.root, "../outside.jpg")
        outside = self.base / "outside.jpg"
        source.rename(outside)
        try:
            source.symlink_to(outside)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this host")
        before = outside.read_bytes()
        result = self.apply(plan)
        self.assertTrue(result.errors)
        self.assertEqual(outside.read_bytes(), before)

    @unittest.skipUnless(FFMPEG, "FFmpeg required for real audio conversion")
    def test_real_audio_conversion_preserves_tags_cover_and_duration(self):
        source = self.flac()
        audio = FLAC(source)
        audio["artist"] = ["Björk", "Another artist"]
        audio["custom_field"] = ["Original value"]
        audio["replaygain_track_gain"] = ["-5.0 dB"]
        pic = Picture()
        pic.type, pic.mime, pic.width, pic.height, pic.depth = 3, "image/jpeg", 10, 10, 24
        buf = io.BytesIO(); Image.new("RGB", (10, 10), "blue").save(buf, "JPEG")
        pic.data = buf.getvalue()
        audio.add_picture(pic)
        audio.save()
        before = source.read_bytes()
        result = self.apply(self.scan().candidates)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.completed), 1)
        output = FLAC(source)
        self.assertEqual((output.info.bits_per_sample, output.info.sample_rate, output.info.channels), (16, 44100, 2))
        self.assertAlmostEqual(output.info.length, 0.25, places=4)
        self.assertEqual(dict(output.tags), dict(audio.tags))
        self.assertEqual([p.write() for p in output.pictures], [pic.write()])
        self.assertEqual((result.backup_folder / "originals" / result.completed[0]).read_bytes(), before)
        self.assertFalse(self.scan().candidates)

    @unittest.skipUnless(FFMPEG, "FFmpeg required for real audio conversion")
    def test_cd_audio_is_unchanged_and_lower_sample_rates_are_not_upsampled(self):
        source = self.flac(rate=44100, bits=16)
        self.assertFalse(self.scan().candidates)
        source.unlink()
        source = self.flac(rate=22050, bits=24, channels=1)
        result = self.apply(self.scan().candidates, backup=False)
        self.assertFalse(result.errors)
        info = media.audio_info(source)
        self.assertEqual((info["rate"], info["bits"], info["channels"]), (22050, 16, 1))

    @unittest.skipUnless(FFMPEG, "FFmpeg required for damaged audio test")
    def test_truncated_audio_and_opaque_metadata_are_not_replaced(self):
        source = self.flac()
        source.write_bytes(source.read_bytes()[:-100])
        before = source.read_bytes()
        result = self.apply(self.scan().candidates)
        self.assertTrue(result.errors)
        self.assertEqual(source.read_bytes(), before)
        source.unlink()
        source = self.flac()
        audio = FLAC(source)
        block = MetadataBlock(b"testapplication"); block.code = 2
        audio.metadata_blocks.append(block); audio.save()
        before = source.read_bytes()
        result = self.apply(self.scan().candidates)
        self.assertIn("manual conversion", result.errors[0])
        self.assertEqual(source.read_bytes(), before)

    def test_missing_ffmpeg_fails_before_changes_but_artwork_still_works(self):
        with patch.object(media.shutil, "which", return_value=None):
            with self.assertRaisesRegex(media.MediaError, "not found"):
                media.find_ffmpeg("missing")
            self.image("cover.jpg", (500, 500))
            self.assertFalse(self.apply(self.scan().candidates).errors)

    def test_cancellation_terminates_ffmpeg_before_returning(self):
        with patch.object(media.subprocess, "Popen") as popen:
            process = popen.return_value
            process.poll.return_value = None
            def wait(timeout=None):
                if timeout is not None:
                    self.cancel.set()
                    raise subprocess.TimeoutExpired("ffmpeg", timeout)
                return -1
            process.wait.side_effect = wait
            with self.assertRaises(media.Cancelled):
                media.run_ffmpeg("ffmpeg", [], self.cancel)
            process.kill.assert_called_once()
            self.assertEqual(process.wait.call_count, 2)

    def test_backups_on_detected_player_are_rejected(self):
        (self.base / ".rockbox").mkdir()
        self.image("folder.jpg", (600, 600))
        with self.assertRaisesRegex(media.MediaError, "outside the Rockbox player"):
            self.apply(self.scan().candidates)

    @unittest.skipUnless(FFMPEG, "FFmpeg required for multichannel test")
    def test_multichannel_is_reported_without_automatic_downmix(self):
        self.flac(channels=6)
        result = self.scan()
        self.assertFalse(result.candidates)
        self.assertIn("Multichannel", result.errors[0])


if __name__ == "__main__":
    unittest.main()
