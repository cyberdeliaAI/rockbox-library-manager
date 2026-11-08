"""Public text never changes music, guesses identities or makes implicit requests."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rockbox_manager import online_info as info
from rockbox_manager.sources import SourceError


class OnlineInformationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "background.sqlite3"
        self.http = Mock()
        self.store = info.OnlineInformation(self.path, self.http)
        self.row = {"strArtist": "Björk", "strAlbum": "Début", "idArtist": "123", "idAlbum": "456",
                    "strBiography": "<p>Artist &amp; music.</p><script>hidden</script>",
                    "strDescriptionEN": "<p>Album background.</p><style>hidden</style>"}

    def test_initialization_is_offline_and_creates_no_cache(self):
        self.http.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_exact_match_current_and_older_fields_sanitization_and_cache(self):
        self.http.json.return_value = {"artists": [self.row]}
        data = self.store.get("artist", " BJÖRK ")
        self.assertEqual(data["text"], "Artist & music.")
        self.assertEqual(data["url"], "https://www.theaudiodb.com/artist/123")
        self.assertFalse(data["cached"])
        self.assertTrue(self.store.get("artist", "Björk")["cached"])
        self.http.json.assert_called_once_with(info.TheAudioDB.API + "/search.php", {"s": " BJÖRK "}, ttl=0)
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.http.json.return_value = {"album": [self.row]}
        album = self.store.get("album", "Björk", "Début")
        self.assertEqual(album["text"], "Album background.")
        self.assertEqual(album["url"], "https://www.theaudiodb.com/album/456")

    def test_accents_punctuation_and_editions_are_not_fuzzy_matched(self):
        self.http.json.return_value = {"album": [self.row]}
        for artist, album in (("Bjork", "Début"), ("The Björk", "Début"), ("Björk", "Debut"),
                              ("Björk", "Début!"), ("Björk", "Début (Live)"), ("Björk", "Début [Deluxe]")):
            with self.subTest(artist=artist, album=album):
                result = self.store.get("album", artist, album)
                self.assertEqual(result["status"], "missing")
                self.assertEqual(result["url"], "")

    def test_missing_ambiguous_and_unavailable_results_are_distinct(self):
        for rows, expected in ((None, "missing"), ([self.row, self.row], "ambiguous"),
                               ([{**self.row, "strBiography": None}], "unavailable")):
            with self.subTest(expected=expected):
                self.http.json.return_value = {"artists": rows}
                self.path.unlink(missing_ok=True)
                self.assertEqual(self.store.get("artist", "Björk")["status"], expected)

    def test_invalid_response_and_ids_do_not_create_links_and_errors_back_off(self):
        for response in ("invalid", {"artists": "invalid"}, {"artists": [{**self.row, "idArtist": "１２３"}]}):
            self.store.failed_at = 0
            self.http.json.return_value = response
            with self.subTest(response=response), self.assertRaises(SourceError):
                self.store.get("artist", "Björk")
            count = self.http.json.call_count
            with self.assertRaises(SourceError):
                self.store.get("artist", "Björk")
            self.assertEqual(self.http.json.call_count, count)

    def test_stale_or_invalid_cached_values_are_not_shown(self):
        self.http.json.return_value = {"artists": [self.row]}
        self.store.get("artist", "Björk")
        with sqlite3.connect(self.path) as db:
            data = json.loads(db.execute("SELECT data FROM background").fetchone()[0])
            db.execute("UPDATE background SET data=?", (json.dumps({**data, "url": "https://evil.example/"}),))
        self.assertFalse(self.store.get("artist", "Björk")["cached"])
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE background SET expires_at=?", (time.time() - 1,))
        self.assertFalse(self.store.get("artist", "Björk")["cached"])
        self.assertEqual(self.http.json.call_count, 3)

    def test_cache_is_bounded_and_write_failures_do_not_hide_a_description(self):
        with patch.object(info, "MAX_ENTRIES", 2), patch.object(info, "MAX_BYTES", 100000):
            for number in range(5):
                self.http.json.return_value = {"artists": [{**self.row, "strArtist": str(number)}]}
                self.store.get("artist", str(number))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM background").fetchone()[0], 2)
        self.http.json.return_value = {"artists": [self.row]}
        with patch.object(self.store, "_db", side_effect=sqlite3.OperationalError("read only")):
            self.assertEqual(self.store.get("artist", "Björk")["status"], "ready")

    def test_text_size_is_bounded_and_unknown_types_are_rejected(self):
        self.assertLessEqual(len(info.plain_text("x" * 200000)), info.MAX_TEXT)
        for args in (("track", "Björk"), ("album", "Björk"), ("artist", "")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.get(*args)
