"""Real image/tag processing with offline providers and temporary output files."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote_plus

import requests
from mutagen.flac import FLAC, Picture
from PIL import Image

from rockbox_manager import artwork_engine as engine

FIXTURE = Path(__file__).parent / "fixtures" / "silence.flac"


class ArtworkEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-artwork-engine-")
        self.addCleanup(self.tmp.cleanup)
        self.artist = Path(self.tmp.name) / "Artist"
        self.album = self.artist / "Album"
        self.album.mkdir(parents=True)
        self.track = self.album / "01.flac"
        self.track.write_bytes(FIXTURE.read_bytes())
        audio = FLAC(self.track)
        audio.update({"artist": ["Artist"], "albumartist": ["Artist"], "album": ["Album"]})
        audio.save()
        self.args = engine.build_parser().parse_args([])
        self.creds = engine.Credentials()
        self.cache = {"artists": {}, "albums": {}}
        self.logs = io.StringIO()
        redirect = contextlib.redirect_stdout(self.logs)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        network = patch.object(requests.Session, "request", side_effect=AssertionError("tests must stay offline"))
        network.start()
        self.addCleanup(network.stop)

    def candidate(self, size, color, url):
        data = io.BytesIO()
        Image.new("RGB", (size, size), color).save(data, "PNG")
        return engine.ImageCandidate("Deezer", "Artist", "Album", image_url=url, image_bytes=data.getvalue())

    def pool(self):
        return [self.candidate(300, "red", "https://example.invalid/first"),
                self.candidate(900, "blue", "https://example.invalid/best")]

    def assert_best_cached(self, kind, folder, key):
        self.assertEqual(self.cache[kind][key]["image_url"], "https://example.invalid/best")
        with Image.open(folder / "folder.jpg") as output:
            self.assertGreater(output.getpixel((150, 150))[2], 240)

    def embed(self, size):
        picture = Picture()
        picture.type = 3
        picture.mime = "image/png"
        picture.data = self.candidate(size, "green", "").image_bytes
        audio = FLAC(self.track)
        audio.add_picture(picture)
        audio.save()

    def test_artist_cache_records_the_selected_candidate_from_the_same_provider(self):
        with patch.object(engine, "artist_candidates", return_value=self.pool()):
            result = engine.process_artist_folder(self.artist, self.args, self.creds, None, None, self.cache)
        self.assertEqual(result, (True, "Deezer"))
        self.assert_best_cached("artists", self.artist, engine.cache_key("Artist"))

    def test_album_cache_records_the_selected_candidate_from_the_same_provider(self):
        with patch.object(engine, "album_candidates", return_value=self.pool()):
            result = engine.process_album_folder(self.album, self.args, self.creds, None, None, self.cache)
        self.assertEqual(result, (True, "Deezer"))
        self.assert_best_cached("albums", self.album, engine.cache_key("Artist", "Album"))

    def test_interactive_retries_also_cache_the_selected_candidate(self):
        self.args.interactive = True
        for kind in ("artist", "album"):
            with self.subTest(kind=kind):
                folder = self.artist if kind == "artist" else self.album
                process = engine.process_artist_folder if kind == "artist" else engine.process_album_folder
                prompt = "prompt_artist" if kind == "artist" else "prompt_album"
                override = "Artist" if kind == "artist" else ("Artist", "Album")
                with patch.object(engine, f"{kind}_candidates", side_effect=[[], self.pool()]), \
                        patch.object(engine, prompt, return_value=override):
                    result = process(folder, self.args, self.creds, None, None, self.cache)
                self.assertEqual(result, (True, "Deezer"))
                key = engine.cache_key("Artist") if kind == "artist" else engine.cache_key("Artist", "Album")
                self.assert_best_cached(kind + "s", folder, key)

    def test_small_embedded_artwork_falls_back_to_online_search(self):
        self.embed(200)
        with patch.object(engine, "album_candidates", return_value=self.pool()) as search:
            result = engine.process_album_folder(self.album, self.args, self.creds, None, None, self.cache)
        self.assertEqual(result, (True, "Deezer"))
        search.assert_called_once()
        self.assert_best_cached("albums", self.album, engine.cache_key("Artist", "Album"))

    def test_usable_embedded_artwork_still_avoids_online_search(self):
        self.embed(600)
        with patch.object(engine, "album_candidates") as search:
            result = engine.process_album_folder(self.album, self.args, self.creds, None, None, self.cache)
        self.assertEqual(result, (True, "embedded"))
        search.assert_not_called()
        self.assertEqual(self.cache["albums"], {})

    def test_rejected_embedded_and_online_artwork_keep_output_missing(self):
        self.embed(200)
        with patch.object(engine, "album_candidates", return_value=[self.candidate(200, "blue", "small")]):
            result = engine.process_album_folder(self.album, self.args, self.creds, None, None, self.cache)
        self.assertEqual(result, (False, "not-found"))
        self.assertFalse((self.album / "folder.jpg").exists())
        self.assertEqual(self.cache["albums"][engine.cache_key("Artist", "Album")]["status"], "not_found")

    def test_existing_artwork_stays_untouched_and_legacy_result_is_preserved(self):
        output = self.artist / "folder.jpg"
        output.write_bytes(b"existing artwork")
        with patch.object(engine, "artist_candidates") as search:
            result = engine.process_artist_folder(self.artist, self.args, self.creds, None, None, self.cache)
        self.assertEqual(result, (False, "exists"))
        search.assert_not_called()
        self.assertEqual(output.read_bytes(), b"existing artwork")
        output.unlink()
        self.assertEqual(engine.try_candidates(self.pool(), output, self.args, None, None), (True, "Deezer", ""))

    def test_http_errors_keep_status_and_provider_but_redact_keys(self):
        self.args.no_theaudiodb = True
        secret = "FAKE_TEST_KEY /+"
        response = requests.Response()
        response.status_code = 403
        response.url = f"https://ws.audioscrobbler.com/2.0/?api_key={quote_plus(secret)}&artist=Artist"
        session = Mock()
        session.get.return_value = response
        creds = engine.Credentials(lastfm_api_key=secret)
        with patch.object(engine, "lookup_online_artist", return_value=[]), \
                patch.object(engine, "lookup_online_album", return_value=[]), \
                patch.object(engine, "lookup_cover_art_archive", return_value=[]):
            engine.artist_candidates("Artist", creds, self.args, session, Mock())
            engine.album_candidates("Artist", "Album", creds, self.args, session, Mock())
        log = self.logs.getvalue()
        self.assertNotIn(secret, log)
        self.assertNotIn(quote_plus(secret), log)
        self.assertIn("last.fm error: 403", log)
        self.assertIn("api_key=[redacted]", log)

    def test_fanart_http_error_is_redacted_too(self):
        response = requests.Response()
        response.status_code = 403
        response.url = "https://webservice.fanart.tv/v3/music/id?api_key=FAKE_FANART_KEY"
        session = Mock()
        session.get.return_value = response
        self.args.no_theaudiodb = True
        with patch.object(engine, "lookup_online_artist", return_value=[]):
            engine.artist_candidates("Artist", engine.Credentials(fanart_api_key="FAKE_FANART_KEY"),
                                     self.args, session, Mock(), mbid="id")
        self.assertNotIn("FAKE_FANART_KEY", self.logs.getvalue())
        self.assertIn("fanart.tv error: 403", self.logs.getvalue())


if __name__ == "__main__":
    unittest.main()
