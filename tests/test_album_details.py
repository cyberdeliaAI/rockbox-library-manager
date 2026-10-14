"""Read-only diagnostics on copied FLACs; no player or network is used."""
import hashlib
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rockbox_manager import album_details, health, tags
from rockbox_manager.track_checks import inspect_tracks
import test_tags_health as fixtures

track = fixtures.track


class TrackChecksTests(unittest.TestCase):
    def rows(self, *entries):
        return {Path(f"Album/{i}.flac"): {"tracknumber": [str(number)], "title": [title],
                                       "album": ["Album"], "discnumber": [str(disc)]}
                for i, (disc, number, title) in enumerate(entries)}

    def test_duplicates_require_number_and_title_on_same_disc(self):
        rows = self.rows((1, 1, "Holdin’ On"), (1, 1, "Holdin' On"), (2, 1, "Holdin' On"),
                         (1, 2, "Holdin' On"), (1, 1, "Holdin' On (Live)"), (1, 0, "Holdin' On"))
        result = inspect_tracks(rows, complete=False)
        self.assertEqual([i["type"] for i in result], ["duplicate_track"])
        self.assertEqual(len(result[0]["paths"]), 2)
        self.assertIn("audio was not compared", result[0]["details"])

    def test_missing_or_ambiguous_titles_numbers_and_disc_tags_do_not_match(self):
        rows = self.rows((1, 1, ""), (1, 1, ""), (1, 0, "Unknown"), (1, 0, "Unknown"))
        self.assertEqual(inspect_tracks(rows, complete=True), [])
        rows = self.rows((1, 1, "X"), (1, 1, "X"))
        list(rows.values())[0]["title"] = ["X", "Y"]
        self.assertEqual(inspect_tracks(rows, complete=False), [])

    def test_gaps_require_complete_numbered_album_and_are_bounded(self):
        rows = self.rows((1, 1, "A"), (1, 2, "B"), (1, 5, "C"))
        self.assertEqual(inspect_tracks(rows, complete=False), [])
        self.assertIn("3–4", inspect_tracks(rows, complete=True)[0]["details"])
        rows = self.rows((1, 1, "A"), (1, 999999999, "B"))
        self.assertLess(len(inspect_tracks(rows, complete=True)[0]["details"]), 300)
        rows = self.rows((1, 1, "A"), (1, 0, "Unknown"), (1, 3, "C"))
        self.assertEqual(inspect_tracks(rows, complete=True), [])

    def test_untagged_disc_folders_are_distinct(self):
        values = {"tracknumber": ["1"], "title": ["Song"], "album": ["A"]}
        rows = {Path("Album/CD1/01.flac"): values, Path("Album/CD2/01.flac"): values}
        self.assertEqual(inspect_tracks(rows, complete=True), [])

    def test_partial_or_invalid_disc_tagging_cannot_infer_gaps(self):
        rows = self.rows((1, 1, "A"), (1, 3, "B"))
        list(rows.values())[0].pop("discnumber")
        self.assertEqual(inspect_tracks(rows, complete=True), [])
        list(rows.values())[0]["discnumber"] = ["not a disc"]
        self.assertEqual(inspect_tracks(rows, complete=True), [])


class AlbumReadTests(unittest.TestCase):
    def setUp(self):
        fixtures.TagTests.setUp(self)
        self.album = self.album.resolve()
    def test_album_details_preserve_bytes_sort_discs_and_report_metadata_errors(self):
        a = track(self.album, "z.flac", title="Björk", artist="Artist", album="Album",
                  genre="Rock", date="2001", tracknumber="2/3", discnumber="1/2", composer="Composer")
        b = track(self.album, "a.flac", title="First", album="Album", tracknumber="1", discnumber="2")
        broken = self.album / "broken.flac"
        broken.write_bytes(b"not audio")
        before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in (a, b, broken)}
        result = album_details.read_album(self.album, threading.Event())
        self.assertEqual(len(result.tracks), 3)
        self.assertEqual(result.tracks[0].path, a)
        self.assertEqual(result.tracks[0].value("composer"), "Composer")
        self.assertIn("FLAC", result.tracks[0].format)
        self.assertIsNotNone(result.tracks[0].duration)
        self.assertTrue(next(t for t in result.tracks if t.path == broken).error)
        self.assertFalse(any(i["type"] == "track_gaps" for i in result.issues))
        self.assertEqual(before, {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in before})

    def test_cancel_and_traversal_errors_do_not_imply_missing_tracks(self):
        track(self.album, "01.flac", album="Album", tracknumber="3")
        cancel = threading.Event()
        cancel.set()
        self.assertTrue(album_details.read_album(self.album, cancel).cancelled)
        real_walk = album_details.os.walk

        def walk(folder, onerror):
            onerror(PermissionError("Unreadable disc"))
            yield from real_walk(folder)

        with patch.object(album_details.os, "walk", side_effect=walk):
            result = album_details.read_album(self.album, threading.Event())
        self.assertTrue(result.errors)
        self.assertFalse(any(i["type"] == "track_gaps" for i in result.issues))

    def test_duration_includes_hours_and_unknown(self):
        self.assertEqual(album_details.duration_text(3661), "1:01:01")
        self.assertEqual(album_details.duration_text(None), "—")


class AdditionalHealthTests(unittest.TestCase):
    setUp = fixtures.HealthTests.setUp
    album = fixtures.HealthTests.album
    scan = fixtures.HealthTests.scan
    def test_full_album_gaps_are_not_inferred_from_three_samples(self):
        self.album("Artist", "Album", *[{} for _ in range(8)])
        self.assertNotIn(("track_gaps", "Album"), self.scan())
        self.assertNotIn(("track_gaps", "Album"), self.scan(thorough=True))
        path = self.music / "Artist/Album/04.flac"
        path.unlink()
        self.assertNotIn(("track_gaps", "Album"), self.scan())
        self.assertIn("4", self.scan(thorough=True)[("track_gaps", "Album")]["details"])

    def test_health_keeps_existing_number_warning_and_adds_metadata_candidates(self):
        self.album("Artist", "Album", {"title": "Song", "tracknumber": "1"},
                   {"title": "Song", "tracknumber": "1"}, {"title": "Different", "tracknumber": "1"})
        issues = self.scan(thorough=True)
        self.assertIn(("track_numbers", "Album"), issues)
        self.assertIn(("duplicate_track", "Album"), issues)
        self.assertIn("audio was not compared", issues[("duplicate_track", "Album")]["details"])

    def test_read_error_and_directory_error_suppress_gap_claim(self):
        self.album("Artist", "Album", {"tracknumber": "1"}, {"tracknumber": "3"})
        with patch.object(health, "album_files", return_value=([], ["Disconnected"])):
            self.assertNotIn(("track_gaps", "Album"), self.scan(thorough=True))
        original = tags.read_values

        def read(path, *args, **kwargs):
            if path.name == "01.flac":
                raise OSError("Disconnected")
            return original(path, *args, **kwargs)

        with patch.object(tags, "read_values", side_effect=read):
            self.assertNotIn(("track_gaps", "Album"), self.scan(thorough=True))
