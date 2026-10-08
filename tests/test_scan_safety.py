"""Incomplete scans must preserve the previous index, root, and scan metadata."""
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rockbox_manager import library


class ScanSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-scan-safety-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "Music"
        self.album = self.root / "Artist" / "Album"
        self.album.mkdir(parents=True)
        (self.album / "01.flac").write_bytes(b"directory scanner does not parse audio")
        self.db = library.LibraryDB(self.base / "index.sqlite3")
        self.addCleanup(self.db.conn.close)
        self.scan()

    def scan(self, root=None, cancel=None, progress=None):
        return library.FastLibraryScanner(self.db, root or self.root, "folder.jpg",
                                          progress or (lambda *_: None), cancel).scan()

    def snapshot(self):
        return ([tuple(r) for r in self.db.conn.execute("SELECT * FROM items ORDER BY id")],
                [tuple(r) for r in self.db.conn.execute("SELECT * FROM meta ORDER BY key")])

    def test_read_errors_at_root_artist_or_album_preserve_the_entire_index(self):
        real = library.os.scandir
        for unreadable in (self.root, self.album.parent, self.album):
            with self.subTest(path=unreadable):
                before = self.snapshot()
                def scandir(path):
                    if Path(path) == unreadable:
                        raise PermissionError("simulated device read error")
                    return real(path)
                with patch.object(library.os, "scandir", side_effect=scandir):
                    result = self.scan()
                self.assertEqual(result["errors"], 1)
                self.assertEqual(result["incomplete"], 1)
                self.assertEqual(self.snapshot(), before)

    def test_partial_new_root_does_not_mix_rows_or_replace_the_indexed_root(self):
        other = self.base / "OtherMusic"
        for artist in ("A readable", "Z unreadable"):
            album = other / artist / "Album"
            album.mkdir(parents=True)
            (album / "01.flac").write_bytes(b"temporary fixture")
        real = library.os.scandir
        before = self.snapshot()
        def scandir(path):
            if Path(path) == other / "Z unreadable":
                raise OSError("simulated disconnect")
            return real(path)
        with patch.object(library.os, "scandir", side_effect=scandir):
            result = self.scan(other)
        self.assertEqual(result["artists"], 1)
        self.assertEqual(result["incomplete"], 1)
        self.assertEqual(self.snapshot(), before)

    def test_cancellation_in_the_last_artist_preserves_the_previous_index(self):
        cancel = threading.Event()
        before = self.snapshot()
        result = self.scan(cancel=cancel, progress=lambda *_: cancel.set())
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(self.snapshot(), before)

    def test_successful_empty_scan_still_removes_deleted_folders(self):
        shutil.rmtree(self.album.parent)
        result = self.scan()
        self.assertEqual(result["errors"], 0)
        self.assertFalse(result["cancelled"])
        self.assertEqual(self.db.physical_items(), [])

    def test_database_error_rolls_back_the_whole_scan(self):
        other = self.base / "OtherMusic" / "Other" / "Album"
        other.mkdir(parents=True)
        (other / "01.flac").write_bytes(b"temporary fixture")
        before = self.snapshot()
        real = self.db._upsert_item
        count = 0
        def upsert(**kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("simulated index write error")
            return real(**kwargs)
        with patch.object(self.db, "_upsert_item", side_effect=upsert), \
                self.assertRaisesRegex(RuntimeError, "index write error"):
            self.scan(other.parent.parent)
        self.assertEqual(self.snapshot(), before)

    def test_successful_scans_prune_when_the_clock_does_not_advance(self):
        with patch.object(library.time, "time_ns", return_value=123):
            self.scan()
            shutil.rmtree(self.album.parent)
            result = self.scan()
        self.assertEqual(result["errors"], 0)
        self.assertEqual(self.db.physical_items(), [])


if __name__ == "__main__":
    unittest.main()
