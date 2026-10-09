# SPDX-License-Identifier: GPL-2.0-only
"""Shared constants and small helpers for the desktop modules."""
from __future__ import annotations

import os
import re
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from . import __version__ as APP_VERSION
from . import artwork_engine as engine
from .frozen_runtime import external_environment

APP_NAME = "Rockbox Library Manager"
USER_AGENT = f"RockboxLibraryManager/{APP_VERSION}"
EXCLUDED_DIRS = {"$recycle.bin", "system volume information"}
IS_MAC = sys.platform == "darwin"
MOD = "Command" if IS_MAC else "Control"
MOD_LABEL = "⌘" if IS_MAC else "Ctrl+"


def to_rgb(img: Image.Image) -> Image.Image:
    """Apply EXIF rotation and flatten transparency onto black."""
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, (0, 0, 0))
        base.paste(rgba, mask=rgba.split()[-1])
        return base
    return img.convert("RGB")


def load_image(source: Any) -> Image.Image:
    """Open a path, bytes or file-like object as a fully loaded RGB image."""
    if isinstance(source, (bytes, bytearray)):
        source = BytesIO(source)
    with Image.open(source) as img:
        img.load()
        return to_rgb(img)


def anchor_crop_square(img: Image.Image, anchor: float = 0.5) -> Image.Image:
    """Square crop along the long axis. anchor 0 = top/left, 0.5 = centre, 1 = bottom/right."""
    if img.width == img.height:
        return img
    if abs(anchor - 0.5) < 1e-6:
        return engine.centre_crop_square(img)
    side = min(img.width, img.height)
    if img.width > img.height:
        x = int(round((img.width - side) * anchor))
        return img.crop((x, 0, x + side, side))
    y = int(round((img.height - side) * anchor))
    return img.crop((0, y, side, y + side))


def format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def humanize_age(ts: int) -> str:
    if not ts:
        return "never"
    delta = max(0, int(time.time()) - int(ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} h ago"
    days = delta // 86400
    return "yesterday" if days == 1 else f"{days} days ago"


def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://\S+$", (text or "").strip(), re.IGNORECASE))


def download_image(url: str, timeout: int = 45) -> Image.Image:
    """Download a pasted or dropped image address (size-limited, see the engine)."""
    with engine.requests.Session() as session:
        return engine.image_from_bytes(engine.download_image_bytes(session, url.strip(), timeout))


def open_in_file_manager(folder: Path) -> None:
    if IS_MAC:
        os.spawnlp(os.P_NOWAIT, "open", "open", str(folder))
    elif os.name == "nt":
        os.startfile(str(folder))  # type: ignore[attr-defined]
    else:
        env = external_environment()
        if env is None:
            os.spawnlp(os.P_NOWAIT, "xdg-open", "xdg-open", str(folder))
        else:
            os.spawnlpe(os.P_NOWAIT, "xdg-open", "xdg-open", str(folder), env)
