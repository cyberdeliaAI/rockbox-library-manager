"""The real tag editor dialog against temporary tagged FLACs (needs a Tk display)."""
import shutil
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen.flac import FLAC

from rockbox_manager import gui, sources, tag_editor
from rockbox_manager.tag_editor import TagEditorDialog

FIXTURE = Path(__file__).parent / "fixtures" / "silence.flac"


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class TagEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-tag-editor-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.folder = self.base / "Music" / "Artist" / "Album"
        self.folder.mkdir(parents=True)
        self.files = []
        for number, genre in enumerate((["Pop", "Rock"], ["Rock"], ["Pop;Rock"]), 1):
            path = self.folder / f"{number:02}.flac"
            shutil.copyfile(FIXTURE, path)
            audio = FLAC(path)
            audio.update({"artist": ["Artist"], "albumartist": ["Artist"], "album": ["Album"],
                          "date": ["1985"], "genre": genre, "tracknumber": [str(number)]})
            audio.save()
            self.files.append(path)
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
        self.dialog = self.open()

    def open(self):
        dialog = TagEditorDialog(self.app, self.folder, "Artist", "Album")
        self.pump(lambda: dialog.save_btn.instate(["!disabled"]))
        return dialog

    def pump(self, done, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if done():
                return
            time.sleep(0.01)
        self.fail("timed out")

    def close_app(self):
        deadline = time.monotonic() + 8
        while self.app.operation_lock.locked() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.app.on_close()
        self.app.db.conn.close()
        self.assertFalse(self.errors, "\n".join(self.errors))

    def genres(self):
        return [FLAC(p).get("genre") for p in self.files]

    def test_several_values_are_shown_and_saved_as_one_value(self):
        hint = self.dialog.hints["genre"].cget("text")
        self.assertIn("1 track has several values (“Pop” + “Rock”)", hint)
        self.assertIn("Combined text such as “Pop;Rock”", hint)
        self.assertEqual(self.dialog.hints["date"].cget("text"), "current value")
        untouched = self.files[1].stat().st_mtime_ns
        self.dialog.use_suggestion("genre", "Rock", "MusicBrainz")
        with patch.object(tag_editor, "ask_review", return_value=True) as review:
            self.dialog.save_btn.invoke()
        self.pump(lambda: not self.dialog._alive())
        title, lines = review.call_args.args[2], review.call_args.args[3]
        self.assertEqual(title, "Save tags to 2 of 3 tracks?")
        self.assertEqual(lines, ["Genre: “Pop” + “Rock” → “Rock” · 1 track",
                                 "Genre: “Pop;Rock” → “Rock” · 1 track"])
        self.assertEqual(self.genres(), [["Rock"], ["Rock"], ["Rock"]])
        self.assertEqual(self.files[1].stat().st_mtime_ns, untouched)

    def test_unreadable_year_is_refused_and_back_keeps_files(self):
        before = [p.read_bytes() for p in self.files]
        self.dialog.change_vars["date"].set(True)
        self.dialog._toggle_field("date")
        self.dialog.vars["date"].set("c. 1985")
        with patch.object(tag_editor, "ask_review") as review:
            self.dialog.save_btn.invoke()
        review.assert_not_called()
        self.assertIn("YYYY", self.dialog.status.cget("text"))
        self.dialog.vars["date"].set("1986")
        with patch.object(tag_editor, "ask_review", return_value=False):
            self.dialog.save_btn.invoke()
        self.root.update()
        self.assertTrue(self.dialog._alive())
        self.assertEqual([p.read_bytes() for p in self.files], before)

    def test_unchanged_values_need_no_save(self):
        self.dialog.use_suggestion("date", "1985", "Deezer")
        with patch.object(tag_editor, "ask_review") as review:
            self.dialog.save_btn.invoke()
        review.assert_not_called()
        self.assertIn("Nothing to save", self.dialog.status.cget("text"))

    def test_online_suggestions_fill_the_form(self):
        def album(lookup, name, artist, title, details=True):
            self.assertEqual((artist, title), ("Artist", "Album"))
            if name == "deezer":
                raise sources.SourceError("Deezer is busy; try again in a minute.")
            if name == "musicbrainz":
                return [{"title": "Album", "artist": "Artist", "year": 1984, "note": "First release",
                         "genres": ["Art Pop", "Rock"], "score": 1.0}]
            return []
        with patch.object(sources.Lookup, "album", autospec=True, side_effect=album):
            self.dialog.find_suggestions()
            self.pump(lambda: self.dialog.pending_sources == 0)
        texts = {w.cget("text"): w for w in descendants(self.dialog.suggest_scroll.inner)
                 if isinstance(w, gui.tk.Label)}
        self.assertIn("Deezer is busy; try again in a minute.", texts)
        self.assertIn("MusicBrainz · Album", texts)
        for chip in ("1984", "Art Pop"):
            texts[chip].event_generate("<Button-1>")
        self.root.update()
        self.assertEqual((self.dialog.vars["date"].get(), self.dialog.vars["genre"].get()), ("1984", "Art Pop"))
        self.assertTrue(self.dialog.change_vars["date"].get())
        self.assertIn("from MusicBrainz", self.dialog.hints["genre"].cget("text"))
        self.assertIn("Click a year or genre", self.dialog.suggest_status.cget("text"))

    def test_review_dialog_confirms_and_cancels(self):
        def press(text):
            win = next(w for w in descendants(self.root) if isinstance(w, gui.tk.Toplevel)
                       and w.title() == "Review tag changes")
            next(b for b in descendants(win) if isinstance(b, gui.ttk.Button) and b.cget("text") == text).invoke()
        for text, expected in (("Save tags", True), ("Back", False)):
            self.root.after(100, lambda t=text: press(t))
            self.assertIs(tag_editor.ask_review(self.dialog.win, self.app.F, "Title", ["line"], ["warning"]), expected)

    def test_controls_visible_at_display_scaling_levels(self):
        self.dialog.win.destroy()
        for percent in (100, 150, 200):
            with self.subTest(percent=percent):
                self.root.tk.call("tk", "scaling", (96 / 72) * percent / 100)
                self.app.F = gui.make_fonts(self.root)
                gui.apply_style(self.root, self.app.F)
                dialog = self.open()
                win = dialog.win
                for button in (dialog.save_btn, dialog.lookup_btn,
                               *[b for b in descendants(win) if isinstance(b, gui.ttk.Button)]):
                    self.assertTrue(button.winfo_ismapped())
                    x = button.winfo_rootx() - win.winfo_rootx()
                    y = button.winfo_rooty() - win.winfo_rooty()
                    self.assertLessEqual(x + button.winfo_width(), win.winfo_width(), button.cget("text"))
                    self.assertLessEqual(y + button.winfo_height(), win.winfo_height(), button.cget("text"))
                win.destroy()


if __name__ == "__main__":
    unittest.main()
