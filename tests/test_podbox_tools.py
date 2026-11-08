"""Real PodBox engines and failure recovery on temporary players only."""
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen.flac import FLAC
from PIL import Image

from rockbox_manager import podbox_tools as pb
from test_tags_health import track

try:
    ENGINES = pb.engine_directory()
except pb.PodBoxError:
    ENGINES = None


def make_player(base):
    device = base / "Player"
    library = device / ".rockbox/library"
    library.mkdir(parents=True)
    (library.parent / "rockbox-info.txt").write_text("Target: ipodvideo\nVersion: fixture\n")
    (library / "format.dat").write_bytes(struct.pack("<IHHIiiiI", 0x544F594C, 5, 1, 0, -1, -1, -1, 0xFFFFFFFF))
    (library / "user").mkdir()
    (library / "user/player_name.txt").write_text("Preserve me")
    album = device / "Music/Artist/Album"
    for number in (1, 2):
        track(album, f"0{number}.flac", artist="Artist", albumartist="Artist", album="Album", title=f"Track {number}",
              genre="Rock", date="2020", tracknumber=str(number))
    Image.new("RGB", (500, 500), "red").save(album / "folder.jpg")
    return device, album


class PodBoxValidationTests(unittest.TestCase):
    def test_missing_master_is_not_mistaken_for_an_empty_new_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            folder = library / "database"
            folder.mkdir(parents=True)
            self.assertIsNone(pb._database(library))
            (folder / "database_0.tcd").write_bytes(b"unfinished existing data")
            with self.assertRaisesRegex(pb.PodBoxError, "no master"):
                pb._database(library)

    def test_crc_matches_non_reflected_reference_and_empty_format(self):
        self.assertEqual(pb.libfile_crc(b"123456789"), 0x0376E6E7)
        self.assertEqual(pb.libfile_crc(b""), 0xFFFFFFFF)

    def test_unsafe_fat_names_and_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            for name in ("../outside", "/absolute", "a/../b", "a\\b", "a:stream", "a.", "a "):
                with self.subTest(name=name), self.assertRaises(pb.PodBoxError):
                    pb._safe_name(name)
            if os.name != "nt":
                (base / "link").symlink_to(base / "elsewhere")
                with self.assertRaisesRegex(pb.PodBoxError, "Symlinks"):
                    pb.manifest(base)


@unittest.skipUnless(ENGINES, "Build the pinned PodBox engines (scripts/build_podbox.py) for native checks")
class NativePodBoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-podbox-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.device, self.album = make_player(self.base)
        self.library = self.device / ".rockbox/library"
        self.backups = self.base / "backups"
        self.plans = []

    def preview(self, operation="update", **kwargs):
        plan = pb.preview(self.device, operation, self.base / "work", timestamp_offset=pb.fat_offset(), **kwargs)
        self.plans.append(plan)
        return plan

    def install(self, operation="update"):
        plan = self.preview(operation)
        pb.apply(plan, self.backups)
        return plan

    def test_create_preview_is_readonly_and_native_unchanged_scan_is_noop(self):
        before = pb.manifest(self.device)
        plan = self.preview()
        self.assertEqual(pb.manifest(self.device), before)
        self.assertEqual(len(plan.changes), 11)
        db = pb._database(plan.library)
        self.assertEqual({db.text(r, 4) for r in db.rows},
                         {f"/<HDD0>/Music/Artist/Album/0{i}.flac" for i in (1, 2)})
        backup = pb.apply(plan, self.backups)
        pb.read_backup(backup, self.device)
        self.assertEqual(self.preview().changes, [])
        self.assertEqual((self.library / "user/player_name.txt").read_text(), "Preserve me")

    def test_add_change_delete_and_rebuild_preserve_seeded_runtime(self):
        self.install()
        db = pb._database(self.library)
        row = next(i for i, r in enumerate(db.rows) if db.text(r, 4).endswith("01.flac"))
        master = bytearray(db.files[pb.database.MASTER])
        expected = (42, 7, 123456, 1234, 4, 567, 890)
        for field, value in zip((15, 16, 17, 18, 19, 21, 22), expected):
            struct.pack_into("<i", master, 24 + row * 96 + field * 4, value)
        struct.pack_into("<i", master, 24 + row * 96 + 23 * 4, db.rows[row][23] | 4)
        (self.library / "database" / pb.database.MASTER).write_bytes(master)
        source = self.album / "01.flac"
        old_time = source.stat().st_mtime
        audio = FLAC(source)
        audio["title"] = ["Changed title"]
        audio.save()
        os.utime(source, (old_time + 4, old_time + 4))
        (self.album / "02.flac").unlink()
        track(self.album, "03.flac", artist="Artist", albumartist="Artist", album="Album", title="New", tracknumber="3")
        self.install()
        db = pb._database(self.library)
        active = [r for r in db.rows if not r[23] & 1]
        self.assertEqual(len(active), 2)
        first = next(r for r in active if db.text(r, 4).endswith("01.flac"))
        self.assertEqual(db.text(first, 3), "Changed title")
        self.assertEqual(tuple(first[i] for i in (15, 16, 17, 18, 21, 22)), expected[:4] + expected[5:])
        self.assertEqual(first[19], expected[4])
        self.install("rebuild")
        final = pb._runtime(pb._database(self.library))
        self.assertEqual(final["/<HDD0>/Music/Artist/Album/01.flac"][:4] + final["/<HDD0>/Music/Artist/Album/01.flac"][5:],
                         expected[:4] + expected[5:])
        self.assertFalse(any("02.flac" in p for p in final))
        self.assertEqual(final["/<HDD0>/Music/Artist/Album/01.flac"][4], expected[4])

    def test_artwork_uses_actual_player_headers_pixels_and_reuses_thumbnails(self):
        self.install()
        before = pb.manifest(self.device)
        plan = self.preview("artwork")
        self.assertEqual(pb.manifest(self.device), before)
        for size, dimension, layout in (("coverflow", 128, 1), ("list", 44, 0), ("wps", 300, 0)):
            files = [p for p in (plan.library / f"cache/art/{size}").glob("*.aat") if p.name != "fallback.aat"]
            self.assertEqual(len(files), 1)
            raw = files[0].read_bytes()
            self.assertEqual(struct.unpack_from("<6H", raw), (0x5441, 8, dimension, dimension, layout, 0))
            pixel = struct.unpack_from("<H", raw, 12)[0]
            # The player's JPEG decoder/scaler quantizes red slightly. Verify
            # native RGB565 channel placement rather than Pillow's RGB output.
            self.assertGreaterEqual(pixel >> 11, 28)
            self.assertEqual(pixel & 0x7FF, 0)
        pb.apply(plan, self.backups)
        again = self.preview("artwork")
        self.assertFalse(any(p.endswith(".aat") for p in again.changes))
        self.assertIn("0 thumbnails written or removed", again.output)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed to generate real soundscan test audio")
    def test_soundscan_runs_real_host_decoder_and_player_analysis(self):
        source = self.album / "tone.flac"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=44100:duration=55", str(source)], check=True, timeout=30)
        before = pb.manifest(self.device)
        plan = self.preview("soundscan")
        self.assertEqual(pb.manifest(self.device), before)
        raw = (plan.library / "sound.dat").read_bytes()
        count = struct.unpack_from("<I", raw, 8)[0]
        self.assertEqual(count, 1)
        record = raw[16:80]
        self.assertEqual(struct.unpack_from("<I", record, 8)[0], 0)  # explicit unknown mtime
        self.assertEqual(struct.unpack_from("<I", record, 12)[0], source.stat().st_size)
        self.assertGreaterEqual(record[39], 39)
        self.assertFalse(record[40] & 8)  # not SOUND_F_FAILED
        self.assertTrue(-1000 < struct.unpack_from("<h", record, 24)[0] < 0)
        pb.apply(plan, self.backups)
        self.assertIn("1 already current", self.preview("soundscan").output)

    def test_stale_music_configuration_library_and_preview_are_refused(self):
        plan = self.preview()
        source = self.album / "01.flac"
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        with self.assertRaisesRegex(pb.PodBoxError, "changed"):
            pb.apply(plan, self.backups)
        self.assertFalse(self.backups.exists())
        plan = self.preview()
        (self.device / ".rockbox/config.cfg").write_text("year from folder: on\n")
        with self.assertRaisesRegex(pb.PodBoxError, "changed"):
            pb.apply(plan, self.backups)
        plan = self.preview()
        (self.library / "user/player_name.txt").write_text("changed")
        with self.assertRaisesRegex(pb.PodBoxError, "changed"):
            pb.apply(plan, self.backups)
        plan = self.preview()
        (plan.library / "database/database_idx.tcd").write_bytes(b"corrupt")
        with self.assertRaisesRegex(pb.PodBoxError, "preview files changed"):
            pb.apply(plan, self.backups)

    def test_swap_failure_recovers_original_and_backup_restore_preserves_music(self):
        original, music = pb.manifest(self.library), pb.manifest(self.device / "Music")
        plan = self.preview()
        replace = os.replace
        def fail_second_rename(source, target):
            if Path(source).name.startswith(".rlm-library-stage-"):
                raise OSError("simulated unplug after first rename")
            return replace(source, target)
        with patch.object(pb.os, "replace", side_effect=fail_second_rename):
            with self.assertRaisesRegex(pb.PodBoxError, "use recovery"):
                pb.apply(plan, self.backups)
        self.assertFalse(self.library.exists())
        self.assertIn("recovered", pb.recover(self.device))
        self.assertEqual(pb.manifest(self.library), original)
        self.assertEqual(pb.manifest(self.device / "Music"), music)
        backup = pb.apply(plan, self.backups)
        pb.restore(self.device, backup, self.backups)
        self.assertEqual(pb.manifest(self.library), original)
        self.assertEqual(pb.manifest(self.device / "Music"), music)
        self.assertGreaterEqual(len(list(self.backups.glob("*/manifest.json"))), 3)

    def test_music_changed_during_device_staging_prevents_swap(self):
        plan = self.preview()
        original = pb.manifest(self.library)
        copy = pb._copy_tree
        def change_after_staging(source, destination, **kwargs):
            copy(source, destination, **kwargs)
            if destination.name.startswith(".rlm-library-stage-"):
                path = self.album / "01.flac"
                stat = path.stat()
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        with patch.object(pb, "_copy_tree", side_effect=change_after_staging):
            with self.assertRaisesRegex(pb.PodBoxError, "changed while staging"):
                pb.apply(plan, self.backups)
        self.assertEqual(pb.manifest(self.library), original)
        self.assertIn("recovered", pb.recover(self.device))
        self.assertEqual(pb.manifest(self.library), original)

    def test_unknown_layout_target_cancel_and_damaged_backup_fail_without_writes(self):
        cancel = threading.Event()
        cancel.set()
        before = pb.manifest(self.device)
        with self.assertRaisesRegex(pb.PodBoxError, "cancelled"):
            self.preview(cancel=cancel)
        self.assertEqual(pb.manifest(self.device), before)
        path = self.library / "format.dat"
        original = path.read_bytes()
        path.write_bytes(original[:4] + b"\x06\0" + original[6:])
        with self.assertRaisesRegex(pb.PodBoxError, "layout 5"):
            self.preview()
        path.write_bytes(original)
        info = self.device / ".rockbox/rockbox-info.txt"
        info.write_text("Target: sansaclip\n")
        with self.assertRaisesRegex(pb.PodBoxError, "Video and Classic"):
            self.preview()
        info.write_text("Target: ipodvideo\n")
        backup = pb.apply(self.preview(), self.backups)
        (backup / "library/format.dat").write_bytes(b"bad")
        current = pb.manifest(self.library)
        with self.assertRaisesRegex(pb.PodBoxError, "damaged"):
            pb.restore(self.device, backup, self.backups)
        self.assertEqual(pb.manifest(self.library), current)

    def test_actual_c_overlay_denies_source_mutations_and_allows_only_local_library(self):
        if not os.environ.get("RLM_TEST_OVERLAY_PROBE"):
            self.skipTest("Set RLM_TEST_OVERLAY_PROBE=1 after building the current adapter probe")
        (self.device / "Music/sentinel").write_text("keep")
        (self.device / "Music/empty").mkdir()
        before = pb.manifest(self.device)
        overlay = self.base / "overlay"
        (overlay / ".rockbox/library").mkdir(parents=True)
        env = dict(os.environ, RLM_PODBOX_STAGE=overlay.as_posix())
        binary = ENGINES / ("database_pb.exe" if os.name == "nt" else "database_pb")
        for root in (self.device.as_posix(), self.device.as_posix() + "/"):
            result = subprocess.run([str(binary), "--rlm-overlay-check", root], env=env, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(pb.manifest(self.device), before)
        self.assertEqual((overlay / ".rockbox/library/overlay-probe").read_text(), "ok")
        env["RLM_PODBOX_STAGE"] = self.device.as_posix()
        result = subprocess.run([str(binary), "--rlm-overlay-check", self.device.as_posix()], env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 88)
        self.assertEqual(pb.manifest(self.device), before)
