"""Read-only media inspection and explicit, verified file replacement.

No FFmpeg binary is bundled. Audio conversion uses a user-installed executable.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from contextlib import closing
from datetime import datetime
from pathlib import Path

from mutagen.flac import FLAC, StreamInfo
from PIL import Image, ImageOps


class MediaError(Exception):
    pass


class Cancelled(MediaError):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("Cancelled")


def signature(path):
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def safe_path(root, relative):
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise MediaError("Path is outside the music folder")
    current = path
    while current != root:
        if linked(current):
            raise MediaError("Symbolic links and junctions are not modified")
        current = current.parent
    if not path.resolve().is_relative_to(root):
        raise MediaError("Path is outside the music folder")
    return path


def audio_info(path):
    # STREAMINFO is the first block: even a very large FLAC needs only 42 bytes.
    with path.open("rb") as f:
        header = f.read(42)
    if len(header) != 42 or header[:4] != b"fLaC" or header[4] & 127 or header[5:8] != b"\x00\x00\x22":
        raise MediaError("Not a standard FLAC header (unsupported or damaged file)")
    info = StreamInfo(header[8:])
    if not info.sample_rate or not info.total_samples:
        raise MediaError("FLAC has no valid sample rate or duration")
    return dict(bits=info.bits_per_sample, rate=info.sample_rate,
                channels=info.channels, samples=info.total_samples)


def image_info(path):
    with Image.open(path) as image:
        if image.format != "JPEG":
            raise MediaError("Artwork with a .jpg filename must contain JPEG data")
        return dict(width=image.width, height=image.height)


@dataclass
class Candidate:
    relative: str
    kind: str
    signature: list
    info: dict

    def description(self, size):
        if self.kind == "flac":
            i = self.info
            return f"{i['bits']}-bit / {i['rate'] / 1000:g} kHz → 16-bit / {min(i['rate'], 44100) / 1000:g} kHz"
        if self.kind == "small_artwork":
            return f"{self.info['width']}×{self.info['height']} — too small; find a larger source"
        crop = " (centre crop)" if self.info['width'] != self.info['height'] else ""
        return f"{self.info['width']}×{self.info['height']} → {size}×{size}{crop}"


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checked: int = 0
    cached: int = 0
    cancelled: bool = False


def scan(root: Path, size: int, cache_path: Path, cancel: threading.Event,
         progress=lambda text: None, *, audio=True, artwork=True, fresh=False):
    root = root.resolve(strict=True)
    if not 64 <= size <= 2000:
        raise MediaError("Artwork size must be between 64 and 2000 pixels")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    result = ScanResult()
    with closing(sqlite3.connect(cache_path)) as cache, cache:
        cache.execute("CREATE TABLE IF NOT EXISTS media (path TEXT PRIMARY KEY, signature TEXT, info TEXT)")
        def walk_error(exc):
            result.errors.append(str(exc))
        for folder, dirs, files in os.walk(root, onerror=walk_error, followlinks=False):
            if cancel.is_set():
                result.cancelled = True
                break
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not linked(Path(folder) / d))
            for name in sorted(files):
                if cancel.is_set():
                    result.cancelled = True
                    break
                kind = "flac" if audio and name.lower().endswith(".flac") else (
                    "artwork" if artwork and name.lower() in ("folder.jpg", "cover.jpg") else None)
                if not kind or name.startswith('.'):
                    continue
                path = Path(folder) / name
                relative = path.relative_to(root).as_posix()
                try:
                    safe_path(root, relative)
                    sig = signature(path)
                    key = str(path)
                    row = None if fresh else cache.execute("SELECT signature, info FROM media WHERE path=?", (key,)).fetchone()
                    if row and json.loads(row[0]) == sig:
                        info = json.loads(row[1])
                        result.cached += 1
                    else:
                        info = audio_info(path) if kind == "flac" else image_info(path)
                        if signature(path) != sig:
                            raise MediaError("File changed during scan; scan again")
                        cache.execute("INSERT OR REPLACE INTO media VALUES (?, ?, ?)",
                                      (key, json.dumps(sig), json.dumps(info)))
                    if kind == "flac" and info["channels"] > 2:
                        raise MediaError("Multichannel FLAC: automatic downmix is not supported")
                    if kind == "artwork" and min(info["width"], info["height"]) < size:
                        kind = "small_artwork"
                    needed = (info["bits"] > 16 or info["rate"] > 44100) if kind == "flac" else (
                        info["width"] != size or info["height"] != size)
                    if needed:
                        result.candidates.append(Candidate(relative, kind, sig, info))
                except Exception as exc:
                    result.errors.append(f"{relative}: {exc}")
                result.checked += 1
                if result.checked % 50 == 0:
                    progress(f"Checked {result.checked} files ({result.cached} cached); {len(result.candidates)} candidates. {relative}")
                    cache.commit()
    return result


def find_ffmpeg(configured=""):
    configured = configured.strip().strip('"')
    found = shutil.which(configured or "ffmpeg")
    if not found:
        raise MediaError("FFmpeg was not found. Install FFmpeg and select its executable in Settings. Artwork resizing does not need FFmpeg.")
    return str(Path(found).resolve())


def run_ffmpeg(executable, args, cancel):
    check_cancel(cancel)
    # A disk-backed log avoids pipe deadlocks and unbounded memory on damaged input.
    with tempfile.TemporaryFile() as log:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        proc = subprocess.Popen([executable, "-hide_banner", "-loglevel", "error", "-nostdin", *args],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, **kwargs)
        try:
            while True:
                check_cancel(cancel)
                try:
                    code = proc.wait(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if code:
            log.seek(0)
            detail = log.read(4000).decode("utf-8", "replace").strip()
            raise MediaError(f"FFmpeg failed: {detail or code}")


def convert_flac(source, target, executable, cancel):
    original = FLAC(source)
    # Cuesheet sample offsets and opaque application blocks cannot safely be
    # copied after resampling. Refuse those files instead of losing information.
    if any(b.code not in (0, 1, 3, 4, 6) for b in original.metadata_blocks):
        raise MediaError("Embedded cuesheet or application metadata needs manual conversion; original kept")
    info = audio_info(source)
    if info["channels"] > 2:
        raise MediaError("Multichannel conversion is not supported")
    rate = min(44100, info["rate"])
    run_ffmpeg(executable, ["-y", "-xerror", "-err_detect", "explode", "-i", str(source),
               "-map", "0:a:0", "-map_metadata", "-1", "-vn", "-sn", "-dn",
               "-af", f"aresample={rate}:osf=s16:dither_method=triangular",
               "-c:a", "flac", "-sample_fmt", "s16", "-compression_level", "5", str(target)], cancel)
    converted = FLAC(target)
    if converted.tags is None:
        converted.add_tags()
    converted.tags.clear()
    converted.tags.update(dict(original.tags or {}))
    converted.clear_pictures()
    for picture in original.pictures:
        converted.add_picture(picture)
    converted.save()
    verify = FLAC(target)
    if dict(verify.tags or {}) != dict(original.tags or {}) or [p.write() for p in verify.pictures] != [p.write() for p in original.pictures]:
        raise MediaError("Metadata verification failed")
    output = audio_info(target)
    expected = round(info["samples"] * rate / info["rate"])
    if (output["bits"] != 16 or output["rate"] != rate or output["channels"] != info["channels"]
            or abs(output["samples"] - expected) > 2):
        raise MediaError("Converted audio format or duration does not match")
    run_ffmpeg(executable, ["-xerror", "-err_detect", "explode", "-i", str(target),
                           "-map", "0:a:0", "-f", "null", "-"], cancel)


def resize_artwork(source, target, size):
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        if min(image.size) < size:
            raise MediaError("Artwork is too small; choose a larger source instead of upscaling")
        image = ImageOps.fit(image, (size, size), Image.Resampling.LANCZOS)
        expected = (size, size)
        image.save(target, "JPEG", quality=92, optimize=True, progressive=False)
    with Image.open(target) as verify:
        verify.load()
        if verify.size != expected or verify.format != "JPEG" or verify.info.get("progressive"):
            raise MediaError("Resized artwork verification failed")


def digest(path, cancel):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            check_cancel(cancel)
            h.update(chunk)
    return h.hexdigest()


def backup_file(source, destination, cancel):
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = hashlib.sha256()
    try:
        with source.open("rb") as src, destination.open("xb") as dst:
            while chunk := src.read(1024 * 1024):
                check_cancel(cancel)
                expected.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if digest(destination, cancel) != expected.hexdigest():
            raise MediaError("Local backup verification failed")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return expected.hexdigest()


def save_manifest(folder, manifest):
    pending = folder / "manifest.tmp"
    with pending.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(pending, folder / "manifest.json")


@dataclass
class ApplyResult:
    completed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    backup_folder: Path | None = None


def apply(root: Path, candidates: list[Candidate], size: int, backup_root: Path | None,
          ffmpeg: str, cancel: threading.Event, progress=lambda text: None):
    root = root.resolve(strict=True)
    if not 64 <= size <= 2000:
        raise MediaError("Artwork size must be between 64 and 2000 pixels")
    if any(c.kind not in ("flac", "artwork") for c in candidates):
        raise MediaError("Small artwork needs a larger source; it cannot be resized automatically")
    executable = find_ffmpeg(ffmpeg) if any(c.kind == "flac" for c in candidates) else ""
    result = ApplyResult()
    manifest = {"music_root": str(root), "files": []}
    if backup_root is not None and candidates:
        backup_root = backup_root.expanduser().resolve()
        if backup_root.is_relative_to(root):
            raise MediaError("Choose a local backup folder outside the music folder")
        for parent in (root, *root.parents):
            if (parent / ".rockbox").is_dir():
                if backup_root.is_relative_to(parent):
                    raise MediaError("Backups must be stored on the computer, outside the Rockbox player")
                break
        backup_root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(backup_root).free < sum(c.signature[2] for c in candidates) + 1024 * 1024:
            raise MediaError("Not enough free space for local backups")
        result.backup_folder = backup_root / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        result.backup_folder.mkdir()
        save_manifest(result.backup_folder, manifest)
    for n, candidate in enumerate(candidates, 1):
        temporary = None
        try:
            check_cancel(cancel)
            source = safe_path(root, candidate.relative)
            if signature(source) != candidate.signature:
                raise MediaError("File changed since scan; scan again")
            progress(f"{n}/{len(candidates)}: {candidate.relative}")
            required = candidate.signature[2] + 1024 * 1024
            if candidate.kind == "flac":
                i = candidate.info
                required += round(i["samples"] * min(44100, i["rate"]) / i["rate"]) * i["channels"] * 2
            if shutil.disk_usage(source.parent).free < required:
                raise MediaError("Not enough free space beside the original for a temporary output")
            fd, tempname = tempfile.mkstemp(prefix=".rlm-", suffix=source.suffix, dir=source.parent)
            os.close(fd)
            temporary = Path(tempname)
            if candidate.kind == "flac":
                convert_flac(source, temporary, executable, cancel)
            else:
                resize_artwork(source, temporary, size)
            check_cancel(cancel)
            if signature(safe_path(root, candidate.relative)) != candidate.signature:
                raise MediaError("File changed during conversion; original kept")
            if result.backup_folder:
                progress(f"{n}/{len(candidates)}: Verifying local backup of {candidate.relative}")
                checksum = backup_file(source, result.backup_folder / "originals" / candidate.relative, cancel)
                manifest["files"].append({"path": candidate.relative, "sha256": checksum})
                save_manifest(result.backup_folder, manifest)
            check_cancel(cancel)
            if signature(safe_path(root, candidate.relative)) != candidate.signature:
                raise MediaError("File changed during backup; original kept")
            os.chmod(temporary, source.stat().st_mode)
            with temporary.open("rb+") as f:
                os.fsync(f.fileno())
            os.replace(temporary, source)
            temporary = None
            result.completed.append(candidate.relative)
        except Cancelled:
            result.cancelled = True
            break
        except Exception as exc:
            result.errors.append(f"{candidate.relative}: {exc}")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    result.errors.append(f"Could not remove temporary file {temporary}: {exc}")
    return result
