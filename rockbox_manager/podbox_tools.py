# SPDX-License-Identifier: GPL-2.0-only
"""Stage the real PodBox engines locally, review their output, then install it.

The C adapter reads the player but permits writes only in the local library
overlay. Music is never copied, retagged, linked or opened for writing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import rockbox_database as database

REVISION = "f0850c4647e7c1cfde11ffc89ab40b55f8aa5635"
JOURNAL = "rockbox-library-manager-podbox-transaction.json"
OPERATIONS = {"update": "database_pb", "rebuild": "database_pb", "artwork": "artcache_pb", "soundscan": "soundscan"}
MAX_BYTES = 16 * 1024**3
MAX_FILES = 500_000
_TRANSACTION = re.compile(r"\.rlm-library-(?:stage|previous)-[0-9a-f]{32}")


def libfile_crc(data):
    """Rockbox's non-reflected CRC-32/MPEG-2 (not zlib's reflected CRC)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc


class PodBoxError(RuntimeError):
    pass


@dataclass
class PodBoxPlan:
    device: Path
    operation: str
    work: Path
    before: dict
    after: dict
    inputs: dict
    configuration: dict
    offset: int
    output: str
    changes: list[str]
    offset_explicit: bool = False

    @property
    def library(self):
        return self.work / "overlay/.rockbox/library"

    def discard(self):
        if self.work.exists():
            shutil.rmtree(self.work)


def _hash(path):
    result = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or str(path) != name or ".." in path.parts
            or any(":" in part or "\\" in part or part.endswith((".", " ")) for part in path.parts)):
        raise PodBoxError("Unsafe path in PodBox library/backup.")
    return path


def manifest(folder: Path) -> dict:
    """Hash regular files only; refuse links, changing files and ambiguous FAT names."""
    if folder.is_symlink() or not folder.is_dir():
        raise PodBoxError(f"Missing or unsafe library directory: {folder}")
    result, keys, total = {}, set(), 0
    def fail(exc):
        raise PodBoxError(f"Cannot completely read the library: {exc}")
    for root, directories, files in os.walk(folder, onerror=fail):
        for name in directories + files:
            path = Path(root) / name
            if path.is_symlink():
                raise PodBoxError(f"Symlinks are unsupported: {path}")
            relative = path.relative_to(folder).as_posix()
            _safe_name(relative)
            key = relative.casefold()
            if key in keys:
                raise PodBoxError(f"Ambiguous case-insensitive filename: {relative}")
            keys.add(key)
            if name not in files:
                continue
            if not path.is_file():
                raise PodBoxError(f"Not a regular file: {relative}")
            before = path.stat()
            total += before.st_size
            if total > MAX_BYTES or len(result) >= MAX_FILES:
                raise PodBoxError("The PodBox library exceeds the supported backup limit (16 GiB / 500,000 files).")
            value = {"size": before.st_size, "sha256": _hash(path)}
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise PodBoxError(f"File changed while reading: {relative}")
            result[relative] = value
    return result


def validate_format(library):
    data = (library / "format.dat").read_bytes()
    if len(data) != 28:
        raise PodBoxError("Unrecognized PodBox layout record. Boot the supported firmware on the player first.")
    magic, version, size, count, _, _, _, crc = struct.unpack("<IHHIiiiI", data)
    if (magic, version, size, count, crc) != (0x544F594C, 5, 1, 0, 0xFFFFFFFF):
        raise PodBoxError("Only initialized PodBox library layout 5 is supported. Nothing was written.")


def _database(library):
    folder = library / "database"
    if not (folder / database.MASTER).exists():
        return None
    return database.TagDatabase.parse({p.name: p.read_bytes() for p in folder.iterdir()
                                       if database.DB_NAME.fullmatch(p.name)})


def _runtime(db):
    if db is None:
        return {}
    return {db.text(row, 4): tuple(row[i] for i in (15, 16, 17, 18, 19, 21, 22))
            for row in db.rows if not row[23] & 1}


def _configuration(device):
    rb = device / ".rockbox"
    result = {}
    for name in ("config.cfg", "rockbox-info.txt"):
        path = rb / name
        if path.is_symlink():
            raise PodBoxError("Player configuration must not be a symlink.")
        result[name] = _hash(path) if path.is_file() else None
    return result


def _profile(device):
    info = device / ".rockbox/rockbox-info.txt"
    if info.is_symlink() or not info.is_file() or info.stat().st_size > 64 * 1024:
        raise PodBoxError("The player needs a regular rockbox-info.txt identifying its target.")
    target = re.search(r"^Target:\s*(\S+)\s*$", info.read_text(errors="replace"), re.M)
    if not target or target[1] not in ("ipodvideo", "ipod6g"):
        raise PodBoxError("These PodBox engines support iPod Video and Classic only; other targets are unchanged.")


def _inventory(device, cancel):
    """Capture source metadata, including hidden music; library data is hashed separately."""
    result = {}
    def fail(exc):
        raise PodBoxError(f"Incomplete player walk: {exc}")
    for root, directories, names in os.walk(device, onerror=fail):
        if cancel.is_set():
            raise PodBoxError("Preview cancelled; the player was not changed.")
        directories[:] = sorted(d for d in directories
                                if d.casefold() not in {"system volume information", "$recycle.bin", ".trashes", ".fseventsd", ".spotlight-v100"}
                                and not d.startswith(".rlm-library-")
                                and (Path(root) / d).relative_to(device).as_posix() != ".rockbox/library")
        for name in directories + names:
            path = Path(root) / name
            relative = path.relative_to(device).as_posix()
            if (relative in (".rockbox/.rockbox-library-manager.lock", ".rockbox/._.rockbox-library-manager.lock")
                    or (path.parent == device / ".rockbox" and (name.startswith("._.rlm-library-")
                        or JOURNAL in name))):
                continue
            if path.is_symlink():
                raise PodBoxError(f"Player symlinks are unsupported: {path}")
            if not path.is_dir() and not path.is_file():
                raise PodBoxError(f"Unsupported player entry: {path}")
            stat = path.stat()
            result[relative] = (0, 0, True) if path.is_dir() else (stat.st_size, stat.st_mtime_ns, False)
            if len(result) > MAX_FILES:
                raise PodBoxError("The player exceeds the supported input limit (500,000 entries).")
    return result


def engine_directory():
    custom = os.environ.get("RLM_PODBOX_TOOLS")
    base = Path(custom) if custom else Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1] / "build")) / "podbox"
    try:
        info = json.loads((base / "engine.json").read_text())
    except (OSError, ValueError) as exc:
        raise PodBoxError("PodBox native engines are not installed. Use a standalone download or follow docs/podbox-tools.md.") from exc
    if info.get("revision") != REVISION or info.get("adapter") != 1 or info.get("library_format") != 5:
        raise PodBoxError("This native engine is incompatible with the application.")
    return base.resolve()


def fat_offset(device=None):
    if device is not None and sys.platform.startswith("linux"):
        candidates = []
        for line in Path("/proc/self/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), parts[1]))
            if device == mount or mount in device.parents:
                candidates.append((len(mount.parts), parts[2], parts[3].split(",")))
        if candidates:
            _, kind, options = max(candidates)
            if kind in ("vfat", "msdos"):
                if "tz=UTC" in options:
                    return 0
                for option in options:
                    if re.fullmatch(r"time_offset=-?\d+", option):
                        return int(option.split("=")[1]) * 60
        raise PodBoxError("On Linux, database/artwork preparation requires a FAT mount with an explicit "
                          "tz=UTC or time_offset option. The implicit kernel timezone is not guessed. "
                          "Soundscan remains available. See docs/podbox-tools.md.")
    now = time.localtime()
    return -int(time.altzone if now.tm_isdst > 0 and time.daylight else time.timezone)


def _execute(engine, device, overlay, work, offset, cancel, progress):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CSC_", "APPLE_", "WIN_CSC_", "RLM_CODESIGN_"))}
    env.update(RLM_PODBOX_STAGE=overlay.as_posix(), RLM_PODBOX_FAT_OFFSET_SECONDS=str(offset),
               TEMP=str(work), TMPDIR=str(work), TMP=str(work))
    log = work / "engine.log"
    with log.open("wb") as fp:
        process = subprocess.Popen(engine, env=env, cwd=work, stdin=subprocess.DEVNULL,
                                   stdout=fp, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            while process.poll() is None:
                if cancel.wait(.2):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise PodBoxError("Preview cancelled; the player was not changed.")
                if log.stat().st_size > 32 * 1024**2:
                    raise PodBoxError("Native engine output exceeded 32 MiB.")
                progress("Preparing a local preview… The player is read-only.")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    text = log.read_text(encoding="utf-8", errors="replace").replace("\r", "\n")
    if process.returncode:
        raise PodBoxError(f"The native engine stopped ({process.returncode}). The player was not changed.\n{text[-8000:]}")
    return text[-32_000:]


def _validate_outputs(library, operation, before, after):
    validate_format(library)
    changes = [name for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]
    def allowed(name):
        if operation in ("update", "rebuild"):
            return name.startswith("database/") or name in ("user/plays.dat", "cache/unreadable.dat", "cache/unreadable.txt")
        if operation == "artwork":
            return name.startswith("cache/art/")
        return name in ("sound.dat", "sound.part")
    for name in changes:
        if not allowed(name):
            raise PodBoxError(f"Native engine changed an unexpected file: {name}. The player was not changed.")
        if name.endswith(".new") or name.endswith("database_tmp.tcd") or "swap" in name:
            raise PodBoxError("Native engine left unfinished work. The player was not changed.")
        if name not in after:
            continue
        path = library / name
        if path.suffix == ".aat":
            data = path.read_bytes()
            if len(data) < 12:
                raise PodBoxError("Truncated artwork cache output.")
            magic, version, width, height, layout, pad = struct.unpack_from("<6H", data)
            if magic != 0x5441 or version != 8 or not 1 <= width <= 600 or not 1 <= height <= 600 or layout > 1 or pad or len(data) != 12 + width * height * 2:
                raise PodBoxError("Unsupported artwork cache output.")
        if path.suffix == ".dat" and name not in ("sound.dat",):
            data = path.read_bytes()
            if len(data) < 28 or libfile_crc(data[28:]) != struct.unpack_from("<I", data, 24)[0]:
                raise PodBoxError(f"Invalid native file checksum: {name}")
    if operation in ("update", "rebuild", "artwork"):
        _database(library)
    if operation == "soundscan":
        path = library / "sound.dat"
        if not path.is_file():
            if not changes:
                return changes
            raise PodBoxError("The engine did not produce a finished sound index.")
        data = path.read_bytes()
        if len(data) < 16:
            raise PodBoxError("Truncated sound index.")
        magic, version, stride, count, reserved = struct.unpack_from("<IHHII", data)
        if (magic, version, stride, reserved) != (0x534E4431, 3, 64, 0) or len(data) != 16 + count * 64:
            raise PodBoxError("Unsupported sound index output.")
        keys = [struct.unpack_from("<Q", data, 16 + i * 64)[0] for i in range(count)]
        if keys != sorted(set(keys)):
            raise PodBoxError("Sound index has duplicate or unsorted paths.")
    return changes


def preview(device: Path, operation: str, work_root: Path, *, cancel=None, progress=lambda _: None, timestamp_offset=None):
    if operation not in OPERATIONS:
        raise PodBoxError("Unknown PodBox operation.")
    cancel = cancel or threading.Event()
    device = Path(device).resolve(strict=True)
    rb = database._rb(device)
    _profile(device)
    if (rb / JOURNAL).exists():
        raise PodBoxError("A PodBox transaction needs recovery. Use Recover interrupted installation first.")
    library = rb / "library"
    validate_format(library)
    tools = engine_directory()
    binary = tools / (OPERATIONS[operation] + (".exe" if os.name == "nt" else ""))
    if not binary.is_file():
        raise PodBoxError("The selected native engine is missing.")
    work_root = Path(work_root).resolve()
    if work_root == device or device in work_root.parents:
        raise PodBoxError("Preview work must be stored locally, outside the player.")
    before = manifest(library)
    configuration = _configuration(device)
    inputs = _inventory(device, cancel)
    old_db = _database(library) if operation in ("update", "rebuild", "artwork") else None
    offset = (timestamp_offset if timestamp_offset is not None else fat_offset(device)) if operation != "soundscan" else 0
    if not isinstance(offset, int) or offset % 60 or not -14 * 3600 <= offset <= 14 * 3600:
        raise PodBoxError("Unsupported explicit FAT timestamp offset.")
    if old_db and operation in ("update", "rebuild"):
        # Reject an offset mismatch before a rebuild can discard the evidence.
        matches, mismatches = 0, 0
        for row in old_db.rows:
            if row[23] & 1:
                continue
            path = database._device_path(device, old_db.text(row, 4))
            if path.is_file() and path.stat().st_size and row[20]:
                delta = row[20] - int(path.stat().st_mtime)
                if delta == offset:
                    matches += 1
                elif abs(delta) <= 14 * 3600 and delta % 60 == 0:
                    mismatches += 1
        if mismatches and not matches:
            raise PodBoxError("Existing database timestamps do not match the current FAT mount offset. "
                              "Check the computer/player timezone and remount the player before trying again.")
    work = work_root / uuid.uuid4().hex
    overlay = work / "overlay"
    staged = overlay / ".rockbox/library"
    work.mkdir(parents=True, mode=0o700)
    try:
        progress("Copying the PodBox library into local preview storage…")
        _copy_tree(library, staged, flush=False)
        if manifest(staged) != before:
            raise PodBoxError("The library changed while staging it. Preview again.")
        for name in ("cache", "user"):
            (staged / name).mkdir(exist_ok=True)
        arguments = [str(binary), "-v", *(["--rebuild"] if operation == "rebuild" else []), device.as_posix()]
        output = _execute(arguments, device, overlay, work, offset, cancel, progress)
        after = manifest(staged)
        changes = _validate_outputs(staged, operation, before, after)
        if operation in ("update", "rebuild"):
            old_runtime, new_runtime = _runtime(old_db), _runtime(_database(staged))
            for path in old_runtime.keys() & new_runtime.keys():
                # Arrival commit may deliberately change for a newly reread row;
                # play history/rating/resume must survive exact retained paths.
                if old_runtime[path][:4] + old_runtime[path][5:] != new_runtime[path][:4] + new_runtime[path][5:]:
                    raise PodBoxError(f"Native engine did not preserve runtime data: {path}. Nothing was installed.")
                if old_runtime[path][4] and old_runtime[path][4] != new_runtime[path][4]:
                    raise PodBoxError(f"Native engine changed a recorded arrival commit: {path}. Nothing was installed.")
        if before != manifest(library) or configuration != _configuration(device) or inputs != _inventory(device, cancel):
            raise PodBoxError("The player changed during preview. Nothing was installed; preview again.")
        return PodBoxPlan(device, operation, work, before, after, inputs, configuration, offset, output, changes,
                          timestamp_offset is not None)
    except Exception:
        shutil.rmtree(work)
        raise


def _backup(device, library, expected, backup_root):
    backup_root = Path(backup_root).resolve()
    if backup_root == device or device in backup_root.parents:
        raise PodBoxError("Backups must be local, outside the player.")
    folder = backup_root / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, mode=0o700)
    _copy_tree(library, folder / "library")
    if manifest(folder / "library") != expected or manifest(library) != expected:
        raise PodBoxError(f"Backup verification failed: {folder}")
    data = {"schema": 1, "kind": "podbox-library", "device": str(device), "revision": REVISION, "files": expected}
    encoded = json.dumps(data, indent=2).encode()
    if len(encoded) > 64 * 1024**2:
        raise PodBoxError("The backup manifest exceeds 64 MiB.")
    database._write_sync(folder / "manifest.json", encoded)
    database._sync_directory(folder)
    read_backup(folder, device)
    return folder


def _copy_tree(source, destination, *, flush=True):
    """Copy file bytes/times without synthesizing AppleDouble sidecar collisions.

    shutil.copytree/copystat copy xattrs on Mac. On FAT that creates ._ files,
    colliding with the explicit ._ files already in the complete source snapshot.
    Existing sidecars are preserved as ordinary verified bytes instead.
    """
    destination.mkdir(parents=True)
    sidecars, expected = [], set()
    def fail(exc):
        raise exc
    def copy_file(path, target, *, sidecar=False):
        stat = path.stat()
        mode = "xb"
        if sidecar and target.exists():
            if target.is_symlink() or not target.is_file() or target.read_bytes()[:4] != b"\x00\x05\x16\x07":
                raise PodBoxError("Unexpected collision while preserving an AppleDouble sidecar.")
            mode = "wb"  # Only an OS-created sidecar in our fresh private copy.
        with path.open("rb") as reader, target.open(mode) as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            if flush:
                writer.flush()
                os.fsync(writer.fileno())
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    for root, directories, files in os.walk(source, onerror=fail):
        target = destination / Path(root).relative_to(source)
        for name in directories:
            path = Path(root) / name
            if path.is_symlink():
                raise PodBoxError("A library directory became a symlink while copying.")
            (target / name).mkdir()
        for name in files:
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                raise PodBoxError("A library file became unsafe while copying.")
            expected.add(path.relative_to(source).as_posix())
            if name.startswith("._"):
                sidecars.append((path, target / name))
            else:
                copy_file(path, target / name)
    # FAT can synthesize sidecars even on ordinary open(), without copystat.
    # Preserve explicit source sidecars last, then discard only newly generated
    # AppleDouble files absent from the snapshot in this fresh owned directory.
    for path, target in sidecars:
        copy_file(path, target, sidecar=True)
    for path in destination.rglob("._*"):
        if path.is_file() and path.relative_to(destination).as_posix() not in expected:
            if path.read_bytes()[:4] != b"\x00\x05\x16\x07":
                raise PodBoxError("Unrecognized sidecar generated while copying.")
            path.unlink()
    if flush:
        for folder in [p for p in destination.rglob("*") if p.is_dir()] + [destination]:
            database._sync_directory(folder)


def read_backup(folder, device):
    folder = Path(folder).resolve(strict=True)
    if folder == device or device in folder.parents:
        raise PodBoxError("Select a verified local backup outside the player.")
    path = folder / "manifest.json"
    if path.is_symlink() or path.stat().st_size > 64 * 1024**2:
        raise PodBoxError("Unsafe backup manifest.")
    data = json.loads(path.read_text())
    if (data.get("schema") != 1 or data.get("kind") != "podbox-library"
            or data.get("device") != str(device) or not isinstance(data.get("files"), dict)):
        raise PodBoxError("This is not a verified PodBox library backup for this player path.")
    if manifest(folder / "library") != data["files"]:
        raise PodBoxError("The backup is damaged or incomplete.")
    validate_format(folder / "library")
    return folder / "library", data["files"]


def _install(device, source, before, after, backup, *, check_inputs=lambda: None):
    rb = database._rb(device)
    transaction = uuid.uuid4().hex
    stage, previous = (rb / f".rlm-library-{kind}-{transaction}" for kind in ("stage", "previous"))
    if (rb / JOURNAL).exists():
        raise PodBoxError("Recover the interrupted PodBox installation first.")
    # Journal precedes all player writes, including staging. Recovery removes
    # only a verified complete stage or restores the intact previous directory.
    data = {"schema": 1, "device": str(device), "stage": stage.name, "previous": previous.name,
            "before": before, "after": after, "backup": str(backup)}
    encoded = json.dumps(data).encode()
    if len(encoded) > 128 * 1024**2:
        raise PodBoxError("The transaction journal exceeds 128 MiB.")
    pending = rb / (JOURNAL + "." + transaction + ".new")
    database._write_sync(pending, encoded)
    os.replace(pending, rb / JOURNAL)
    database._sync_directory(rb)
    try:
        _copy_tree(source, stage)
        if manifest(stage) != after or manifest(rb / "library") != before:
            raise PodBoxError("The player or staged library changed during installation.")
        check_inputs()
        os.replace(rb / "library", previous)
        database._sync_directory(rb)
        os.replace(stage, rb / "library")
        database._sync_directory(rb)
        if manifest(rb / "library") != after:
            raise PodBoxError("Installed library verification failed.")
        if manifest(previous) != before:
            raise PodBoxError("The previous library changed; retained for recovery.")
        shutil.rmtree(previous)
        (rb / JOURNAL).unlink()
        database._sync_directory(rb)
    except Exception as exc:
        raise PodBoxError(f"Installation stopped. Keep the player connected and use recovery. "
                          f"Verified local backup: {backup}\n{exc}") from exc


def apply(plan: PodBoxPlan, backup_root: Path):
    if not plan.changes:
        return None
    with database._device_lock(plan.device):
        library = database._rb(plan.device) / "library"
        if (manifest(library) != plan.before or _configuration(plan.device) != plan.configuration
                or _inventory(plan.device, threading.Event()) != plan.inputs
                or (plan.operation != "soundscan" and not plan.offset_explicit and fat_offset(plan.device) != plan.offset)):
            raise PodBoxError("The player, music or timezone changed. Preview again before installing.")
        if manifest(plan.library) != plan.after:
            raise PodBoxError("Local preview files changed. Preview again.")
        _validate_outputs(plan.library, plan.operation, plan.before, plan.after)
        backup = _backup(plan.device, library, plan.before, backup_root)
        def check_inputs():
            if (_configuration(plan.device) != plan.configuration
                    or _inventory(plan.device, threading.Event()) != plan.inputs):
                raise PodBoxError("Music or configuration changed while staging; use recovery and preview again.")
        _install(plan.device, plan.library, plan.before, plan.after, backup, check_inputs=check_inputs)
        return backup


def recover(device: Path):
    device = Path(device).resolve(strict=True)
    with database._device_lock(device):
        rb = database._rb(device)
        journal = rb / JOURNAL
        if not journal.exists():
            return "No interrupted PodBox installation was found."
        if journal.is_symlink() or journal.stat().st_size > 128 * 1024**2:
            raise PodBoxError("Unsafe PodBox recovery journal.")
        data = json.loads(journal.read_text())
        if data.get("schema") != 1 or data.get("device") != str(device):
            raise PodBoxError("Unsupported PodBox recovery journal.")
        for key in ("stage", "previous"):
            if not isinstance(data.get(key), str) or not _TRANSACTION.fullmatch(data[key]):
                raise PodBoxError("Unsafe transaction path in recovery journal.")
        # The backup is verified again before any recovery mutation.
        read_backup(Path(data["backup"]), device)
        stage, previous, live = rb / data["stage"], rb / data["previous"], rb / "library"
        if not live.exists() and previous.is_dir() and manifest(previous) == data["before"]:
            os.replace(previous, live)
            database._sync_directory(rb)
        current = manifest(live)
        if current not in (data["before"], data["after"]):
            raise PodBoxError("The current library differs from both transaction states; files were retained. Restore the verified backup manually.")
        if previous.exists():
            if manifest(previous) != data["before"]:
                raise PodBoxError("Previous library differs from the journal; retained for manual recovery.")
            shutil.rmtree(previous)
        retained = ""
        if stage.exists():
            # Incomplete staging can contain only a subset of exact target files.
            partial = manifest(stage)
            if any(data["after"].get(name) != value for name, value in partial.items()):
                quarantine = rb / (".rlm-library-inspect-" + uuid.uuid4().hex)
                os.replace(stage, quarantine)
                retained = f"\nIncomplete staging was retained for inspection: {quarantine}"
            else:
                shutil.rmtree(stage)
        journal.unlink()
        database._sync_directory(rb)
        return "Interrupted installation recovered. Local backups were retained. Safely eject and reboot PodBox." + retained


def restore(device: Path, folder: Path, backup_root: Path):
    device = Path(device).resolve(strict=True)
    _profile(device)
    source, desired = read_backup(folder, device)
    with database._device_lock(device):
        rb = database._rb(device)
        validate_format(rb / "library")
        if (rb / JOURNAL).exists():
            raise PodBoxError("Recover the interrupted installation before restoring a backup.")
        current = manifest(rb / "library")
        backup = _backup(device, rb / "library", current, backup_root)
        _install(device, source, current, desired, backup)
        return backup
