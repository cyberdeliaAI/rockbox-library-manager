"""Actual full decodes on copied fixtures, plus cancellation and stale-source checks."""
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rockbox_manager import integrity


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.audio = self.base / "Björk 日本語.flac"
        shutil.copyfile(Path(__file__).parent / "fixtures/silence.flac", self.audio)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for actual decode checks")
    def test_real_audio_and_corrupt_files_are_checked_without_source_writes(self):
        broken = self.base / "broken.flac"
        broken.write_bytes(b"not an audio file")
        before = {p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.base.iterdir()}
        result = integrity.scan(self.base)
        statuses = {Path(t.path).name:t.status for t in result.items}
        self.assertEqual(statuses[self.audio.name], "passed")
        self.assertEqual(statuses[broken.name], "error")
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.base.iterdir()})
        self.assertFalse(result.cancelled)

    def test_missing_ffmpeg_does_not_touch_the_library(self):
        with patch.object(integrity,"find_ffmpeg",side_effect=ValueError("missing")):
            with self.assertRaises(ValueError): integrity.scan(self.base)

    def test_cancel_kills_child_and_never_reports_success(self):
        cancel = threading.Event()
        cancel.set()
        class Child:
            returncode = -9
            killed = False
            def poll(self): return None
            def kill(self): self.killed = True
            def wait(self): return -9
        child = Child()
        with patch.object(integrity.subprocess,"Popen",return_value=child) as popen:
            item = integrity.check_file(self.audio,"ffmpeg",cancel)
        self.assertEqual(item.status,"incomplete")
        self.assertTrue(child.killed)
        args = popen.call_args.args[0]
        self.assertIn("file,pipe",args)
        self.assertEqual(args[-3:],["-f","null","-"])

    def test_changed_files_and_links_are_never_reported_as_success(self):
        class Child:
            returncode = 0
            def poll(self): return 0
            def wait(self): return 0
        def spawn(*args,**kwargs):
            st=self.audio.stat()
            os.utime(self.audio, ns=(st.st_atime_ns,st.st_mtime_ns+2_000_000_000))
            return Child()
        with patch.object(integrity.subprocess,"Popen",side_effect=spawn):
            self.assertEqual(integrity.check_file(self.audio,"ffmpeg",threading.Event()).status,"changed")
        if os.name != "nt":
            link=self.base/"link.flac"
            link.symlink_to(self.audio)
            self.assertEqual(integrity.check_file(link,"ffmpeg",threading.Event()).status,"skipped")
