# SPDX-License-Identifier: GPL-2.0-only
"""The local library index: SQLite rows for every artist/album folder and a fast
directory-only scanner for slow removable players."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from . import artwork_engine as engine
from .common import EXCLUDED_DIRS

DEFAULT_PAGE_SIZE = 30


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def image_file_signature(path: Path) -> tuple[int, int]:
    """Return mtime_ns + size without opening the image."""
    try:
        st = path.stat()
        return int(st.st_mtime_ns), int(st.st_size)
    except OSError:
        return 0, 0


def contains_audio_immediate(folder: Path) -> bool:
    """Fast check: inspect directory entries only, never parse tags."""
    with os.scandir(folder) as it:
        for entry in it:
            if entry.is_file(follow_symlinks=False):
                if Path(entry.name).suffix.casefold() in engine.AUDIO_EXTS:
                    return True
    return False


def immediate_subdirs(folder: Path) -> list[Path]:
    out: list[Path] = []
    with os.scandir(folder) as it:
        for entry in it:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name.casefold() in EXCLUDED_DIRS or entry.name.startswith("."):
                continue
            out.append(Path(entry.path))
    out.sort(key=lambda p: p.name.casefold())
    return out


def detect_artwork(folder: Path, preferred_name: str) -> tuple[bool, str, int, int]:
    """Find preferred artwork name first, then the other supported Rockbox name."""
    candidates = [preferred_name]
    for name in ("folder.jpg", "cover.jpg"):
        if name not in candidates:
            candidates.append(name)
    for name in candidates:
        path = folder / name
        if path.is_file():
            mtime_ns, size = image_file_signature(path)
            return True, name, mtime_ns, size
    return False, preferred_name, 0, 0


def artist_identity_key(value: str) -> str:
    """Conservative identity key used for virtual artist merging.

    It deliberately ignores accents, case, punctuation and spacing, but does not
    remove words. This safely merges names such as ``Ali Farka Toure`` and
    ``Ali Farka Touré`` without fuzzy-merging unrelated artists.
    """
    import unicodedata

    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^\w\s]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def display_name_score(value: str) -> tuple[int, int, int, str]:
    """Prefer informative, nicely-cased Unicode spellings as display names."""
    import unicodedata

    letters = [ch for ch in value if ch.isalpha()]
    accents = sum(1 for ch in letters if ord(ch) > 127 and unicodedata.category(ch).startswith("L"))
    punctuation = sum(1 for ch in value if ch in ".'’/&-")
    upper_words = sum(1 for word in value.split() if word[:1].isupper())
    # More accents/punctuation can preserve the canonical spelling; shorter is a useful tie-breaker.
    return accents, punctuation, upper_words, -len(value), value.casefold()


def choose_display_name(values: list[str]) -> str:
    clean = sorted({(v or "").strip() for v in values if (v or "").strip()})
    return max(clean, key=display_name_score) if clean else ""


def sample_audio_files(folder: Path, limit: int = 3) -> list[Path]:
    files = [p for p in engine.iter_audio_files(folder)]
    if len(files) <= limit:
        return files
    if limit <= 1:
        return [files[0]]
    idxs = sorted({0, len(files) // 2, len(files) - 1})
    return [files[i] for i in idxs]


# ----------------------------------------------------------------------
# Library model + SQLite index
# ----------------------------------------------------------------------
@dataclass
class LibraryItem:
    id: int
    kind: str
    artist: str
    album: str
    relative_path: str
    artwork_name: str
    artwork_exists: bool
    artwork_mtime_ns: int
    artwork_size: int
    problem: str
    group_paths: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    group_missing_paths: tuple[str, ...] = ()
    artwork_relative_path: str = ""

    @property
    def title(self) -> str:
        return self.artist if self.kind == "artist" else self.album

    @property
    def subtitle(self) -> str:
        return "Artist" if self.kind == "artist" else self.artist

    @property
    def status(self) -> str:
        if self.problem:
            return "Problem"
        return "Existing" if self.artwork_exists else "Missing"

    @property
    def status_label(self) -> str:
        return {"Problem": "Problem", "Existing": "Has artwork", "Missing": "Missing"}[self.status]


class LibraryDB:
    """SQLite-backed physical index with a cached virtual/canonical library view.

    The database always keeps one row per real artist/album folder. Artist identity
    merging happens only in the cached virtual view, so filesystem state remains
    explicit and artwork can never be hidden by a merged card.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._view_cache: Optional[list[LibraryItem]] = None
        self._view_by_id: dict[int, LibraryItem] = {}
        self._search_blob_cache: dict[int, str] = {}
        self._album_counts_cache: Optional[dict[str, int]] = None
        self._init_schema()

    def _init_schema(self) -> None:
        with self.lock, self.conn:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('artist', 'album')),
                    artist TEXT NOT NULL,
                    album TEXT NOT NULL DEFAULT '',
                    relative_path TEXT NOT NULL,
                    artwork_name TEXT NOT NULL DEFAULT 'folder.jpg',
                    artwork_exists INTEGER NOT NULL DEFAULT 0,
                    artwork_mtime_ns INTEGER NOT NULL DEFAULT 0,
                    artwork_size INTEGER NOT NULL DEFAULT 0,
                    problem TEXT NOT NULL DEFAULT '',
                    last_seen_scan INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(kind, relative_path)
                );

                CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
                CREATE INDEX IF NOT EXISTS idx_items_exists ON items(artwork_exists);
                CREATE INDEX IF NOT EXISTS idx_items_artist ON items(artist COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_items_album ON items(album COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_items_problem ON items(problem);

                CREATE TABLE IF NOT EXISTS artist_aliases (
                    alias TEXT PRIMARY KEY COLLATE NOCASE,
                    canonical TEXT NOT NULL
                );

                -- Exact artist names in this table are deliberately excluded from
                -- automatic identity merging. COLLATE BINARY is important: Sophie
                -- and SOPHIE can be distinct artists.
                CREATE TABLE IF NOT EXISTS artist_auto_merge_disabled (
                    artist TEXT PRIMARY KEY COLLATE BINARY
                );

                CREATE TABLE IF NOT EXISTS health_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'warn',
                    artist TEXT NOT NULL DEFAULT '',
                    album TEXT NOT NULL DEFAULT '',
                    relative_path TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}',
                    last_seen_scan INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_health_type ON health_issues(issue_type);
                CREATE INDEX IF NOT EXISTS idx_health_path ON health_issues(relative_path);
                """
            )

    def invalidate_view_cache(self) -> None:
        with self.lock:
            self._view_cache = None
            self._view_by_id = {}
            self._search_blob_cache = {}
            self._album_counts_cache = None

    def set_meta(self, key: str, value: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def upsert_item(self, *, kind: str, artist: str, album: str, relative_path: str,
                    artwork_name: str, artwork_exists: bool, artwork_mtime_ns: int,
                    artwork_size: int, scan_id: int) -> None:
        with self.lock, self.conn:
            self._upsert_item(kind=kind, artist=artist, album=album, relative_path=relative_path,
                              artwork_name=artwork_name, artwork_exists=artwork_exists,
                              artwork_mtime_ns=artwork_mtime_ns, artwork_size=artwork_size, scan_id=scan_id)

    def _upsert_item(self, *, kind: str, artist: str, album: str, relative_path: str,
                    artwork_name: str, artwork_exists: bool, artwork_mtime_ns: int,
                    artwork_size: int, scan_id: int) -> None:
        """Execute inside the caller's database lock and transaction."""
        self.conn.execute(
            """
            INSERT INTO items(
                kind, artist, album, relative_path, artwork_name,
                artwork_exists, artwork_mtime_ns, artwork_size, last_seen_scan
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, relative_path) DO UPDATE SET
                artist=excluded.artist,
                album=excluded.album,
                artwork_name=excluded.artwork_name,
                artwork_exists=excluded.artwork_exists,
                artwork_mtime_ns=excluded.artwork_mtime_ns,
                artwork_size=excluded.artwork_size,
                last_seen_scan=excluded.last_seen_scan,
                problem=CASE
                    WHEN excluded.artwork_exists=1 THEN ''
                    ELSE items.problem
                END
            """,
            (kind, artist, album, relative_path, artwork_name,
             1 if artwork_exists else 0, artwork_mtime_ns, artwork_size, scan_id),
        )

    def commit_scan(self, items: list[dict[str, Any]], scan_id: int, root: Path, output_name: str) -> None:
        """Publish a fully read scan atomically; failures leave the old index/root intact."""
        with self.lock, self.conn:
            # Windows wall-clock resolution can give consecutive scans the
            # same timestamp. Never reuse a marker already present in the index.
            previous = self.conn.execute("SELECT COALESCE(MAX(last_seen_scan), 0) FROM items").fetchone()[0]
            scan_id = max(scan_id, int(previous) + 1)
            for item in items:
                self._upsert_item(**item, scan_id=scan_id)
            self.conn.execute("DELETE FROM items WHERE last_seen_scan <> ?", (scan_id,))
            stamp = str(int(time.time()))
            for key, value in (("music_root", str(root)), ("output_name", output_name),
                               ("scan_started", stamp), ("last_scan", stamp)):
                self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.invalidate_view_cache()

    def finish_scan(self, scan_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM items WHERE last_seen_scan <> ?", (scan_id,))
        self.invalidate_view_cache()

    def clear(self) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM items")
            self.conn.execute("DELETE FROM health_issues")
            self.conn.execute("DELETE FROM meta")
        self.invalidate_view_cache()

    def alias_map(self) -> dict[str, str]:
        with self.lock:
            rows = self.conn.execute("SELECT alias, canonical FROM artist_aliases").fetchall()
        return {str(r["alias"]).casefold(): str(r["canonical"]) for r in rows}

    def set_artist_aliases(self, aliases: list[str], canonical: str) -> None:
        canonical = canonical.strip()
        with self.lock, self.conn:
            for alias in aliases:
                alias = alias.strip()
                if alias:
                    self.conn.execute(
                        "INSERT INTO artist_aliases(alias, canonical) VALUES(?, ?) "
                        "ON CONFLICT(alias) DO UPDATE SET canonical=excluded.canonical",
                        (alias, canonical),
                    )
        self.invalidate_view_cache()

    def auto_merge_disabled(self) -> set[str]:
        with self.lock:
            rows = self.conn.execute("SELECT artist FROM artist_auto_merge_disabled").fetchall()
        return {str(r["artist"]) for r in rows}

    def set_artist_auto_merge(self, aliases: list[str], enabled: bool) -> None:
        clean = sorted({str(a).strip() for a in aliases if str(a).strip()})
        with self.lock, self.conn:
            if enabled:
                self.conn.executemany(
                    "DELETE FROM artist_auto_merge_disabled WHERE artist=? COLLATE BINARY",
                    [(a,) for a in clean],
                )
            else:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO artist_auto_merge_disabled(artist) VALUES(?)",
                    [(a,) for a in clean],
                )
        self.invalidate_view_cache()

    def _artist_resolution(self, names: list[str]) -> dict[str, tuple[str, str]]:
        """Return original name -> (group key, display name)."""
        explicit = self.alias_map()
        disabled = self.auto_merge_disabled()
        provisional: dict[str, tuple[str, str]] = {}
        group_values: dict[str, list[str]] = {}
        for name in names:
            if name in disabled:
                # Exact/BINARY key prevents Sophie and SOPHIE from being folded.
                group_key = "exact\0" + name
                shown = name
            else:
                shown = explicit.get(name.casefold(), name)
                group_key = "auto\0" + artist_identity_key(shown)
            provisional[name] = (group_key, shown)
            group_values.setdefault(group_key, []).append(shown)
        canonical = {key: choose_display_name(vals) for key, vals in group_values.items()}
        return {name: (key, canonical.get(key, shown)) for name, (key, shown) in provisional.items()}

    def artist_name_map(self) -> dict[str, str]:
        with self.lock:
            rows = self.conn.execute("SELECT DISTINCT artist FROM items WHERE artist<>''").fetchall()
        names = [str(r["artist"]) for r in rows]
        return {name: display for name, (_key, display) in self._artist_resolution(names).items()}

    def replace_health_issues(self, issues: list[dict[str, Any]], scan_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM health_issues")
            self.conn.executemany(
                "INSERT INTO health_issues(issue_type,severity,artist,album,relative_path,details,data_json,last_seen_scan) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(i.get("issue_type", ""), i.get("severity", "warn"), i.get("artist", ""),
                  i.get("album", ""), i.get("relative_path", ""), i.get("details", ""),
                  json.dumps(i.get("data", {}), ensure_ascii=False), scan_id) for i in issues],
            )
            self.set_meta("last_health_scan", str(int(time.time())))

    def health_counts(self) -> dict[str, int]:
        out = {"all": 0}
        with self.lock:
            rows = self.conn.execute("SELECT issue_type, COUNT(*) AS n FROM health_issues GROUP BY issue_type").fetchall()
        for row in rows:
            key, n = str(row["issue_type"]), int(row["n"] or 0)
            out[key] = n
            out["all"] += n
        return out

    def health_issues(self, issue_type: str | tuple[str, ...] = "all") -> list[sqlite3.Row]:
        """Issues of one type, of several types (a tuple), or all of them."""
        with self.lock:
            if issue_type == "all":
                return self.conn.execute("SELECT * FROM health_issues ORDER BY issue_type, artist COLLATE NOCASE, album COLLATE NOCASE").fetchall()
            types = (issue_type,) if isinstance(issue_type, str) else tuple(issue_type)
            marks = ",".join("?" for _ in types)
            return self.conn.execute(f"SELECT * FROM health_issues WHERE issue_type IN ({marks}) ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, issue_type", types).fetchall()

    def get_health_issue(self, issue_id: int) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM health_issues WHERE id=?", (issue_id,)).fetchone()

    def delete_health_issue(self, issue_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM health_issues WHERE id=?", (int(issue_id),))

    def physical_items(self, kind: Optional[str] = None) -> list[LibraryItem]:
        with self.lock:
            if kind in {"artist", "album"}:
                rows = self.conn.execute(
                    "SELECT * FROM items WHERE kind=? ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE",
                    (kind,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM items ORDER BY artist COLLATE NOCASE, kind, album COLLATE NOCASE"
                ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def items_for_paths(self, paths: tuple[str, ...] | list[str], *, kind: Optional[str] = None) -> list[LibraryItem]:
        clean = [str(p) for p in paths if str(p)]
        if not clean:
            return []
        marks = ",".join("?" for _ in clean)
        params: list[Any] = list(clean)
        sql = f"SELECT * FROM items WHERE relative_path IN ({marks})"
        if kind in {"artist", "album"}:
            sql += " AND kind=?"
            params.append(kind)
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        by_path = {str(r["relative_path"]): self._row_to_item(r) for r in rows}
        return [by_path[p] for p in clean if p in by_path]

    @staticmethod
    def _search_blob(item: LibraryItem) -> str:
        # Same accent/case/punctuation normalization used for artist identity also
        # gives predictable Unicode-insensitive search (BJÖRK -> bjork).
        aliases = " ".join(item.aliases)
        return artist_identity_key(f"{item.artist} {item.album} {aliases}")

    def _build_view_cache(self) -> list[LibraryItem]:
        # Keep construction *and* publication of the cache under the same RLock.
        # Without this, another worker can invalidate the cache between publication
        # and get_item(), producing a spurious None even though the item still exists.
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM items ORDER BY artist COLLATE NOCASE, CASE WHEN kind='artist' THEN 0 ELSE 1 END, album COLLATE NOCASE"
            ).fetchall()
            raw = [self._row_to_item(row) for row in rows]
            names = sorted({i.artist for i in raw if i.artist}, key=str.casefold)
            resolution = self._artist_resolution(names)

            albums: list[LibraryItem] = []
            artist_groups: dict[str, list[LibraryItem]] = {}
            for item in raw:
                group_key, canonical = resolution.get(
                    item.artist,
                    ("auto\0" + artist_identity_key(item.artist), item.artist),
                )
                if item.kind == "album":
                    albums.append(replace(item, artist=canonical))
                else:
                    artist_groups.setdefault(group_key, []).append(item)

            artists: list[LibraryItem] = []
            for group_key, group in artist_groups.items():
                _unused, canonical = resolution.get(group[0].artist, (group_key, group[0].artist))
                # Representative ID/path is stable and independent of artwork state.
                rep = sorted(
                    group,
                    key=lambda i: (i.artist.casefold() != canonical.casefold(), i.relative_path.casefold()),
                )[0]
                aliases = tuple(sorted({i.artist for i in group}, key=lambda x: (x.casefold(), x)))
                paths = tuple(i.relative_path for i in sorted(group, key=lambda x: x.relative_path.casefold()))
                missing_paths = tuple(i.relative_path for i in group if not i.artwork_exists)
                art_items = [i for i in group if i.artwork_exists]
                art_rep = sorted(
                    art_items,
                    key=lambda i: (i.artist.casefold() != canonical.casefold(), i.relative_path.casefold()),
                )[0] if art_items else rep
                all_have_art = bool(group) and not missing_paths
                problem = next((i.problem for i in group if i.problem), "")
                artists.append(replace(
                    rep,
                    artist=canonical,
                    artwork_name=art_rep.artwork_name,
                    artwork_exists=all_have_art,
                    artwork_mtime_ns=art_rep.artwork_mtime_ns if art_items else 0,
                    artwork_size=art_rep.artwork_size if art_items else 0,
                    aliases=aliases,
                    group_paths=paths,
                    group_missing_paths=missing_paths,
                    artwork_relative_path=art_rep.relative_path if art_items else "",
                    problem=problem,
                ))

            out = artists + albums
            out.sort(key=lambda i: (i.artist.casefold(), 0 if i.kind == "artist" else 1, i.album.casefold()))
            self._view_cache = out
            self._view_by_id = {i.id: i for i in out}
            self._search_blob_cache = {i.id: self._search_blob(i) for i in out}
            counts: dict[str, int] = {}
            for i in albums:
                counts[i.artist] = counts.get(i.artist, 0) + 1
            self._album_counts_cache = counts
            return out

    def canonical_items(self) -> list[LibraryItem]:
        with self.lock:
            if self._view_cache is None:
                self._build_view_cache()
            return list(self._view_cache or [])

    def counts(self, kind_filter: str = "All") -> dict[str, int]:
        items = self.canonical_items()
        if kind_filter == "Artists":
            items = [i for i in items if i.kind == "artist"]
        elif kind_filter == "Albums":
            items = [i for i in items if i.kind == "album"]
        return {
            "total": len(items),
            "artists": sum(1 for i in items if i.kind == "artist"),
            "albums": sum(1 for i in items if i.kind == "album"),
            "existing": sum(1 for i in items if i.status == "Existing"),
            "missing": sum(1 for i in items if i.status == "Missing"),
            "problems": sum(1 for i in items if i.status == "Problem"),
        }

    def query_all(self, *, kind_filter: str = "All", status_filter: str = "All",
                  search: str = "") -> list[LibraryItem]:
        # Merge identities first, then filter/search. This prevents search text from
        # changing which physical folder becomes the virtual artist card.
        with self.lock:
            if self._view_cache is None:
                self._build_view_cache()
            items = list(self._view_cache or [])
            blobs = dict(self._search_blob_cache)
        if kind_filter == "Artists":
            items = [i for i in items if i.kind == "artist"]
        elif kind_filter == "Albums":
            items = [i for i in items if i.kind == "album"]
        if status_filter == "Existing":
            items = [i for i in items if i.status == "Existing"]
        elif status_filter == "Missing":
            items = [i for i in items if i.status == "Missing"]
        elif status_filter == "Problems":
            items = [i for i in items if i.status == "Problem"]
        words = [artist_identity_key(w) for w in (search or "").split() if artist_identity_key(w)]
        if words:
            items = [i for i in items if all(w in blobs.get(i.id, "") for w in words)]
        return list(items)

    def query_items(self, *, kind_filter: str = "All", status_filter: str = "All",
                    search: str = "", limit: int = DEFAULT_PAGE_SIZE,
                    offset: int = 0) -> tuple[list[LibraryItem], int]:
        items = self.query_all(kind_filter=kind_filter, status_filter=status_filter, search=search)
        return items[offset:offset + limit], len(items)

    def album_counts(self) -> dict[str, int]:
        with self.lock:
            if self._album_counts_cache is None:
                self._build_view_cache()
            return dict(self._album_counts_cache or {})

    def get_item(self, item_id: int) -> Optional[LibraryItem]:
        with self.lock:
            if self._view_cache is None:
                self._build_view_cache()
            return self._view_by_id.get(int(item_id))

    def refresh_cached_item(self, item_id: int) -> Optional[LibraryItem]:
        """Patch one virtual item after an artwork-only database update.

        Batch artwork fetches update physical rows one item at a time. Rebuilding
        the complete canonical view after every item defeats the cache, but leaving
        it untouched means cards cannot update live. This method updates just the
        affected album or merged artist while holding the cache lock.
        """
        with self.lock:
            if self._view_cache is None:
                self._build_view_cache()
            old = self._view_by_id.get(int(item_id))
            if old is None:
                return None

            if old.kind == "album":
                row = self.conn.execute("SELECT * FROM items WHERE id=?", (old.id,)).fetchone()
                if row is None:
                    return None
                raw = self._row_to_item(row)
                new = replace(raw, artist=old.artist)
            else:
                paths = old.group_paths or (old.relative_path,)
                marks = ",".join("?" for _ in paths)
                rows = self.conn.execute(
                    f"SELECT * FROM items WHERE kind='artist' AND relative_path IN ({marks})",
                    list(paths),
                ).fetchall()
                group = [self._row_to_item(r) for r in rows]
                if not group:
                    return None
                by_path = {i.relative_path: i for i in group}
                ordered = [by_path[p] for p in paths if p in by_path]
                rep = next((i for i in ordered if i.id == old.id), ordered[0])
                missing_paths = tuple(i.relative_path for i in ordered if not i.artwork_exists)
                art_items = [i for i in ordered if i.artwork_exists]
                art_rep = None
                if old.artwork_relative_path:
                    art_rep = next((i for i in art_items if i.relative_path == old.artwork_relative_path), None)
                if art_rep is None and art_items:
                    art_rep = art_items[0]
                all_have_art = bool(ordered) and not missing_paths
                problem = next((i.problem for i in ordered if i.problem), "")
                new = replace(
                    rep,
                    artist=old.artist,
                    aliases=old.aliases,
                    group_paths=old.group_paths,
                    group_missing_paths=missing_paths,
                    artwork_name=(art_rep.artwork_name if art_rep else rep.artwork_name),
                    artwork_exists=all_have_art,
                    artwork_mtime_ns=(art_rep.artwork_mtime_ns if art_rep else 0),
                    artwork_size=(art_rep.artwork_size if art_rep else 0),
                    artwork_relative_path=(art_rep.relative_path if art_rep else ""),
                    problem=problem,
                )

            for idx, cached in enumerate(self._view_cache or []):
                if cached.id == old.id:
                    self._view_cache[idx] = new  # type: ignore[index]
                    break
            self._view_by_id[old.id] = new
            self._search_blob_cache[old.id] = self._search_blob(new)
            return new

    def set_item_state(self, item_id: int, *, artwork_name: Optional[str] = None,
                       artwork_exists: Optional[bool] = None,
                       artwork_mtime_ns: Optional[int] = None,
                       artwork_size: Optional[int] = None,
                       problem: Optional[str] = None) -> None:
        fields: list[str] = []
        params: list[Any] = []
        mapping = {
            "artwork_name": artwork_name,
            "artwork_exists": None if artwork_exists is None else (1 if artwork_exists else 0),
            "artwork_mtime_ns": artwork_mtime_ns,
            "artwork_size": artwork_size,
            "problem": problem,
        }
        for key, value in mapping.items():
            if value is not None:
                fields.append(f"{key}=?")
                params.append(value)
        if not fields:
            return
        params.append(item_id)
        with self.lock, self.conn:
            self.conn.execute(f"UPDATE items SET {', '.join(fields)} WHERE id=?", params)
        self.invalidate_view_cache()

    def set_item_states_bulk(self, updates: list[tuple[int, dict[str, Any]]], *, invalidate: bool = True) -> None:
        if not updates:
            return
        with self.lock, self.conn:
            for item_id, values in updates:
                fields: list[str] = []
                params: list[Any] = []
                for key in ("artwork_name", "artwork_exists", "artwork_mtime_ns", "artwork_size", "problem"):
                    if key not in values:
                        continue
                    value = values[key]
                    if key == "artwork_exists":
                        value = 1 if value else 0
                    fields.append(f"{key}=?")
                    params.append(value)
                if fields:
                    params.append(int(item_id))
                    self.conn.execute(f"UPDATE items SET {', '.join(fields)} WHERE id=?", params)
        if invalidate:
            self.invalidate_view_cache()

    def missing_items(self, kind_filter: str = "All", search: str = "") -> list[LibraryItem]:
        return self.query_all(kind_filter=kind_filter, status_filter="Missing", search=search)

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> LibraryItem:
        return LibraryItem(
            id=int(row["id"]),
            kind=str(row["kind"]),
            artist=str(row["artist"]),
            album=str(row["album"]),
            relative_path=str(row["relative_path"]),
            artwork_name=str(row["artwork_name"]),
            artwork_exists=bool(row["artwork_exists"]),
            artwork_mtime_ns=int(row["artwork_mtime_ns"] or 0),
            artwork_size=int(row["artwork_size"] or 0),
            problem=str(row["problem"] or ""),
        )

class FastLibraryScanner:
    """Directory-only scanner designed for slower removable devices."""

    def __init__(self, db: LibraryDB, root: Path, output_name: str,
                 progress: Callable[[int, int, str], None],
                 cancel: Optional[threading.Event] = None) -> None:
        self.db = db
        self.root = root
        self.output_name = output_name
        self.progress = progress
        self.cancel = cancel or threading.Event()

    def scan(self) -> dict[str, int]:
        scan_id = time.time_ns()
        artists = albums = missing = errors = 0
        items: list[dict[str, Any]] = []
        try:
            artist_dirs = immediate_subdirs(self.root)
        except OSError:
            return {"artists": 0, "albums": 0, "missing": 0, "errors": 1,
                    "cancelled": int(self.cancel.is_set()), "incomplete": 1}
        total_dirs = len(artist_dirs)

        for index, artist_dir in enumerate(artist_dirs, start=1):
            if self.cancel.is_set():
                # Leave the previous index intact; unseen rows are only pruned on a full scan.
                return {"artists": artists, "albums": albums, "missing": missing,
                        "errors": errors, "cancelled": 1}
            self.progress(index, total_dirs, artist_dir.name)
            try:
                album_dirs = immediate_subdirs(artist_dir)
                usable_albums = [d for d in album_dirs if contains_audio_immediate(d)]
                has_direct_artist_audio = contains_audio_immediate(artist_dir)
                if not usable_albums and not has_direct_artist_audio:
                    continue

                rel_artist = artist_dir.relative_to(self.root).as_posix()
                exists, art_name, mtime_ns, art_size = detect_artwork(artist_dir, self.output_name)
                items.append(dict(kind="artist", artist=artist_dir.name, album="",
                                    relative_path=rel_artist, artwork_name=art_name,
                                    artwork_exists=exists, artwork_mtime_ns=mtime_ns,
                                    artwork_size=art_size))
                artists += 1
                missing += 0 if exists else 1

                for album_dir in usable_albums:
                    rel_album = album_dir.relative_to(self.root).as_posix()
                    exists, art_name, mtime_ns, art_size = detect_artwork(album_dir, self.output_name)
                    items.append(dict(kind="album", artist=artist_dir.name, album=album_dir.name,
                                        relative_path=rel_album, artwork_name=art_name,
                                        artwork_exists=exists, artwork_mtime_ns=mtime_ns,
                                        artwork_size=art_size))
                    albums += 1
                    missing += 0 if exists else 1
            except Exception:
                errors += 1

        if self.cancel.is_set() or errors:
            return {"artists": artists, "albums": albums, "missing": missing, "errors": errors,
                    "cancelled": int(self.cancel.is_set()), "incomplete": 1}
        self.db.commit_scan(items, scan_id, self.root, self.output_name)
        return {"artists": artists, "albums": albums, "missing": missing, "errors": errors, "cancelled": 0}
