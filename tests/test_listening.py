"""Temporary portable-player logs: timezone review, exact imports and local statistics."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from rockbox_manager.listening import ListeningStore, preview_log


def log_line(timestamp=1700000000, title="Track", rating="L", duration=180, artist="Artist"):
    return f"{artist}\tAlbum\t{title}\t1\t{duration}\t{rating}\t{timestamp}\t\n"


class ListeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.log = self.base / ".scrobbler.log"
        self.store = ListeningStore(self.base / "local/history.sqlite3")

    def write(self, body, header="#AUDIOSCROBBLER/1.1\n#TZ/UTC\n"):
        self.log.write_text(header + body, encoding="utf-8")
        return preview_log(self.log)

    def test_unknown_timezone_is_never_guessed_and_explicit_offsets_are_applied(self):
        self.log.write_text("#TZ/UNKNOWN\n" + log_line())
        with self.assertRaisesRegex(ValueError, "Unknown log timezone"):
            preview_log(self.log)
        first = preview_log(self.log, 60)
        self.assertEqual(first.events[0].timestamp, 1700000000 - 3600)
        self.assertTrue(first.fixed_offset)
        self.assertEqual(preview_log(self.log, -300).events[0].timestamp, 1700000000 + 18000)
        self.log.write_text(log_line())
        with self.assertRaisesRegex(ValueError, "Unknown"):
            preview_log(self.log)

    def test_bom_utf8_fixed_header_and_invalid_lines_are_reported(self):
        self.log.write_bytes(("\ufeff#TZ/+0130\n" + log_line(title="Björk 日本語") + "bad\n").encode() + b"\xff\n")
        result = preview_log(self.log)
        self.assertEqual(result.events[0].timestamp, 1700000000 - 5400)
        self.assertEqual(result.events[0].title, "Björk 日本語")
        self.assertEqual(result.invalid, 2)
        for zone in ("+1460", "+1500", "nonsense", "+0160"):
            self.log.write_text("#TZ/" + zone + "\n" + log_line())
            with self.subTest(zone=zone), self.assertRaises(ValueError):
                preview_log(self.log)

    def test_reimport_and_same_log_at_another_path_do_not_duplicate_events(self):
        plan = self.write(log_line() + log_line() + log_line(1700000300, rating="S"))
        source = self.log.read_bytes(), self.log.stat().st_mtime_ns
        self.assertEqual(plan.duplicates, 1)
        self.assertEqual(self.store.import_preview(plan), 2)
        self.assertEqual(self.store.import_preview(plan), 0)
        other = self.base / "copy.log"
        other.write_bytes(source[0])
        self.assertEqual(self.store.import_preview(preview_log(other)), 0)
        self.assertEqual((self.log.read_bytes(), self.log.stat().st_mtime_ns), source)
        self.assertEqual(self.store.statistics()["skipped"], 1)
        if os.name == "posix":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_stale_preview_and_cancelled_import_leave_history_unchanged(self):
        plan = self.write(log_line())
        self.log.write_text(self.log.read_text() + log_line(1700000400))
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.import_preview(plan)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(ValueError, "rolled back"):
            self.store.import_preview(preview_log(self.log), cancel)
        self.assertEqual(self.store.page()[1], 0)

    def test_sessions_use_estimated_end_and_statistics_do_not_include_skipped_time(self):
        plan = self.write(log_line(1700000000, duration=3600) + log_line(1700003601) + log_line(1700005000)
                          + log_line(1700006000, rating="S"))
        self.store.import_preview(plan)
        stats = self.store.statistics()
        self.assertEqual(stats["listened"], 3)
        self.assertEqual(stats["seconds"], 3960)
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["artists"], [("Artist", 3)])

    def test_literal_search_export_and_existing_destinations(self):
        self.store.import_preview(self.write(log_line(title="100%_song") + log_line(1700000010, title="=HYPERLINK()")))
        self.assertEqual(self.store.page("%_")[1], 1)
        dest = self.base / "history.json"
        self.store.export(dest)
        self.assertEqual(len(json.loads(dest.read_text())), 2)
        with self.assertRaises(FileExistsError):
            self.store.export(dest)
        csv = self.base / "history.csv"
        self.store.export(csv)
        self.assertIn("'=HYPERLINK()", csv.read_text())

    def test_symlink_and_oversized_sources_are_refused(self):
        self.write(log_line())
        if os.name != "nt":
            link = self.base / "link.log"
            link.symlink_to(self.log)
            with self.assertRaises(ValueError):
                preview_log(link)
        with self.log.open("wb") as fp:
            fp.truncate(64 * 1024**2 + 1)
        with self.assertRaises(ValueError):
            preview_log(self.log)

    def test_unknown_format_and_timeless_logs_are_not_treated_as_real_utc_history(self):
        for header in ("#AUDIOSCROBBLER/9.0\n#TZ/UTC\n", "#CLIENT/Rockbox Timeless\n#TZ/UTC\n"):
            self.log.write_text(header+log_line())
            with self.assertRaises(ValueError):
                preview_log(self.log)
