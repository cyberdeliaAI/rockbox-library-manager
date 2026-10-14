# SPDX-License-Identifier: GPL-2.0-only
"""Read-only track diagnostics. Matching metadata never establishes audio identity."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path


def position(value: str) -> int:
    head = str(value or "").split("/", 1)[0].strip()
    return int(head) if head.isdigit() and len(head) <= 9 else 0


def title_key(value: str) -> str:
    # Keep version text and punctuation apart from straight/curly quotation marks.
    return " ".join(re.sub("['\"‘’“”]", "", value.casefold()).split())


def inspect_tracks(read: dict[Path, dict[str, list[str]]], *, complete: bool) -> list[dict]:
    """Find same-disc numbered title matches and possible gaps in a complete listing.

    Missing disc tags use the physical parent as their scope, so CD1/CD2 folders
    do not manufacture duplicates. Ambiguous multi-value tags are not guessed.
    """
    groups = defaultdict(list)
    disc_states = defaultdict(set)
    for path, values in read.items():
        disc, album = values.get("discnumber", []), values.get("album", [])
        if len(disc) > 1 or len(album) > 1 or disc and not position(disc[0]):
            complete = False
            continue
        disc_states[path.parent].add(bool(disc))
        scope = ("disc", position(disc[0])) if disc else ("folder", str(path.parent))
        groups[(tuple(album), scope)].append((path, values))
    issues = []
    for (_album, scope), rows in groups.items():
        matched = defaultdict(list)
        numbers = set()
        all_numbered = True
        for path, values in rows:
            tracks, titles = values.get("tracknumber", []), values.get("title", [])
            number = position(tracks[0]) if len(tracks) == 1 else 0
            all_numbered &= bool(number)
            if number:
                numbers.add(number)
            key = title_key(titles[0]) if len(titles) == 1 else ""
            if number and key:
                matched[(number, key)].append(path)
        for (number, _title), paths in matched.items():
            if len(paths) > 1:
                issues.append({"type": "duplicate_track", "paths": paths,
                               "details": f"Track {number} has the same title at the same disc position: "
                                          + ", ".join(p.name for p in paths[:6])
                                          + ". Metadata matches only; audio was not compared."})
        mixed_disc_tags = any(len(disc_states[path.parent]) > 1 for path, _ in rows)
        if complete and all_numbered and numbers and not mixed_disc_tags:
            previous, missing, ranges = 0, 0, []
            for number in sorted(numbers):
                start, end = previous + 1, number - 1
                if start <= end:
                    missing += end - start + 1
                    if len(ranges) < 6:
                        ranges.append(str(start) if start == end else f"{start}–{end}")
                previous = number
            if missing:
                label = f"disc {scope[1]}" if scope[0] == "disc" else Path(scope[1]).name
                issues.append({"type": "track_gaps", "paths": [p for p, _ in rows],
                               "details": f"Possibly incomplete ({label}): {missing} missing numbered position(s) "
                                          f"below the highest track: {', '.join(ranges)}. "
                                          "This may be an intentional selection; no files were changed."})
    return issues
