"""Real numerical fits and fixed upstream guest-credit cases; no player writes."""
import json
import tempfile
import unittest
from pathlib import Path

from rockbox_manager.eq_tools import export_refit, preview_refit
from rockbox_manager.guest_credits import guest_names, track_guests


class GuestCreditTests(unittest.TestCase):
    def test_upstream_cases_and_documented_heuristic_limits(self):
        data = json.loads((Path(__file__).parent / "fixtures/guest-credits.json").read_text())
        self.assertEqual(len(data["cases"]), 56)
        for case in data["cases"]:
            with self.subTest(text=case["text"]):
                self.assertEqual(guest_names(case["text"], data["known"]), case["expected"])

    def test_boundaries_unicode_byte_limits_and_ascii_identity(self):
        for text in ("Défeat Guest One", "Song with Guest One", "Song w/o vocals", "Song w/O vocals", "🙂feat. Guest One"):
            self.assertEqual(guest_names(text), [])
        self.assertEqual(guest_names("Song_feat. Guest One"), ["Guest One"])
        self.assertEqual(guest_names("Song feat. " + "é" * 32), [])
        self.assertEqual(guest_names("Song feat. " + ", ".join(f"Guest {i}" for i in range(20))),
                         [f"Guest {i}" for i in range(8)])
        values = {"title": ["Song feat. Björk"], "artist": ["Artist feat. BJöRK"]}
        before = json.dumps(values)
        self.assertEqual(track_guests(values), ["Björk"])
        self.assertEqual(json.dumps(values), before)


class EqToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rlm-eq-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.path = self.base / "preset.cfg"
        self.original = (b"\xef\xbb\xbf# Preserve this comment\r\neq enabled: off\r\neq precut: 90\r\n"
                         b"volume: -20\r\neq low shelf filter: 60, 7, 50\r\n"
                         b"eq peak filter 1: 250, 10, 20\r\neq high shelf filter: 14000, 7, -40")
        self.path.write_bytes(self.original)

    def fit(self, **kwargs):
        return preview_refit(self.path, max_bands=2, tolerance=2.0, restarts=1, **kwargs)

    def test_real_fit_determinism_response_and_lossless_unrelated_settings(self):
        plan = self.fit()
        again = self.fit()
        self.assertEqual(plan.output, again.output)
        self.assertEqual(plan.before, 3)
        self.assertTrue(1 <= plan.after <= 2)
        self.assertLess(plan.weighted_error, 2.0)
        self.assertGreaterEqual(plan.max_error, plan.weighted_error)
        self.assertEqual(len(plan.frequencies), 320)
        for preserved in (b"# Preserve this comment\r\n", b"eq enabled: off", b"eq precut: 90", b"volume: -20"):
            self.assertIn(preserved, plan.output)
        self.assertTrue(plan.output.startswith(b"\xef\xbb\xbf"))
        self.assertFalse(plan.output.endswith(b"\n"))
        self.assertEqual(plan.output.count(b"filter"), 10)
        self.assertEqual(self.path.read_bytes(), self.original)
        destination = self.base / "new.cfg"
        self.assertEqual(len(export_refit(plan, destination)), 64)
        self.assertEqual(destination.read_bytes(), plan.output)
        self.assertEqual(self.path.read_bytes(), self.original)
        with self.assertRaises(FileExistsError):
            export_refit(plan, destination)
        with self.assertRaisesRegex(ValueError, "new .cfg"):
            export_refit(plan, self.path)

    def test_changed_source_over_tolerance_and_invalid_inputs_are_refused(self):
        plan = self.fit()
        self.path.write_bytes(self.original + b"\r\n# changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            export_refit(plan, self.base / "new.cfg")
        self.path.write_bytes(self.original)
        plan.tolerance = .05
        with self.assertRaisesRegex(ValueError, "tolerance"):
            export_refit(plan, self.base / "new.cfg")
        for content in (b"volume: -10", b"eq peak filter 0: 100, 10, 0", b"eq low shelf filter: 0, 7, 50",
                        b"eq low shelf filter: x", self.original + b"\r\neq low shelf filter: 50, 7, 0"):
            self.path.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.fit()
        self.assertFalse((self.base / "new.cfg").exists())

    def test_flat_and_already_minimal_presets_do_not_export(self):
        for gain in (0, 50):
            self.path.write_text(f"eq low shelf filter: 60, 7, {gain}\n")
            plan = self.fit()
            self.assertEqual(plan.before, plan.after)
            with self.assertRaisesRegex(ValueError, "No bands"):
                export_refit(plan, self.base / "new.cfg")
