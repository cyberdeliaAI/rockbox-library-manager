"""Optional interoperability test against Rockbox's own host database builder.

Set ROCKBOX_DATABASE_TOOL to the built tools/database executable. The native
program only sees temporary test libraries. No downloaded code is run by default.
"""
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from mutagen.flac import FLAC
from rockbox_manager import rockbox_database as rb
from test_rockbox_database import make_device


@unittest.skipUnless(os.environ.get("ROCKBOX_DATABASE_TOOL"), "Set ROCKBOX_DATABASE_TOOL for native interoperability checks")
class NativeDatabaseTests(unittest.TestCase):
    def test_native_build_python_update_and_native_rebuild_preserve_runtime(self):
        with tempfile.TemporaryDirectory(prefix="rockbox-native-db-") as temp:
            base = Path(temp).resolve()
            device, tracks = make_device(base)
            # Remove only the artificial DB files we just made. Rockbox creates
            # the actual fixture from the FLACs using its own production parser.
            for name in rb.snapshot(device):
                (device / ".rockbox" / name).unlink()
            tool = str(Path(os.environ["ROCKBOX_DATABASE_TOOL"]).resolve())
            def native_build():
                result = subprocess.run([tool], cwd=device, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return rb.TagDatabase.parse(rb.snapshot(device))
            original = native_build()
            idx = next(i for i, row in enumerate(original.rows) if not row[23] & 1 and original.text(row, 4).endswith("01.flac"))
            master = bytearray(original.files[rb.MASTER])
            runtime = [42, 7, 1200000, 1700000000]
            for field, value in zip(range(15, 19), runtime):
                struct.pack_into(original.endian + "i", master, 24 + idx * 96 + field * 4, value)
            struct.pack_into(original.endian + "i", master, 24 + idx * 96 + 23 * 4, original.rows[idx][23] | 4)
            (device / ".rockbox" / rb.MASTER).write_bytes(master)
            old_time = tracks[0].stat().st_mtime
            audio = FLAC(tracks[0]); audio["artist"] = ["Ali Farka Touré"]
            audio["albumartist"] = ["Ali Farka Touré"]; audio["album"] = ["New Album"]; audio.save()
            os.utime(tracks[0], (old_time + 4, old_time + 4))
            plan = rb.preview_update(device / "Music")
            self.assertEqual(plan.changed_tracks, 1)
            rb.apply_update(plan, base / "backups")
            # Force Rockbox to load and rebuild the updated string tables by
            # adding a track. Also let it rescan the changed FLAC itself.
            added = tracks[1].parent / "03.flac"
            shutil.copyfile(tracks[1], added)
            final = native_build()
            active = [row for row in final.rows if not row[23] & 1]
            self.assertEqual(len(active), 3)
            row = next(r for r in active if final.text(r, 4).endswith("01.flac"))
            self.assertEqual(final.text(row, 0), "Ali Farka Touré")
            self.assertEqual(final.text(row, 1), "New Album")
            self.assertEqual(row[15:19], runtime)
