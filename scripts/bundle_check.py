# SPDX-License-Identifier: GPL-2.0-only
"""Exercise frozen dependencies and the real desktop against temporary data only."""
import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path


def run(args):
    if len(args) != 1:
        return 2
    report = Path(args[0]).resolve()
    result = {}
    app = root = None
    try:
        if not getattr(sys, "frozen", False):
            raise RuntimeError("This check must run inside the built executable")
        import certifi
        from mutagen.flac import FLAC
        from PIL import Image
        from rockbox_manager import __version__, gui, media_tools, tags
        from rockbox_manager import database_dialog, tag_editor, picker  # noqa: F401
        from rockbox_manager.media_dialog import MediaDialog
        from rockbox_manager.library import FastLibraryScanner

        if not Path(certifi.where()).is_file():
            raise RuntimeError("Bundled HTTPS certificates are missing")
        with tempfile.TemporaryDirectory(prefix="rlm-bundle-check-") as directory:
            base = Path(directory)
            original_config = gui.engine.get_config_path
            gui.engine.get_config_path = lambda: base / "Config" / "credentials.json"
            try:
                music = base / "Music"
                album = music / "Test Artist" / "Test Album"
                album.mkdir(parents=True)
                audio = album / "01.flac"
                shutil.copyfile(Path(sys._MEIPASS) / "build-check" / "silence.flac", audio)
                flac = FLAC(audio)
                flac["artist"], flac["album"] = ["Test Artist"], ["Test Album"]
                flac.save()
                if tags.read_values(audio)["artist"] != ["Test Artist"]:
                    raise RuntimeError("Bundled metadata support failed")
                Image.new("RGB", (600, 600), "#35b8a6").save(album / "folder.jpg")
                root = gui.create_root()
                if not root.tk.call("package", "present", "tkdnd"):
                    raise RuntimeError("Native drag-and-drop failed to load")
                errors = []
                root.report_callback_exception = lambda kind, value, tb: errors.append(str(value))
                app = gui.ArtworkApp(root)
                app.music_root_var.set(str(music))
                scanned = FastLibraryScanner(app.db, music, "folder.jpg", lambda *_: None).scan()
                if (scanned["artists"], scanned["albums"], scanned["errors"]) != (1, 1, 0):
                    raise RuntimeError("Bundled SQLite library scan failed")
                app.reload_gallery()
                root.update()
                dialog = MediaDialog(app, music, base / "Config", gui.C, lambda _: None)
                root.update()
                dialog.close()
                cancel = threading.Event()
                scanned_media = media_tools.scan(music, 300, base / "media.sqlite3", cancel)
                applied = media_tools.apply(music, scanned_media.candidates, 300, base / "backups", "", cancel)
                if applied.errors or not applied.completed:
                    raise RuntimeError(f"Bundled image/backup processing failed: {applied.errors}")
                with Image.open(album / "folder.jpg") as image:
                    if image.size != (300, 300) or image.info.get("progressive"):
                        raise RuntimeError("Bundled JPEG output failed")
                ffmpeg = shutil.which("ffmpeg")
                if os.environ.get("RLM_REQUIRE_FFMPEG") == "1" and not ffmpeg:
                    raise RuntimeError("CI must exercise external FFmpeg")
                if ffmpeg:
                    high = album / "02.flac"
                    media_tools.run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i", "anullsrc=r=96000:cl=stereo",
                                                   "-t", "0.25", "-sample_fmt", "s32", "-c:a", "flac", str(high)], cancel)
                    high_tags = FLAC(high)
                    high_tags["artist"] = ["Test Artist"]
                    high_tags.save()
                    audio_scan = media_tools.scan(music, 300, base / "media.sqlite3", cancel, fresh=True)
                    audio_result = media_tools.apply(music, audio_scan.candidates, 300,
                                                    base / "backups", ffmpeg, cancel)
                    converted = FLAC(high)
                    if (audio_result.errors or not audio_result.completed
                            or (converted.info.bits_per_sample, converted.info.sample_rate) != (16, 44100)
                            or converted["artist"] != ["Test Artist"]):
                        raise RuntimeError(f"Bundled FFmpeg conversion failed: {audio_result.errors}")
                root.update()
                if errors or app._icon_photo is None:
                    raise RuntimeError(f"Tk/Pillow desktop callbacks failed: {errors}")
                result = {"ok": True, "version": __version__, "frozen": True,
                          "tk": root.tk.call("info", "patchlevel"), "tkdnd": root.TkdndVersion,
                          "ffmpeg_checked": bool(ffmpeg), "platform": sys.platform}
            finally:
                if app is not None:
                    for job in root.tk.splitlist(root.tk.call("after", "info")):
                        root.tk.call("after", "cancel", job)
                    app.on_close()
                    app.db.conn.close()
                elif root is not None:
                    root.destroy()
                gui.engine.get_config_path = original_config
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result.get("ok") else 1
