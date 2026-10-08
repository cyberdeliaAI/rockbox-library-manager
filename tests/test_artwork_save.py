"""Real Tk artwork saves remain tied to the scanned root and pending selection."""
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from rockbox_manager import gui


class ArtworkSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-artwork-save-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.music = self.base / "Music"
        self.other = self.base / "OtherMusic"
        for root in (self.music, self.other):
            album = root / "Artist" / "Album"
            album.mkdir(parents=True)
            (album / "01.flac").write_bytes(b"scanner fixture")
        self.other_artwork = self.other / "Artist" / "folder.jpg"
        Image.new("RGB", (300, 300), "red").save(self.other_artwork)
        self.other_original = self.other_artwork.read_bytes()
        for target, name, value in ((gui, "local_app_dir", self.base / "Config"),
                                   (gui.engine, "get_config_path", self.base / "Config/credentials.json")):
            patcher = patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = gui.tk.Tk()
        self.errors = []
        self.root.report_callback_exception = lambda kind, value, tb: self.errors.append(
            "".join(traceback.format_exception(kind, value, tb)))
        self.app = gui.ArtworkApp(self.root)
        self.addCleanup(self.close_app)
        self.app.music_root_var.set(str(self.music))
        gui.FastLibraryScanner(self.app.db, self.music, "folder.jpg", lambda *_: None).scan()
        self.app.refresh_counts()
        self.app.reload_gallery(reset_scroll=True)
        self.item = self.app.db.physical_items("artist")[0]
        self.app.select_item(self.item.id)
        self.app.set_pending(Image.new("RGB", (600, 600), "blue"), "test image")
        self.root.update()

    def wait(self):
        deadline = time.monotonic() + 8
        while self.app.operation_lock.locked() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        # Drain the existing UI queue after the writer has released the lock.
        self.app._process_ui_queue()
        self.root.update()
        self.assertFalse(self.app.operation_lock.locked())

    def close_app(self):
        self.wait()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.app.on_close()
        self.app.db.conn.close()
        self.assertFalse(self.errors, "\n".join(self.errors))

    def test_settings_root_edit_blocks_old_pending_artwork(self):
        self.app.set_view("settings")
        self.app.music_root_var.set(str(self.other))
        with patch.object(gui.engine, "save_square_jpeg") as save:
            self.app.save_manual_artwork()
        save.assert_not_called()
        self.assertEqual(self.other_artwork.read_bytes(), self.other_original)
        self.assertIsNone(self.app.item_folder(self.item))

    def test_successful_new_root_scan_does_not_reassign_old_pending_artwork(self):
        self.app.music_root_var.set(str(self.other))
        gui.FastLibraryScanner(self.app.db, self.other, "folder.jpg", lambda *_: None).scan()
        self.assertEqual(self.app.db.physical_items("artist")[0].id, self.item.id)
        with patch.object(gui.engine, "save_square_jpeg") as save:
            self.app.save_manual_artwork()
        save.assert_not_called()
        self.assertEqual(self.other_artwork.read_bytes(), self.other_original)

    def test_unchanged_root_save_writes_artwork_and_updates_the_index(self):
        self.app.save_btn.invoke()
        self.wait()
        with Image.open(self.music / "Artist" / "folder.jpg") as image:
            self.assertEqual(image.size, (300, 300))
            self.assertGreater(image.getpixel((150, 150))[2], 240)
        self.assertTrue(self.app.db.get_item(self.item.id).artwork_exists)
        self.assertIsNone(self.app.pending_item_id)

    def test_equivalent_root_path_is_accepted(self):
        self.app.music_root_var.set(str(self.music / "Artist" / ".."))
        self.app.save_manual_artwork()
        self.wait()
        self.assertTrue((self.music / "Artist" / "folder.jpg").is_file())

    def test_save_waits_for_an_existing_operation(self):
        self.app.operation_lock.acquire()
        try:
            with patch.object(gui.engine, "save_square_jpeg") as save:
                self.app.save_manual_artwork()
            save.assert_not_called()
        finally:
            self.app.operation_lock.release()

    def test_save_failure_releases_the_lock_and_keeps_pending_artwork(self):
        with patch.object(gui.engine, "save_square_jpeg", side_effect=OSError("simulated failure")), \
                patch.object(gui.messagebox, "showerror") as error:
            self.app.save_manual_artwork()
            self.wait()
        self.assertTrue(error.called)
        self.assertEqual(self.app.pending_item_id, self.item.id)
        self.assertFalse((self.music / "Artist" / "folder.jpg").exists())

    def test_late_save_result_does_not_mark_a_different_root_as_written(self):
        old_result = {"saved": [(self.item.id, "Artist", "folder.jpg", (1, 10))], "failed": [],
                      "music_root": str(self.music)}
        self.other_artwork.unlink()
        self.app.music_root_var.set(str(self.other))
        gui.FastLibraryScanner(self.app.db, self.other, "folder.jpg", lambda *_: None).scan()
        self.app._on_saved(self.item.id, old_result)
        self.assertFalse(self.app.db.get_item(self.item.id).artwork_exists)


if __name__ == "__main__":
    unittest.main()
