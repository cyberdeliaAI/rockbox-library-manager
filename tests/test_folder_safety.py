"""Exercise duplicate consolidation on temporary files, including partial I/O failures."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rockbox_manager import folders


class FolderSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-folder-safety-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.names = ["Ali Farka Toure", "Ali Farka Touré", "Ali Farka Tourè"]
        self.rows = []
        for name in self.names:
            (self.root / name).mkdir()
            self.rows.append(SimpleNamespace(artist=name, relative_path=name, artwork_exists=False))
        self.target = self.root / self.names[0]

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def merge(self):
        return folders.consolidate_artist_folders(self.root, self.rows, self.names[0])

    def test_conflicting_files_in_two_sources_abort_before_any_change(self):
        for name, content in zip(self.names[1:], (b"first original", b"second original")):
            (self.root / name / "notes.txt").write_bytes(content)
        before = self.snapshot()
        result = self.merge()
        self.assertFalse(result["ok"])
        self.assertFalse(result["changes_started"])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(all((self.root / name).is_dir() for name in self.names))

    def test_conflicting_albums_in_two_sources_are_not_nested(self):
        for name in self.names[1:]:
            album = self.root / name / "Album"
            album.mkdir()
            (album / "01.flac").write_bytes(name.encode())
        before = self.snapshot()
        result = self.merge()
        self.assertFalse(result["ok"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.target / "Album").exists())

    def test_case_variant_source_destinations_are_checked_before_moves(self):
        for name, album_name in zip(self.names[1:], ("Album", "album")):
            album = self.root / name / album_name
            album.mkdir()
            (album / "01.flac").write_bytes(name.encode())
        before = self.snapshot()
        self.assertFalse(self.merge()["ok"])
        self.assertEqual(self.snapshot(), before)

    def test_identical_root_files_in_two_sources_still_deduplicate(self):
        for name in self.names[1:]:
            (self.root / name / "notes.txt").write_bytes(b"same content")
        result = self.merge()
        self.assertTrue(result["ok"])
        self.assertEqual((self.target / "notes.txt").read_bytes(), b"same content")
        self.assertTrue(all(not (self.root / name).exists() for name in self.names[1:]))

    def test_partial_failure_reports_only_completed_entry_moves(self):
        source = self.root / self.names[1]
        for album_name in ("Album A", "Album B"):
            album = source / album_name
            album.mkdir()
            (album / "01.flac").write_bytes(album_name.encode())
        real = folders.shutil.move
        completed = []
        def move(src, dst):
            if completed:
                raise OSError("simulated write failure")
            result = real(src, dst)
            completed.append([src, dst])
            return result
        with patch.object(folders.shutil, "move", side_effect=move):
            result = self.merge()
        self.assertFalse(result["ok"])
        self.assertTrue(result["changes_started"])
        self.assertEqual(result["moved_entries"], 1)
        self.assertEqual(result["folder_moves"], completed)
        self.assertEqual(len(list(self.root.rglob("01.flac"))), 2)
        old, new = map(Path, completed[0])
        self.assertFalse(old.exists())
        self.assertTrue((new / "01.flac").is_file())

    def test_destination_appearing_after_preflight_is_not_overwritten(self):
        source = self.root / self.names[1] / "notes.txt"
        source.write_bytes(b"original source")
        destination = self.target / source.name
        def progress(_text):
            destination.write_bytes(b"new external file")
        result = folders.consolidate_artist_folders(self.root, self.rows, self.names[0], progress=progress)
        self.assertFalse(result["ok"])
        self.assertEqual(source.read_bytes(), b"original source")
        self.assertEqual(destination.read_bytes(), b"new external file")

    def test_partial_album_moves_can_update_the_database_without_remapping_unmoved_tracks(self):
        from rockbox_manager import rockbox_database
        from test_rockbox_database import make_device

        device, tracks = make_device(self.root, paths=[
            "/Music/Old Artist/Album A/01.flac", "/Music/Old Artist/Album B/02.flac"])
        music = device / "Music"
        (music / "Other").mkdir()
        rows = [SimpleNamespace(artist=name, relative_path=name, artwork_exists=False)
                for name in ("Other", "Old Artist")]
        real = folders.shutil.move
        completed = []
        def move(src, dst):
            if completed:
                raise OSError("simulated write failure")
            result = real(src, dst)
            completed.append([src, dst])
            return result
        with patch.object(folders.shutil, "move", side_effect=move):
            result = folders.consolidate_artist_folders(music, rows, "Other")
        self.assertFalse(result["ok"])
        self.assertEqual(sum(p.exists() for p in tracks), 1)
        plan = rockbox_database.preview_update(music, moves=result["folder_moves"])
        self.assertEqual(plan.changed_tracks, 1)


if __name__ == "__main__":
    unittest.main()
