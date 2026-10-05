"""Real FLAC tags and explicit binary fixtures; every device is a temp folder."""
import hashlib
import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from mutagen.flac import FLAC

from rockbox_manager import rockbox_database as rb


def make_device(base: Path, endian="<", paths=None):
    device = base / "IPOD"
    folder = device / ".rockbox"
    folder.mkdir(parents=True)
    (folder / "rockbox-info.txt").write_text("Target: ipodvideo\nTarget id: 15\nVersion: -260920\n")
    paths = paths or ["/Music/Old Artist/Album/01.flac", "/Music/Other/Other Album/02.flac"]
    names = ["Old Artist", "Other"]
    tracks = []
    values = []
    for i, path in enumerate(paths):
        local_path = path.removeprefix("/<HDD0>")
        if local_path.startswith("/Music/") and ".." not in local_path:
            track = device / local_path.lstrip("/")
            track.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(__file__).parent / "fixtures/silence.flac", track)
            audio = FLAC(track)
            for key, value in {"artist": names[i], "albumartist": names[i], "album": "Album",
                               "genre": "Blues", "date": "2020", "title": f"Track {i}"}.items():
                audio[key] = [value]
            audio.save()
            tracks.append(track)
        values.append({0: names[i], 1: "Album", 2: "Blues", 3: f"Track {i}", 4: path,
                       5: "<Untagged>", 6: "<Untagged>", 7: names[i], 8: f"Track {i}", 12: names[i]})
    # Explicit wire layout: 23 fields and a flag, independent of the encoder.
    rows = [[0] * 24 for _ in values]
    for i, row in enumerate(rows):
        row[9:12] = [2020, 1, i + 1]
        row[13:23] = [320, 180000, 42 + i, 7, 1200000, 1700000000, 11, 1700000000, 81000, 131072]
        row[23] = 4  # Runtime data has been modified by Rockbox.
    rows.append([123456] * 23 + [1])  # Deleted record has CRCs, not string offsets.
    total = 24 + len(rows) * 96
    for tag in [0, 1, 2, 3, 4, 5, 6, 7, 8, 12]:
        entries = []
        for i, tags in enumerate(values):
            data = tags[tag].encode() + b"\0"
            if tag != 4:
                data += b"X" * (-len(data) % 8)
            entries.append((tags[tag], i, data))
        if tag != 4:
            entries.sort(key=lambda e: e[0].encode().lower())
        body = bytearray()
        unique = {}
        count = 0
        for text, i, data in entries:
            if tag not in [3, 4] and text.lower() in unique:
                rows[i][tag] = unique[text.lower()]
                continue
            offset = 12 + len(body)
            rows[i][tag] = offset
            unique[text.lower()] = offset
            body += struct.pack(endian + "ii", len(data), i if tag in [3, 4] else -1) + data
            count += 1
        (folder / f"database_{tag}.tcd").write_bytes(struct.pack(endian + "iii", 0x54434810, len(body), count) + body)
        if tag != 4:
            total += len(body)
    (folder / "database_idx.tcd").write_bytes(struct.pack(endian + "6i", 0x54434810, total, len(rows), 71, 12, 0)
                                           + b"".join(struct.pack(endian + "24i", *r) for r in rows))
    (folder / "database_state.tcd").write_bytes(b"old RAM cache")
    (folder / "database_changelog.txt").write_text("## Changelog version 1\n")
    return device, tracks


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rockbox-db-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.device, self.tracks = make_device(self.base)
        self.music = self.device / "Music"
        self.backups = self.base / "local-backups"

    def edit(self):
        audio = FLAC(self.tracks[0])
        audio["artist"] = ["Ali Farka Touré"]
        audio["albumartist"] = ["Ali Farka Touré"]
        audio["album"] = ["New Album Name"]
        audio["date"] = ["2026-09-20"]
        audio.save()

    def test_backup_is_local_complete_verified_and_unique(self):
        before = rb.snapshot(self.device)
        one = rb.backup_database(self.device, self.backups)
        two = rb.backup_database(self.device, self.backups)
        self.assertNotEqual(one, two)
        self.assertEqual(rb.read_backup(one)[1], before)
        self.assertEqual(rb.snapshot(self.device), before)
        self.assertTrue(one.is_relative_to(self.backups))
        manifest = json.loads((one / "manifest.json").read_text())
        self.assertEqual(manifest["files"][rb.MASTER]["sha256"], hashlib.sha256(before[rb.MASTER]).hexdigest())

    def test_backup_on_ipod_or_via_symlink_is_rejected(self):
        with self.assertRaises(rb.DatabaseError):
            rb.backup_database(self.device, self.device / "Backups")
        link = self.base / "fake-local"
        try:
            link.symlink_to(self.device, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(rb.DatabaseError):
            rb.backup_database(self.device, link / "Backups")

    def test_no_changes_do_not_write_database_or_make_backup(self):
        before = rb.snapshot(self.device)
        plan = rb.preview_update(self.music)
        self.assertEqual(plan.changed_tracks, 0)
        self.assertIsNone(rb.apply_update(plan, self.backups))
        self.assertFalse(self.backups.exists())
        self.assertEqual(rb.snapshot(self.device), before)

    def test_preview_apply_restore_keep_runtime_data_and_music_bytes(self):
        before = rb.snapshot(self.device)
        self.edit()
        music_bytes = [p.read_bytes() for p in self.tracks]
        plan = rb.preview_update(self.music)
        self.assertEqual(plan.changed_tracks, 1)
        self.assertEqual(rb.snapshot(self.device), before)
        backup = rb.apply_update(plan, self.backups)
        self.assertEqual(rb.read_backup(backup)[1], before)
        after = rb.snapshot(self.device)
        # Check the raw row bytes, independently of TagDatabase.parse.
        old_rows = list(struct.iter_unpack("<24i", before[rb.MASTER][24:]))
        new_rows = list(struct.iter_unpack("<24i", after[rb.MASTER][24:]))
        for old, new in zip(old_rows, new_rows):
            self.assertEqual(old[13:], new[13:])
        self.assertEqual(old_rows[2], new_rows[2])
        self.assertEqual(new_rows[0][9], 2026)
        self.assertEqual(after[rb.MASTER][12:24], before[rb.MASTER][12:24])
        for tag, expected in [(0, "Ali Farka Touré"), (1, "New Album Name"), (12, "Ali Farka Touré")]:
            offset = new_rows[0][tag]
            data = after[f"database_{tag}.tcd"]
            self.assertEqual(data[offset + 8:].split(b"\0", 1)[0].decode(), expected)
        self.assertNotIn(rb.STATE, after)
        self.assertEqual([p.read_bytes() for p in self.tracks], music_bytes)
        safety = rb.restore_backup(self.device, backup, self.backups)
        self.assertEqual(rb.read_backup(safety)[1], after)
        self.assertEqual(rb.snapshot(self.device), {n: b for n, b in before.items() if n != rb.STATE})
        self.assertEqual([p.read_bytes() for p in self.tracks], music_bytes)

    def test_big_endian_format_and_sorted_unique_tables(self):
        device, tracks = make_device(self.base / "big-endian", endian=">")
        audio = FLAC(tracks[0]); audio["artist"] = ["Zebra"]; audio.save()
        plan = rb.preview_update(device / "Music")
        backup = rb.apply_update(plan, self.backups)
        db = rb.TagDatabase.parse(rb.snapshot(device))
        self.assertEqual(db.endian, ">")
        self.assertEqual([v[0] for v in db.texts[0].values()], ["Other", "Zebra"])
        self.assertEqual(db.rows[0][15:23], [42, 7, 1200000, 1700000000, 11, 1700000000, 81000, 131072])
        rb.restore_backup(device, backup, self.backups)

    def test_recorded_folder_move_updates_path_and_reverse_index(self):
        source = self.music / "Old Artist"
        destination = self.music / "Canonical Artist"
        source.rename(destination)
        plan = rb.preview_update(self.music, moves=[[str(source), str(destination)]])
        self.assertEqual(plan.changed_tracks, 1)
        rb.apply_update(plan, self.backups)
        db = rb.TagDatabase.parse(rb.snapshot(self.device))
        self.assertEqual(db.text(db.rows[0], 4), "/Music/Canonical Artist/Album/01.flac")
        self.assertEqual(db.texts[4][db.rows[0][4]][1], 0)

    def test_hdd0_preview_apply_restore_preserve_paths_and_runtime(self):
        paths = ["/<HDD0>/Music/Old Artist/Album/01.flac", "/<HDD0>/Music/Other/Album/02.flac"]
        for endian in ("<", ">"):
            with self.subTest(endian=endian):
                device, tracks = make_device(self.base / ("little" if endian == "<" else "big"), endian=endian, paths=paths)
                before = rb.snapshot(device)
                unchanged = rb.preview_update(device / "Music")
                self.assertEqual(unchanged.checked_tracks, 2)
                self.assertEqual(unchanged.changed_tracks, 0)
                self.assertEqual(unchanged.after, before)
                audio = FLAC(tracks[0]); audio["album"] = ["Updated Album"]; audio.save()
                music_bytes = tracks[0].read_bytes()
                plan = rb.preview_update(device / "Music")
                self.assertEqual(plan.changed_tracks, 1)
                self.assertEqual(plan.after["database_4.tcd"], before["database_4.tcd"])
                backup = rb.apply_update(plan, self.backups)
                self.assertEqual(rb.read_backup(backup)[1], before)
                db = rb.TagDatabase.parse(rb.snapshot(device))
                self.assertEqual(db.text(db.rows[0], 1), "Updated Album")
                self.assertEqual([db.text(row, 4) for row in db.rows if not row[23] & 1], paths)
                old_rows = list(struct.iter_unpack(endian + "24i", before[rb.MASTER][24:]))
                self.assertEqual([row[13:] for row in db.rows], [list(row[13:]) for row in old_rows])
                self.assertEqual(tracks[0].read_bytes(), music_bytes)
                rb.restore_backup(device, backup, self.backups)
                self.assertEqual(rb.snapshot(device), {n: b for n, b in before.items() if n != rb.STATE})

    def test_hdd0_recorded_move_preserves_volume_and_reverse_indices(self):
        paths = ["/<HDD0>/Music/Old Artist/Album/01.flac", "/Music/Other/Album/02.flac"]
        device, _ = make_device(self.base / "volume-move", paths=paths)
        source = device / "Music/Old Artist"
        destination = device / "Music/New Artist"
        source.rename(destination)
        plan = rb.preview_update(device / "Music", moves=[[str(source), str(destination)]])
        self.assertEqual(plan.changed_tracks, 1)
        rb.apply_update(plan, self.backups)
        db = rb.TagDatabase.parse(rb.snapshot(device))
        self.assertEqual(db.text(db.rows[0], 4), "/<HDD0>/Music/New Artist/Album/01.flac")
        self.assertEqual(db.text(db.rows[1], 4), paths[1])
        for i in (0, 1):
            self.assertEqual(db.texts[4][db.rows[i][4]][1], i)

    def test_volume_aliases_cannot_update_the_same_file_twice(self):
        device, _ = make_device(self.base / "aliases", paths=[
            "/<HDD0>/Music/Old Artist/Album/01.flac", "/Music/Old Artist/Album/01.flac"])
        before = rb.snapshot(device)
        with self.assertRaisesRegex(rb.DatabaseError, "same local file"):
            rb.preview_update(device / "Music")
        self.assertEqual(rb.snapshot(device), before)
        self.assertFalse(self.backups.exists())

    def test_volume_mapping_stays_on_windows_device_and_rejects_unsafe_paths(self):
        prefix, parts = rb._split_device_path("/<HDD0>/Music/Artist/Album/01.flac")
        self.assertEqual(prefix, "/<HDD0>")
        self.assertEqual(PureWindowsPath("D:/").joinpath(*parts), PureWindowsPath("D:/Music/Artist/Album/01.flac"))
        for name in ["/<HDD1>/Music/01.flac", "/<microSD0>/Music/01.flac", "/<HDD0>",
                     "/<HDD0>/../outside.flac", "/<HDD0>/Music/../../outside.flac",
                     "/<HDD0>/E:/Music/01.flac", "/<HDD0>/Music/01.flac:stream",
                     "/<HDD0>/Music/\\outside.flac", "//<HDD0>/Music/01.flac",
                     "/<HDD0>/<HDD0>/Music/01.flac", "/<HDD0>/Music/\0.flac"]:
            with self.subTest(path=name), self.assertRaises(rb.DatabaseError):
                rb._device_path(self.device, name)

    def test_hdd0_symlink_cannot_escape_device(self):
        link = self.music / "outside"
        try:
            link.symlink_to(self.base, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaisesRegex(rb.DatabaseError, "escapes"):
            rb._device_path(self.device, "/<HDD0>/Music/outside/private.flac")

    def test_missing_track_or_multivalue_tags_abort_preview(self):
        before = rb.snapshot(self.device)
        audio = FLAC(self.tracks[0]); audio["artist"] = ["One", "Two"]; audio.save()
        with self.assertRaisesRegex(rb.DatabaseError, "Multiple"):
            rb.preview_update(self.music)
        self.tracks[0].unlink()
        with self.assertRaisesRegex(rb.DatabaseError, "missing"):
            rb.preview_update(self.music)
        self.assertEqual(rb.snapshot(self.device), before)

    def test_unsupported_pending_dirty_and_bad_offsets_are_rejected(self):
        before = rb.snapshot(self.device)
        variants = []
        for pos, value in [(0, 0x5443480F), (20, 1), (24, 9999999), (4, 0)]:
            damaged = dict(before); data = bytearray(damaged[rb.MASTER])
            struct.pack_into("<i", data, pos, value); damaged[rb.MASTER] = bytes(data)
            variants.append(damaged)
        variants.extend([{**before, "database_tmp.tcd": b"pending"}, {**before, "database_99.tcd": b"unknown"}])
        for damaged in variants:
            with self.subTest(files=list(damaged)), self.assertRaises(rb.DatabaseError):
                rb.TagDatabase.parse(damaged)

    def test_symlink_database_file_and_escaping_track_are_rejected(self):
        p = self.device / ".rockbox/database_changelog.txt"
        outside = self.base / "outside.txt"; outside.write_text("private")
        p.unlink()
        try:
            p.symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(rb.DatabaseError):
            rb.backup_database(self.device, self.backups)
        device, _ = make_device(self.base / "escaping", paths=["/Music/../../outside.flac", "/Music/Other/02.flac"])
        with self.assertRaisesRegex(rb.DatabaseError, "Unsupported device path"):
            rb.preview_update(device / "Music")

    def test_backup_failure_prevents_any_database_write(self):
        self.edit(); before = rb.snapshot(self.device); plan = rb.preview_update(self.music)
        real_write = rb._write_sync
        def fail_backup(path, data):
            if path.parent.name.endswith(".partial"):
                raise OSError("Disk full")
            return real_write(path, data)
        with patch.object(rb, "_write_sync", side_effect=fail_backup), self.assertRaises(OSError):
            rb.apply_update(plan, self.backups)
        self.assertEqual(rb.snapshot(self.device), before)
        self.assertFalse(list(self.backups.glob("*.partial")))
        self.assertFalse((self.device / ".rockbox" / rb.JOURNAL).exists())

    def test_database_or_tags_changed_after_preview_block_apply(self):
        self.edit(); plan = rb.preview_update(self.music)
        audio = FLAC(self.tracks[0]); audio["album"] = ["Edited again"]; audio.save()
        # Reproduce coarse/preserved timestamps, rather than relying on elapsed
        # wall-clock time between two tag writes on the CI filesystem.
        stamp = plan.track_stamps[self.tracks[0].resolve()]
        self.assertEqual(self.tracks[0].stat().st_size, stamp[0])
        rb.os.utime(self.tracks[0], ns=(self.tracks[0].stat().st_atime_ns, stamp[1]))
        with self.assertRaisesRegex(rb.DatabaseError, "Music files changed"):
            rb.apply_update(plan, self.backups)
        plan = rb.preview_update(self.music)
        p = self.device / ".rockbox/database_changelog.txt"; p.write_text("new play history")
        before = rb.snapshot(self.device)
        with self.assertRaisesRegex(rb.DatabaseError, "database changed"):
            rb.apply_update(plan, self.backups)
        self.assertEqual(rb.snapshot(self.device), before)

    def test_write_failure_rolls_back_and_keeps_local_backup(self):
        self.edit(); before = rb.snapshot(self.device); plan = rb.preview_update(self.music)
        real_replace = rb.os.replace; failed = False
        def fail_once(src, dst):
            nonlocal failed
            if Path(dst).name == "database_7.tcd" and not failed:
                failed = True
                raise OSError("Simulated write error")
            return real_replace(src, dst)
        with patch.object(rb.os, "replace", side_effect=fail_once), self.assertRaisesRegex(rb.DatabaseError, "retained/restored"):
            rb.apply_update(plan, self.backups)
        self.assertEqual(rb.snapshot(self.device), before)
        self.assertEqual(len(list(self.backups.glob("*/manifest.json"))), 1)
        self.assertFalse((self.device / ".rockbox" / rb.JOURNAL).exists())

    def test_interruption_blocks_updates_and_can_be_restored(self):
        self.edit(); before = rb.snapshot(self.device); plan = rb.preview_update(self.music)
        real_replace = rb.os.replace; disconnected = False
        def disconnect(src, dst):
            nonlocal disconnected
            if Path(dst).name == "database_7.tcd":
                disconnected = True
            if disconnected:
                raise OSError("Device disconnected")
            return real_replace(src, dst)
        with patch.object(rb.os, "replace", side_effect=disconnect), self.assertRaisesRegex(rb.DatabaseError, "automatic recovery failed"):
            rb.apply_update(plan, self.backups)
        journal = self.device / ".rockbox" / rb.JOURNAL
        self.assertTrue(journal.exists())
        with self.assertRaisesRegex(rb.DatabaseError, "interrupted"):
            rb.preview_update(self.music)
        backup = Path(json.loads(journal.read_text())["backup"])
        rb.restore_backup(self.device, backup, self.backups)
        self.assertFalse(journal.exists())
        self.assertEqual(rb.snapshot(self.device), {n: b for n, b in before.items() if n != rb.STATE})

    def test_damaged_or_foreign_backup_is_rejected_before_writing(self):
        backup = rb.backup_database(self.device, self.backups)
        before = rb.snapshot(self.device)
        (backup / "database_0.tcd").write_bytes(b"damaged")
        with self.assertRaises(rb.DatabaseError):
            rb.restore_backup(self.device, backup, self.backups)
        self.assertEqual(rb.snapshot(self.device), before)
        valid = rb.backup_database(self.device, self.backups)
        other, _ = make_device(self.base / "other-device")
        with self.assertRaisesRegex(rb.DatabaseError, "another device"):
            rb.restore_backup(other, valid, self.backups)

    def test_os_lock_prevents_overlapping_writes(self):
        self.edit(); plan = rb.preview_update(self.music); before = rb.snapshot(self.device)
        with rb._device_lock(self.device), self.assertRaisesRegex(rb.DatabaseError, "Another application"):
            rb.apply_update(plan, self.backups)
        self.assertEqual(rb.snapshot(self.device), before)

    def test_custom_database_location_is_not_mistaken_for_active_database(self):
        before = rb.snapshot(self.device)
        config = self.device / ".rockbox/config.cfg"
        config.write_text("database path: /OtherDatabase\n")
        with self.assertRaisesRegex(rb.DatabaseError, "custom Rockbox database"):
            rb.preview_update(self.music)
        config.write_text("database path: /.rockbox\n")
        self.assertEqual(rb.snapshot(self.device), before)


if __name__ == "__main__":
    unittest.main()
