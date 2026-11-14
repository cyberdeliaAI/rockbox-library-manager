# SPDX-License-Identifier: GPL-2.0-only
"""Explicit full audio decoding. Never repair, normalize or replace source files."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .frozen_runtime import external_environment
from .media_tools import find_ffmpeg, linked, signature

EXTENSIONS = {".flac", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".wav", ".aif", ".aiff", ".wv", ".wma"}


@dataclass(frozen=True)
class IntegrityItem:
    path: str
    status: str
    detail: str


@dataclass
class IntegrityResult:
    items: list[IntegrityItem] = field(default_factory=list)
    cancelled: bool = False


def check_file(path, executable, cancel, *, timeout=300):
    path = Path(path)
    if linked(path) or not path.is_file():
        return IntegrityItem(str(path), "skipped", "Not a regular, unlinked audio file")
    before = signature(path)
    if not before[2]:
        return IntegrityItem(str(path), "error", "Empty audio file")
    env = external_environment() or os.environ.copy()
    env = {k: v for k, v in env.items() if not k.startswith(("CSC_", "WIN_CSC_", "APPLE_", "RLM_CODESIGN_"))}
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    command = [executable, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
               "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:a", "-vn", "-sn", "-dn", "-f", "null", "-"]
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, env=env, **kwargs)
        start, stopped = time.monotonic(), ""
        try:
            while proc.poll() is None:
                if cancel.wait(.05):
                    stopped = "Cancelled; no complete result"
                elif time.monotonic() - start > timeout:
                    stopped = "Decode timeout; no complete result"
                elif os.fstat(log.fileno()).st_size > 1024 * 1024:
                    stopped = "Decoder error log exceeded 1 MiB; no complete result"
                if stopped:
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        if stopped:
            return IntegrityItem(str(path), "incomplete", stopped)
        log.seek(0)
        detail = log.read(1000).decode("utf-8", "replace").strip()
        if linked(path) or not path.is_file() or signature(path) != before:
            return IntegrityItem(str(path), "changed", "File changed during decoding; scan again")
        if proc.returncode:
            return IntegrityItem(str(path), "error", detail or f"Decoder exited with code {proc.returncode}")
        if detail:
            return IntegrityItem(str(path), "warning", detail)
        return IntegrityItem(str(path), "passed", "Complete audio decode succeeded; provenance/mastering quality is not established")


def scan(root, configured="", cancel=None, progress=lambda _: None):
    cancel = cancel or threading.Event()
    root = Path(root).absolute()
    if linked(root) or not root.exists():
        raise ValueError("Choose an existing, unlinked music folder or audio file.")
    executable = find_ffmpeg(configured)
    if not executable:
        raise ValueError("Install FFmpeg or choose its executable in Settings before running a full integrity scan.")
    result = IntegrityResult()
    def walk_error(exc):
        result.items.append(IntegrityItem(str(exc.filename or root), "unreadable", str(exc)))
    if root.is_file():
        candidates = iter([root])
    else:
        def walk():
            for folder, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
                if cancel.is_set():
                    return
                kept = []
                for name in sorted(dirs):
                    child = Path(folder) / name
                    if linked(child):
                        result.items.append(IntegrityItem(str(child), "skipped", "Linked directory was not followed"))
                    elif not name.startswith(".") and name.casefold() not in ("$recycle.bin", "system volume information"):
                        kept.append(name)
                dirs[:] = kept
                for name in sorted(files):
                    if not name.startswith(".") and Path(name).suffix.lower() in EXTENSIONS:
                        yield Path(folder) / name
        candidates = walk()
    for path in candidates:
        if cancel.is_set():
            break
        if len(result.items) >= 100_000:
            raise ValueError("Select a smaller folder: an integrity report is limited to 100,000 entries.")
        progress(f"Full decode: {len(result.items) + 1} · {path.name}")
        try:
            item = check_file(path, executable, cancel)
        except OSError as exc:
            item = IntegrityItem(str(path), "unreadable", str(exc))
        result.items.append(item)
    result.cancelled = cancel.is_set()
    return result
