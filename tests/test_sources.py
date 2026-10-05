"""Online sources and the artwork engine's use of them, with canned answers (no network)."""
import argparse
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from rockbox_manager import artwork_engine as engine
from rockbox_manager import sources

FIXTURE = Path(__file__).parent / "fixtures" / "silence.flac"


class FakeHttp:
    """Answers by URL fragment; records every request and every pacing wait."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.waits = []

    def json(self, url, params=None, headers=None, ttl=3600):
        self.calls.append((url, dict(params or {})))
        for fragment, answer in self.answers.items():
            if fragment in url:
                return answer(params) if callable(answer) else answer
        return None

    def wait(self, host, gap=None):
        self.waits.append((host, gap))


MB_GROUPS = {"release-groups": [
    {"id": "rg-1", "title": "Hounds of Love", "first-release-date": "1985-09-16", "primary-type": "Album",
     "artist-credit": [{"name": "Kate Bush"}]},
    {"id": "rg-2", "title": "The Whole Story", "first-release-date": "1986", "primary-type": "Album",
     "artist-credit": [{"name": "Kate Bush"}]},
]}
DEEZER_ALBUMS = {"data": [
    {"id": 1, "title": "Hounds Of Love", "artist": {"name": "Kate Bush"}, "cover_xl": "https://dz/xl.jpg",
     "cover_medium": "https://dz/m.jpg"},
    {"id": 2, "title": "Hounds Of Love (from \"Stranger Things 4\")", "artist": {"name": "Kate Bush"},
     "cover_xl": "https://dz/single.jpg"},
]}
ITUNES = {"results": [
    {"collectionName": "Hounds of Love (2018 Remaster)", "artistName": "Kate Bush",
     "releaseDate": "1985-09-16T07:00:00Z", "primaryGenreName": "Pop",
     "artworkUrl100": "https://is1/100x100bb.jpg"},
]}


class MatchingTests(unittest.TestCase):
    def test_normalize_ignores_accents_case_the_and_editions(self):
        self.assertEqual(sources.normalize("The Ali Farka Touré"), "ali farka toure")
        self.assertEqual(sources.similarity("Ali Farka Toure", "Ali Farka Touré"), 1.0)
        self.assertEqual(sources.search_title("Hounds of Love (2018 Remaster)"), "Hounds of Love")
        self.assertEqual(sources.search_title("Rumours - Super Deluxe Edition"), "Rumours")
        self.assertEqual(sources.search_title("(What's the Story) Morning Glory?"), "(What's the Story) Morning Glory?")
        self.assertEqual(sources.tidy_genre("trip-hop"), "Trip-Hop")
        self.assertEqual(engine.normalise_name("Ali Farka Touré"), engine.normalise_name("ali farka toure"))

    def test_quotes_in_titles_are_escaped_for_musicbrainz(self):
        self.assertEqual(sources.MusicBrainz._quote('"Heroes"'), '"\\"Heroes\\""')


class SourceTests(unittest.TestCase):
    def test_musicbrainz_details_are_skipped_when_only_covers_are_needed(self):
        http = FakeHttp({"/release-group/rg-": {"genres": [{"name": "art pop", "count": 3}]},
                         "/release-group": MB_GROUPS})
        found = sources.MusicBrainz(http, {}).album("Kate Bush", "Hounds of Love", details=False)
        self.assertEqual(found[0]["cover"], "https://coverartarchive.org/release-group/rg-1/front-1200")
        self.assertEqual((found[0]["year"], found[0]["genres"]), (1985, []))
        self.assertEqual(len(http.calls), 1)
        detailed = sources.MusicBrainz(http, {}).album("Kate Bush", "Hounds of Love")
        self.assertEqual(detailed[0]["genres"], ["Art Pop"])

    def test_lookup_strips_edition_text_and_labels_errors(self):
        http = FakeHttp({"itunes.apple.com": ITUNES})
        lookup = sources.Lookup(lambda: {}, http=http)
        found = lookup.album("itunes", "Kate Bush", "Hounds of Love (2018 Remaster)")
        self.assertEqual(http.calls[0][1]["term"], "Kate Bush Hounds of Love")
        self.assertEqual(found[0]["cover"], "https://is1/1200x1200bb.jpg")
        self.assertEqual((found[0]["year"], found[0]["genres"]), (1985, ["Pop"]))
        with self.assertRaisesRegex(sources.SourceError, "Discogs needs a key"):
            lookup.album("discogs", "Kate Bush", "Hounds of Love")

        class Busy(FakeHttp):
            def json(self, *args, **kwargs):
                raise sources.SourceError("is busy; try again in a minute")
        with self.assertRaisesRegex(sources.SourceError, "^Deezer is busy"):
            sources.Lookup(lambda: {}, http=Busy({})).album("deezer", "A", "B")

    def test_enabled_sources_follow_the_keys(self):
        lookup = sources.Lookup(lambda: {"discogs_token": "t"}, http=FakeHttp({}))
        self.assertIn("discogs", lookup.enabled("albums"))
        self.assertNotIn("lastfm", lookup.enabled("albums"))
        self.assertEqual(lookup.enabled("artists"), ["deezer", "discogs", "theaudiodb"])

    def test_http_maps_refusals_and_busy_answers(self):
        class Response:
            def __init__(self, status):
                self.status_code, self.ok = status, status < 400
        http = sources.Http({"example.org": 0})
        for status, message in ((401, "check the key"), (429, "busy"), (503, "busy"), (500, "HTTP 500")):
            with patch.object(http.session, "get", return_value=Response(status)), \
                    self.assertRaisesRegex(sources.SourceError, message):
                http.json(f"https://example.org/{status}")
        with patch.object(http.session, "get", return_value=Response(404)) as get:
            self.assertIsNone(http.json("https://example.org/missing"))
            self.assertIsNone(http.json("https://example.org/missing"))  # cached
            get.assert_called_once()


class EngineSourceTests(unittest.TestCase):
    def setUp(self):
        self.args = engine.build_parser().parse_args([])
        self.args.no_theaudiodb = True
        self.limiter = engine.RateLimiter(self.args)
        self.limiter.http = FakeHttp({"musicbrainz.org/ws/2/release-group": MB_GROUPS,
                                      "api.deezer.com/search/album": DEEZER_ALBUMS,
                                      "itunes.apple.com": ITUNES})

    def test_album_candidates_use_keyless_sources_and_require_a_close_match(self):
        found = engine.album_candidates("Kate Bush", "Hounds of Love (2018 Remaster)", engine.Credentials(),
                                        self.args, None, self.limiter)
        self.assertEqual([(c.source, c.image_url) for c in found], [
            ("Cover Art Archive", "https://coverartarchive.org/release-group/rg-1/front-1200"),
            ("Deezer", "https://dz/xl.jpg"),
            ("Apple Music", "https://is1/1200x1200bb.jpg"),
        ])
        self.args.max_album_candidates_per_provider = 4
        self.args.min_album_match = 0.6  # the picker shows near matches too
        found = engine.album_candidates("Kate Bush", "Hounds of Love", engine.Credentials(),
                                        self.args, None, self.limiter)
        self.assertIn("https://dz/single.jpg", [c.image_url for c in found])

    def test_artist_candidates_include_deezer_pictures(self):
        self.limiter.http.answers = {"api.deezer.com/search/artist": {"data": [
            {"name": "Ali Farka Toure", "picture_xl": "https://dz/a.jpg"},
            {"name": "Ali Farka Touré & Toumani Diabaté", "picture_xl": "https://dz/duo.jpg"}]}}
        self.args.no_theaudiodb = True
        with patch.object(engine, "lookup_lastfm_artist_candidates", return_value=[]):
            found = engine.artist_candidates("Ali Farka Touré", engine.Credentials(), self.args, None, self.limiter)
        self.assertEqual([(c.source, c.image_url) for c in found], [("Deezer", "https://dz/a.jpg")])

    def test_rate_limiter_paces_provider_hosts_through_the_shared_client(self):
        self.limiter.wait("musicbrainz")
        self.limiter.wait("theaudiodb")
        self.assertEqual(self.limiter.http.waits, [("musicbrainz.org", 1.1), ("www.theaudiodb.com", 2.5)])
        self.assertIs(engine.RateLimiter(self.args).http, sources.shared_http())

    def test_find_best_image_checks_the_folder_and_rejects_small_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            album = Path(tmp) / "Kate Bush" / "Hounds of Love"
            album.mkdir(parents=True)
            track = album / "01.flac"
            track.write_bytes(FIXTURE.read_bytes())
            from mutagen.flac import FLAC
            audio = FLAC(track)
            audio.update({"artist": ["Kate Bush"], "albumartist": ["Kate Bush"], "album": ["Hounds of Love"]})
            audio.save()
            self.assertEqual(engine.folder_kind(album, Path(tmp)), "album")
            self.assertEqual(engine.folder_kind(album.parent, Path(tmp)), "artist")

            def candidate(size):
                from io import BytesIO
                data = BytesIO(); Image.new("RGB", (size, size)).save(data, "JPEG")
                return engine.ImageCandidate("Deezer", "Kate Bush", "Hounds of Love", image_bytes=data.getvalue())
            self.args.min_source_size = 300
            with patch.object(engine, "album_candidates", return_value=[candidate(200)]):
                image, source, notes = engine.find_best_image("album", album, self.args, engine.Credentials(), None, self.limiter)
            self.assertIsNone(image)
            self.assertIn("too small", notes)
            with patch.object(engine, "album_candidates", return_value=[candidate(200), candidate(600)]):
                image, source, notes = engine.find_best_image("album", album, self.args, engine.Credentials(), None, self.limiter)
            self.assertEqual((image.size, source), ((600, 600), "Deezer"))
            wrong = Path(tmp) / "Someone Else" / "Other"
            wrong.mkdir(parents=True)
            (wrong / "01.flac").write_bytes(track.read_bytes())
            image, _source, notes = engine.find_best_image("album", wrong, self.args, engine.Credentials(), None, self.limiter)
            self.assertIsNone(image)
            self.assertIn("do not match", notes)


class CredentialTests(unittest.TestCase):
    def test_old_secret_is_dropped_and_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conf" / "credentials.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"lastfm_api_key": "k", "lastfm_api_secret": "s", "fanart_api_key": "f"}))
            with patch.object(engine, "get_config_path", return_value=path):
                creds = engine.load_credentials()
                self.assertEqual((creds.lastfm_api_key, creds.fanart_api_key, creds.discogs_token), ("k", "f", ""))
                creds.discogs_token = " token "
                engine.save_credentials(creds)
                saved = json.loads(path.read_text())
                self.assertEqual(saved, {"lastfm_api_key": "k", "fanart_api_key": "f", "discogs_token": "token"})
                self.assertTrue(engine.load_credentials().has_discogs)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["credentials.json"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_cli_installs_a_discogs_token_and_ignores_the_old_secret_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            with patch.object(engine, "get_config_path", return_value=path), patch.object(sys, "stdout"):
                engine.main(["--install-credentials", "--discogs-token", "abc", "--lastfm-api-secret", "old"])
            self.assertEqual(json.loads(path.read_text())["discogs_token"], "abc")
            self.assertNotIn("lastfm_api_secret", path.read_text())
            self.assertIsInstance(engine.build_parser().parse_args([]), argparse.Namespace)


if __name__ == "__main__":
    unittest.main()
