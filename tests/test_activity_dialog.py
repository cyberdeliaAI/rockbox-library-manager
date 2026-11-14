"""Real Tk import/review/send and cancellable integrity work on temporary data."""
import threading
import time
import unittest
from unittest.mock import patch

import test_album_dialog as album_tests
from test_listening import log_line
from rockbox_manager import activity_dialog as ui, scrobbling


class ActivityDialogTests(unittest.TestCase):
    setUp = album_tests.AlbumDialogTests.setUp
    wait = album_tests.AlbumDialogTests.wait

    def cleanup(self):
        if self.dialog is not None:
            self.wait(lambda: not self.dialog.running)
        album_tests.AlbumDialogTests.cleanup(self)

    def open(self):
        self.app.open_listening_history()
        self.dialog = self.app._listening_dialog
        self.wait(lambda: not self.dialog.running)
        return self.dialog

    def import_log(self):
        path=self.base/".scrobbler.log"
        path.write_text("#TZ/UTC\n"+log_line(int(time.time())-300,title="Björk 日本語"),encoding="utf-8")
        self.dialog.path.set(str(path))
        self.dialog.preview_log()
        self.wait(lambda: not self.dialog.running)
        with patch.object(ui.messagebox,"askyesno",return_value=True):
            self.dialog.import_log()
            self.wait(lambda: not self.dialog.running)
        return path

    def test_open_preview_import_statistics_and_export_are_offline(self):
        with patch.object(scrobbling,"request_json",side_effect=AssertionError("Opening/importing is offline")) as http:
            dialog=self.open()
            self.assertFalse(dialog.enabled.get())
            self.assertIn("disabled",dialog.send_btn.state())
            path=self.import_log()
            self.assertEqual(dialog.store.page()[1],1)
            self.assertIn("Listened: 1",dialog.stats_text.get("1.0","end"))
            self.assertIn("Björk 日本語",dialog.stats_text.get("1.0","end"))
            before=path.read_bytes()
            target=self.base/"export.json"
            with patch.object(ui.filedialog,"asksaveasfilename",return_value=str(target)):
                dialog.export()
                self.wait(lambda: not dialog.running)
            self.assertTrue(target.exists())
            self.assertEqual(path.read_bytes(),before)
            http.assert_not_called()

    def test_scrobbling_requires_opt_in_review_confirmation_and_remains_deduplicated(self):
        dialog=self.open()
        self.import_log()
        credentials={"username":"Test", "token":"a"*32}
        dialog.accounts["listenbrainz"]=credentials
        dialog.service.set("listenbrainz")
        self.wait(lambda: not dialog.running)
        row=dialog.tree.get_children()[0]
        dialog.tree.selection_set(row)
        self.root.update()
        dialog.review()
        self.assertIsNone(dialog.plan)
        dialog.enabled.set(True)
        dialog.review()
        self.wait(lambda: not dialog.running)
        self.assertEqual(len(dialog.plan.events),1)
        with patch.object(scrobbling,"request_json",return_value={"status":"ok"}) as http:
            with patch.object(ui.messagebox,"askyesno",return_value=False):
                dialog.send()
                http.assert_not_called()
            with patch.object(ui.messagebox,"askyesno",return_value=True):
                dialog.send()
                self.wait(lambda: not dialog.running)
            self.assertEqual(http.call_count,1)
        self.assertIn("accepted: 1",dialog.review_text.get("1.0","end"))
        row=dialog.tree.get_children()[0]
        self.assertEqual(dialog.tree.item(row,"values")[-1],"accepted")
        dialog.tree.selection_set(row)
        self.root.update()
        dialog.review()
        self.wait(lambda: not dialog.running)
        self.assertEqual(len(dialog.plan.events),0)
        self.assertFalse(self.app.operation_lock.locked())

    def test_integrity_runs_in_worker_and_close_waits_for_cancellation(self):
        self.app.open_audio_integrity()
        self.dialog=self.app._integrity_dialog
        entered=threading.Event()
        threads=[]
        def scan(root,ffmpeg,cancel,progress):
            threads.append(threading.get_ident())
            entered.set()
            cancel.wait(3)
            return ui.integrity.IntegrityResult(cancelled=True)
        with patch.object(ui.integrity,"scan",side_effect=scan):
            self.dialog.scan_btn.invoke()
            self.assertTrue(entered.wait(2))
            self.dialog.close()
            self.assertTrue(self.dialog.alive())
            with patch("rockbox_manager.gui.messagebox.showinfo") as info:
                self.app.on_close()
                info.assert_called_once()
            self.dialog.cancel_btn.invoke()
            self.wait(lambda: not self.dialog.running)
        self.assertNotEqual(threads[0],threading.get_ident())
        self.assertIn("Cancelled",self.dialog.status.cget("text"))
        self.assertFalse(self.app.operation_lock.locked())

    def test_small_window_keeps_history_actions_and_results_visible(self):
        self.root.tk.call("tk", "scaling", 2.0)
        dialog=self.open()
        dialog.win.geometry("850x640")
        self.import_log()
        self.root.update()
        self.assertGreater(dialog.tree.winfo_height(),60)
        for button in (dialog.import_btn,dialog.cancel_btn):
            self.assertTrue(button.winfo_ismapped())
            bottom=button.winfo_rooty()+button.winfo_height()-dialog.win.winfo_rooty()
            self.assertLess(bottom,dialog.win.winfo_height())
