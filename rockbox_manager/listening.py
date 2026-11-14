# SPDX-License-Identifier: GPL-2.0-only
"""Read-only portable-player logs and a separate, private local listening ledger."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_LOG_BYTES = 64 * 1024**2
MAX_EVENTS = 250_000
PAGE_SIZE = 200


@dataclass(frozen=True)
class Listen:
    artist: str
    album: str
    title: str
    number: int
    duration: int
    rating: str
    timestamp: int
    mbid: str = ""

    @property
    def id(self):
        # Exact text, UTC start and rating identify an event, independently of
        # import location, service, duration corrections or optional MBIDs.
        value = (self.artist, self.album, self.title, self.timestamp, self.rating)
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def snapshot(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LOG_BYTES:
        raise ValueError("Choose a regular scrobbler log no larger than 64 MiB.")
    before = path.stat()
    with path.open("rb") as fp:
        data = fp.read(MAX_LOG_BYTES + 1)
    after = path.stat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(data) > MAX_LOG_BYTES or signature(before) != signature(after):
        raise ValueError("The log changed while reading; preview it again.")
    return data, (signature(after), hashlib.sha256(data).hexdigest())


@dataclass(frozen=True)
class LogPreview:
    path: Path
    fingerprint: tuple
    events: tuple[Listen, ...]
    invalid: int
    duplicates: int
    fixed_offset: bool


def preview_log(path, offset_minutes=None, cancel=None):
    """Do not guess #TZ/UNKNOWN from the desktop's current DST/timezone."""
    cancel = cancel or threading.Event()
    if offset_minutes is not None and (type(offset_minutes) is not int or not -840 <= offset_minutes <= 840):
        raise ValueError("UTC offset must be an integer between -840 and +840 minutes.")
    path = Path(path).absolute()
    if path.name.casefold() == ".scrobbler-timeless.log":
        raise ValueError("Timeless logs do not contain real UTC listening dates.")
    data, fingerprint = snapshot(path)
    events, seen, invalid, duplicates = [], set(), 0, 0
    offset, fixed = offset_minutes, False
    for raw in data.splitlines():
        if cancel.is_set():
            raise ValueError("Log preview cancelled; nothing was imported.")
        if len(raw) > 16384:
            invalid += 1
            continue
        try:
            line = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            invalid += 1
            continue
        if line.startswith("#AUDIOSCROBBLER/") and line.split("/", 1)[1].strip() not in ("1.0", "1.1"):
            raise ValueError("Unsupported portable-player log version.")
        if line.startswith("#CLIENT/") and "timeless" in line.casefold():
            raise ValueError("Timeless logs do not contain real UTC listening dates.")
        if line.startswith("#TZ/"):
            value = line[4:].strip().upper()
            if value == "UTC":
                offset = 0
            elif value == "UNKNOWN":
                offset = offset_minutes
            else:
                match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", value)
                if not match or int(match[3]) >= 60:
                    raise ValueError("Unsupported timezone header; the log was not imported.")
                offset = (int(match[2]) * 60 + int(match[3])) * (-1 if match[1] == "-" else 1)
                if abs(offset) > 840:
                    raise ValueError("Timezone header exceeds UTC±14 hours.")
            continue
        if not line or line.startswith("#"):
            continue
        if offset is None:
            raise ValueError("Unknown log timezone. Enter the historical UTC offset in minutes, then preview again.")
        fixed |= offset_minutes is not None and offset == offset_minutes
        parts = line.split("\t")
        try:
            if len(parts) not in (7, 8) or any("\0" in s or len(s) > 4096 for s in parts):
                raise ValueError()
            artist, album, title = (s.strip() for s in parts[:3])
            duration, timestamp = int(parts[4]), int(parts[6]) - offset * 60
            rating = parts[5].strip().upper()
            if not title or rating not in ("L", "S") or not 0 <= duration <= 7 * 86400 or not 0 < timestamp <= 4102444800:
                raise ValueError()
            number = int(parts[3]) if parts[3].strip() else 0
            item = Listen(artist, album, title, number, duration, rating, timestamp, parts[7].strip() if len(parts) == 8 else "")
        except (ValueError, IndexError):
            invalid += 1
            continue
        if item.id in seen:
            duplicates += 1
            continue
        seen.add(item.id)
        events.append(item)
        if len(events) > MAX_EVENTS:
            raise ValueError("A log may contain at most 250,000 unique events.")
    return LogPreview(path, fingerprint, tuple(sorted(events, key=lambda t: t.timestamp)), invalid, duplicates, fixed)


class ListeningStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("The listening database must not be a symlink.")
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        if os.name == "posix":
            self.path.chmod(0o600)
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA synchronous=FULL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS listens (
                    id TEXT PRIMARY KEY, artist TEXT, album TEXT, title TEXT,
                    number INTEGER, duration INTEGER, rating TEXT, timestamp INTEGER, mbid TEXT);
                CREATE INDEX IF NOT EXISTS listen_time ON listens(timestamp);
                CREATE TABLE IF NOT EXISTS submissions (
                    event TEXT, service TEXT, account TEXT, status TEXT, detail TEXT, attempt TEXT,
                    PRIMARY KEY(event,service,account));
            """)
            with con:
                yield con
        finally:
            con.close()

    def import_preview(self, preview, cancel=None):
        cancel = cancel or threading.Event()
        if snapshot(preview.path)[1] != preview.fingerprint:
            raise ValueError("The log changed after preview; preview again.")
        with self.connection() as con:
            count = con.execute("SELECT count(*) FROM listens").fetchone()[0]
            if count + len(preview.events) > MAX_EVENTS:
                existing = {r[0] for r in con.execute("SELECT id FROM listens")}
                if count + sum(t.id not in existing for t in preview.events) > MAX_EVENTS:
                    raise ValueError("The local history limit is 250,000 unique events. Export before starting another history file.")
            before = con.total_changes
            for t in preview.events:
                if cancel.is_set():
                    raise ValueError("Import cancelled; this import was rolled back.")
                con.execute("INSERT OR IGNORE INTO listens VALUES (?,?,?,?,?,?,?,?,?)", (t.id, *asdict(t).values()))
            added = con.total_changes - before
        return added

    def page(self, search="", page=0):
        query = "%" + str(search).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.connection() as con:
            where = "WHERE artist LIKE ? ESCAPE '\\' OR album LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\'"
            count = con.execute("SELECT count(*) FROM listens " + where, (query,) * 3).fetchone()[0]
            rows = con.execute("SELECT * FROM listens " + where + " ORDER BY timestamp DESC,id LIMIT ? OFFSET ?",
                               (*((query,) * 3), PAGE_SIZE, max(0, page) * PAGE_SIZE)).fetchall()
        return [Listen(**{k: r[k] for k in Listen.__dataclass_fields__}) for r in rows], count

    def statistics(self):
        with self.connection() as con:
            counts = dict(con.execute("SELECT rating,count(*) FROM listens GROUP BY rating").fetchall())
            seconds = con.execute("SELECT coalesce(sum(duration),0) FROM listens WHERE rating='L'").fetchone()[0]
            top = {}
            for name, group in (("artists", "artist"), ("albums", "artist,album"), ("tracks", "artist,title")):
                top[name] = [tuple(r) for r in con.execute(f"SELECT {group},count(*) AS n FROM listens WHERE rating='L' "
                                                         f"GROUP BY {group} ORDER BY n DESC,{group} LIMIT 10")]
            sessions, end = 0, None
            for ts, duration in con.execute("SELECT timestamp,duration FROM listens WHERE rating='L' ORDER BY timestamp"):
                if end is None or ts - end > 1200:
                    sessions += 1
                end = max(end or 0, ts + duration)
        return {"listened": counts.get("L", 0), "skipped": counts.get("S", 0), "seconds": seconds,
                "sessions": sessions, **top}

    def export(self, path):
        path = Path(path)
        if path.suffix.lower() not in (".json", ".csv"):
            raise ValueError("Choose a new .json or .csv file.")
        with self.connection() as con, path.open("x", encoding="utf-8", newline="") as fp:
            records = con.execute("SELECT artist,album,title,number,duration,rating,timestamp,mbid FROM listens ORDER BY timestamp,id")
            if path.suffix.lower() == ".csv":
                writer = csv.writer(fp)
                writer.writerow([*Listen.__dataclass_fields__, "utc_time"])
                for row in records:
                    # Spreadsheet programs can execute formula-like text on open.
                    values = [("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v) for v in row]
                    writer.writerow([*values, datetime.fromtimestamp(row[6], timezone.utc).isoformat()])
            else:
                fp.write("[\n")
                first = True
                for row in records:
                    fp.write(("" if first else ",\n") + json.dumps(dict(row), ensure_ascii=False))
                    first = False
                fp.write("\n]\n")
            fp.flush()
            os.fsync(fp.fileno())
