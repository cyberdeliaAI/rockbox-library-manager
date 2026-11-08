# SPDX-License-Identifier: GPL-2.0-only
# Text matching and sanitization adapted from Tagcast 1.6.5; upstream MIT notice follows.
# MIT License
# 
# Copyright (c) 2026 Cyberdelia
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Explicit, read-only public background lookup with a bounded local text cache."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path

from .sources import SourceError, TheAudioDB, shared_http

MAX_TEXT = 20000
MAX_ENTRIES = 5000
MAX_BYTES = 20 * 1024 * 1024
READY_TTL = 30 * 86400
MISSING_TTL = 86400


def identity(value):
    """Background text needs exact identities, retaining accents, punctuation and editions."""
    return " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "br", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = PlainText()
    parser.feed(value[:100000])
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n\n".join(line for line in lines if line)[:MAX_TEXT]


def valid_result(data, kind, artist, album):
    return (isinstance(data, dict) and data.get("status") in ("ready", "missing", "ambiguous", "unavailable")
            and data.get("source") == "TheAudioDB" and data.get("language") == "en"
            and data.get("kind") == kind and identity(data.get("artist")) == identity(artist)
            and identity(data.get("album")) == identity(album)
            and all(isinstance(data.get(key), str) for key in ("artist", "album", "text", "url"))
            and len(data["text"]) <= MAX_TEXT
            and (not data["url"] or re.fullmatch(r"https://www\.theaudiodb\.com/" + kind + r"/[0-9]{1,20}", data["url"])))


class OnlineInformation:
    def __init__(self, path: Path, http=None):
        self.path = path
        self.http = http
        self.lock = threading.Lock()
        self.failed_at = 0

    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS background (key TEXT PRIMARY KEY, data TEXT NOT NULL, "
                       "stored_at REAL NOT NULL, expires_at REAL NOT NULL)")
        except Exception:
            db.close()
            raise
        return db

    def _cached(self, key, kind, artist, album):
        try:
            with closing(self._db()) as db:
                row = db.execute("SELECT data, expires_at FROM background WHERE key=?", (key,)).fetchone()
            if row and type(row[1]) in (int, float) and math.isfinite(row[1]) and 0 < row[1] - time.time() <= READY_TTL:
                data = json.loads(row[0])
                if valid_result(data, kind, artist, album):
                    return {**data, "cached": True}
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass
        return None

    def _save(self, key, data):
        now = time.time()
        ttl = READY_TTL if data["status"] == "ready" else MISSING_TTL
        try:
            with closing(self._db()) as db, db:
                db.execute("INSERT OR REPLACE INTO background VALUES (?,?,?,?)",
                           (key, json.dumps(data, ensure_ascii=False), now, now + ttl))
                rows = db.execute("SELECT key, LENGTH(CAST(data AS BLOB)) FROM background ORDER BY stored_at DESC").fetchall()
                total = 0
                for index, (old_key, size) in enumerate(rows):
                    total += size
                    if index >= MAX_ENTRIES or total > MAX_BYTES:
                        db.execute("DELETE FROM background WHERE key=?", (old_key,))
        except (OSError, sqlite3.Error):
            # A cache failure must not hide a successfully retrieved public description.
            pass

    def get(self, kind, artist, album=""):
        if kind not in ("artist", "album") or not identity(artist) or kind == "album" and not identity(album):
            raise ValueError("Choose an artist or album")
        if kind == "artist":
            album = ""
        key = json.dumps([kind, identity(artist), identity(album)], ensure_ascii=False)
        with self.lock:
            cached = self._cached(key, kind, artist, album)
            if cached is not None:
                return cached
            if self.failed_at and time.monotonic() - self.failed_at < 60:
                raise SourceError("TheAudioDB could not be reached. Wait a minute before trying again.")
            try:
                result = self._fetch(kind, artist, album)
            except SourceError:
                self.failed_at = time.monotonic()
                raise
            self.failed_at = 0
            self._save(key, result)
            return {**result, "cached": False}

    def _fetch(self, kind, artist, album):
        endpoint = "search.php" if kind == "artist" else "searchalbum.php"
        params = {"s": artist} if kind == "artist" else {"s": artist, "a": album}
        data = (self.http or shared_http()).json(f"{TheAudioDB.API}/{endpoint}", params, ttl=0)
        field = "artists" if kind == "artist" else "album"
        if data is None:
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get(field) or [], list):
            raise SourceError("TheAudioDB returned an invalid response")
        matches = [row for row in data.get(field) or [] if isinstance(row, dict)
                   and identity(row.get("strArtist")) == identity(artist)
                   and (kind == "artist" or identity(row.get("strAlbum")) == identity(album))]
        result = {"kind": kind, "artist": artist, "album": album, "source": "TheAudioDB", "language": "en",
                  "status": "missing" if not matches else "ambiguous", "text": "", "url": ""}
        if len(matches) != 1:
            return result
        row = matches[0]
        identifier = str(row.get("idArtist" if kind == "artist" else "idAlbum") or "")
        if not re.fullmatch(r"[0-9]{1,20}", identifier):
            raise SourceError("TheAudioDB returned an invalid identifier")
        value = (row.get("strBiographyEN") or row.get("strBiography")) if kind == "artist" else (
            row.get("strDescriptionEN") or row.get("strDescription"))
        text = plain_text(value) if isinstance(value, str) else ""
        return {**result, "status": "ready" if text else "unavailable", "text": text,
                "url": f"https://www.theaudiodb.com/{kind}/{identifier}"}
