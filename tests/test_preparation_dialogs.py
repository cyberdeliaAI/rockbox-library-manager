"""Real Tk controls, worker coordination, reviewed exports and guest suggestions."""
import threading
import unittest
from unittest.mock import patch

from mutagen.flac import FLAC

from rockbox_manager import podbox_tools as pb
from rockbox_manager.album_dialog import AlbumDialog
import test_album_dialog as album_tests


class PreparationDialogTests(unittest.TestCase):
    setUp = album_tests.AlbumDialogTests.setUp
    wait = album_tests.AlbumDialogTests.wait

    def cleanup(self):
        if self.dialog is not None:
            self.wait(lambda: not self.dialog.running)
        album_tests.AlbumDialogTests.cleanup(self)

    def test_podbox_button_background_preview_review_and_apply(self):
        self.app.open_podbox_tools()
        self.dialog = self.app._podbox_dialog
        self.assertIn("disabled", self.dialog.apply_btn.state())
        device, work = self.base / "Player", self.base / "Preview"
        work.mkdir()
        self.dialog.device_var.set(str(device))
        plan = pb.PodBoxPlan(device, "update", work, {}, {"database/new.tcd": {"size": 1}}, {}, {}, 0,
                             "1 tracks: 1 new, 0 changed, 0 gone.", ["database/new.tcd"])
        threads = []
        def preview(*args, **kwargs):
            threads.append(threading.get_ident())
            return plan
        with patch.object(pb, "preview", side_effect=preview):
            self.dialog.preview_btn.invoke()
            self.wait(lambda: not self.dialog.running)
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertNotIn("disabled", self.dialog.apply_btn.state())
        self.assertIn("Add: database/new.tcd", self.dialog.text.get("1.0", "end"))
        with patch("rockbox_manager.podbox_dialog.messagebox.askyesno", return_value=True), \
                patch.object(pb, "apply", return_value=self.base / "Verified backup") as apply:
            self.dialog.apply_btn.invoke()
            self.wait(lambda: not self.dialog.running)
            apply.assert_called_once_with(plan, self.dialog.backups)
        self.assertIsNone(self.dialog.plan)
        self.assertFalse(self.app.operation_lock.locked())

    def test_preview_cancellation_blocks_close_until_worker_finishes(self):
        self.app.open_podbox_tools()
        self.dialog = self.app._podbox_dialog
        entered = threading.Event()
        def preview(*args, cancel, **kwargs):
            entered.set()
            cancel.wait(3)
            raise pb.PodBoxError("Preview cancelled; player unchanged.")
        with patch.object(pb, "preview", side_effect=preview), \
                patch("rockbox_manager.podbox_dialog.messagebox.showinfo") as info:
            self.dialog.preview_btn.invoke()
            self.assertTrue(entered.wait(2))
            self.dialog.close()
            info.assert_called_once()
            self.assertTrue(self.dialog.alive())
            self.dialog.cancel_btn.invoke()
            self.wait(lambda: not self.dialog.running)
        self.assertIn("cancelled", self.dialog.text.get("1.0", "end"))
        self.assertFalse(self.app.operation_lock.locked())

    def test_eq_real_fit_plot_export_and_busy_retry(self):
        self.app.open_eq_refit()
        self.dialog = self.app._eq_dialog
        preset = self.base / "eq.cfg"
        original = "# note\neq precut: 80\neq low shelf filter: 60, 7, 50\neq peak filter 1: 250, 10, 20\neq high shelf filter: 14000, 7, -40\n"
        preset.write_text(original)
        self.dialog.path.set(str(preset))
        self.dialog.bands.set("2")
        self.dialog.tolerance.set("2.0")
        self.app.operation_lock.acquire()
        self.dialog.preview_btn.invoke()
        self.assertFalse(self.dialog.running)
        self.assertIn("Another operation", self.dialog.status.cget("text"))
        self.app.operation_lock.release()
        self.dialog.preview_btn.invoke()
        self.wait(lambda: not self.dialog.running)
        self.assertLess(self.dialog.plan.after, self.dialog.plan.before)
        self.assertTrue(self.dialog.canvas.find_all())
        destination = self.base / "refit.cfg"
        with patch("rockbox_manager.eq_dialog.filedialog.asksaveasfilename", return_value=str(destination)):
            self.dialog.export_btn.invoke()
        self.assertTrue(destination.is_file())
        self.assertEqual(preset.read_text(), original)
        self.dialog.tolerance.set("0.5")
        self.assertIsNone(self.dialog.plan)
        self.assertIn("disabled", self.dialog.export_btn.state())

    def test_guest_filter_and_notes_are_readonly(self):
        path = self.album / "01.flac"
        audio = FLAC(path)
        audio["title"] = ["Song (feat. Guest One)"]
        audio.save()
        before = path.read_bytes()
        self.dialog = AlbumDialog(self.app, self.album, "Artist", "Album")
        self.wait(lambda: not self.dialog.running)
        self.dialog.filter_var.set("Guest credits")
        children = self.dialog.tree.get_children()
        self.assertEqual(len(children), 1)
        self.dialog.tree.selection_set(children[0])
        self.root.update()
        self.assertIn("Suggested guest credits: Guest One", self.dialog.notes.get("1.0", "end"))
        self.assertIn("No tags are changed", self.dialog.notes.get("1.0", "end"))
        self.assertEqual(path.read_bytes(), before)
