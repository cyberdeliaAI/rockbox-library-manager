"""Exercise the actual backup/update dialog with a temporary iPod."""
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen.flac import FLAC
from rockbox_manager import gui, rockbox_database as rb
from rockbox_manager.database_dialog import DatabaseDialog
from test_rockbox_database import make_device


class DatabaseDialogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rockbox-db-ui-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.device, self.tracks = make_device(self.base, paths=[
            "/<HDD0>/Music/Old Artist/Album/01.flac",
            "/<HDD0>/Music/Other/Album/02.flac",
        ])
        for target, name, value in [(gui, "local_app_dir", self.base / "Config"),
                                    (gui.engine, "get_config_path", self.base / "Config/credentials.json")]:
            patcher = patch.object(target, name, return_value=value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.root = gui.tk.Tk()
        self.errors = []
        self.root.report_callback_exception = lambda kind, value, tb: self.errors.append(
            "".join(traceback.format_exception(kind, value, tb)))
        self.app = gui.ArtworkApp(self.root)
        self.app.music_root_var.set(str(self.device / "Music"))
        self.addCleanup(self.cleanup)
        self.dialog = DatabaseDialog(self.app, self.device / "Music", self.base / "backups", gui.C, lambda _: None)
        self.root.update()

    def cleanup(self):
        self.wait()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.app.on_close()
        self.app.db.conn.close()

    def wait(self):
        deadline = time.monotonic() + 10
        while getattr(self, "dialog", None) and self.dialog.running and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(getattr(self, "dialog", None) and self.dialog.running, "Database worker timed out")
        self.assertFalse(self.errors, "\n".join(self.errors))

    def test_preview_decline_apply_then_apply_and_restore(self):
        before = rb.snapshot(self.device)
        audio = FLAC(self.tracks[0]); audio["album"] = ["New Album"]; audio.save()
        self.dialog.buttons[0].invoke()
        self.wait()
        self.assertEqual(self.dialog.plan.changed_tracks, 1)
        self.assertTrue(self.dialog.apply_btn.instate(["!disabled"]))
        with patch("rockbox_manager.database_dialog.messagebox.askyesno", return_value=False):
            self.dialog.apply_btn.invoke()
        self.assertEqual(rb.snapshot(self.device), before)
        self.assertFalse(self.dialog.backup_root.exists())
        with patch("rockbox_manager.database_dialog.messagebox.askyesno", return_value=True):
            self.dialog.apply_btn.invoke()
        self.wait()
        self.assertIn("reboot", self.dialog.text.get("1.0", "end"))
        backups = list(self.dialog.backup_root.glob("*/manifest.json"))
        self.assertEqual(len(backups), 1)
        with patch("rockbox_manager.database_dialog.filedialog.askopenfilename", return_value=str(backups[0])), \
             patch("rockbox_manager.database_dialog.messagebox.askyesno", return_value=True):
            self.dialog.buttons[2].invoke()
        self.wait()
        self.assertEqual(rb.snapshot(self.device), {n: b for n, b in before.items() if n != rb.STATE})
        self.assertEqual(len(list(self.dialog.backup_root.glob("*/manifest.json"))), 2)

    def test_manual_backup_button_and_quit_guard(self):
        before = rb.snapshot(self.device)
        self.dialog.buttons[1].invoke()
        with patch.object(gui.messagebox, "showinfo") as info:
            self.app.on_close()
            info.assert_called_once()
        self.wait()
        self.assertEqual(rb.snapshot(self.device), before)
        self.assertEqual(len(list(self.dialog.backup_root.glob("*/manifest.json"))), 1)
        for button in [*self.dialog.buttons, self.dialog.apply_btn, self.dialog.close_btn]:
            self.assertTrue(button.winfo_ismapped())
            bottom = button.winfo_rooty() - self.dialog.win.winfo_rooty() + button.winfo_height()
            self.assertLessEqual(bottom, self.dialog.win.winfo_height())

    def test_worker_error_is_shown_and_releases_lock(self):
        with patch("rockbox_manager.database_dialog.database.preview_update", side_effect=rb.DatabaseError("Unsupported format")), \
             patch("rockbox_manager.database_dialog.messagebox.showerror") as error:
            self.dialog.buttons[0].invoke()
            self.wait()
            error.assert_called_once()
        self.assertFalse(self.app.operation_lock.locked())
        self.assertTrue(self.dialog.apply_btn.instate(["disabled"]))
        self.assertIn("Unsupported format", self.dialog.text.get("1.0", "end"))
