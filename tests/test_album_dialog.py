"""Actual Tk album details and update controls, against a temporary music library."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rockbox_manager import gui
from rockbox_manager.album_details import AlbumDetails
from rockbox_manager.album_dialog import AlbumDialog
from rockbox_manager.library import FastLibraryScanner
from rockbox_manager.updates import RELEASE_PAGE
from test_tags_health import track


class AlbumDialogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-album-ui-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for target, name, value in ((gui, "local_app_dir", self.base / "Config"),
                                   (gui.engine, "get_config_path", self.base / "Config/credentials.json")):
            patcher = patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.music = self.base / "Music"
        self.album = self.music / "Artist/Album"
        for name, number, title, genre in (("01.flac", "1", "Björk", "Rock"),
                                          ("02.flac", "1", "BjÖrk", ""), ("03.flac", "3", "", "Jazz")):
            track(self.album, name, title=title, genre=genre, tracknumber=number,
                  artist="Artist", albumartist="Artist", album="Album", discnumber="1")
        self.root = gui.tk.Tk()
        self.errors = []
        self.root.report_callback_exception = lambda kind, value, tb: self.errors.append(str(value))
        self.network = patch("rockbox_manager.updates.requests.Session")
        self.request = self.network.start()
        self.addCleanup(self.network.stop)
        self.app = gui.ArtworkApp(self.root)
        self.app.music_root_var.set(str(self.music))
        FastLibraryScanner(self.app.db, self.music, "folder.jpg", lambda *_: None).scan()
        self.dialog = None
        self.addCleanup(self.cleanup)
        self.root.update()

    def cleanup(self):
        if self.dialog is not None and self.dialog.alive():
            self.dialog.close()
        if self.dialog is not None:
            self.wait(lambda: not self.dialog.running)
        self.app.auto_updates_var.set(False)
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.app.on_close()
        self.app.db.conn.close()

    def wait(self, done):
        deadline = time.monotonic() + 10
        while not done() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.root.update()
        self.assertTrue(done())
        self.assertFalse(self.errors, "\n".join(self.errors))

    def open(self):
        item = self.app.db.physical_items("album")[0]
        self.app.selected_item_id = item.id
        self.app.show_detail(item)
        self.app.album_details_btn.invoke()
        self.dialog = self.app._album_dialog
        self.wait(lambda: not self.dialog.running)

    def test_button_loads_tracks_search_filters_and_readonly_notes(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.album.iterdir()}
        self.open()
        self.assertEqual(len(self.dialog.tree.get_children()), 3)
        self.assertIn("3 tracks", self.dialog.summary.cget("text"))
        self.dialog.search_var.set("bjork")
        self.assertEqual(len(self.dialog.tree.get_children()), 2)
        self.dialog.search_var.set("")
        self.dialog.filter_var.set("Possible duplicates")
        self.assertEqual(len(self.dialog.tree.get_children()), 2)
        self.dialog.filter_var.set("Missing genre")
        self.assertEqual(len(self.dialog.tree.get_children()), 1)
        self.dialog.tree.selection_set("1")
        self.root.update()
        self.assertIn("audio was not compared", self.dialog.notes.get("1.0", "end"))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        self.request.assert_not_called()

    def test_busy_operation_can_be_retried_and_close_cancels_a_read(self):
        self.app.operation_lock.acquire()
        self.dialog = AlbumDialog(self.app, self.album, "Artist", "Album")
        self.assertFalse(self.dialog.running)
        self.assertIn("Another operation", self.dialog.status.cget("text"))
        self.app.operation_lock.release()
        self.dialog.refresh_btn.invoke()
        self.wait(lambda: not self.dialog.running)
        self.assertEqual(len(self.dialog.data.tracks), 3)
        started, release = threading.Event(), threading.Event()

        def read(folder, cancel):
            started.set()
            release.wait(3)
            return AlbumDetails(cancelled=cancel.is_set())

        with patch("rockbox_manager.album_dialog.read_album", side_effect=read):
            self.dialog.load()
            try:
                self.assertTrue(started.wait(2))
                self.dialog.close()
                self.assertTrue(self.dialog.cancel.is_set())
            finally:
                release.set()
            self.wait(lambda: not self.dialog.running)
        self.assertFalse(self.app.operation_lock.locked())

    def test_manual_updates_are_asynchronous_and_open_only_fixed_repository_links(self):
        self.assertFalse(self.app.auto_updates_var.get())
        self.request.assert_not_called()
        result = {"current": "1.3.0", "latest": "9.0.0", "available": True,
                  "url": "https://evil.example/", "error": ""}
        with patch.object(self.app.update_checker, "check", return_value=result) as check:
            self.app.update_check_btn.invoke()
            self.assertTrue(self.app._update_running)
            self.wait(lambda: not self.app._update_running)
            check.assert_called_once_with(enabled=False, manual=True)
        self.assertIn("9.0.0", self.app.update_status.cget("text"))
        with patch.object(gui.webbrowser, "open") as browser:
            self.app.update_open_btn.invoke()
            browser.assert_called_once_with(RELEASE_PAGE + "9.0.0")
            browser.reset_mock()
            self.app._update_result = {"latest": "1.2.2", "available": False}
            self.app.update_open_btn.invoke()
            browser.assert_called_once_with("https://github.com/cyberdeliaAI/rockbox-library-manager/releases")
        self.app.auto_updates_var.set(True)
        with patch.object(self.app, "check_updates") as automatic:
            self.app._toggle_auto_updates()
            automatic.assert_called_once()
        self.assertTrue(gui.load_gui_config()["auto_updates"])

    def test_update_failure_does_not_disturb_library_or_lock(self):
        with patch.object(self.app.update_checker, "check", side_effect=OSError("secret")):
            self.app.check_updates(manual=True)
            self.wait(lambda: not self.app._update_running)
        self.assertNotIn("secret", self.app.update_status.cget("text"))
        self.assertIn("Could not check", self.app.update_status.cget("text"))
        self.assertTrue(self.app.update_check_btn.instate(["!disabled"]))
        self.assertFalse(self.app.operation_lock.locked())
        self.assertEqual(self.app.db.counts()["albums"], 1)

    def test_large_fonts_keep_controls_visible_and_headers_readable(self):
        self.app.F.small_b.configure(size=20)
        self.app.F.small.configure(size=18)
        self.open()
        self.dialog.win.geometry("900x580")
        self.root.update()
        for key in self.dialog.tree["columns"]:
            label = self.dialog.tree.heading(key, "text")
            self.assertGreaterEqual(self.dialog.tree.column(key, "width"), self.app.F.small_b.measure(label) + 28)
        for button in (self.dialog.close_btn, self.dialog.refresh_btn, self.dialog.filter_box):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rooty() - self.dialog.win.winfo_rooty() + button.winfo_height(),
                                 self.dialog.win.winfo_height())
        self.assertEqual(gui.ttk.Style(self.root).lookup("Album.TCombobox", "foreground", ("readonly",)), gui.C["text"])
