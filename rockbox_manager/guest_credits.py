# SPDX-License-Identifier: GPL-2.0-only
"""Read-only suggestions, ported from Anthony Fletcher's PodBox db_featured_parse.c.

Reference: anthonyfletcher/podbox f0850c4647e7c1cfde11ffc89ab40b55f8aa5635.
These are prose heuristics, never instructions to rewrite tags.
"""
from __future__ import annotations

import re

_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_MARKER = re.compile(r"(?<![A-Za-z0-9\x80-\U0010ffff])(?:duet with|featuring|feat|ft)(?:[.:]|[ \t])|"
                     r"(?<![A-Za-z0-9\x80-\U0010ffff])(?:w/|f/)(?![oO](?![A-Za-z0-9\x80-\U0010ffff]))",
                     re.ASCII | re.IGNORECASE)
_SEP = re.compile(r"[&,;+/]|(?<=[ \t])(?:and|x)(?=[ \t])", re.IGNORECASE | re.ASCII)


def name_key(value: str) -> str:
    """The player's ASCII-only equality; preserve accents and punctuation."""
    return value.strip(" \t").translate(_LOWER)


class KnownArtists(frozenset):
    """Normalize once per album view, rather than once per track/render."""
    def __new__(cls, values=()):
        return super().__new__(cls, (name_key(value) for value in values))


def guest_names(text: str, known_artists=()) -> list[str]:
    """Return at most eight bounded guest names from the first credit marker."""
    if len(text) > 4096:
        return []  # Prose heuristics must not make large metadata block the UI.
    text = text.split("\0", 1)[0]
    match = _MARKER.search(text)
    if match is None:
        return []
    stack = []
    for char in text[:match.end()]:
        if char in "([":
            stack.append(char)
        elif char in ")]" and stack:
            stack.pop()
    end = len(text)
    if stack and len(stack) <= 4:
        closer = "]" if stack[-1] == "[" else ")"
        pos = text.find(closer, match.end())
        if pos >= 0:
            end = pos
    for char in "([":
        pos = text.find(char, match.end(), end)
        if pos >= 0:
            end = pos
    rest = text[match.end():end]
    known = known_artists if isinstance(known_artists, KnownArtists) else KnownArtists(known_artists)
    out, rejected = [], 0
    while len(out) < 8 and rejected < 8:
        cut = None
        if name_key(rest) not in known:
            cuts = [s for s in _SEP.finditer(rest)
                    if rest[:s.start()].strip(" \t") and rest[s.end():].strip(" \t")]
            if cuts:
                recognized = [s for s in cuts if name_key(rest[:s.start()]) in known]
                cut = recognized[-1] if recognized else cuts[0]
        value = (rest[:cut.start()] if cut else rest).strip(" \t")
        if 2 <= len(value.encode("utf-8")) <= 63:
            out.append(value)
        else:
            rejected += 1
        if cut is None:
            break
        rest = rest[cut.end():]
    return out


def track_guests(values: dict[str, list[str]], known_artists=()) -> list[str]:
    result, seen = [], set()
    for field in ("title", "artist"):
        for value in values.get(field, []):
            for name in guest_names(value, known_artists):
                key = name_key(name)
                if key not in seen:
                    result.append(name)
                    seen.add(key)
    return result
