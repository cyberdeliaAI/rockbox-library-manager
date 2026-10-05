# SPDX-License-Identifier: GPL-2.0-only
"""Reading, planning and writing album tags, without any user interface.

Rockbox's database keeps one value per tag and track, and the direct database
update refuses files with several values for artist, album artist, album, genre
or date. These helpers therefore always see every value a file has, and an edit
always writes exactly one value (or removes the tag).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import sources

EDIT_FIELDS = [
    ("albumartist", "Album artist"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("date", "Year / date"),
    ("genre", "Genre"),
]
LABELS = dict(EDIT_FIELDS)
HEALTH_FIELDS = ("artist", "albumartist", "album", "genre", "date", "tracknumber", "discnumber")
YEAR_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")
# What rockbox_database.preview_update accepts as a year: four digits, then the end or a separator.
DATABASE_YEAR_RE = re.compile(r"^\d{4}(?:$|[-/T ])")


class TagError(Exception):
    pass


def valid_year(value: str) -> bool:
    """YYYY, YYYY-MM or YYYY-MM-DD: what the Rockbox database update can interpret."""
    return bool(YEAR_RE.match(str(value or "").strip()))


def database_can_read_year(value: str) -> bool:
    return bool(DATABASE_YEAR_RE.match(str(value or "")))


def read_values(path: Path, keys: Iterable[str] = HEALTH_FIELDS) -> dict[str, list[str]]:
    """Every non-empty value of each tag (mutagen's easy names), in file order."""
    from mutagen import File as MutagenFile

    audio = MutagenFile(str(path), easy=True)
    if audio is None:
        raise TagError("unsupported metadata")
    out: dict[str, list[str]] = {}
    for key in keys:
        try:
            raw = audio.get(key) or []
        except Exception:  # e.g. EasyMP4 raising on unsupported keys
            raw = []
        if isinstance(raw, (str, bytes)):
            raw = [raw]
        out[key] = [str(v).strip() for v in raw if str(v).strip()]
    return out


def show(values: list[str]) -> str:
    return " + ".join(f"“{v}”" for v in values) if values else "(empty)"


# -- genres -----------------------------------------------------------------

def combined_genre(value: str) -> bool:
    """One text holding several genres, such as 'Pop;Rock'. Rockbox lists it as its own genre."""
    return ";" in str(value or "")


def split_genre(value: str) -> list[str]:
    return sources.unique([part.strip() for part in str(value or "").split(";") if part.strip()])


def genre_key(value: str) -> str:
    """Spellings Rockbox lists separately but people mean as one: Hip Hop, Hip-Hop, Hiphop.

    Rockbox already merges differences in case, so only accents, spaces and
    punctuation are folded here.
    """
    value = sources.strip_accents(value).casefold().replace("&", "and")
    return re.sub(r"[\W_]+", "", value)


# -- planning an album edit ---------------------------------------------------

@dataclass
class TrackPlan:
    path: Path
    before: dict[str, list[str]]
    after: dict[str, list[str]] = field(default_factory=dict)

    @property
    def changed(self) -> list[str]:
        return [key for key, value in self.after.items() if value != self.before.get(key, [])]


def plan_edits(files: dict[Path, dict[str, list[str]]],
               edits: dict[str, tuple[str, str]]) -> list[TrackPlan]:
    """The tracks whose tags actually change. ``edits`` maps key -> ("set", value) or ("clear", "")."""
    plans = []
    for path, before in files.items():
        after = {key: ([] if mode == "clear" else [value]) for key, (mode, value) in edits.items()}
        plan = TrackPlan(path, before, after)
        if plan.changed:
            plans.append(plan)
    return plans


def review_lines(plans: list[TrackPlan]) -> list[str]:
    """Identical changes grouped into one line each, e.g. 'Genre: “Pop;Rock” → “Rock” · 12 tracks'."""
    groups: Counter = Counter()
    order: list[tuple] = []
    for plan in plans:
        for key in plan.changed:
            group = (key, tuple(plan.before.get(key, [])), tuple(plan.after[key]))
            if group not in groups:
                order.append(group)
            groups[group] += 1
    field_order = {key: i for i, (key, _label) in enumerate(EDIT_FIELDS)}
    order.sort(key=lambda g: (field_order.get(g[0], 99), g[1]))
    lines = []
    for group in order:
        key, before, after = group
        count = groups[group]
        lines.append(f"{LABELS.get(key, key)}: {show(list(before))} → {show(list(after))}"
                     f" · {count} track{'' if count == 1 else 's'}")
    return lines


def write_edits(path: Path, edits: dict[str, tuple[str, str]]) -> bool:
    """Apply ``edits`` to one file; returns False when it already had these values.

    The file is read again first, so a value changed elsewhere since the editor
    loaded is respected, and an unchanged file is not rewritten (on an iPod that
    saves a write and keeps its timestamp).
    """
    from mutagen import File as MutagenFile

    audio = MutagenFile(str(path), easy=True)
    if audio is None:
        raise TagError("unsupported metadata")
    changed = False
    for key, (mode, value) in edits.items():
        current = [str(v) for v in (audio.get(key) or [])]
        if mode == "clear":
            if key in audio:
                del audio[key]
                changed = True
        elif current != [value]:
            audio[key] = [value]
            changed = True
    if changed:
        audio.save()
    return changed
