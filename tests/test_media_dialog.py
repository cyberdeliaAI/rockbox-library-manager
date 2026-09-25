"""Test the real media dialog and settings against a temporary library."""
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from rockbox_manager import gui
from rockbox_manager.media_dialog import MediaDialog


class MediaDialogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-media-ui-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.music = self.base / "Music"
        for folder, size in (("Large", 600), ("Small", 200)):
            path = self.music / folder
            path.mkdir(parents=True)
            Image.new("RGB", (size, size), "blue").save(path / "folder.jpg")
        for target, name, value in ((gui, "local_app_dir", self.base / "Config"),
                                   (gui.engine, "get_config_path", self.base / "Config/credentials.json")):
            patcher = patch.object(target, name, return_value=value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.root = gui.tk.Tk()
        self.errors = []
        self.root.report_callback_exception = lambda kind, value, tb: self.errors.append("".join(traceback.format_exception(kind, value, tb)))
        self.app = gui.ArtworkApp(self.root)
        self.app.music_root_var.set(str(self.music))
        self.addCleanup(self.cleanup)
        self.dialog = MediaDialog(self.app, self.music, self.base / "Config", gui.C, lambda _: None)
        self.root.update()

    def cleanup(self):
        self.wait()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.app.on_close()
        self.app.db.conn.close()

    def wait(self):
        deadline = time.monotonic() + 10
        while self.dialog.running and time.monotonic() < deadline:
            self.root.update(); time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.dialog.running)
        self.assertFalse(self.errors, "\n".join(self.errors))

    def test_scan_select_decline_then_resize_and_preserve_small_source(self):
        before = (self.music / "Large/folder.jpg").read_bytes()
        small = (self.music / "Small/folder.jpg").read_bytes()
        self.dialog.scan_btn.invoke()
        with patch.object(gui.messagebox, "showinfo") as info:
            self.app.on_close()
            info.assert_called_once()
        self.wait()
        self.assertEqual(len(self.dialog.candidates), 2)
        self.assertTrue(self.dialog.apply_btn.instate(["disabled"]))
        small_key = next(k for k, c in self.dialog.candidates.items() if c.kind == "small_artwork")
        self.dialog.tree.selection_set(small_key)
        self.root.update()
        self.assertTrue(self.dialog.apply_btn.instate(["disabled"]))
        self.assertTrue(self.dialog.find_btn.instate(["!disabled"]))
        self.dialog.select("all")
        self.root.update()
        self.assertEqual(len(self.dialog.selected()), 1)
        with patch("rockbox_manager.media_dialog.messagebox.askyesno", return_value=False):
            self.dialog.apply_btn.invoke()
        self.assertEqual((self.music / "Large/folder.jpg").read_bytes(), before)
        with patch("rockbox_manager.media_dialog.messagebox.askyesno", return_value=True):
            self.dialog.apply_btn.invoke()
        self.wait()
        self.assertIn("1 files replaced", self.dialog.status.cget("text"))
        self.assertEqual(len(self.dialog.candidates), 1)
        self.assertEqual((self.music / "Small/folder.jpg").read_bytes(), small)
        backups = list((self.base / "Config/media-backups").glob("*/originals/Large/folder.jpg"))
        self.assertEqual(backups[0].read_bytes(), before)
        for button in (self.dialog.apply_btn, self.dialog.find_btn, self.dialog.close_btn, self.dialog.cancel_btn):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rooty() - self.dialog.win.winfo_rooty() + button.winfo_height(), self.dialog.win.winfo_height())

    def test_settings_persist_and_backup_disabled_confirmation_is_explicit(self):
        self.dialog.close()
        self.app.media_backup_var.set(False)
        self.app.output_size_var.set("500")
        self.app.ffmpeg_path_var.set("C:/Tools/ffmpeg.exe")
        self.app.save_settings()
        settings = gui.load_gui_config()
        self.assertFalse(settings["media_backup"])
        self.assertEqual(settings["output_size"], 500)
        self.assertEqual(settings["ffmpeg_path"], "C:/Tools/ffmpeg.exe")
        self.dialog = MediaDialog(self.app, self.music, self.base / "Config", gui.C, lambda _: None)
        self.dialog.scan_btn.invoke(); self.wait()
        self.dialog.select("artwork"); self.root.update()
        with patch("rockbox_manager.media_dialog.messagebox.askyesno", return_value=True) as confirm:
            self.dialog.apply_btn.invoke()
        self.assertIn("LOCAL BACKUPS ARE OFF", confirm.call_args.args[1])
        self.wait()
        self.assertFalse((self.base / "Config/media-backups").exists())
        with Image.open(self.music / "Large/folder.jpg") as image:
            self.assertEqual(image.size, (500, 500))

    def test_worker_error_releases_lock_and_allows_rescan(self):
        with patch("rockbox_manager.media_dialog.media.scan", side_effect=OSError("Disconnected")), \
             patch("rockbox_manager.media_dialog.messagebox.showerror") as error:
            self.dialog.scan_btn.invoke(); self.wait()
            error.assert_called_once()
        self.assertFalse(self.app.operation_lock.locked())
        self.assertTrue(self.dialog.scan_btn.instate(["!disabled"]))
        self.assertIn("Disconnected", self.dialog.details.get("1.0", "end"))
        self.dialog.scan_btn.invoke(); self.wait()
        self.assertEqual(len(self.dialog.candidates), 2)

    def test_actions_and_results_remain_visible_at_display_scaling_levels(self):
        self.dialog.close()
        for percent in (100, 125, 150, 200):
            with self.subTest(percent=percent):
                self.root.tk.call("tk", "scaling", (96 / 72) * percent / 100)
                self.app.F = gui.make_fonts(self.root)
                gui.apply_style(self.root, self.app.F)
                self.dialog = MediaDialog(self.app, self.music, self.base / "Config", gui.C, lambda _: None)
                self.root.update()
                for button in [*self.dialog.controls, self.dialog.apply_btn, self.dialog.find_btn, self.dialog.cancel_btn, self.dialog.close_btn]:
                    self.assertTrue(button.winfo_ismapped())
                    x = button.winfo_rootx() - self.dialog.win.winfo_rootx()
                    y = button.winfo_rooty() - self.dialog.win.winfo_rooty()
                    self.assertLessEqual(x + button.winfo_width(), self.dialog.win.winfo_width())
                    self.assertLessEqual(y + button.winfo_height(), self.dialog.win.winfo_height())
                self.assertGreater(self.dialog.tree.winfo_height(), 60)
                self.dialog.close()
