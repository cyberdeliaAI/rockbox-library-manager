"""Conservative, offline updates of an existing Rockbox tagcache (TCH version 16).

The binary layout is documented by Rockbox's apps/tagcache.[ch]. We retain master
row IDs and all runtime/numeric fields except an explicitly updated year. Only
changed string tables are rebuilt. New/deleted tracks remain Rockbox's job.
See docs/rockbox-database.md for the format contract and recovery procedure.
"""
from __future__ import annotations

import hashlib
import errno
from contextlib import contextmanager
import json
import os
import re
import shutil
import struct
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

MAGIC = 0x54434810
MASTER = "database_idx.tcd"
STATE = "database_state.tcd"
JOURNAL = "rockbox-library-manager-transaction.json"
STRINGS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12)
UNIQUE = frozenset(STRINGS) - {3, 4}
FIELDS = {0: "artist", 1: "album", 2: "genre", 7: "albumartist", 9: "date"}
EXPECTED = {MASTER} | {f"database_{tag}.tcd" for tag in STRINGS}
DB_NAME = re.compile(r"database_[A-Za-z0-9_]+\.tcd|database_changelog\.txt|database_commit\.ignore")
MAX_TOTAL_BYTES = 512 * 1024 * 1024
Progress = Callable[[str], None]


class DatabaseError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover_device(music_root: Path) -> Path:
    """Find the nearest real .rockbox directory above the selected music folder."""
    root = Path(music_root).resolve(strict=True)
    for parent in (root, *root.parents):
        rb = parent / ".rockbox"
        if rb.is_dir() and not rb.is_symlink():
            return parent
    raise DatabaseError("No .rockbox folder was found above the music folder. Connect the iPod and select its music folder.")


def _rb(device: Path) -> Path:
    device = Path(device).resolve(strict=True)
    rb = device / ".rockbox"
    if rb.is_symlink() or not rb.is_dir():
        raise DatabaseError("The device must contain a real .rockbox directory.")
    config = rb / "config.cfg"
    if config.exists():
        for line in _read_regular(config).decode("utf-8", errors="replace").splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip().lower() == "database path" and value.strip().rstrip("/") != "/.rockbox":
                raise DatabaseError("A custom Rockbox database location is configured. This tool only supports /.rockbox.")
    return rb


def _read_regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise DatabaseError(f"Refusing a missing file, directory or symlink: {path.name}")
    before = path.stat()
    if before.st_size > MAX_TOTAL_BYTES:
        raise DatabaseError(f"Database file is too large: {path.name}")
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise DatabaseError(f"File changed while reading: {path.name}")
    return data


def snapshot(device: Path) -> dict[str, bytes]:
    rb = _rb(device)
    names = sorted(p.name for p in rb.iterdir() if DB_NAME.fullmatch(p.name))
    if MASTER not in names:
        raise DatabaseError("No Rockbox database exists here. Initialize it on the iPod first.")
    result = {}
    total = 0
    for name in names:
        result[name] = _read_regular(rb / name)
        total += len(result[name])
        if total > MAX_TOTAL_BYTES:
            raise DatabaseError("The database exceeds the supported backup size (512 MiB).")
    if names != sorted(p.name for p in rb.iterdir() if DB_NAME.fullmatch(p.name)):
        raise DatabaseError("The database file list changed while reading. Try again after Rockbox finishes updating.")
    return result


def _fingerprints(files: dict[str, bytes]) -> dict[str, str]:
    return {name: digest(data) for name, data in files.items()}


def _check_current(device: Path, expected: dict[str, bytes]) -> None:
    if _fingerprints(snapshot(device)) != _fingerprints(expected):
        raise DatabaseError("The iPod database changed after it was read. Nothing was written; create a new preview.")


def _write_sync(path: Path, data: bytes) -> None:
    with path.open("xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _sync_directory(path: Path) -> None:
    # Windows does not expose fsync for directories. Each file is still flushed.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            try:
                os.fsync(fd)
            except OSError as exc:
                if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                    raise
        finally:
            os.close(fd)


@contextmanager
def _device_lock(device: Path):
    """An OS lock survives neither a crash nor process exit; the journal does."""
    path = _rb(device) / ".rockbox-library-manager.lock"
    if path.is_symlink():
        raise DatabaseError("The database lock must not be a symlink.")
    with path.open("a+b") as lock:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    lock.write(b"0"); lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise DatabaseError("Another application instance is using this Rockbox database.") from exc
        try:
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _firmware(device: Path) -> str:
    p = _rb(device) / "rockbox-info.txt"
    return _read_regular(p).decode("utf-8", errors="replace") if p.exists() else ""


def backup_database(device: Path, backup_root: Path, *, expected: dict[str, bytes] | None = None) -> Path:
    """A backup is complete only after its last, verified manifest is written."""
    device = Path(device).resolve(strict=True)
    backup_root = Path(backup_root).expanduser().resolve()
    if backup_root.is_relative_to(device):
        raise DatabaseError("Choose a backup folder on this computer, outside the iPod.")
    files = snapshot(device) if expected is None else expected
    _check_current(device, files)
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{uuid.uuid4().hex[:8]}"
    partial = backup_root / (name + ".partial")
    partial.mkdir(mode=0o700)
    try:
        for filename, data in files.items():
            _write_sync(partial / filename, data)
            if digest(_read_regular(partial / filename)) != digest(data):
                raise DatabaseError(f"Backup verification failed: {filename}")
        _check_current(device, files)
        manifest = {"schema": 1, "created_utc": stamp, "device_root": str(device),
                    "firmware": _firmware(device),
                    "files": {n: {"sha256": digest(b), "size": len(b)} for n, b in files.items()}}
        _write_sync(partial / "manifest.json", json.dumps(manifest, indent=2).encode())
        _sync_directory(partial)
        complete = backup_root / name
        partial.rename(complete)
        _sync_directory(backup_root)
        return complete
    except BaseException:
        # Incomplete backups are never listed or used for recovery.
        shutil.rmtree(partial, ignore_errors=True)
        raise


def read_backup(folder: Path) -> tuple[dict, dict[str, bytes]]:
    folder = Path(folder).resolve(strict=True)
    manifest = json.loads(_read_regular(folder / "manifest.json"))
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        raise DatabaseError("Unsupported backup manifest.")
    files = {}
    total = 0
    for name, info in manifest["files"].items():
        if not DB_NAME.fullmatch(name):
            raise DatabaseError("Unsafe filename in backup manifest.")
        data = _read_regular(folder / name)
        total += len(data)
        if total > MAX_TOTAL_BYTES or len(data) != info.get("size") or digest(data) != info.get("sha256"):
            raise DatabaseError(f"Damaged or incomplete backup: {name}")
        files[name] = data
    if MASTER not in files:
        raise DatabaseError("The backup has no master database.")
    return manifest, files


@dataclass
class TagDatabase:
    files: dict[str, bytes]
    endian: str
    header: list[int]
    rows: list[list[int]]
    texts: dict[int, dict[int, tuple[str, int]]]

    @classmethod
    def parse(cls, files: dict[str, bytes]) -> "TagDatabase":
        if not EXPECTED.issubset(files):
            raise DatabaseError("The Rockbox database is incomplete; update it on the iPod first.")
        if "database_tmp.tcd" in files or "database_commit.ignore" in files:
            raise DatabaseError("Rockbox has a pending/paused database update. Let it finish on the iPod first.")
        allowed = EXPECTED | {STATE, "database_changelog.txt"}
        if set(files) - allowed:
            raise DatabaseError("Unrecognized Rockbox database files; direct updates are disabled.")
        master = files[MASTER]
        if len(master) < 24:
            raise DatabaseError("Truncated master database.")
        endian = next((e for e in ("<", ">") if struct.unpack_from(e + "I", master)[0] == MAGIC), None)
        if endian is None:
            raise DatabaseError("Unsupported database format. Only Rockbox TCH version 16 is supported; nothing was changed.")
        header = list(struct.unpack_from(endian + "6i", master))
        count = header[2]
        if count < 0 or len(master) != 24 + count * 96 or header[5] != 0:
            raise DatabaseError("The database is truncated or marked as an unfinished write.")
        rows = [list(r) for r in struct.iter_unpack(endian + "24i", master[24:])]
        texts = {}
        for tag in STRINGS:
            data = files[f"database_{tag}.tcd"]
            if len(data) < 12:
                raise DatabaseError(f"Truncated string table {tag}.")
            magic, size, entries = struct.unpack_from(endian + "3i", data)
            if magic != MAGIC or size != len(data) - 12 or entries < 0:
                raise DatabaseError(f"Invalid string table header {tag}.")
            table = {}; pos = 12
            for _ in range(entries):
                if pos + 8 > len(data):
                    raise DatabaseError(f"Truncated string entry in table {tag}.")
                length, idx = struct.unpack_from(endian + "2i", data, pos)
                if length < 1 or length > 544 or pos + 8 + length > len(data):
                    raise DatabaseError(f"Invalid string entry length in table {tag}.")
                raw = data[pos + 8:pos + 8 + length]
                if b"\0" not in raw:
                    raise DatabaseError(f"Unterminated string in table {tag}.")
                try:
                    value = raw.split(b"\0", 1)[0].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise DatabaseError(f"Invalid UTF-8 in table {tag}.") from exc
                table[pos] = (value, idx)
                pos += 8 + length
            if pos != len(data):
                raise DatabaseError(f"Unexpected trailing bytes in table {tag}.")
            texts[tag] = table
        expected_size = len(master) + sum(len(files[f"database_{t}.tcd"]) - 12 for t in STRINGS if t != 4)
        if header[1] != expected_size:
            raise DatabaseError("The master database size does not match its string tables.")
        paths = set()
        for idx, row in enumerate(rows):
            if row[23] & ~0x1F or row[23] & 2:
                raise DatabaseError("Unsupported database flags (possibly an in-memory cache).")
            if row[23] & 1:
                continue  # Deleted rows hold CRCs, not offsets. Preserve them verbatim.
            for tag in STRINGS:
                item = texts[tag].get(row[tag])
                if item is None or not item[0]:
                    raise DatabaseError(f"Invalid string reference for track {idx}, table {tag}.")
                if tag not in UNIQUE and item[1] != idx:
                    raise DatabaseError(f"Incorrect reverse index for track {idx}, table {tag}.")
            path = texts[4][row[4]][0]
            if path in paths:
                raise DatabaseError("The database contains duplicate active file paths.")
            paths.add(path)
        return cls(files, endian, header, rows, texts)

    def text(self, row: list[int], tag: int) -> str:
        return self.texts[tag][row[tag]][0]

    def updated(self, changes: dict[int, dict[int, str | int]]) -> dict[str, bytes]:
        if not changes:
            return dict(self.files)
        rows = [r.copy() for r in self.rows]
        out = dict(self.files)
        changed_tags = {t for values in changes.values() for t in values if t in STRINGS}
        for idx, values in changes.items():
            if 9 in values:
                rows[idx][9] = int(values[9])
        for tag in changed_tags:
            entries: list[tuple[bytes, int, list[int]]] = []
            unique: dict[bytes, int] = {}
            for idx, row in enumerate(self.rows):
                if row[23] & 1:
                    continue
                text = str(changes.get(idx, {}).get(tag, self.text(row, tag)))
                raw = text.encode("utf-8")
                if not raw or b"\0" in raw or len(raw) > (259 if tag == 4 else 511):
                    raise DatabaseError("A tag or filename exceeds the supported Rockbox length.")
                key = raw.lower()  # Rockbox's bytewise strcasecmp, not Unicode casefold.
                if tag in UNIQUE and key in unique:
                    entries[unique[key]][2].append(idx)
                else:
                    unique[key] = len(entries)
                    entries.append((raw, -1 if tag in UNIQUE else idx, [idx]))
            if tag != 4:
                entries.sort(key=lambda e: (e[0] != b"<Untagged>", e[0].lower()))
            body = bytearray()
            for raw, owner, users in entries:
                offset = 12 + len(body)
                value = raw + b"\0"
                if tag != 4:
                    value += b"X" * (-len(value) % 8)
                body.extend(struct.pack(self.endian + "2i", len(value), owner))
                body.extend(value)
                for idx in users:
                    rows[idx][tag] = offset
            out[f"database_{tag}.tcd"] = struct.pack(self.endian + "3i", MAGIC, len(body), len(entries)) + body
        header = self.header.copy()
        header[1] = 24 + len(rows) * 96 + sum(len(out[f"database_{t}.tcd"]) - 12 for t in STRINGS if t != 4)
        out[MASTER] = struct.pack(self.endian + "6i", *header) + b"".join(struct.pack(self.endian + "24i", *r) for r in rows)
        out.pop(STATE, None)  # A serialized RAM cache would reintroduce old values.
        TagDatabase.parse(out)
        return out


@dataclass
class UpdatePlan:
    device: Path
    before: dict[str, bytes]
    after: dict[str, bytes]
    changes: list[str]
    changed_tracks: int
    checked_tracks: int
    track_stamps: dict[Path, tuple[int, int]] = field(default_factory=dict)


def _stamp(path: Path) -> tuple[int, int]:
    s = path.stat()
    return s.st_size, s.st_mtime_ns


def _split_device_path(name: str) -> tuple[str, tuple[str, ...]]:
    """Separate PodBox's internal-volume label from a device-relative path.

    Rockbox names ATA volume zero <HDD0> (firmware/export/mv.h). Accept only
    that known alias; another volume must never be mapped to this mounted iPod.
    Keep the prefix so a recorded folder move retains the database's spelling.
    """
    p = PurePosixPath(name)
    if len(p.parts) < 2 or not name.startswith("/") or name.startswith("//") or ".." in p.parts or "\\" in name or "\0" in name:
        raise DatabaseError(f"Unsupported device path: {name}")
    parts = p.parts[1:]
    prefix = ""
    if parts[0] == "<HDD0>":
        prefix, parts = "/<HDD0>", parts[1:]
    # Colons can introduce a Windows drive or alternate data stream. Reject
    # them on every host, as well as unknown or nested volume specifiers.
    if not parts or any(any(c in part for c in "<>:") for part in parts):
        raise DatabaseError(f"Unsupported device path: {name}")
    return prefix, parts


def _device_path(device: Path, name: str) -> Path:
    _, parts = _split_device_path(name)
    path = device.joinpath(*parts)
    if path.is_symlink() or not path.resolve().is_relative_to(device):
        raise DatabaseError(f"Device path escapes the iPod: {name}")
    return path


def _metadata(path: Path) -> dict[str, str]:
    from mutagen import File
    audio = File(str(path), easy=True)
    if audio is None:
        raise DatabaseError(f"Cannot read tags: {path.name}")
    values = {}
    for key in FIELDS.values():
        items = [str(v).strip() for v in audio.get(key, []) if str(v).strip()]
        if len(set(items)) > 1:
            raise DatabaseError(f"Multiple {key} values in {path.name}; direct update cannot choose between them.")
        values[key] = items[0] if items else ""
    return values


def preview_update(music_root: Path, *, moves: list[list[str]] | None = None,
                   progress: Progress = lambda _: None) -> UpdatePlan:
    music_root = Path(music_root).resolve(strict=True)
    device = discover_device(music_root)
    if (_rb(device) / JOURNAL).exists():
        raise DatabaseError("An interrupted update needs recovery. Restore its local backup before updating again.")
    before = snapshot(device)
    db = TagDatabase.parse(before)
    changes = {}; descriptions = []; stamps = {}; checked = 0
    for idx, row in enumerate(db.rows):
        if row[23] & 1:
            continue
        old_name = db.text(row, 4)
        path = _device_path(device, old_name)
        original_path = path
        if not path.resolve().is_relative_to(music_root):
            continue
        # Only use moves explicitly recorded by this application. Never guess a
        # missing track's identity from a filename, artist or album alone.
        for source, destination in moves or []:
            src, dst = Path(source), Path(destination)
            if path.is_relative_to(src):
                if path.exists():
                    raise DatabaseError(f"A previously moved path exists again: {old_name}. Update the database on the iPod to resolve this ambiguity.")
                path = dst / path.relative_to(src)
        if not path.resolve().is_relative_to(music_root) or not path.is_file():
            raise DatabaseError(f"Indexed track is missing: {old_name}. Use Rockbox's Update Now for unrecorded moves or deleted tracks.")
        if path.resolve() in stamps:
            raise DatabaseError(f"Multiple database records refer to the same local file: {old_name}. Update the database on the iPod first.")
        initial = _stamp(path)
        values = _metadata(path)
        if _stamp(path) != initial:
            raise DatabaseError(f"Tags changed while reading: {path.name}")
        stamps[path.resolve()] = initial
        delta: dict[int, str | int] = {}
        for tag, key in FIELDS.items():
            if tag == 9:
                date = values[key]
                if date and not re.match(r"^\d{4}(?:$|[-/T ])", date):
                    raise DatabaseError(f"Cannot interpret year/date in {path.name}: {date}")
                value = int(date[:4]) if date else 0
                previous = row[tag]
            else:
                value = values[key] or "<Untagged>"
                previous = db.text(row, tag)
            if value != previous:
                delta[tag] = value
                descriptions.append(f"{old_name}: {key}: {previous} → {value}")
        if 0 in delta or 7 in delta:
            canonical = values["artist"] or values["albumartist"] or "<Untagged>"
            if canonical != db.text(row, 12):
                delta[12] = canonical
        if path != original_path:
            prefix, _ = _split_device_path(old_name)
            new_name = prefix + "/" + path.relative_to(device).as_posix()
            delta[4] = new_name
            descriptions.append(f"{old_name} → {new_name}")
        if delta:
            changes[idx] = delta
        checked += 1
        if checked % 50 == 0:
            progress(f"Reading tags: {checked} tracks")
    _check_current(device, before)
    return UpdatePlan(device, before, db.updated(changes), descriptions, len(changes), checked, stamps)


def _install(device: Path, desired: dict[str, bytes], current: dict[str, bytes], backup: Path) -> None:
    """Stage all bytes before touching the DB; clean master is always last.

    A disconnect can interrupt a multi-file commit. The dirty master and durable
    journal block use until recovery; the verified local backup stays available.
    """
    rb = _rb(device)
    stage = rb / (".rlm-stage-" + uuid.uuid4().hex)
    journal = rb / JOURNAL
    stage.mkdir()
    wrote_db = False
    owns_journal = False
    try:
        final = dict(desired); final.pop(STATE, None)
        for name, data in final.items():
            _write_sync(stage / name, data)
            if _read_regular(stage / name) != data:
                raise DatabaseError(f"Staging verification failed: {name}")
        endian = TagDatabase.parse(final).endian
        dirty = bytearray(final[MASTER]); struct.pack_into(endian + "i", dirty, 20, 1)
        _write_sync(stage / "dirty-master", dirty)
        _check_current(device, current)
        _write_sync(journal, json.dumps({"backup": str(backup), "created_utc": datetime.now(timezone.utc).isoformat()}).encode())
        owns_journal = True
        _sync_directory(rb)
        # If any rename/write fails from here, roll back from the local backup.
        wrote_db = True
        os.replace(stage / "dirty-master", rb / MASTER)
        _sync_directory(rb)
        for name in sorted(final):
            if name != MASTER:
                os.replace(stage / name, rb / name)
        for name in set(current) - set(final):
            (rb / name).unlink()
        _sync_directory(rb)
        os.replace(stage / MASTER, rb / MASTER)
        _sync_directory(rb)
        if _fingerprints(snapshot(device)) != _fingerprints(final):
            raise DatabaseError("Written database failed verification.")
        journal.unlink(); owns_journal = False
        _sync_directory(rb)
    except Exception as exc:
        if wrote_db:
            try:
                # Separate files avoid reusing consumed staged paths. Restore
                # the original master last, including its original dirty state.
                for name in sorted(current, key=lambda n: n == MASTER):
                    temp = stage / ("rollback-" + name)
                    _write_sync(temp, current[name])
                    os.replace(temp, rb / name)
                for name in set(final) - set(current):
                    (rb / name).unlink(missing_ok=True)
                _sync_directory(rb)
                _check_current(device, current)
                if owns_journal:
                    journal.unlink(); owns_journal = False
            except Exception as rollback_error:
                raise DatabaseError(f"Update interrupted; automatic recovery failed: {rollback_error}. Keep the iPod connected and restore the local backup: {backup}") from exc
        raise DatabaseError(f"Database update failed. Original database retained/restored. Local backup: {backup}. {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if owns_journal and not wrote_db:
            journal.unlink(missing_ok=True)


def apply_update(plan: UpdatePlan, backup_root: Path) -> Path | None:
    if not plan.changed_tracks:
        return None
    with _device_lock(plan.device):
        if (_rb(plan.device) / JOURNAL).exists():
            raise DatabaseError("Another or interrupted database update exists. Restore a backup first.")
        for path, stamp in plan.track_stamps.items():
            if not path.is_file() or _stamp(path) != stamp:
                raise DatabaseError("Music files changed after the preview. Create a new preview.")
        backup = backup_database(plan.device, backup_root, expected=plan.before)
        for path, stamp in plan.track_stamps.items():
            if not path.is_file() or _stamp(path) != stamp:
                raise DatabaseError(f"Music files changed while backing up. Nothing was written. Backup: {backup}")
        _install(plan.device, plan.after, plan.before, backup)
        return backup


def restore_backup(device: Path, folder: Path, backup_root: Path) -> Path:
    device = Path(device).resolve(strict=True)
    manifest, files = read_backup(folder)
    TagDatabase.parse(files)
    if manifest.get("device_root") != str(device) or manifest.get("firmware") != _firmware(device):
        raise DatabaseError("This backup belongs to another device location or Rockbox build. Reconnect the original device at its original location.")
    with _device_lock(device):
        current = snapshot(device)
        safety = backup_database(device, backup_root, expected=current)
        # Recovery may replace our own interrupted transaction marker. Keep its
        # details in the safety backup so no recovery history is lost.
        journal = _rb(device) / JOURNAL
        if journal.exists():
            old = _read_regular(journal)
            _write_sync(safety / "interrupted-transaction.json", old)
            journal.unlink()
        _install(device, files, current, safety)
        return safety
