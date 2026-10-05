# SPDX-License-Identifier: GPL-2.0-only
"""Library Health: tag-aware analysis over the indexed artist and album folders.

Besides folder/tag consistency, Health looks for what matters on a Rockbox
player: tags with several values (the direct database update refuses them),
years the database update cannot read, combined genre text and genre spellings
that Rockbox lists as separate genres, and missing years, genres and track
numbers. Analysis only reads files.
"""
from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

from . import artwork_engine as engine
from . import tags
from .library import LibraryDB, LibraryItem, artist_identity_key, choose_display_name

ISSUE_LABELS = {
    "duplicate_artist": "Duplicate artist",
    "tag_mismatch": "Tag mismatch",
    "inconsistent_album": "Inconsistent album",
    "missing_tags": "Missing tags",
    "multiple_values": "Several values",
    "unreadable_year": "Unreadable year",
    "combined_genre": "Combined genre",
    "genre_spelling": "Genre spelling",
    "missing_year": "Missing year",
    "missing_genre": "Missing genre",
    "track_numbers": "Track numbers",
}

# Health view filters: one segment per group of issue types.
FILTERS = [
    ("all", "All"),
    ("duplicate_artist", "Duplicates"),
    ("tags", "Tags"),
    ("database", "Database"),
    ("genres", "Genres"),
    ("missing", "Missing info"),
]
CATEGORIES = {
    "duplicate_artist": ("duplicate_artist",),
    "tags": ("tag_mismatch", "inconsistent_album", "missing_tags"),
    "database": ("multiple_values", "unreadable_year"),   # these stop the direct database update
    "genres": ("combined_genre", "genre_spelling"),
    "missing": ("missing_year", "missing_genre", "track_numbers"),
}
DATABASE_FIELDS = ("artist", "albumartist", "album", "genre", "date")


def first(values: dict[str, list[str]], key: str) -> str:
    found = values.get(key) or []
    return found[0] if found else ""


def track_number(value: str) -> int:
    """'3', '03' or '3/12' -> 3; anything else -> 0."""
    head = str(value or "").split("/", 1)[0].strip()
    return int(head) if head.isdigit() else 0


class HealthScanner:
    """Tag-aware analysis over the *indexed* physical library.

    Using the existing index keeps Health in lockstep with the normal scan and
    avoids recursively discovering the same folders a second time. By default
    three tracks per album are read (first, middle, last), which keeps a scan of
    a slow iPod disk short; ``thorough`` reads every track.
    """

    def __init__(self, db: LibraryDB, root: Path, progress: Callable[[int, int, str], None],
                 cancel: Optional[threading.Event] = None, *, thorough: bool = False) -> None:
        self.db = db
        self.root = root
        self.progress = progress
        self.cancel = cancel or threading.Event()
        self.thorough = thorough

    @staticmethod
    def _sample_files(files: list[Path], limit: int = 3) -> list[Path]:
        if len(files) <= limit:
            return files
        return [files[0], files[len(files) // 2], files[-1]]

    def scan(self) -> dict[str, int]:
        issues: list[dict[str, Any]] = []
        scan_id = time.time_ns()

        issues += self._duplicate_artists()

        # Analyze exactly the album folders already accepted by the normal scan.
        album_items = self.db.physical_items("album")
        genre_albums: dict[str, dict[str, list[LibraryItem]]] = defaultdict(lambda: defaultdict(list))
        total = len(album_items)
        for index, item in enumerate(album_items, start=1):
            if self.cancel.is_set():
                return {"cancelled": 1, "issues": len(issues)}
            album_dir = self.root / Path(item.relative_path)
            self.progress(index, total, f"{item.artist} - {item.album}")

            # One recursive walk per album, then sample from that list. This keeps
            # multi-disc files usable without discovering CD1/CD2 as extra albums.
            files = list(engine.iter_audio_files(album_dir))
            samples = files if self.thorough else self._sample_files(files, 3)
            if not samples:
                continue
            read: dict[Path, dict[str, list[str]]] = {}
            unreadable: list[str] = []
            for path in samples:
                try:
                    read[path] = tags.read_values(path)
                except Exception as exc:
                    unreadable.append(f"{path.name}: {exc}")
            album_issues = self._album_issues(item, samples, read, unreadable)
            issues += album_issues
            for values in read.values():
                for genre in values.get("genre", []):
                    if not tags.combined_genre(genre):
                        genre_albums[tags.genre_key(genre)][genre].append(item)

        issues += self._genre_spellings(genre_albums)

        if self.cancel.is_set():
            return {"cancelled": 1, "issues": len(issues)}
        self.db.replace_health_issues(issues, scan_id)
        self.db.set_meta("health_dirty", "0")
        counts = self.db.health_counts()
        counts["cancelled"] = 0
        return counts

    # -- artists ------------------------------------------------------------
    def _duplicate_artists(self) -> list[dict[str, Any]]:
        # Duplicate detection is based on the physical artist rows in the same
        # index used by the gallery. A duplicate can remain deliberately split.
        issues = []
        groups: dict[str, list[LibraryItem]] = {}
        for item in self.db.physical_items("artist"):
            groups.setdefault(artist_identity_key(item.artist), []).append(item)
        explicit = self.db.alias_map()
        disabled = self.db.auto_merge_disabled()
        for group in groups.values():
            names = [i.artist for i in group]
            if len(set(names)) < 2:
                continue
            # "Keep separate" is a resolved identity decision, not a recurring
            # health problem. Do not re-add it on every analysis pass.
            if any(n in disabled for n in names):
                continue

            # An explicit library merge is also a resolved decision. Automatic
            # normalization may discover Toure/Touré every scan, but once the
            # user has deliberately mapped every physical alias to one canonical
            # name it should not keep appearing as a Health problem.
            explicit_targets = [explicit.get(n.casefold()) for n in names]
            if explicit_targets and all(explicit_targets) and len({x.casefold() for x in explicit_targets if x}) == 1:
                continue

            canonical = choose_display_name([
                explicit.get(n.casefold(), n) for n in names
            ])
            paths = [i.relative_path for i in group]
            issues.append({
                "issue_type": "duplicate_artist", "severity": "info", "artist": canonical,
                "relative_path": paths[0],
                "details": f"{len(paths)} artist folders normalize to the same identity (auto-merged): " + " / ".join(names),
                "data": {"aliases": names, "canonical": canonical, "paths": paths,
                         "auto_merge_enabled": True},
            })
        return issues

    # -- albums ---------------------------------------------------------------
    def _album_issues(self, item: LibraryItem, samples: list[Path], read: dict[Path, dict[str, list[str]]],
                      unreadable: list[str]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        base = {"artist": item.artist, "album": item.album, "relative_path": item.relative_path}
        names = [p.name for p in samples]
        checked = "every track" if self.thorough else f"{len(samples)} sampled track" + ("" if len(samples) == 1 else "s")

        def add(issue_type: str, severity: str, details: str, **data: Any) -> None:
            issues.append({**base, "issue_type": issue_type, "severity": severity, "details": details,
                           "data": {"samples": names, **data}})

        # Rockbox keeps one value per tag; the direct database update stops at a
        # file with several, so these are reported even when the rest is fine.
        several = Counter()
        examples: dict[str, list[str]] = {}
        for values in read.values():
            for key in DATABASE_FIELDS:
                if len(values.get(key, [])) > 1:
                    several[key] += 1
                    examples.setdefault(key, values[key])
        if several:
            parts = [f"{tags.LABELS.get(k, k).lower()} {tags.show(examples[k])} on {n} track" + ("" if n == 1 else "s")
                     for k, n in several.items()]
            add("multiple_values", "bad", "Several values in one tag: " + "; ".join(parts) +
                ". Rockbox keeps one; Settings > Rockbox database refuses these files until each has one value.",
                fields=sorted(several))
        bad_years = sorted({first(v, "date") for v in read.values()
                            if first(v, "date") and not tags.database_can_read_year(first(v, "date"))})
        if bad_years:
            add("unreadable_year", "bad", "Year/date the Rockbox database update cannot read: " +
                ", ".join(f"“{y}”" for y in bad_years) + ". Use YYYY or YYYY-MM-DD.", years=bad_years)

        tracks = [v for v in read.values()]
        missing = unreadable or [v for v in tracks if not first(v, "album") or
                                 not (first(v, "albumartist") or first(v, "artist"))]
        if missing:
            fields: list[str] = []
            if any(not first(v, "album") for v in tracks):
                fields.append("album")
            if any(not (first(v, "albumartist") or first(v, "artist")) for v in tracks):
                fields.append("artist/album artist")
            if unreadable:
                fields.append("unreadable metadata")
            add("missing_tags", "bad", "Missing or unreadable: " + ", ".join(fields))
            return issues

        albums = sorted({first(v, "album") for v in tracks}, key=str.casefold)
        aas = sorted({first(v, "albumartist") for v in tracks if first(v, "albumartist")}, key=str.casefold)
        if len(albums) > 1 or len(aas) > 1:
            bits = []
            if len(albums) > 1:
                bits.append("album: " + " / ".join(albums))
            if len(aas) > 1:
                bits.append("album artist: " + " / ".join(aas))
            add("inconsistent_album", "bad", f"Tags differ between tracks ({checked}) - " + "; ".join(bits),
                albums=albums, album_artists=aas)
        else:
            self._folder_mismatch(item, albums, aas, tracks, add)

        self._missing_info(tracks, checked, add)
        return issues

    @staticmethod
    def _folder_mismatch(item: LibraryItem, albums: list[str], aas: list[str],
                         tracks: list[dict[str, list[str]]], add: Callable[..., None]) -> None:
        tag_album = albums[0] if albums else ""
        if aas:
            tag_artist = aas[0]
        else:
            track_artists = sorted({first(v, "artist") for v in tracks if first(v, "artist")}, key=str.casefold)
            tag_artist = track_artists[0] if len(track_artists) == 1 else ""

        artist_score = engine.similarity(item.artist, tag_artist) if tag_artist else 0.0
        album_score = engine.similarity(item.album, tag_album) if tag_album else 0.0
        if (tag_artist and artist_score < 0.72) or (tag_album and album_score < 0.62):
            diffs = []
            if tag_artist and artist_score < 0.72:
                diffs.append(f"artist folder '{item.artist}' vs tag '{tag_artist}' ({artist_score:.2f})")
            if tag_album and album_score < 0.62:
                diffs.append(f"album folder '{item.album}' vs tag '{tag_album}' ({album_score:.2f})")
            add("tag_mismatch", "warn", "; ".join(diffs), tag_artist=tag_artist, tag_album=tag_album,
                artist_score=artist_score, album_score=album_score)

    @staticmethod
    def _missing_info(tracks: list[dict[str, list[str]]], checked: str, add: Callable[..., None]) -> None:
        count = len(tracks)

        def without(key: str) -> int:
            return sum(1 for v in tracks if not first(v, key))

        def amount(n: int) -> str:
            return "any track" if n == count else f"{n} of {count} tracks"

        if without("date"):
            add("missing_year", "info", f"No year on {amount(without('date'))} ({checked}).")
        if without("genre"):
            add("missing_genre", "info", f"No genre on {amount(without('genre'))} ({checked}). "
                "Rockbox lists these tracks under <Untagged> in its Genre view.")
        combined = sorted({g for v in tracks for g in v.get("genre", []) if tags.combined_genre(g)})
        if combined:
            add("combined_genre", "warn", "Combined genre text " + ", ".join(f"“{g}”" for g in combined) +
                " is listed by Rockbox as a genre of its own. Keep one main genre, e.g. “" +
                tags.split_genre(combined[0])[0] + "”.", genres=combined)
        numbers = [track_number(first(v, "tracknumber")) for v in tracks]
        problems = []
        if any(n == 0 for n in numbers):
            problems.append(f"no track number on {amount(sum(1 for n in numbers if n == 0))}")
        pairs = Counter((first(v, "discnumber").split("/", 1)[0].strip() or "1", n)
                        for v, n in zip(tracks, numbers) if n)
        duplicates = sorted(n for (_disc, n), c in pairs.items() if c > 1)
        if duplicates:
            problems.append("track number used twice: " + ", ".join(str(n) for n in duplicates))
        if problems:
            text = "; ".join(problems)
            add("track_numbers", "warn", text[:1].upper() + text[1:] + f" ({checked}). Without unique track "
                "numbers Rockbox's database can't list the album in order.")

    # -- genres across the library ---------------------------------------------
    @staticmethod
    def _genre_spellings(genre_albums: dict[str, dict[str, list[LibraryItem]]]) -> list[dict[str, Any]]:
        """Albums using a less common spelling of a genre (Hip-Hop next to Hip Hop).

        Rockbox merges differences in case only, so 'Hip Hop' and 'hip hop' are
        one genre but 'Hip Hop' and 'Hip-Hop' are two.
        """
        issues = []
        for spellings in genre_albums.values():
            by_case: dict[str, set[int]] = defaultdict(set)
            shown: dict[str, str] = {}
            for spelling, items in sorted(spellings.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                key = spelling.casefold()
                by_case[key].update(i.id for i in items)
                # Name a case group by its most used spelling, preferring capitals on a tie.
                current = shown.get(key)
                if current is None or (len(items) == len(spellings[current])
                                       and sum(c.isupper() for c in spelling) > sum(c.isupper() for c in current)):
                    shown[key] = spelling
            if len(by_case) < 2:
                continue
            ranked = sorted(by_case, key=lambda k: (-len(by_case[k]), shown[k]))
            main = ranked[0]
            others = ", ".join(f"“{shown[k]}” ({len(by_case[k])} album" + ("" if len(by_case[k]) == 1 else "s") + ")"
                               for k in ranked)
            for key in ranked[1:]:
                seen: set[int] = set()
                for spelling, items in spellings.items():
                    if spelling.casefold() != key:
                        continue
                    for item in items:
                        if item.id in seen:
                            continue
                        seen.add(item.id)
                        issues.append({
                            "issue_type": "genre_spelling", "severity": "info", "artist": item.artist,
                            "album": item.album, "relative_path": item.relative_path,
                            "details": f"Genre “{spelling}” is listed by Rockbox apart from “{shown[main]}”. "
                                       f"Spellings in the library: {others}.",
                            "data": {"genre": spelling, "main": shown[main]},
                        })
        return issues
