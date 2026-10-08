"""Tag planning/writing and the Rockbox-specific Library Health checks, on real tagged FLACs."""
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from mutagen.flac import FLAC

from rockbox_manager import health, tags
from rockbox_manager.library import FastLibraryScanner, LibraryDB

FIXTURE = Path(__file__).parent / "fixtures" / "silence.flac"


def track(folder: Path, name: str, **values) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    shutil.copyfile(FIXTURE, path)
    audio = FLAC(path)
    for key, value in values.items():
        audio[key] = value if isinstance(value, list) else [value]
    audio.save()
    return path


class TagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-tags-")
        self.addCleanup(self.tmp.cleanup)
        self.album = Path(self.tmp.name) / "Artist" / "Album"

    def test_years_and_genres(self):
        for value in ("1985", "1985-09", "1985-09-16"):
            self.assertTrue(tags.valid_year(value), value)
        for value in ("85", "1985?", "c. 1985", "1985/09/16", ""):
            self.assertFalse(tags.valid_year(value), value)
        self.assertTrue(tags.database_can_read_year("1985 (remaster)"))
        self.assertFalse(tags.database_can_read_year("c. 1985"))
        self.assertEqual(tags.genre_key("Hip Hop"), tags.genre_key("hip-hop"))
        self.assertEqual(tags.genre_key("Hip-Hop"), tags.genre_key("HipHop"))
        self.assertNotEqual(tags.genre_key("Rock"), tags.genre_key("Pop"))
        self.assertTrue(tags.combined_genre("Pop;Rock"))
        self.assertEqual(tags.split_genre("Pop; Rock;pop"), ["Pop", "Rock"])

    def test_every_value_is_read_and_one_value_is_written(self):
        a = track(self.album, "01.flac", genre=["Pop", "Rock"], date="1985")
        b = track(self.album, "02.flac", genre="Rock", date="1985")
        files = {p: tags.read_values(p, ["genre", "date"]) for p in (a, b)}
        self.assertEqual(files[a]["genre"], ["Pop", "Rock"])
        edits = {"genre": ("set", "Rock"), "date": ("set", "1985")}
        plans = tags.plan_edits(files, edits)
        self.assertEqual([p.path for p in plans], [a])  # b already has these values
        self.assertEqual(tags.review_lines(plans), ["Genre: “Pop” + “Rock” → “Rock” · 1 track"])
        before = b.stat().st_mtime_ns
        time.sleep(0.01)
        self.assertTrue(tags.write_edits(a, edits))
        self.assertFalse(tags.write_edits(b, edits))
        self.assertEqual(b.stat().st_mtime_ns, before)
        self.assertEqual(FLAC(a)["genre"], ["Rock"])
        self.assertTrue(tags.write_edits(a, {"date": ("clear", "")}))
        self.assertNotIn("date", FLAC(a))

    def test_review_groups_identical_changes(self):
        files = {self.album / f"{n:02}.flac": {"genre": ["Pop;Rock"]} for n in range(14)}
        files[self.album / "15.flac"] = {"genre": []}
        lines = tags.review_lines(tags.plan_edits(files, {"genre": ("set", "Pop")}))
        self.assertEqual(lines, ["Genre: (empty) → “Pop” · 1 track", "Genre: “Pop;Rock” → “Pop” · 14 tracks"])


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-health-")
        self.addCleanup(self.tmp.cleanup)
        self.music = Path(self.tmp.name) / "Music"
        self.db = LibraryDB(Path(self.tmp.name) / "index.sqlite3")
        self.addCleanup(self.db.conn.close)

    def album(self, artist, album, *tracks):
        for number, values in enumerate(tracks, 1):
            values = {"artist": artist, "albumartist": artist, "album": album, "title": f"Track {number}",
                      "tracknumber": str(number), "date": "2001", "genre": "Rock", **values}
            track(self.music / artist / album, f"{number:02}.flac", **{k: v for k, v in values.items() if v is not None})

    def scan(self, thorough=False):
        FastLibraryScanner(self.db, self.music, "folder.jpg", lambda *_: None).scan()
        result = health.HealthScanner(self.db, self.music, lambda *_: None, thorough=thorough).scan()
        self.assertEqual(result["cancelled"], 0)
        return {(row["issue_type"], row["album"]): row for row in self.db.health_issues()}

    def test_clean_album_has_no_issues(self):
        self.album("Clean", "Album", {}, {}, {})
        self.assertEqual(self.scan(), {})

    def test_separator_only_genres_are_reported_without_aborting_health(self):
        for genre in (";", " ; ; ", ";Pop;Rock"):
            self.album("Artist", f"Genre {len(genre)}", {"genre": genre})
        issues = self.scan()
        self.assertEqual(sum(kind == "combined_genre" for kind, _album in issues), 3)
        self.assertIn("clear the empty genre tag", issues[("combined_genre", "Genre 1")]["details"])
        self.assertIn("e.g. “Pop”", issues[("combined_genre", "Genre 9")]["details"])

    def test_database_blockers_genres_and_missing_information(self):
        self.album("Band", "Several", {"genre": ["Pop", "Rock"]}, {}, {})
        self.album("Band", "Odd Year", {"date": "c. 1999"}, {"date": "c. 1999"}, {"date": "c. 1999"})
        self.album("Band", "Combined", {"genre": "Pop;Rock"}, {"genre": "Pop;Rock"}, {"genre": "Pop;Rock"})
        self.album("Band", "Bare", {"date": None, "genre": None, "tracknumber": None}, {"date": None}, {})
        for name in ("One", "Two"):
            self.album("Rap", name, {"genre": "Hip Hop"}, {"genre": "Hip Hop"}, {"genre": "Hip Hop"})
        self.album("Rap", "Three", {"genre": "Hip-Hop"}, {"genre": "Hip-Hop"}, {"genre": "Hip-Hop"})
        self.album("Rap", "Four", {"genre": "hip hop"}, {"genre": "hip hop"}, {"genre": "hip hop"})
        issues = self.scan()
        self.assertEqual(issues[("multiple_values", "Several")]["severity"], "bad")
        self.assertIn("“Pop” + “Rock”", issues[("multiple_values", "Several")]["details"])
        self.assertIn("“c. 1999”", issues[("unreadable_year", "Odd Year")]["details"])
        self.assertIn("Keep one main genre, e.g. “Pop”", issues[("combined_genre", "Combined")]["details"])
        self.assertIn("No year on 2 of 3 tracks", issues[("missing_year", "Bare")]["details"])
        self.assertIn("No genre on 1 of 3 tracks", issues[("missing_genre", "Bare")]["details"])
        self.assertIn("No track number on 1 of 3 tracks", issues[("track_numbers", "Bare")]["details"])
        # Hip-Hop is the odd one out; 'hip hop' only differs in case, which Rockbox merges.
        spelling = [album for kind, album in issues if kind == "genre_spelling"]
        self.assertEqual(spelling, ["Three"])
        self.assertIn("apart from “Hip Hop”", issues[("genre_spelling", "Three")]["details"])
        database = self.db.health_issues(health.CATEGORIES["database"])
        self.assertEqual(sorted(row["album"] for row in database), ["Odd Year", "Several"])
        self.assertEqual(self.db.health_counts()["all"], len(issues))

    def test_thorough_mode_reads_every_track(self):
        tracks = [{} for _ in range(6)]
        tracks[2] = {"genre": ["Pop", "Rock"]}   # neither first, middle nor last of six
        tracks[4] = {"tracknumber": "4"}         # number 4 twice
        self.album("Deep", "Album", *tracks)
        self.assertEqual(self.scan(), {})
        issues = self.scan(thorough=True)
        self.assertIn(("multiple_values", "Album"), issues)
        self.assertIn("Track number used twice: 4", issues[("track_numbers", "Album")]["details"])
        self.assertIn("every track", issues[("track_numbers", "Album")]["details"])

    def test_every_issue_type_has_a_label_and_a_filter(self):
        grouped = {t for types in health.CATEGORIES.values() for t in types}
        self.assertEqual(grouped, set(health.ISSUE_LABELS))
        self.assertEqual({key for key, _label in health.FILTERS} - {"all"}, set(health.CATEGORIES))


if __name__ == "__main__":
    unittest.main()
