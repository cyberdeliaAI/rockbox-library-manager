# SPDX-License-Identifier: GPL-2.0-only
"""Load one album on demand; no persistent track index or music writes."""
from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from mutagen import File as MutagenFile

from . import artwork_engine as engine, tags
from .common import EXCLUDED_DIRS
from .track_checks import inspect_tracks, position


@dataclass
class Track:
    path: Path
    values: dict[str, list[str]] = field(default_factory=dict)
    duration: float | None = None
    format: str = ""
    error: str = ""

    def value(self, key):
        return " / ".join(self.values.get(key, []))


@dataclass
class AlbumDetails:
    tracks: list[Track] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    cancelled: bool = False
    folder: Path | None = None


def duration_text(seconds):
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def album_files(folder: Path, cancel: threading.Event) -> tuple[list[Path], list[str]]:
    """Enumerate with explicit traversal errors, so partial listings cannot imply gaps."""
    folder = folder.resolve()
    errors = []
    files = []
    for parent, directories, names in os.walk(folder, onerror=lambda e: errors.append(str(e))):
        if cancel.is_set():
            break
        directories[:] = sorted(d for d in directories if not d.startswith(".") and d.casefold() not in EXCLUDED_DIRS)
        for name in names:
            if cancel.is_set():
                return files, errors
            path = Path(parent) / name
            if name.startswith(".") or path.suffix.casefold() not in engine.AUDIO_EXTS or not path.is_file():
                continue
            try:
                path.resolve(strict=True).relative_to(folder)
            except (OSError, ValueError) as exc:
                errors.append(f"{name}: {exc}")
                continue
            files.append(path)
    return files, errors


def read_album(folder: Path, cancel: threading.Event) -> AlbumDetails:
    result = AlbumDetails()
    folder = folder.resolve(strict=True)
    result.folder = folder
    if not folder.is_dir():
        raise NotADirectoryError(str(folder))
    files, result.errors = album_files(folder, cancel)
    if cancel.is_set():
        result.cancelled = True
        return result
    for path in sorted(files, key=lambda p: str(p).casefold()):
        if cancel.is_set():
            result.cancelled = True
            return result
        track = Track(path)
        try:
            audio = MutagenFile(str(path), easy=True)
            if audio is None:
                raise tags.TagError("unsupported metadata")
            track.values = tags.values_from_audio(audio, (*tags.HEALTH_FIELDS, "composer"))
            seconds = getattr(audio.info, "length", None)
            if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= 0:
                track.duration = seconds
            # Report the decoder's format, rather than guessing from the filename.
            info = [type(audio).__name__.removeprefix("Easy").upper()]
            for attribute, unit in (("bits_per_sample", "-bit"), ("sample_rate", " Hz"), ("channels", " ch")):
                value = getattr(audio.info, attribute, None)
                if value:
                    info.append(f"{value}{unit}")
            track.format = " · ".join(info)
        except Exception as exc:
            track.error = str(exc)
        result.tracks.append(track)
    result.tracks.sort(key=lambda t: (position(t.value("discnumber")) or 1,
                                     position(t.value("tracknumber")) or 999999999, str(t.path).casefold()))
    result.issues = inspect_tracks({t.path: t.values for t in result.tracks if not t.error},
                                   complete=not result.errors and not any(t.error for t in result.tracks))
    return result
