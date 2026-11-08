"""Real Tk background controls use temporary libraries and recorded provider responses."""
import threading
import unittest
from unittest.mock import Mock, patch

import test_album_dialog as album_ui
from rockbox_manager import info_dialog


class InformationDialogTests(unittest.TestCase):
    setUp = album_ui.AlbumDialogTests.setUp
    cleanup = album_ui.AlbumDialogTests.cleanup
    wait = album_ui.AlbumDialogTests.wait

    def open(self, kind="album"):
        item = self.app.db.physical_items(kind)[0]
        self.app.selected_item_id = item.id
        self.app.show_detail(item)
        self.app.background_btn.invoke()
        self.dialog = self.app._information_dialog
        self.root.update()

    def test_opening_is_offline_and_explicit_fetch_is_readonly_and_cached(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.album.iterdir()}
        http = Mock()
        http.json.return_value = {"album": [{"strArtist": "Artist", "strAlbum": "Album", "idAlbum": "456",
                                              "strDescription": "<p>Album background.</p>"}]}
        self.app.online_information.http = http
        self.open()
        http.json.assert_not_called()
        self.assertFalse(self.app.online_information.path.exists())
        self.dialog.fetch_btn.invoke()
        self.wait(lambda: not self.dialog.running)
        self.assertIn("Album background.", self.dialog.text.get("1.0", "end"))
        self.assertEqual(str(self.dialog.text.cget("state")), "disabled")
        self.dialog.fetch_btn.invoke()
        self.wait(lambda: not self.dialog.running)
        http.json.assert_called_once()
        self.assertIn("Local cache", self.dialog.status.cget("text"))
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})
        with patch.object(info_dialog.webbrowser, "open") as browser:
            self.dialog.source_btn.invoke()
            browser.assert_called_once_with("https://www.theaudiodb.com/album/456")
            self.dialog.url = "https://evil.example/"
            self.dialog.open_source()
            self.assertEqual(browser.call_count, 1)

    def test_artist_selection_busy_retry_failure_and_close_during_fetch(self):
        self.open("artist")
        self.assertEqual(self.dialog.kind, "artist")
        self.app.operation_lock.acquire()
        try:
            self.dialog.fetch_btn.invoke()
            self.assertFalse(self.dialog.running)
            self.assertIn("Another operation", self.dialog.status.cget("text"))
        finally:
            self.app.operation_lock.release()
        with patch.object(self.app.online_information, "get", side_effect=RuntimeError("private-token")):
            self.dialog.fetch_btn.invoke()
            self.wait(lambda: not self.dialog.running)
        self.assertNotIn("private-token", self.dialog.status.cget("text"))
        self.assertTrue(self.dialog.fetch_btn.instate(["!disabled"]))
        started, release = threading.Event(), threading.Event()

        def fetch(*args):
            started.set()
            release.wait(3)
            return None

        with patch.object(self.app.online_information, "get", side_effect=fetch):
            self.dialog.fetch_btn.invoke()
            try:
                self.assertTrue(started.wait(2))
                self.dialog.close()
            finally:
                release.set()
            self.wait(lambda: not self.dialog.running)
        self.assertFalse(self.app.operation_lock.locked())

    def test_small_window_with_large_fonts_keeps_actions_visible(self):
        self.app.F.body.configure(size=19)
        self.app.F.small.configure(size=18)
        self.open()
        self.dialog.win.geometry("600x420")
        self.root.update()
        for button in (self.dialog.fetch_btn, self.dialog.source_btn, self.dialog.close_btn):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rooty() - self.dialog.win.winfo_rooty() + button.winfo_height(),
                                 self.dialog.win.winfo_height())
