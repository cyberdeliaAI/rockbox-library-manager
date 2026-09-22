"""Regression checks for the real duplicate-resolver UI.

Run from the package directory: python -m unittest discover -s tests -v
Requires a desktop/Tk display (Linux CI can use xvfb-run). All data, artwork,
FLAC tags, SQLite rows and settings used by these tests are temporary.
"""
from __future__ import annotations

import ast
import hashlib
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rockbox_manager import gui
from mutagen.flac import FLAC
from PIL import Image

FIXTURE = Path(__file__).parent / 'fixtures' / 'silence.flac'
ALIASES = ['Ali Farka Toure', 'Ali Farka Touré']
BUTTONS = {'Keep separate', 'Cancel', 'Fix folders on disk', 'Merge in library'}


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DuplicateDialogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rockbox-dialog-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.music = self.base / 'Music'
        self.music.mkdir()
        patcher = patch.object(gui, 'local_app_dir', return_value=self.base / 'Config')
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(gui.engine, 'get_config_path', return_value=self.base / 'Config' / 'credentials.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = gui.tk.Tk()
        self.errors = []
        self.root.report_callback_exception = lambda kind, value, tb: self.errors.append(
            ''.join(traceback.format_exception(kind, value, tb)))
        self.app = gui.ArtworkApp(self.root)
        self.addCleanup(self.close_app)
        self.app.music_root_var.set(str(self.music))
        self.files = []
        for name, album in zip(ALIASES, ['Album A', 'Album B']):
            folder = self.music / name / album
            folder.mkdir(parents=True)
            track = folder / '01 Silence.flac'
            track.write_bytes(FIXTURE.read_bytes())
            tags = FLAC(track)
            tags['artist'] = [name]
            tags['albumartist'] = [name]
            tags['album'] = [album]
            tags['title'] = ['Silence']
            tags.save()
            self.files.append(track)
        gui.FastLibraryScanner(self.app.db, self.music, 'folder.jpg', lambda *_: None).scan()
        gui.HealthScanner(self.app.db, self.music, lambda *_: None).scan()
        self.app.refresh_counts()
        self.app.set_view('health')
        self.root.update()

    def close_app(self):
        deadline = time.monotonic() + 8
        while self.app.operation_lock.locked() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        for job in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.tk.call('after', 'cancel', job)
        self.app.on_close()
        self.app.db.conn.close()

    def open_dialog(self):
        issues = self.app.db.health_issues('duplicate_artist')
        self.assertEqual(len(issues), 1)
        self.app.refresh_health_view()
        self.app.health_tree.selection_set(str(issues[0]['id']))
        self.app._on_health_select()
        # Invoke the real Tk button: this catches runtime errors that py_compile
        # cannot detect, including the old missing F.micro attribute.
        self.app.health_merge_btn.invoke()
        self.root.update()
        self.assertFalse(self.errors, '\n'.join(self.errors))
        windows = [w for w in self.root.winfo_children() if isinstance(w, gui.tk.Toplevel)]
        self.assertEqual(len(windows), 1)
        win = windows[0]
        buttons = {w.cget('text'): w for w in descendants(win) if isinstance(w, gui.ttk.Button)}
        self.assertEqual(set(buttons), BUTTONS)
        for name, button in buttons.items():
            self.assertTrue(button.winfo_ismapped(), name)
            x = button.winfo_rootx() - win.winfo_rootx()
            y = button.winfo_rooty() - win.winfo_rooty()
            self.assertGreaterEqual(x, 0, name)
            self.assertGreaterEqual(y, 0, name)
            self.assertLessEqual(x + button.winfo_width(), win.winfo_width(), name)
            self.assertLessEqual(y + button.winfo_height(), win.winfo_height(), name)
        return win, buttons

    def wait_for_merge(self, win):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.root.update()
            if not win.winfo_exists() and not self.app.operation_lock.locked() and not self.app._busy_kind:
                break
            time.sleep(0.01)
        else:
            self.fail('Merge/scan did not complete in time')
        self.assertFalse(self.errors, '\n'.join(self.errors))

    def test_font_roles_exist(self):
        tree = ast.parse(Path(gui.__file__).read_text(encoding='utf-8'))
        used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                and ((isinstance(node.value, ast.Attribute) and node.value.attr == 'F')
                     or (isinstance(node.value, ast.Name) and node.value.id == 'F'))}
        self.assertFalse(used - set(vars(self.app.F)), f'Undefined font roles: {used - set(vars(self.app.F))}')

    def test_all_buttons_visible_at_four_scaling_levels(self):
        for percent in [100, 125, 150, 200]:
            with self.subTest(percent=percent):
                self.root.tk.call('tk', 'scaling', (96 / 72) * percent / 100)
                self.app.F = gui.make_fonts(self.root)
                gui.apply_style(self.root, self.app.F)
                win, buttons = self.open_dialog()
                self.assertEqual(f'Resolve duplicate artist - v{gui.APP_VERSION}', win.title())
                checkbox = next(w for w in descendants(win) if isinstance(w, gui.ttk.Checkbutton))
                self.assertFalse(checkbox.instate(['selected']))
                buttons['Cancel'].invoke()
                self.root.update()
                self.assertFalse(win.winfo_exists())
                self.assertEqual(len(self.app.db.health_issues('duplicate_artist')), 1)

    def test_library_merge_keeps_files_and_resolves_issue(self):
        before = {str(p): digest(p) for p in self.files}
        win, buttons = self.open_dialog()
        buttons['Merge in library'].invoke()
        self.root.update()
        self.assertFalse(win.winfo_exists())
        self.assertFalse(self.app.db.health_issues('duplicate_artist'))
        self.assertEqual(before, {str(p): digest(p) for p in self.files})
        gui.HealthScanner(self.app.db, self.music, lambda *_: None).scan()
        self.assertFalse(self.app.db.health_issues('duplicate_artist'))

    def test_keep_separate_resolves_issue(self):
        win, buttons = self.open_dialog()
        buttons['Keep separate'].invoke()
        self.root.update()
        self.assertFalse(win.winfo_exists())
        self.assertTrue(set(ALIASES) <= self.app.db.auto_merge_disabled())
        gui.HealthScanner(self.app.db, self.music, lambda *_: None).scan()
        self.assertFalse(self.app.db.health_issues('duplicate_artist'))
        self.assertTrue(all(p.exists() for p in self.files))

    def test_disk_merge_requires_confirmation(self):
        win, buttons = self.open_dialog()
        with patch.object(gui.messagebox, 'askyesno', return_value=False) as confirm:
            buttons['Fix folders on disk'].invoke()
        self.root.update()
        confirm.assert_called_once()
        self.assertTrue(win.winfo_exists())
        self.assertTrue(all(p.exists() for p in self.files))
        self.assertEqual(len(self.app.db.health_issues('duplicate_artist')), 1)
        buttons['Cancel'].invoke()

    def test_disk_merge_preserves_tracks_and_alternate_artwork(self):
        images = []
        for n, colour in zip(ALIASES, [(200, 30, 30), (30, 80, 200)]):
            path = self.music / n / 'folder.jpg'
            Image.new('RGB', (32, 32), colour).save(path)
            images.append(digest(path))
        before = {p.parent.name: digest(p) for p in self.files}
        win, buttons = self.open_dialog()
        with patch.object(gui.messagebox, 'askyesno', return_value=True):
            buttons['Fix folders on disk'].invoke()
        self.wait_for_merge(win)
        self.assertFalse((self.music / ALIASES[0]).exists())
        target = self.music / ALIASES[1]
        self.assertEqual(before, {p.parent.name: digest(p) for p in target.rglob('*.flac')})
        self.assertEqual(set(images), {digest(p) for p in target.glob('*.jpg')})
        self.assertFalse(self.app.db.health_issues('duplicate_artist'))

    def test_disk_merge_blocks_conflicting_albums(self):
        collision = self.music / ALIASES[1] / 'Album A'
        collision.mkdir()
        sentinel = collision / 'do-not-change.txt'
        sentinel.write_text('existing destination', encoding='utf-8')
        before = {p.relative_to(self.music).as_posix(): digest(p) for p in self.music.rglob('*') if p.is_file()}
        win, buttons = self.open_dialog()
        with patch.object(gui.messagebox, 'askyesno', return_value=True), \
                patch.object(gui.messagebox, 'showerror') as error:
            buttons['Fix folders on disk'].invoke()
            deadline = time.monotonic() + 10
            while not error.called and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertTrue(error.called)
        self.assertTrue(win.winfo_exists())
        self.assertEqual(before, {p.relative_to(self.music).as_posix(): digest(p) for p in self.music.rglob('*') if p.is_file()})
        self.assertTrue(all(not b.instate(['disabled']) for b in buttons.values()))
        buttons['Cancel'].invoke()


if __name__ == '__main__':
    unittest.main()
