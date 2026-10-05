#!/usr/bin/env python3
"""
Rockbox Library Manager GUI

Integrated desktop interface for the Rockbox Library Manager.

Design goals:
- Use the integrated artwork engine for fetch/scoring.
- Keep the iPod / music device access light: folder scans first, tags only when needed.
- Store the library index in a local SQLite database.
- Cache small gallery thumbnails locally so browsing does not repeatedly hit the iPod.
- Only load thumbnails for cards that are actually on screen (virtualized gallery).
- Support existing and missing artist/album artwork.
- Replace artwork by file, drag-and-drop, clipboard paste, image URL or an online
  candidate picker that reuses the engine's own providers.
- Saved artwork uses the integrated artwork engine's Lanczos resize + JPEG helper.

Required:
    pip install pillow requests mutagen

Optional, for drag-and-drop support:
    pip install tkinterdnd2

Run through the single application entry point:
    python rockbox_library_manager.py
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

try:
    from . import artwork_engine as engine
except Exception as exc:  # pragma: no cover - startup error path
    raise SystemExit(
        "Could not load the integrated artwork engine.\n\n"
        f"Original error: {exc}"
    )

try:
    from tkinterdnd2 import DND_FILES, DND_TEXT, TkinterDnD  # type: ignore

    DND_AVAILABLE = True
except Exception:
    DND_FILES = DND_TEXT = None
    TkinterDnD = None
    DND_AVAILABLE = False


from . import __version__ as APP_VERSION
from .common import (APP_NAME, EXCLUDED_DIRS, IS_MAC, MOD, MOD_LABEL, USER_AGENT, anchor_crop_square,  # noqa: F401
                     download_image, format_bytes, humanize_age, is_url, load_image, open_in_file_manager, to_rgb)
from .folders import consolidate_artist_folders  # noqa: F401
from .health import CATEGORIES as HEALTH_CATEGORIES
from .health import FILTERS as HEALTH_FILTERS
from .health import ISSUE_LABELS as HEALTH_LABELS
from .health import HealthScanner  # noqa: F401
from .library import (FastLibraryScanner, LibraryDB, LibraryItem, artist_identity_key,  # noqa: F401
                      choose_display_name, contains_audio_immediate, detect_artwork, display_name_score,
                      image_file_signature, immediate_subdirs, sample_audio_files)
from .picker import PickerDialog  # noqa: F401
from .tag_editor import TagEditorDialog  # noqa: F401
from .ui_theme import (C, app_icon_image, apply_style, blend, hex_to_rgb, make_fonts,  # noqa: F401
                       placeholder_art, rounded_fit, rounded_panel, shape_mask, shaped_thumbnail)
from .widgets import (Dot, DropZone, FieldEntry, NavItem, ScrollFrame, Segmented, Toast, Tooltip,  # noqa: F401
                      recolor, wheel_pixels)

GUI_CONFIG_FILENAME = "artwork_gui_config.json"
DB_FILENAME = "artwork_gui.sqlite3"
THUMB_DIRNAME = "artwork_gui_thumbs"
DEFAULT_PAGE_SIZE = 30  # kept for backward compatibility with older configs
THUMB_CACHE_SIZE = 256
DEFAULT_OUTPUT_SIZE = 300
ZOOM_MIN, ZOOM_MAX, ZOOM_DEFAULT = 110, 230, 150
PHOTO_CACHE_LIMIT = 320
LOG_MAX_LINES = 4000
SUPPORTED_MANUAL_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif",
}

FRIENDLY_REASONS = {
    "unmatched": "The folder name didn't match the audio tags, so auto-fetch skipped it. "
    "Use Search online to pick artwork yourself.",
    "not-found": "No source returned usable artwork.",
    "not-found-cache": "Cached as not found. Fetch again to retry.",
    "Artwork not found": "No source returned usable artwork.",
}


def friendly_reason(text: str) -> str:
    text = (text or "").strip()
    return FRIENDLY_REASONS.get(text, text)


# ----------------------------------------------------------------------
# Paths and config
# ----------------------------------------------------------------------
def local_app_dir() -> Path:
    """Reuse the existing Rockbox artwork config location for a seamless upgrade."""
    return engine.get_config_path().parent


def gui_config_path() -> Path:
    return local_app_dir() / GUI_CONFIG_FILENAME


def database_path() -> Path:
    return local_app_dir() / DB_FILENAME


def thumbnail_root() -> Path:
    return local_app_dir() / THUMB_DIRNAME


def load_gui_config() -> dict[str, Any]:
    path = gui_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_gui_config(data: dict[str, Any]) -> None:
    path = gui_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def file_drop_to_path(data: str) -> Optional[Path]:
    """Parse the first path from a TkDND file list (kept for compatibility)."""
    value = (data or "").strip()
    if not value:
        return None
    if value.startswith("{"):
        end = value.find("}")
        if end >= 0:
            value = value[1:end]
    else:
        value = value.split()[0]
    value = value.strip().strip('"')
    return Path(value) if value else None



# ----------------------------------------------------------------------
# Thumbnail cache: local JPEGs, only generated for cards on screen
# ----------------------------------------------------------------------
class ThumbnailCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        # Cached thumbnails live on the local disk and can load in parallel.
        self.local_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="thumb-local")
        # Reads from the music device stay gentle: an iPod HDD hates random parallel reads.
        self.device_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="thumb-device")
        self.wanted: set[int] = set()
        self._inflight: set[tuple[int, int, int]] = set()
        self._lock = threading.Lock()

    def cache_path(self, item: LibraryItem) -> Path:
        return self.root / item.kind / (
            f"{item.id}-{item.artwork_mtime_ns}-{item.artwork_size}-{THUMB_CACHE_SIZE}.jpg"
        )

    def request(self, item: LibraryItem, artwork_path: Path, display_size: int, bg: str,
                callback: Callable[[int, tuple[int, int, int], Any], None]) -> None:
        key = (item.id, item.artwork_mtime_ns, display_size)
        with self._lock:
            if key in self._inflight:
                return
            self._inflight.add(key)
        target = self.cache_path(item)
        shape = "circle" if item.kind == "artist" else "rounded"
        pool = self.local_pool if target.exists() else self.device_pool

        def work() -> Any:
            try:
                if item.id not in self.wanted:
                    return None  # scrolled away before we got to it
                if not target.exists():
                    with Image.open(artwork_path) as img:
                        img.load()
                        small = to_rgb(img)
                    small.thumbnail((THUMB_CACHE_SIZE, THUMB_CACHE_SIZE), Image.Resampling.LANCZOS)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    small.save(target, "JPEG", quality=88, optimize=True)
                with Image.open(target) as img:
                    img.load()
                    thumb = img.convert("RGB")
                return shaped_thumbnail(thumb, display_size, shape, bg)
            except Exception:
                return "error"
            finally:
                with self._lock:
                    self._inflight.discard(key)

        def done(future: Any) -> None:
            try:
                result = future.result()
            except Exception:
                result = "error"
            callback(item.id, key, result)

        pool.submit(work).add_done_callback(done)

    def run_device_task(self, fn: Callable[[], Any], callback: Callable[[Any], None]) -> None:
        def done(future: Any) -> None:
            try:
                callback(future.result())
            except Exception as exc:
                callback(exc)
        self.device_pool.submit(fn).add_done_callback(done)

    def clear(self) -> None:
        if not self.root.exists():
            return
        for path in self.root.rglob("*.jpg"):
            try:
                path.unlink()
            except OSError:
                pass

    def disk_usage(self) -> int:
        total = 0
        if self.root.exists():
            for path in self.root.rglob("*.jpg"):
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    def shutdown(self) -> None:
        for pool in (self.local_pool, self.device_pool):
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # Python < 3.9
                pool.shutdown(wait=False)


class LogTee:
    """Mirror engine print() output into the Activity view without losing the console."""

    def __init__(self, original: Any, sink: Callable[[str], None]) -> None:
        self.original = original
        self.sink = sink
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if self.original is not None:
            try:
                self.original.write(text)
            except Exception:
                pass
        with self._lock:
            self._buf += text
            if "\n" in self._buf:
                lines = self._buf.split("\n")
                self._buf = lines.pop()
                for line in lines:
                    self.sink(line)
        return len(text)

    def flush(self) -> None:
        if self.original is not None:
            try:
                self.original.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return getattr(self.original, "encoding", None) or "utf-8"


# ----------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------
STATUS_NAV = [
    ("All", "All items", "faint"),
    ("Existing", "Has artwork", "ok"),
    ("Missing", "Missing", "warn"),
    ("Problems", "Problems", "bad"),
]
VIEW_TITLES = {"All": "Library", "Existing": "Has artwork", "Missing": "Missing artwork", "Problems": "Problems"}


class ArtworkApp:
    def __init__(self, root_window: tk.Tk) -> None:
        self.root = root_window
        self.root.title(APP_NAME)
        self.config = load_gui_config()
        self.root.geometry(str(self.config.get("geometry") or "1280x820"))
        self.root.minsize(1040, 680)

        self.F = make_fonts(self.root)
        apply_style(self.root, self.F)
        self.root.configure(bg=C["bg"])
        try:
            self._icon_photo = ImageTk.PhotoImage(app_icon_image(C["accent"]))
            self.root.iconphoto(True, self._icon_photo)
        except Exception:
            self._icon_photo = None

        self.db = LibraryDB(database_path())
        self.thumbs = ThumbnailCache(thumbnail_root())
        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.operation_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self._scrollables: dict[str, Callable[[int], None]] = {}

        # Library / gallery state
        self.items: list[LibraryItem] = []
        self.index_by_id: dict[int, int] = {}
        self.album_counts: dict[str, int] = {}
        self.selected_item_id: Optional[int] = None
        self.hover_index: Optional[int] = None
        self.drawn: set[int] = set()
        self.photo_cache: OrderedDict[int, tuple[tuple[int, int, int], ImageTk.PhotoImage]] = OrderedDict()
        self.static_photos: dict[Any, ImageTk.PhotoImage] = {}
        self._text_cache: dict[tuple[str, int, str], str] = {}
        self.L = SimpleNamespace(cols=1, card_w=0, card_h=0, gap=16, row_gap=18, x0=20, top=6, total_h=0, S=0, pad=10)
        self._render_job: Optional[str] = None
        self._relayout_job: Optional[str] = None
        self._search_job: Optional[str] = None
        self._busy_kind = ""
        self._thumb_errors: set[tuple[int, int, int]] = set()
        self._settings_loading = True
        self._known_root = ""
        self._detail_item: Optional[LibraryItem] = None
        self._preview_token: Any = None
        self._counts: dict[str, int] = {}
        self._empty_widgets: list[tk.Misc] = []
        self.health_filter = "all"
        self.health_thorough_var = tk.BooleanVar(value=bool(self.config.get("health_thorough", False)))
        self.selected_health_issue_id: Optional[int] = None

        zoom = int(self.config.get("zoom") or ZOOM_DEFAULT)
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, zoom))
        kind = self.config.get("kind_filter")
        status = self.config.get("status_filter")
        self.kind_filter = kind if kind in ("All", "Artists", "Albums") else "All"
        self.status_filter = status if status in ("All", "Existing", "Missing", "Problems") else "All"
        self.view = "library"

        self.search_var = tk.StringVar(value="")
        self.music_root_var = tk.StringVar(
            value=str(self.config.get("music_root") or self.db.get_meta("music_root", "")))
        output_name = str(self.config.get("output_name") or self.db.get_meta("output_name", "folder.jpg"))
        self.output_name = output_name if output_name in ("folder.jpg", "cover.jpg") else "folder.jpg"
        self.output_size_var = tk.StringVar(value=str(self.config.get("output_size") or DEFAULT_OUTPUT_SIZE))
        self.media_backup_var = tk.BooleanVar(value=self.config.get("media_backup", True))
        self.media_backup_path_var = tk.StringVar(value=str(self.config.get("media_backup_path") or local_app_dir() / "media-backups"))
        self.ffmpeg_path_var = tk.StringVar(value=str(self.config.get("ffmpeg_path") or ""))
        self.api_lastfm_key_var = tk.StringVar()
        self.api_discogs_token_var = tk.StringVar()
        self.api_fanart_key_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")

        # Detail / pending artwork state
        self.pending_image: Optional[Image.Image] = None
        self.pending_label = ""
        self.pending_anchor = 0.5
        self.pending_item_id: Optional[int] = None
        self.current_preview: Optional[Image.Image] = None
        self.detail_photo: Optional[ImageTk.PhotoImage] = None
        self.detail_photo2: Optional[ImageTk.PhotoImage] = None

        self._build_ui()
        self._install_wheel_router()
        self._install_shortcuts()
        self._install_log_tee()
        self._load_credentials_into_ui()
        self.refresh_counts()
        self.set_view("library")
        self.reload_gallery(reset_scroll=True)
        self.show_detail(None)
        self.root.after(40, self._process_ui_queue)
        self.root.after(800, self._poll_device)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # First-time convenience: if a root is configured but the index is empty, scan automatically.
        if self.db.counts()["total"] == 0 and self._valid_music_root(silent=True):
            self.root.after(300, self.scan_library)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.columnconfigure(2, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._build_sidebar()
        tk.Frame(self.root, bg=C["border"], width=1).grid(row=0, column=1, sticky="ns")

        main = tk.Frame(self.root, bg=C["bg"])
        main.grid(row=0, column=2, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        content = tk.Frame(main, bg=C["bg"])
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        self.library_view = tk.Frame(content, bg=C["bg"])
        self.health_view = tk.Frame(content, bg=C["bg"])
        self.settings_view = tk.Frame(content, bg=C["bg"])
        self.activity_view = tk.Frame(content, bg=C["bg"])
        for view in (self.library_view, self.health_view, self.settings_view, self.activity_view):
            view.grid(row=0, column=0, sticky="nsew")

        self._build_library_view(self.library_view)
        self._build_health_view(self.health_view)
        self._build_settings_view(self.settings_view)
        self._build_activity_view(self.activity_view)
        self._build_statusbar(main)

    def _section_label(self, parent: tk.Misc, text: str, bg: str, pady: Any = (16, 6), padx: Any = 0) -> tk.Label:
        lbl = tk.Label(parent, text=text, bg=bg, fg=C["muted"], font=self.F.small_b, anchor="w")
        lbl.pack(fill="x", pady=pady, padx=padx)
        return lbl

    def _build_sidebar(self) -> None:
        bar = tk.Frame(self.root, bg=C["sidebar"], width=196)
        bar.grid(row=0, column=0, sticky="ns")
        bar.pack_propagate(False)

        # Compact app identity; keep navigation visually dominant.
        brand = tk.Frame(bar, bg=C["sidebar"], padx=15, pady=16)
        brand.pack(fill="x")
        logo = tk.Canvas(brand, width=26, height=26, bg=C["sidebar"], highlightthickness=0)
        try:
            self._logo_photo = ImageTk.PhotoImage(
                app_icon_image(C["accent"]).resize((26, 26), Image.Resampling.LANCZOS)
            )
            logo.create_image(0, 0, image=self._logo_photo, anchor="nw")
        except Exception:
            pass
        logo.pack(side="left")
        names = tk.Frame(brand, bg=C["sidebar"])
        names.pack(side="left", padx=(9, 0))
        tk.Label(names, text="Rockbox Library", bg=C["sidebar"], fg=C["text"],
                 font=self.F.body_b).pack(anchor="w")
        tk.Label(names, text=f"Manager  ·  v{APP_VERSION}", bg=C["sidebar"], fg=C["faint"],
                 font=self.F.tiny).pack(anchor="w")

        tk.Frame(bar, bg=C["border"], height=1).pack(fill="x", padx=12, pady=(0, 6))

        self._section_label(bar, "Browse", C["sidebar"], pady=(8, 4), padx=15)
        self.nav_status: dict[str, NavItem] = {}
        for key, label, color in STATUS_NAV:
            item = NavItem(bar, self.F, label, lambda k=key: self.set_status_filter(k), dot=C[color])
            item.pack(fill="x")
            self.nav_status[key] = item

        self._section_label(bar, "Library health", C["sidebar"], pady=(16, 4), padx=15)
        self.nav_health = NavItem(bar, self.F, "Health check", lambda: self.set_view("health"),
                                  dot=C["warn"], count=True)
        self.nav_health.pack(fill="x")

        self._section_label(bar, "Tools", C["sidebar"], pady=(16, 4), padx=15)
        self.nav_activity = NavItem(bar, self.F, "Activity log", lambda: self.set_view("activity"), count=False)
        self.nav_activity.pack(fill="x")
        self.nav_settings = NavItem(bar, self.F, "Settings", lambda: self.set_view("settings"), count=False)
        self.nav_settings.pack(fill="x")

        # Device information is a quiet footer, not another floating card.
        device = tk.Frame(bar, bg=C["sidebar"], padx=15, pady=14)
        device.pack(side="bottom", fill="x")
        tk.Frame(device, bg=C["border"], height=1).pack(fill="x", pady=(0, 12))
        head = tk.Frame(device, bg=C["sidebar"])
        head.pack(fill="x")
        self.device_dot = Dot(head, C["faint"], C["sidebar"], 7)
        self.device_dot.pack(side="left")
        self.device_label = tk.Label(head, text="Music folder", bg=C["sidebar"], fg=C["muted"],
                                     font=self.F.small_b)
        self.device_label.pack(side="left", padx=(7, 0))
        self.root_name_label = tk.Label(device, text="", bg=C["sidebar"], fg=C["text"], font=self.F.small_b,
                                        anchor="w", justify="left", wraplength=164)
        self.root_name_label.pack(fill="x", pady=(7, 0))
        self.root_path_label = tk.Label(device, text="", bg=C["sidebar"], fg=C["faint"], font=self.F.tiny,
                                        anchor="w", justify="left", wraplength=164)
        self.root_path_label.pack(fill="x")
        self.scan_age_label = tk.Label(device, text="", bg=C["sidebar"], fg=C["muted"],
                                       font=self.F.tiny, anchor="w")
        self.scan_age_label.pack(fill="x", pady=(5, 8))
        ttk.Button(device, text="Music folder…", style="Small.TButton",
                   command=self.choose_music_root).pack(fill="x")
        self._update_root_card()

    def _build_library_view(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        # Header: compact page title + primary actions.
        header = tk.Frame(parent, bg=C["bg"], padx=20)
        header.grid(row=0, column=0, sticky="ew", pady=(14, 8))
        header.columnconfigure(0, weight=1)
        titles = tk.Frame(header, bg=C["bg"])
        titles.grid(row=0, column=0, sticky="w")
        self.view_title = tk.Label(titles, text="Library", bg=C["bg"], fg=C["text"], font=self.F.h1)
        self.view_title.pack(anchor="w")
        self.view_subtitle = tk.Label(titles, text="", bg=C["bg"], fg=C["muted"], font=self.F.small)
        self.view_subtitle.pack(anchor="w", pady=(1, 0))
        actions = tk.Frame(header, bg=C["bg"])
        actions.grid(row=0, column=1, sticky="e")
        self.scan_btn = ttk.Button(actions, text="Rescan", command=self.scan_library)
        self.scan_btn.pack(side="left")
        self.fetch_btn = ttk.Button(actions, text="Fetch missing", style="Accent.TButton",
                                    command=self.fetch_missing)
        self.fetch_btn.pack(side="left", padx=(7, 0))

        # Single desktop-style toolbar rather than several floating controls.
        toolbar = tk.Frame(parent, bg=C["panel"], padx=12, pady=9,
                           highlightthickness=1, highlightbackground=C["border"])
        toolbar.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 12))
        toolbar.columnconfigure(0, weight=1)

        self.search_entry = FieldEntry(toolbar, self.F, self.search_var,
                                       placeholder="Search artists and albums", icon=True,
                                       clearable=True, width=28)
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.search_var.trace_add("write", lambda *_a: self._on_search_changed())
        self.search_entry.entry.bind("<Escape>", lambda _e: self.search_var.set(""))
        self.search_entry.entry.bind("<Return>", lambda _e: self._apply_search_now())
        self.search_entry.entry.bind("<Down>", lambda _e: (self.gallery.focus_set(), self.move_selection(0)))

        self.kind_seg = Segmented(toolbar, self.F,
                                  [("All", "All"), ("Artists", "Artists"), ("Albums", "Albums")],
                                  self.kind_filter, command=self.set_kind_filter)
        self.kind_seg.grid(row=0, column=1, padx=(0, 14))

        zoom = tk.Frame(toolbar, bg=C["panel"])
        zoom.grid(row=0, column=2, sticky="e")
        tk.Label(zoom, text="Size", bg=C["panel"], fg=C["faint"], font=self.F.tiny).pack(side="left")
        self.zoom_scale = ttk.Scale(zoom, from_=ZOOM_MIN, to=ZOOM_MAX, orient="horizontal", length=96,
                                    command=self._on_zoom)
        self.zoom_scale.set(self.zoom)
        self.zoom_scale.pack(side="left", padx=(7, 0))

        body = tk.Frame(parent, bg=C["bg"])
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        gallery_frame = tk.Frame(body, bg=C["bg"])
        gallery_frame.grid(row=0, column=0, sticky="nsew")
        gallery_frame.columnconfigure(0, weight=1)
        gallery_frame.rowconfigure(0, weight=1)
        self.gallery = tk.Canvas(gallery_frame, bg=C["bg"], highlightthickness=0, bd=0,
                                 yscrollincrement=1, takefocus=1)
        self.gallery_vsb = ttk.Scrollbar(gallery_frame, orient="vertical",
                                         style="Slim.Vertical.TScrollbar", command=self.gallery.yview)
        self.gallery.configure(yscrollcommand=self._on_gallery_yview)
        self.gallery.grid(row=0, column=0, sticky="nsew", padx=(12, 0))
        self.gallery_vsb.grid(row=0, column=1, sticky="ns", padx=(0, 4))
        self._register_scrollable(self.gallery, self._scroll_gallery)
        self._bind_gallery_events()
        self.toast = Toast(gallery_frame, self.F)
        self.tooltip = Tooltip(self.root, self.F)

        tk.Frame(body, bg=C["border"], width=1).grid(row=0, column=1, sticky="ns")
        panel = tk.Frame(body, bg=C["panel"], width=316)
        panel.grid(row=0, column=2, sticky="nsew")
        panel.grid_propagate(False)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)
        self._build_detail_panel(panel)

    def _build_detail_panel(self, panel: tk.Frame) -> None:
        sf = ScrollFrame(panel, C["panel"], self._register_scrollable, "Panel.Slim.Vertical.TScrollbar")
        sf.grid(row=0, column=0, sticky="nsew")
        self.detail_sf = sf
        inner = tk.Frame(sf.inner, bg=C["panel"], padx=18, pady=18)
        inner.pack(fill="both", expand=True)
        P = 268
        self.preview_size = P

        preview_wrap = tk.Frame(inner, bg=C["input"], padx=1, pady=1,
                                highlightthickness=1, highlightbackground=C["border"])
        preview_wrap.pack(anchor="w")
        self.preview = tk.Canvas(preview_wrap, width=P, height=P, bg=C["panel"],
                                 highlightthickness=0, bd=0)
        self.preview.pack()

        self.d_title = tk.Label(inner, text="", bg=C["panel"], fg=C["text"], font=self.F.title,
                                anchor="w", justify="left", wraplength=P)
        self.d_title.pack(fill="x", pady=(13, 0))
        self.d_sub = tk.Label(inner, text="", bg=C["panel"], fg=C["muted"], font=self.F.body,
                              anchor="w", justify="left", wraplength=P)
        self.d_sub.pack(fill="x", pady=(1, 0))

        self.detail_body = tk.Frame(inner, bg=C["panel"])
        self.detail_body.pack(fill="x")
        chips = tk.Frame(self.detail_body, bg=C["panel"])
        chips.pack(fill="x", pady=(8, 0))
        self.chip_status = tk.Label(chips, text="", font=self.F.tiny_b, padx=7, pady=2)
        self.chip_status._keep_bg = True  # type: ignore[attr-defined]
        self.chip_status.pack(side="left")
        self.chip_kind = tk.Label(chips, text="", bg=C["input"], fg=C["muted"],
                                  font=self.F.tiny_b, padx=7, pady=2)
        self.chip_kind._keep_bg = True  # type: ignore[attr-defined]
        self.chip_kind.pack(side="left", padx=(6, 0))
        self.d_meta = tk.Label(self.detail_body, text="", bg=C["panel"], fg=C["muted"], font=self.F.small,
                               anchor="w", justify="left", wraplength=P)
        self.d_meta.pack(fill="x", pady=(8, 0))
        self.d_path = tk.Label(self.detail_body, text="", bg=C["panel"], fg=C["faint"], font=self.F.tiny,
                               anchor="w", justify="left", wraplength=P)
        self.d_path.pack(fill="x", pady=(2, 0))

        tint = blend(C["panel"], C["bad"], 0.10)
        self.problem_box = tk.Frame(self.detail_body, bg=tint, padx=10, pady=9,
                                    highlightthickness=1, highlightbackground=blend(C["panel"], C["bad"], 0.30))
        self.problem_label = tk.Label(self.problem_box, text="", bg=tint, fg=C["text"], font=self.F.small,
                                      justify="left", anchor="w", wraplength=P - 24)
        self.problem_label.pack(fill="x")

        self._section_label(self.detail_body, "Replace artwork", C["panel"], pady=(18, 7))
        self.drop_zone = DropZone(self.detail_body, self.F, self.select_manual_image)
        self.drop_zone.configure(height=72)
        self.drop_zone.pack(fill="x")
        self._register_drop_target(self.drop_zone, self._on_drop_zone,
                                   lambda _e, inside: self.drop_zone.set_active(inside))

        self.pending_box = tk.Frame(self.detail_body, bg=C["panel"])
        self.pending_info = tk.Label(self.pending_box, text="", bg=C["panel"], fg=C["muted"], font=self.F.small,
                                     anchor="w", justify="left", wraplength=P)
        self.pending_info.pack(fill="x", pady=(8, 0))
        self.crop_row = tk.Frame(self.pending_box, bg=C["panel"])
        tk.Label(self.crop_row, text="Crop", bg=C["panel"], fg=C["muted"], font=self.F.small).pack(side="left")
        self.crop_seg = Segmented(self.crop_row, self.F, [(0.5, "Center")], 0.5, command=self._on_crop_anchor)
        self.crop_seg.pack(side="left", padx=(9, 0))

        self.pending_bar = tk.Frame(panel, bg=C["panel"])
        tk.Frame(self.pending_bar, bg=C["border"], height=1).pack(fill="x")
        bar = tk.Frame(self.pending_bar, bg=C["panel"], padx=18, pady=10)
        bar.pack(fill="x")
        bar.columnconfigure(0, weight=1)
        self.save_btn = ttk.Button(bar, text="Save artwork", style="Accent.TButton",
                                   command=self.save_manual_artwork)
        self.save_btn.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="Discard", style="Ghost.TButton",
                   command=self.clear_pending).grid(row=0, column=1, padx=(7, 0))
        self.pending_bar.grid(row=1, column=0, sticky="ew")
        self.pending_bar.grid_remove()

        self._section_label(self.detail_body, "Artwork tools", C["panel"], pady=(18, 7))
        self.search_online_btn = ttk.Button(self.detail_body, text="Search online…", command=self.open_picker)
        self.search_online_btn.pack(fill="x")
        row = tk.Frame(self.detail_body, bg=C["panel"])
        row.pack(fill="x", pady=(7, 0))
        row.columnconfigure(0, weight=1, uniform="r")
        row.columnconfigure(1, weight=1, uniform="r")
        self.autofetch_btn = ttk.Button(row, text="Auto-fetch", command=self.fetch_selected_item)
        self.autofetch_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.open_folder_btn = ttk.Button(row, text="Open folder", command=self.open_selected_folder)
        self.open_folder_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.edit_tags_btn = ttk.Button(self.detail_body, text="Edit album tags…", command=self.edit_selected_tags)
        self.edit_tags_btn.pack(fill="x", pady=(7, 0))
        self.detail_hint = tk.Label(self.detail_body, text="", bg=C["panel"], fg=C["faint"], font=self.F.tiny,
                                    anchor="w", justify="left", wraplength=P)
        self.detail_hint.pack(fill="x", pady=(8, 0))

    def _build_health_view(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        header = tk.Frame(parent, bg=C["bg"], padx=20)
        header.grid(row=0, column=0, sticky="ew", pady=(14, 8))
        header.columnconfigure(0, weight=1)
        titles = tk.Frame(header, bg=C["bg"])
        titles.grid(row=0, column=0, sticky="w")
        tk.Label(titles, text="Library Health", bg=C["bg"], fg=C["text"], font=self.F.h1).pack(anchor="w")
        self.health_subtitle = tk.Label(titles, text="Analyze tags and artist identities without changing files.",
                                        bg=C["bg"], fg=C["muted"], font=self.F.small)
        self.health_subtitle.pack(anchor="w", pady=(1, 0))
        self.health_analyze_btn = ttk.Button(header, text="Analyze library", style="Accent.TButton",
                                             command=self.analyze_library_health)
        self.health_analyze_btn.grid(row=0, column=1, sticky="e")

        toolbar = tk.Frame(parent, bg=C["panel"], padx=12, pady=9,
                           highlightthickness=1, highlightbackground=C["border"])
        toolbar.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 12))
        self.health_seg = Segmented(toolbar, self.F, HEALTH_FILTERS, self.health_filter,
                                    command=self.set_health_filter)
        self.health_seg.pack(side="left")
        ttk.Checkbutton(toolbar, text="Check every track (slower)", variable=self.health_thorough_var,
                        command=self._persist_basic_config).pack(side="right")

        body = tk.Frame(parent, bg=C["bg"])
        body.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 14))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        tree_wrap = tk.Frame(body, bg=C["bg"], highlightthickness=1, highlightbackground=C["border"])
        tree_wrap.grid(row=0, column=0, sticky="nsew")
        tree_wrap.columnconfigure(0, weight=1)
        tree_wrap.rowconfigure(0, weight=1)
        cols = ("issue", "item", "details")
        self.health_tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", style="Health.Treeview",
                                        selectmode="browse")
        self.health_tree.heading("issue", text="Issue")
        self.health_tree.heading("item", text="Artist / album")
        self.health_tree.heading("details", text="Details")
        self.health_tree.column("issue", width=145, minwidth=120, stretch=False)
        self.health_tree.column("item", width=270, minwidth=180, stretch=False)
        self.health_tree.column("details", width=500, minwidth=260, stretch=True)
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", style="Slim.Vertical.TScrollbar",
                            command=self.health_tree.yview)
        self.health_tree.configure(yscrollcommand=vsb.set)
        self.health_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.health_tree.bind("<<TreeviewSelect>>", self._on_health_select)
        self.health_tree.bind("<Double-1>", lambda _e: self.edit_health_tags())

        self.health_detail = tk.Frame(body, bg=C["panel"], padx=14, pady=12,
                                      highlightthickness=1, highlightbackground=C["border"])
        self.health_detail.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.health_detail.columnconfigure(0, weight=1)
        self.health_detail_title = tk.Label(self.health_detail, text="Select an issue", bg=C["panel"],
                                            fg=C["text"], font=self.F.body_b, anchor="w")
        self.health_detail_title.grid(row=0, column=0, sticky="ew")
        self.health_detail_text = tk.Label(self.health_detail, text="", bg=C["panel"], fg=C["muted"],
                                           font=self.F.small, anchor="w", justify="left", wraplength=760)
        self.health_detail_text.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        actions = tk.Frame(self.health_detail, bg=C["panel"])
        actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))
        self.health_open_btn = ttk.Button(actions, text="Open folder", style="Small.TButton",
                                          command=self.open_health_folder)
        self.health_open_btn.pack(side="left")
        self.health_tag_btn = ttk.Button(actions, text="Edit tags…", style="Small.TButton",
                                         command=self.edit_health_tags)
        self.health_tag_btn.pack(side="left", padx=(6, 0))
        self.health_merge_btn = ttk.Button(actions, text="Resolve duplicate…", style="SmallAccent.TButton",
                                           command=self.choose_duplicate_display_name)
        self.health_merge_btn.pack(side="left", padx=(6, 0))
        self.health_open_btn.state(["disabled"])
        self.health_tag_btn.state(["disabled"])
        self.health_merge_btn.state(["disabled"])

    def _settings_card(self, parent: tk.Misc, title: str, desc: str) -> tk.Frame:
        card = tk.Frame(parent, bg=C["panel"], padx=18, pady=16, highlightthickness=1,
                        highlightbackground=C["border"])
        card.pack(fill="x", pady=(0, 12))
        tk.Label(card, text=title, bg=C["panel"], fg=C["text"], font=self.F.title, anchor="w").pack(fill="x")
        if desc:
            tk.Label(card, text=desc, bg=C["panel"], fg=C["muted"], font=self.F.small, anchor="w",
                     justify="left", wraplength=420).pack(fill="x", pady=(2, 12))
        return card

    def open_rockbox_database(self) -> None:
        root = self._valid_music_root(silent=False)
        if root is None:
            return
        if self.operation_lock.locked():
            messagebox.showinfo(APP_NAME, "Wait for the current operation to finish first.")
            return
        from .database_dialog import DatabaseDialog
        try:
            DatabaseDialog(self, root, local_app_dir() / "rockbox-database-backups", C, open_in_file_manager)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _field_label(self, parent: tk.Misc, text: str, hint: str = "") -> None:
        tk.Label(parent, text=text, bg=C["panel"], fg=C["text"], font=self.F.body_b, anchor="w").pack(fill="x", pady=(8, 0))
        if hint:
            tk.Label(parent, text=hint, bg=C["panel"], fg=C["faint"], font=self.F.tiny, anchor="w",
                     justify="left", wraplength=420).pack(fill="x", pady=(1, 5))

    def open_media_tools(self) -> None:
        if self.operation_lock.locked():
            return
        root = self._valid_music_root(silent=True)
        if root is None:
            chosen = filedialog.askdirectory(title="Choose a folder to prepare for PodBox")
            if not chosen:
                return
            root = Path(chosen)
        raw = self.output_size_var.get().strip()
        if not raw.isdigit() or not 64 <= int(raw) <= 2000:
            messagebox.showerror(APP_NAME, "Artwork size must be a whole number between 64 and 2000.")
            return
        if self.media_backup_var.get() and not Path(self.media_backup_path_var.get()).expanduser().is_absolute():
            messagebox.showerror(APP_NAME, "Choose an absolute local backup folder path first.")
            return
        from .media_dialog import MediaDialog
        self._persist_basic_config()
        MediaDialog(self, root, local_app_dir(), C, open_in_file_manager)

    def choose_ffmpeg(self) -> None:
        chosen = filedialog.askopenfilename(title="Select FFmpeg executable",
                    filetypes=[("FFmpeg", "ffmpeg.exe" if os.name == "nt" else "ffmpeg"), ("All files", "*")])
        if chosen:
            self.ffmpeg_path_var.set(chosen)

    def choose_media_backup_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Select a backup folder on this computer",
                                         initialdir=self.media_backup_path_var.get())
        if chosen:
            self.media_backup_path_var.set(chosen)

    def _build_settings_view(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        sf = ScrollFrame(parent, C["bg"], self._register_scrollable)
        sf.grid(row=0, column=0, sticky="nsew")
        outer = tk.Frame(sf.inner, bg=C["bg"], padx=26, pady=20)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="Settings", bg=C["bg"], fg=C["text"], font=self.F.h1, anchor="w").pack(fill="x")
        tk.Label(outer, text="Library location, artwork output and online providers.",
                 bg=C["bg"], fg=C["muted"], font=self.F.small, anchor="w").pack(fill="x", pady=(1, 16))

        grid = tk.Frame(outer, bg=C["bg"])
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1, uniform="settings")
        grid.columnconfigure(1, weight=1, uniform="settings")
        left = tk.Frame(grid, bg=C["bg"])
        right = tk.Frame(grid, bg=C["bg"])
        left.grid(row=0, column=0, sticky="new", padx=(0, 7))
        right.grid(row=0, column=1, sticky="new", padx=(7, 0))

        lib = self._settings_card(left, "Library", "")
        self._field_label(lib, "Music folder", "Folder containing Artist/Album directories.")
        row = tk.Frame(lib, bg=C["panel"])
        row.pack(fill="x")
        FieldEntry(row, self.F, self.music_root_var, placeholder="/Volumes/IPOD/Music", width=40).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", style="Small.TButton", command=self.choose_music_root).pack(
            side="left", padx=(7, 0))

        self._field_label(lib, "Artwork filename", "Used for new artwork; existing artwork keeps its filename.")
        self.output_seg = Segmented(lib, self.F, [("folder.jpg", "folder.jpg"), ("cover.jpg", "cover.jpg")],
                                    self.output_name, command=lambda v: self._mark_settings_dirty())
        self.output_seg.pack(anchor="w")

        self._field_label(lib, "Artwork size", "Square JPEG written to the device.")
        size_row = tk.Frame(lib, bg=C["panel"])
        size_row.pack(anchor="w")
        FieldEntry(size_row, self.F, self.output_size_var, width=6).pack(side="left")
        tk.Label(size_row, text="px", bg=C["panel"], fg=C["muted"], font=self.F.small).pack(
            side="left", padx=(7, 0))
        self.output_size_var.trace_add("write", lambda *_a: self._mark_settings_dirty())

        media_card = self._settings_card(left, "Prepare media for PodBox",
            "Scan hi-res FLAC and artwork sizes. Preview and select files before converting or resizing; find better sources for small artwork.")
        ttk.Checkbutton(media_card, text="Back up originals before converting or resizing",
                        variable=self.media_backup_var).pack(anchor="w")
        self._field_label(media_card, "Local backup folder", "Applies to media conversion and resizing. Rockbox database backups remain mandatory.")
        backup_row = tk.Frame(media_card, bg=C["panel"])
        backup_row.pack(fill="x")
        FieldEntry(backup_row, self.F, self.media_backup_path_var, width=28).pack(side="left", fill="x", expand=True)
        ttk.Button(backup_row, text="Browse…", style="Small.TButton", command=self.choose_media_backup_folder).pack(side="left", padx=(7, 0))
        self._field_label(media_card, "FFmpeg executable", "Optional for scans and artwork; required for FLAC conversion. Leave blank to use PATH.")
        ffmpeg_row = tk.Frame(media_card, bg=C["panel"])
        ffmpeg_row.pack(fill="x")
        FieldEntry(ffmpeg_row, self.F, self.ffmpeg_path_var, width=28, placeholder="ffmpeg.exe / ffmpeg").pack(side="left", fill="x", expand=True)
        ttk.Button(ffmpeg_row, text="Browse…", style="Small.TButton", command=self.choose_ffmpeg).pack(side="left", padx=(7, 0))
        for var in (self.media_backup_var, self.media_backup_path_var, self.ffmpeg_path_var):
            var.trace_add("write", lambda *_a: self._mark_settings_dirty())
        media_buttons = tk.Frame(media_card, bg=C["panel"])
        media_buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(media_buttons, text="Scan and prepare…", command=self.open_media_tools).pack(side="left")
        ttk.Button(media_buttons, text="Open backups", command=lambda: open_in_file_manager(Path(self.media_backup_path_var.get()).expanduser())).pack(side="left", padx=(7, 0))

        store = self._settings_card(left, "Local data", "Index and thumbnails are stored on this computer.")
        self._field_label(store, "Database")
        tk.Label(store, text=str(database_path()), bg=C["panel"], fg=C["muted"], font=self.F.tiny, anchor="w",
                 justify="left", wraplength=390).pack(fill="x")
        self._field_label(store, "Thumbnail cache")
        self.cache_label = tk.Label(store, text=str(thumbnail_root()), bg=C["panel"], fg=C["muted"],
                                    font=self.F.tiny, anchor="w", justify="left", wraplength=390)
        self.cache_label.pack(fill="x")
        brow = tk.Frame(store, bg=C["panel"])
        brow.pack(fill="x", pady=(12, 0))
        ttk.Button(brow, text="Clear cache", style="Small.TButton",
                   command=self.clear_thumbnail_cache).pack(side="left")
        ttk.Button(brow, text="Open folder", style="Small.TButton",
                   command=lambda: open_in_file_manager(local_app_dir())).pack(side="left", padx=(7, 0))
        ttk.Button(store, text="Rebuild database", style="Small.TButton",
                   command=self.rebuild_database).pack(anchor="w", pady=(7, 0))

        device_db = self._settings_card(left, "Rockbox database", "Update the iPod database after editing tags, with verified local backups.")
        ttk.Button(device_db, text="Rockbox database…", command=self.open_rockbox_database).pack(anchor="w")

        src = self._settings_card(right, "Online sources",
                                  "MusicBrainz, Cover Art Archive, Deezer, Apple Music and TheAudioDB work "
                                  "without a key. Last.fm, fanart.tv and Discogs add more artwork, and Discogs "
                                  "adds original years and styles to the tag editor's suggestions.")
        self._field_label(src, "Last.fm API key", "Only the API key is needed, not the shared secret.")
        FieldEntry(src, self.F, self.api_lastfm_key_var, width=42).pack(fill="x")
        self._field_label(src, "fanart.tv API key")
        FieldEntry(src, self.F, self.api_fanart_key_var, width=42).pack(fill="x")
        self._field_label(src, "Discogs personal token", "Discogs → Settings → Developers → Generate new token.")
        secret_row = tk.Frame(src, bg=C["panel"])
        secret_row.pack(fill="x")
        self.secret_entry = FieldEntry(secret_row, self.F, self.api_discogs_token_var, show="•", width=34)
        self.secret_entry.pack(side="left", fill="x", expand=True)
        self.secret_toggle = tk.Label(secret_row, text="Show", bg=C["panel"], fg=C["accent"],
                                      font=self.F.small, cursor="hand2", padx=9)
        self.secret_toggle.pack(side="left")
        self.secret_toggle.bind("<Button-1>", lambda _e: self._toggle_secret())
        for var in (self.api_lastfm_key_var, self.api_fanart_key_var, self.api_discogs_token_var):
            var.trace_add("write", lambda *_a: self._mark_settings_dirty())

        status = tk.Frame(src, bg=C["panel"])
        status.pack(fill="x", pady=(14, 0))
        self.source_rows: dict[str, tuple[Dot, tk.Label]] = {}
        for key, label in (("lastfm", "Last.fm"), ("fanart", "fanart.tv"), ("discogs", "Discogs"),
                           ("mb", "MusicBrainz + Cover Art Archive"), ("deezer", "Deezer + Apple Music"),
                           ("adb", "TheAudioDB")):
            r = tk.Frame(status, bg=C["panel"])
            r.pack(fill="x", pady=2)
            dot = Dot(r, C["faint"], C["panel"], 7)
            dot.pack(side="left")
            tk.Label(r, text=label, bg=C["panel"], fg=C["text"], font=self.F.small).pack(
                side="left", padx=(7, 0))
            state = tk.Label(r, text="", bg=C["panel"], fg=C["muted"], font=self.F.small)
            state.pack(side="left", padx=(6, 0))
            self.source_rows[key] = (dot, state)
        links = tk.Frame(src, bg=C["panel"])
        links.pack(fill="x", pady=(10, 0))
        for text, url in (("Last.fm key ↗", "https://www.last.fm/api/account/create"),
                          ("fanart.tv key ↗", "https://fanart.tv/get-an-api-key/"),
                          ("Discogs token ↗", "https://www.discogs.com/settings/developers")):
            link = tk.Label(links, text=text, bg=C["panel"], fg=C["accent"],
                            font=self.F.small, cursor="hand2")
            link.pack(side="left", padx=(0, 16))
            link.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u))

        footer = tk.Frame(outer, bg=C["bg"])
        footer.pack(fill="x", pady=(4, 14))
        self.settings_state = tk.Label(footer, text="", bg=C["bg"], fg=C["muted"], font=self.F.small)
        self.settings_state.pack(side="left")
        ttk.Button(footer, text="Save settings", style="Accent.TButton", command=self.save_settings).pack(side="right")

    def _build_activity_view(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        header = tk.Frame(parent, bg=C["bg"], padx=20)
        header.grid(row=0, column=0, sticky="ew", pady=(14, 10))
        header.columnconfigure(0, weight=1)
        titles = tk.Frame(header, bg=C["bg"])
        titles.grid(row=0, column=0, sticky="w")
        tk.Label(titles, text="Activity log", bg=C["bg"], fg=C["text"], font=self.F.h1).pack(anchor="w")
        tk.Label(titles, text="What the scanner and the fetch engine did, including why artwork was skipped.",
                 bg=C["bg"], fg=C["muted"], font=self.F.small).pack(anchor="w", pady=(2, 0))
        acts = tk.Frame(header, bg=C["bg"])
        acts.grid(row=0, column=1, sticky="e")
        ttk.Button(acts, text="Copy", command=self._copy_log).pack(side="left")
        ttk.Button(acts, text="Clear", command=self._clear_log).pack(side="left", padx=(8, 0))

        box = tk.Frame(parent, bg=C["panel"], highlightthickness=1, highlightbackground=C["border"])
        box.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 16))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.log_text = tk.Text(box, bg=C["panel"], fg=C["muted"], insertbackground=C["text"], relief="flat",
                                bd=0, highlightthickness=0, font=self.F.mono, wrap="word", padx=16, pady=12,
                                selectbackground=C["accent_lo"], state="disabled")
        vsb = ttk.Scrollbar(box, orient="vertical", style="Panel.Slim.Vertical.TScrollbar", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.log_text.tag_configure("head", foreground=C["text"], font=self.F.mono)
        self.log_text.tag_configure("ok", foreground=C["ok"])
        self.log_text.tag_configure("bad", foreground=C["bad"])
        self.log_text.tag_configure("warn", foreground=C["warn"])
        self.log_text.tag_configure("app", foreground=C["accent_hi"])

    def _build_statusbar(self, parent: tk.Frame) -> None:
        tk.Frame(parent, bg=C["border"], height=1).grid(row=1, column=0, sticky="ew")
        bar = tk.Frame(parent, bg=C["sidebar"], padx=14, pady=5)
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        tk.Label(bar, textvariable=self.status_var, bg=C["sidebar"], fg=C["muted"], font=self.F.small,
                 anchor="w").grid(row=0, column=0, sticky="ew")
        self.progress_label = tk.Label(bar, text="", bg=C["sidebar"], fg=C["faint"], font=self.F.tiny)
        self.progress = ttk.Progressbar(bar, mode="determinate", length=220,
                                        style="Accent.Horizontal.TProgressbar", maximum=100)
        self.cancel_btn = ttk.Button(bar, text="Cancel", style="Small.TButton", command=self.cancel_operation)

    # ------------------------------------------------------------------
    # Input plumbing: wheel routing, shortcuts, drag and drop
    # ------------------------------------------------------------------
    def _register_scrollable(self, widget: tk.Misc, fn: Callable[[int], None]) -> None:
        self._scrollables[str(widget)] = fn

    def _install_wheel_router(self) -> None:
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(seq, self._route_wheel, add="+")

    def _route_wheel(self, event: tk.Event) -> Optional[str]:
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return None
        while widget is not None:
            fn = self._scrollables.get(str(widget))
            if fn:
                fn(wheel_pixels(event))
                return "break"
            widget = getattr(widget, "master", None)
        return None

    def _install_shortcuts(self) -> None:
        def bind(keys: str, fn: Callable[[], Any], guard_text: bool = False) -> None:
            def handler(_event: tk.Event) -> Optional[str]:
                if guard_text and isinstance(self.root.focus_get(), (tk.Entry, tk.Text, ttk.Entry)):
                    return None
                fn()
                return "break"
            mod, key = keys[1:-1].rsplit("-", 1)
            variants = [keys]
            if len(key) == 1 and key.isalpha():
                variants.append(f"<{mod}-{key.upper()}>")
            for seq in variants:
                self.root.bind_all(seq, handler)

        bind(f"<{MOD}-f>", self.focus_search)
        bind(f"<{MOD}-v>", self.paste_image, guard_text=True)
        bind(f"<{MOD}-o>", self.select_manual_image)
        bind(f"<{MOD}-s>", self.save_manual_artwork)
        bind(f"<{MOD}-r>", self.scan_library)
        bind(f"<{MOD}-l>", lambda: self.set_view("activity"))
        bind(f"<{MOD}-comma>", lambda: self.set_view("settings"))
        bind(f"<{MOD}-equal>", lambda: self.set_zoom(self.zoom + 20))
        bind(f"<{MOD}-minus>", lambda: self.set_zoom(self.zoom - 20))

    def _register_drop_target(self, widget: tk.Misc, on_drop: Callable[[Any], None],
                              on_hover: Optional[Callable[[Any, bool], None]] = None) -> None:
        if not DND_AVAILABLE:
            return
        try:
            widget.drop_target_register(DND_FILES, DND_TEXT)  # type: ignore[attr-defined]

            def enter(event: Any) -> str:
                if on_hover:
                    on_hover(event, True)
                return getattr(event, "action", "copy")

            def leave(event: Any) -> str:
                if on_hover:
                    on_hover(event, False)
                return getattr(event, "action", "copy")

            def drop(event: Any) -> str:
                if on_hover:
                    on_hover(event, False)
                on_drop(event)
                return getattr(event, "action", "copy")

            widget.dnd_bind("<<DropEnter>>", enter)  # type: ignore[attr-defined]
            widget.dnd_bind("<<DropPosition>>", enter)  # type: ignore[attr-defined]
            widget.dnd_bind("<<DropLeave>>", leave)  # type: ignore[attr-defined]
            widget.dnd_bind("<<Drop>>", drop)  # type: ignore[attr-defined]
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Views and filters
    # ------------------------------------------------------------------
    def set_health_filter(self, value: str) -> None:
        self.health_filter = value
        self.refresh_health_view()

    def refresh_health_view(self) -> None:
        counts = self.db.health_counts()
        last = int(self.db.get_meta("last_health_scan", "0") or 0)
        if hasattr(self, "nav_health"):
            self.nav_health.set_count(counts.get("all", 0) if last else "-")
        if not hasattr(self, "health_tree"):
            return
        for iid in self.health_tree.get_children():
            self.health_tree.delete(iid)
        labels = HEALTH_LABELS
        for row in self.db.health_issues(HEALTH_CATEGORIES.get(self.health_filter, self.health_filter)):
            item = str(row["artist"])
            if row["album"]:
                item += " - " + str(row["album"])
            self.health_tree.insert("", "end", iid=str(row["id"]),
                                    values=(labels.get(str(row["issue_type"]), str(row["issue_type"])),
                                            item, str(row["details"])))
        dirty = self.db.get_meta("health_dirty", "0") == "1"
        stale = " · re-check recommended" if dirty and last else ""
        if counts.get("all", 0):
            self.health_subtitle.configure(
                text=f"{counts['all']} issue" + ("" if counts['all'] == 1 else "s") +
                     f" found · analyzed {humanize_age(last)}{stale}"
            )
        elif last:
            self.health_subtitle.configure(text=f"No issues found · analyzed {humanize_age(last)}{stale}")
        else:
            self.health_subtitle.configure(text="Analyze tags and artist identities without changing files.")
        self.selected_health_issue_id = None
        self._update_health_detail(None)

    def _on_health_select(self, _event: Any = None) -> None:
        sel = self.health_tree.selection()
        issue = None
        if sel:
            try:
                self.selected_health_issue_id = int(sel[0])
                issue = self.db.get_health_issue(self.selected_health_issue_id)
            except (TypeError, ValueError):
                issue = None
        self._update_health_detail(issue)

    def _update_health_detail(self, issue: Optional[sqlite3.Row]) -> None:
        for btn in (self.health_open_btn, self.health_tag_btn, self.health_merge_btn):
            btn.state(["disabled"])
        if issue is None:
            self.health_detail_title.configure(text="Select an issue")
            self.health_detail_text.configure(text="")
            return
        title = str(issue["artist"])
        if issue["album"]:
            title += " - " + str(issue["album"])
        self.health_detail_title.configure(text=title or "Library issue")
        self.health_detail_text.configure(text=str(issue["details"]))
        if issue["relative_path"]:
            self.health_open_btn.state(["!disabled"])
        if issue["album"] and issue["relative_path"]:
            self.health_tag_btn.state(["!disabled"])
        if str(issue["issue_type"]) == "duplicate_artist":
            self.health_merge_btn.state(["!disabled"])

    def analyze_library_health(self) -> None:
        root = self._valid_music_root()
        if not root:
            return
        if not self.operation_lock.acquire(blocking=False):
            self.toast.show("Another task is still running. Wait for it or cancel it first.", "warn")
            return
        self.cancel_event.clear()
        thorough = self.health_thorough_var.get()
        self._set_busy("health", "Analyzing library health" + (", every track" if thorough else "") + "…")
        self.log_app(f"Health analysis started: {root}" + (" (every track)" if thorough else ""))

        def worker() -> None:
            try:
                scanner = HealthScanner(
                    self.db, root,
                    lambda i, total, name: self.ui_queue.put(("progress", (i, total, f"Checking {name}"))),
                    self.cancel_event, thorough=thorough,
                )
                self.ui_queue.put(("health_done", scanner.scan()))
            except Exception as exc:
                self.ui_queue.put(("error", f"Library health analysis failed:\n{exc}"))
            finally:
                self.operation_lock.release()

        threading.Thread(target=worker, name="health-scan", daemon=True).start()

    def _selected_health_issue(self) -> Optional[sqlite3.Row]:
        return self.db.get_health_issue(self.selected_health_issue_id) if self.selected_health_issue_id else None

    def open_health_folder(self) -> None:
        issue = self._selected_health_issue()
        root = self._valid_music_root(silent=True)
        if not issue or not root or not issue["relative_path"]:
            return
        path = root / Path(str(issue["relative_path"]))
        if path.is_dir():
            open_in_file_manager(path)

    def edit_health_tags(self) -> None:
        issue = self._selected_health_issue()
        root = self._valid_music_root(silent=True)
        if not issue or not root or not issue["album"] or not issue["relative_path"]:
            return
        folder = root / Path(str(issue["relative_path"]))
        if folder.is_dir():
            TagEditorDialog(self, folder, str(issue["artist"]), str(issue["album"]))

    def choose_duplicate_display_name(self) -> None:
        issue = self._selected_health_issue()
        if not issue or str(issue["issue_type"]) != "duplicate_artist":
            return
        try:
            data = json.loads(str(issue["data_json"]) or "{}")
        except Exception:
            data = {}
        aliases = [str(x) for x in data.get("aliases", []) if str(x).strip()]
        paths = [str(x) for x in data.get("paths", []) if str(x).strip()]
        if len(aliases) < 2:
            return

        physical = self.db.items_for_paths(paths, kind="artist") if paths else []
        if len(physical) < 2:
            # Older/stale health data can lack paths. Resolve from the current
            # physical index rather than guessing filesystem paths from names.
            physical = [i for i in self.db.physical_items("artist") if i.artist in aliases]

        win = tk.Toplevel(self.root)
        win.title(f"Resolve duplicate artist - v{APP_VERSION}")
        win.configure(bg=C["panel"])
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        frame = tk.Frame(win, bg=C["panel"], padx=18, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="Choose the canonical artist name", bg=C["panel"], fg=C["text"],
                 font=self.F.title, anchor="w").pack(fill="x")
        tk.Label(
            frame,
            text=("Merge in library only keeps the physical folders unchanged but treats this as a resolved identity.\n"
                  "Fix folders on disk moves the albums into the selected artist folder. Nothing is overwritten."),
            bg=C["panel"], fg=C["muted"], font=self.F.small, anchor="w", justify="left",
            wraplength=520,
        ).pack(fill="x", pady=(3, 12))

        var = tk.StringVar(value=str(data.get("canonical") or choose_display_name(aliases)))
        for name in sorted(aliases, key=str.casefold):
            ttk.Radiobutton(frame, text=name, value=name, variable=var).pack(anchor="w", pady=2)

        retag_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="Also set ALBUMARTIST to the selected name",
            variable=retag_var,
        ).pack(anchor="w", pady=(12, 0))
        tk.Label(
            frame,
            text="Optional. Leave this off if collaborations or special album-artist tags should stay untouched.",
            bg=C["panel"], fg=C["faint"], font=self.F.tiny, anchor="w", justify="left", wraplength=500,
        ).pack(fill="x", pady=(2, 0))

        state_label = tk.Label(frame, text="", bg=C["panel"], fg=C["muted"], font=self.F.small,
                               anchor="w", justify="left", wraplength=500)
        state_label.pack(fill="x", pady=(12, 0))

        buttons = tk.Frame(frame, bg=C["panel"])
        buttons.pack(fill="x", pady=(16, 0))

        def refresh_after_identity_change(*, health_dirty: bool = False) -> None:
            self.db.set_meta("health_dirty", "1" if health_dirty else "0")
            win.destroy()
            self.reload_gallery(reset_scroll=False)
            self.refresh_counts()
            self.refresh_health_view()

        def merge_library_only() -> None:
            canonical = var.get().strip()
            if not canonical:
                return
            self.db.set_artist_auto_merge(aliases, True)
            self.db.set_artist_aliases(aliases, canonical)
            # This is an explicit resolution. Remove the existing Health row now;
            # future Health scans also suppress groups whose aliases all map to the
            # same explicit canonical identity.
            self.db.delete_health_issue(int(issue["id"]))
            refresh_after_identity_change(health_dirty=False)
            self.toast.show(f"Merged in the library as {canonical}", "ok")

        def keep_separate() -> None:
            self.db.set_artist_auto_merge(aliases, False)
            self.db.delete_health_issue(int(issue["id"]))
            refresh_after_identity_change(health_dirty=False)
            self.toast.show("These artist folders will stay separate in the library.", "ok")

        def finish_disk_merge(result: dict[str, Any], canonical: str) -> None:
            try:
                if not win.winfo_exists():
                    return
            except tk.TclError:
                return
            if not result.get("ok"):
                for btn in action_buttons:
                    btn.state(["!disabled"])
                conflicts = result.get("conflicts") or []
                detail = str(result.get("error") or "Could not merge the artist folders.")
                if conflicts:
                    detail += "\n\nConflicts:\n" + "\n".join(f"- {x}" for x in conflicts[:12])
                    if len(conflicts) > 12:
                        detail += f"\n- ... and {len(conflicts) - 12} more"
                state_label.configure(text="No files were changed.", fg=C["bad"])
                messagebox.showerror(APP_NAME, detail, parent=win)
                return

            recorded = json.loads(self.db.get_meta("rockbox_folder_moves", "[]"))
            recorded.extend(result.get("folder_moves") or [])
            self.db.set_meta("rockbox_folder_moves", json.dumps(recorded))
            self.db.set_artist_auto_merge(aliases, True)
            self.db.set_artist_aliases(aliases, canonical)
            self.db.delete_health_issue(int(issue["id"]))
            self.db.set_meta("health_dirty", "1")
            preserved = result.get("preserved_artwork") or []
            tag_failed = result.get("tag_failed") or []
            self.log_app(
                f"Artist folders consolidated as {canonical}: "
                f"{result.get('moved_entries', 0)} entries moved; "
                f"{len(result.get('removed_sources') or [])} source folder(s) removed"
            )
            if preserved:
                self.log_app("Preserved alternate artist artwork: " + ", ".join(map(str, preserved)))
            if tag_failed:
                for line in tag_failed:
                    self.log_app("ALBUMARTIST update failed: " + str(line))

            win.destroy()
            suffix = ""
            if tag_failed:
                suffix = f"; {len(tag_failed)} tag write(s) failed - see Activity log"
            self.toast.show(f"Artist folders fixed on disk{suffix}", "warn" if tag_failed else "ok", ms=5500)
            # A fresh fast scan removes the old physical rows and makes the fixed
            # filesystem state immediately visible. Health can be rerun afterwards.
            self.scan_library()

        def set_merge_state(text: str) -> None:
            try:
                if win.winfo_exists():
                    state_label.configure(text=text, fg=C["muted"])
            except tk.TclError:
                pass

        def fix_on_disk() -> None:
            root = self._valid_music_root(silent=True)
            canonical = var.get().strip()
            if not root or not canonical:
                return
            if len(physical) < 2:
                messagebox.showerror(APP_NAME, "The physical artist folders could not be resolved from the current index.", parent=win)
                return

            target_row = next((i for i in physical if i.artist == canonical), None)
            if target_row is None:
                target_row = next((i for i in physical if i.artist.casefold() == canonical.casefold()), None)
            if target_row is None:
                messagebox.showerror(APP_NAME, "Choose one of the existing physical folder names as the canonical artist.", parent=win)
                return
            source_names = [i.artist for i in physical if i.relative_path != target_row.relative_path]
            extra = "\n\nALBUMARTIST will also be changed on every track under the merged artist." if retag_var.get() else ""
            if not messagebox.askyesno(
                APP_NAME,
                "Fix this duplicate on the music device?\n\n"
                f"Keep folder: {canonical}\n"
                f"Merge from: {', '.join(source_names)}\n\n"
                "Album folders and other files are moved into the selected folder. "
                "Existing destination names are never overwritten; the operation stops before changing anything if a conflict is found. "
                "Different artist artwork is preserved under an alternate filename."
                + extra,
                parent=win,
            ):
                return
            if not self.operation_lock.acquire(blocking=False):
                self.toast.show("Another task is still running. Wait for it to finish first.", "warn")
                return

            retag = bool(retag_var.get())
            for btn in action_buttons:
                btn.state(["disabled"])
            state_label.configure(text="Checking folders and applying the fix...", fg=C["muted"])

            def worker() -> None:
                try:
                    result = consolidate_artist_folders(
                        root,
                        physical,
                        canonical,
                        output_name=self.output_name,
                        retag_albumartist=retag,
                        progress=lambda text: self.ui_queue.put(("ui_callback", (set_merge_state, (text,)))),
                    )
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
                finally:
                    self.operation_lock.release()
                # ui_callback historically carries callback + positional args only;
                # use a tiny closure here rather than touching Tk from the worker.
                self.ui_queue.put(("ui_callback", (finish_disk_merge, (result, canonical))))

            threading.Thread(target=worker, name="artist-folder-merge", daemon=True).start()

        # Keep action buttons so the background operation can disable/re-enable all.
        keep_btn = ttk.Button(buttons, text="Keep separate", style="Small.TButton", command=keep_separate)
        keep_btn.pack(side="left")
        cancel_btn = ttk.Button(buttons, text="Cancel", style="Ghost.TButton", command=win.destroy)
        cancel_btn.pack(side="right")
        fix_btn = ttk.Button(buttons, text="Fix folders on disk", style="Accent.TButton", command=fix_on_disk)
        fix_btn.pack(side="right", padx=(0, 7))
        library_btn = ttk.Button(buttons, text="Merge in library", style="SmallAccent.TButton", command=merge_library_only)
        library_btn.pack(side="right", padx=(0, 7))
        action_buttons = (keep_btn, cancel_btn, fix_btn, library_btn)

        # Some Windows/Tk DPI combinations size a newly created non-resizable
        # Toplevel before the final button row has contributed its requested
        # height. The result is a clipped dialog with no visible action buttons.
        # Force a final geometry pass after *all* widgets are present, then add
        # a little breathing room for platform-specific font metrics.
        win.update_idletasks()
        req_w = max(620, win.winfo_reqwidth() + 12)
        req_h = max(360, win.winfo_reqheight() + 18)
        try:
            root_x = self.root.winfo_rootx()
            root_y = self.root.winfo_rooty()
            root_w = self.root.winfo_width()
            root_h = self.root.winfo_height()
            x = max(0, root_x + (root_w - req_w) // 2)
            y = max(0, root_y + (root_h - req_h) // 2)
            win.geometry(f"{req_w}x{req_h}+{x}+{y}")
        except tk.TclError:
            win.geometry(f"{req_w}x{req_h}")
        win.minsize(req_w, req_h)
        win.resizable(False, False)

    def edit_selected_tags(self) -> None:
        if self.selected_item_id is None:
            return
        item = self.db.get_item(self.selected_item_id)
        folder = self.item_folder(item) if item else None
        if not item or item.kind != "album" or not folder:
            return
        TagEditorDialog(self, folder, item.artist, item.album)

    def set_view(self, view: str) -> None:
        self.view = view
        {"library": self.library_view, "health": self.health_view, "settings": self.settings_view,
         "activity": self.activity_view}[view].tkraise()
        for key, nav in self.nav_status.items():
            nav.set_active(view == "library" and key == self.status_filter)
        self.nav_health.set_active(view == "health")
        self.nav_activity.set_active(view == "activity")
        self.nav_settings.set_active(view == "settings")
        if view == "health":
            self.refresh_health_view()
        if view == "settings":
            self._refresh_settings_info()
        if view == "activity":
            self.log_text.see("end")

    def set_status_filter(self, key: str) -> None:
        self.status_filter = key
        self.set_view("library")
        self.reload_gallery(reset_scroll=True)
        self._persist_basic_config()

    def set_kind_filter(self, value: str) -> None:
        self.kind_filter = value
        self.refresh_counts()
        self.reload_gallery(reset_scroll=True)
        self._persist_basic_config()

    def focus_search(self) -> None:
        self.set_view("library")
        self.search_entry.entry.focus_set()
        self.search_entry.entry.select_range(0, "end")

    def _on_search_changed(self) -> None:
        if self._search_job:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(220, self._apply_search_now)

    def _apply_search_now(self) -> None:
        if self._search_job:
            self.root.after_cancel(self._search_job)
            self._search_job = None
        self.reload_gallery(reset_scroll=True)

    def _on_zoom(self, value: str) -> None:
        size = int(round(float(value) / 10.0) * 10)
        if size != self.zoom:
            self.set_zoom(size, from_scale=True)

    def set_zoom(self, size: int, from_scale: bool = False) -> None:
        size = max(ZOOM_MIN, min(ZOOM_MAX, int(size)))
        if size == self.zoom:
            return
        anchor = self._first_visible_index()
        self.zoom = size
        if not from_scale:
            self.zoom_scale.set(size)
        self._text_cache.clear()
        self._relayout(anchor)
        self.config["zoom"] = size

    def refresh_counts(self) -> None:
        c = self.db.counts(self.kind_filter)
        self._counts = c
        self.nav_status["All"].set_count(c["total"])
        self.nav_status["Existing"].set_count(c["existing"])
        self.nav_status["Missing"].set_count(c["missing"])
        self.nav_status["Problems"].set_count(c["problems"])
        if hasattr(self, "nav_health"):
            last_health = int(self.db.get_meta("last_health_scan", "0") or 0)
            self.nav_health.set_count(self.db.health_counts().get("all", 0) if last_health else "-")
        self.fetch_btn.configure(text=f"Fetch missing ({c['missing']})" if c["missing"] else "Fetch missing")
        self._update_root_card()

    def _update_header(self) -> None:
        title = VIEW_TITLES.get(self.status_filter, "Library")
        if self.kind_filter != "All" and self.status_filter == "All":
            title = self.kind_filter
        self.view_title.configure(text=title)
        n = len(self.items)
        parts = [f"{n} item" + ("" if n == 1 else "s")]
        if self.kind_filter != "All" and self.status_filter != "All":
            parts.append(self.kind_filter.lower())
        search = self.search_var.get().strip()
        if search:
            parts.append(f"matching '{search}'")
        self.view_subtitle.configure(text="  ·  ".join(parts))

    # ------------------------------------------------------------------
    # Gallery: a single canvas that only draws the cards on screen
    # ------------------------------------------------------------------
    def reload_gallery(self, *, reset_scroll: bool = False, reset_page: bool = False) -> None:
        reset_scroll = reset_scroll or reset_page
        anchor = None if reset_scroll else self._first_visible_index()
        self.items = self.db.query_all(kind_filter=self.kind_filter, status_filter=self.status_filter,
                                       search=self.search_var.get())
        self.index_by_id = {it.id: i for i, it in enumerate(self.items)}
        self.album_counts = self.db.album_counts()
        self.hover_index = None
        self._relayout(anchor, top=reset_scroll)
        self._update_header()

    def _relayout(self, anchor: Optional[int] = None, top: bool = False) -> None:
        for w in getattr(self, "_empty_widgets", []):
            w.destroy()
        self._empty_widgets: list[tk.Misc] = []
        self.gallery.delete("all")
        self.drawn = set()
        self._layout()
        if top or anchor is None:
            self.gallery.yview_moveto(0)
        else:
            self._scroll_to_index(anchor, align="top")
        if not self.items:
            self._draw_empty_state()
        self._render_visible()

    def _layout(self) -> None:
        L = self.L
        width = max(200, self.gallery.winfo_width())
        height = max(200, self.gallery.winfo_height())
        pad = 6
        lt = self.F.body_b.metrics("linespace")
        ls = self.F.small.metrics("linespace")
        margin, gap = 10, 18
        avail = width - 2 * margin
        # Artwork-first media tiles: almost all of the tile width is useful artwork.
        target = self.zoom + 2 * pad
        cols = max(1, int(round((avail + gap) / (target + gap))))
        S = max(82, int((avail - (cols - 1) * gap) / cols) - 2 * pad)
        if S != L.S:
            old = L.S
            self.static_photos = {k: v for k, v in self.static_photos.items()
                                  if not (k[0] == "bg" or (k[0] == "ph" and k[3] == old))}
            self._text_cache.clear()
        L.S, L.pad, L.lt, L.cols, L.gap = S, pad, lt, cols, gap
        L.card_w = S + 2 * pad
        L.card_h = pad + S + 8 + lt + 1 + ls + pad
        L.x0 = margin
        L.row_gap = 12
        L.top = 2
        rows = (len(self.items) + L.cols - 1) // L.cols
        L.total_h = L.top + rows * (L.card_h + L.row_gap) + 24
        self.gallery.configure(scrollregion=(0, 0, width, max(L.total_h, height)))

    def _card_xy(self, idx: int) -> tuple[float, float]:
        L = self.L
        row, col = divmod(idx, L.cols)
        return L.x0 + col * (L.card_w + L.gap), L.top + row * (L.card_h + L.row_gap)

    def _first_visible_index(self) -> Optional[int]:
        if not self.items or not self.L.cols:
            return None
        row_h = self.L.card_h + self.L.row_gap
        if row_h <= 0:
            return None
        row = int(max(0, self.gallery.canvasy(0) - self.L.top + row_h * 0.35) // row_h)
        return min(len(self.items) - 1, row * self.L.cols)

    def _scroll_to_index(self, idx: int, align: str = "nearest") -> None:
        if not self.items or self.L.total_h <= 0:
            return
        _x, y = self._card_xy(idx)
        view_h = self.gallery.winfo_height()
        top = self.gallery.canvasy(0)
        total = max(self.L.total_h, view_h)
        if align == "top":
            target = y - self.L.top
        elif y - 8 < top:
            target = y - 8
        elif y + self.L.card_h + 8 > top + view_h:
            target = y + self.L.card_h + 8 - view_h
        else:
            return
        self.gallery.yview_moveto(max(0.0, target) / total)

    def _on_gallery_yview(self, first: str, last: str) -> None:
        self.gallery_vsb.set(first, last)
        needed = not (float(first) <= 0.0 and float(last) >= 1.0)
        if needed != bool(self.gallery_vsb.winfo_ismapped()):
            if needed:
                self.gallery_vsb.grid()
            else:
                self.gallery_vsb.grid_remove()
        self._schedule_render()

    def _scroll_gallery(self, px: int) -> None:
        if self.L.total_h > self.gallery.winfo_height():
            self.gallery.yview_scroll(px, "units")
        self.tooltip.hide()

    def _schedule_render(self) -> None:
        if not self._render_job:
            self._render_job = self.root.after_idle(self._render_visible)

    def _render_visible(self) -> None:
        self._render_job = None
        n = len(self.items)
        if not n:
            self.thumbs.wanted = set()
            return
        L = self.L
        row_h = L.card_h + L.row_gap
        top = self.gallery.canvasy(0)
        height = self.gallery.winfo_height()
        first = max(0, int((top - L.top) // row_h) - 1)
        last = int((top + height - L.top) // row_h) + 1
        needed = set(range(first * L.cols, min(n, (last + 1) * L.cols)))
        for idx in self.drawn - needed:
            self.gallery.delete(f"c{idx}")
        self.thumbs.wanted = {self.items[i].id for i in needed}
        for idx in sorted(needed - self.drawn):
            self._draw_card(idx)
        self.drawn = needed

    def _card_bg(self, state: str) -> ImageTk.PhotoImage:
        L = self.L
        key = ("bg", state, L.card_w, L.card_h)
        photo = self.static_photos.get(key)
        if photo is None:
            fill, border, bw = {
                "normal": (C["bg"], C["bg"], 0),
                "hover": (C["bg"], C["border_hi"], 1),
                "selected": (C["bg"], C["accent"], 2),
            }[state]
            img = rounded_panel(int(L.card_w), int(L.card_h), fill, C["bg"], 7, border, bw)
            photo = ImageTk.PhotoImage(img)
            self.static_photos[key] = photo
        return photo

    def _placeholder(self, kind: str, variant: str, size: Optional[int] = None, bg: Optional[str] = None) -> ImageTk.PhotoImage:
        size = size or self.L.S
        bg = bg or C["bg"]
        key = ("ph", kind, variant, size, bg)
        photo = self.static_photos.get(key)
        if photo is None:
            if variant == "problem":
                fill, glyph = blend(C["input"], C["bad"], 0.14), blend(C["border_hi"], C["bad"], 0.45)
            else:
                fill, glyph = C["input"], C["border_hi"]
            photo = ImageTk.PhotoImage(placeholder_art(kind, variant, size, fill, glyph, bg))
            self.static_photos[key] = photo
        return photo

    def _truncate(self, text: str, width: float, font_key: str) -> str:
        key = (text, int(width), font_key)
        cached = self._text_cache.get(key)
        if cached is not None:
            return cached
        font = self.F.body_b if font_key == "b" else self.F.small
        result = text
        if font.measure(text) > width:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.measure(text[:mid].rstrip() + "…") <= width:
                    lo = mid
                else:
                    hi = mid - 1
            result = text[:lo].rstrip() + "…"
        if len(self._text_cache) > 5000:
            self._text_cache.clear()
        self._text_cache[key] = result
        return result

    def _subtitle(self, item: LibraryItem) -> str:
        if item.kind == "album":
            return item.artist
        n = self.album_counts.get(item.artist, 0)
        return f"Artist  ·  {n} album" + ("" if n == 1 else "s") if n else "Artist"

    def _card_state(self, idx: int) -> str:
        item = self.items[idx]
        if item.id == self.selected_item_id:
            return "selected"
        return "hover" if idx == self.hover_index else "normal"

    def _draw_card(self, idx: int) -> None:
        item = self.items[idx]
        L = self.L
        S, pad = L.S, L.pad
        x, y = self._card_xy(idx)
        tag = f"c{idx}"
        g = self.gallery
        g.create_image(x, y, image=self._card_bg(self._card_state(idx)), anchor="nw", tags=(tag, f"bg{idx}"))
        g.create_image(x + pad, y + pad, image=self._thumb_for(item), anchor="nw", tags=(tag, f"img{idx}"))
        if item.status != "Existing":
            color = C["bad"] if item.problem else C["warn"]
            if item.kind == "artist":
                bx, by = x + pad + S / 2 + S * 0.35, y + pad + S / 2 - S * 0.35
            else:
                bx, by = x + pad + S - 11, y + pad + 11
            g.create_oval(bx - 6, by - 6, bx + 6, by + 6, fill=color, outline=C["bg"], width=2, tags=(tag,))
        ty = y + pad + S + 9
        if item.kind == "artist":
            tx, anchor = x + L.card_w / 2, "n"
        else:
            tx, anchor = x + pad, "nw"
        g.create_text(tx, ty, text=self._truncate(item.title, S, "b"), anchor=anchor,
                      fill=C["text"], font=self.F.body_b, tags=(tag,))
        g.create_text(tx, ty + L.lt + 1, text=self._truncate(self._subtitle(item), S, "s"), anchor=anchor,
                      fill=C["muted"], font=self.F.small, tags=(tag,))

    def _thumb_for(self, item: LibraryItem) -> ImageTk.PhotoImage:
        S = self.L.S
        if not item.artwork_exists and not item.artwork_relative_path:
            return self._placeholder(item.kind, "problem" if item.problem else "missing")
        key = (item.id, item.artwork_mtime_ns, S)
        entry = self.photo_cache.get(item.id)
        if entry and entry[0] == key:
            self.photo_cache.move_to_end(item.id)
            return entry[1]
        if key in self._thumb_errors:
            return self._placeholder(item.kind, "missing")
        path = self.artwork_path(item)
        if path:
            self.thumbs.wanted.add(item.id)
            self.thumbs.request(item, path, S, C["bg"],
                                lambda item_id, k, result: self.ui_queue.put(("thumb", (item_id, k, result))))
        return self._placeholder(item.kind, "loading")

    def _on_thumb(self, item_id: int, key: tuple[int, int, int], result: Any) -> None:
        if result is None:
            return
        idx = self.index_by_id.get(item_id)
        item = self.items[idx] if idx is not None and idx < len(self.items) else None
        if isinstance(result, Image.Image):
            if key[2] != self.L.S:
                return
            photo = ImageTk.PhotoImage(result)
            self.photo_cache[item_id] = (key, photo)
            self.photo_cache.move_to_end(item_id)
            self._trim_photo_cache()
        else:
            self._thumb_errors.add(key)
            photo = self._placeholder(item.kind if item else "album", "missing")
        if idx is not None and idx in self.drawn and item and item.id == item_id:
            self.gallery.itemconfigure(f"img{idx}", image=photo)

    def _trim_photo_cache(self) -> None:
        if len(self.photo_cache) <= PHOTO_CACHE_LIMIT:
            return
        visible = {self.items[i].id for i in self.drawn if i < len(self.items)}
        for item_id in list(self.photo_cache.keys()):
            if len(self.photo_cache) <= PHOTO_CACHE_LIMIT:
                break
            if item_id not in visible:
                del self.photo_cache[item_id]

    def _redraw_card(self, idx: int) -> None:
        if idx in self.drawn:
            self.gallery.delete(f"c{idx}")
            self._draw_card(idx)

    def _set_card_state(self, idx: Optional[int]) -> None:
        if idx is not None and idx in self.drawn and idx < len(self.items):
            self.gallery.itemconfigure(f"bg{idx}", image=self._card_bg(self._card_state(idx)))

    def update_item_in_gallery(self, item_id: int) -> None:
        """Refresh one card in place (after a save or fetch) without reshuffling the grid."""
        item = self.db.get_item(item_id)
        idx = self.index_by_id.get(item_id)
        if item is None or idx is None:
            return
        self.items[idx] = item
        self.photo_cache.pop(item_id, None)
        self._redraw_card(idx)

    def _draw_empty_state(self) -> None:
        g = self.gallery
        w, h = max(300, g.winfo_width()), max(300, g.winfo_height())
        search = self.search_var.get().strip()
        button: Optional[tuple[str, Callable[[], None]]] = None
        if not self._valid_music_root(silent=True):
            title = "Choose your music folder"
            body = "Point the app at the Music folder on your iPod. The scan only reads folder names, so it's quick."
            button = ("Choose folder…", self.choose_music_root)
        elif self.db.counts()["total"] == 0:
            title = "Library not scanned yet"
            body = "Scan the music folder to list every artist and album with its artwork status."
            button = ("Scan library", self.scan_library)
        elif search:
            title = "No matches"
            body = f"Nothing matches '{search}' with the current filters."
            button = ("Clear search", lambda: self.search_var.set(""))
        elif self.status_filter == "Missing":
            title, body = "Nothing missing", "Every artist and album in this view has artwork."
        elif self.status_filter == "Problems":
            title, body = "No problems", "Fetch failures and skipped folders show up here."
        else:
            title, body = "Nothing here", "No items match the current filters."
        cy = h * 0.40
        g.create_text(w / 2, cy, text=title, fill=C["text"], font=self.F.title)
        g.create_text(w / 2, cy + 30, text=body, fill=C["muted"], font=self.F.body, width=min(440, w - 40),
                      justify="center")
        if button:
            btn = ttk.Button(g, text=button[0], style="Accent.TButton", command=button[1])
            g.create_window(w / 2, cy + 82, window=btn)
            self._empty_widgets.append(btn)

    # ------------------------------------------------------------------
    # Gallery mouse + keyboard
    # ------------------------------------------------------------------
    def _bind_gallery_events(self) -> None:
        g = self.gallery
        g.bind("<Configure>", self._on_gallery_resize)
        g.bind("<Motion>", self._on_gallery_motion)
        g.bind("<Leave>", lambda _e: self._set_hover(None))
        g.bind("<Button-1>", self._on_gallery_click)
        g.bind("<Double-Button-1>", self._on_gallery_double)
        for seq in (("<Button-2>", "<Button-3>", "<Control-Button-1>") if IS_MAC else ("<Button-3>",)):
            g.bind(seq, self._on_gallery_context)
        g.bind("<Left>", lambda _e: self.move_selection(-1))
        g.bind("<Right>", lambda _e: self.move_selection(1))
        g.bind("<Up>", lambda _e: self.move_selection(-self.L.cols))
        g.bind("<Down>", lambda _e: self.move_selection(self.L.cols))
        g.bind("<Home>", lambda _e: self.select_index(0))
        g.bind("<End>", lambda _e: self.select_index(len(self.items) - 1))
        g.bind("<Prior>", lambda _e: self.move_selection(-self.L.cols * 3))
        g.bind("<Next>", lambda _e: self.move_selection(self.L.cols * 3))
        g.bind("<Return>", lambda _e: self.save_manual_artwork() if self.pending_image else self.open_picker())
        g.bind("<Escape>", lambda _e: self.clear_pending())
        self._register_drop_target(g, self._on_gallery_drop, self._on_gallery_drag)

    def _on_gallery_resize(self, _event: tk.Event) -> None:
        if self._relayout_job:
            self.root.after_cancel(self._relayout_job)
        anchor = self._first_visible_index()
        self._relayout_job = self.root.after(40, lambda: self._after_resize(anchor))

    def _after_resize(self, anchor: Optional[int]) -> None:
        self._relayout_job = None
        self._text_cache.clear()
        self._relayout(anchor)

    def _hit_test(self, x: int, y: int) -> Optional[int]:
        if not self.items:
            return None
        L = self.L
        cx, cy = self.gallery.canvasx(x), self.gallery.canvasy(y)
        step_x, step_y = L.card_w + L.gap, L.card_h + L.row_gap
        col = int((cx - L.x0) // step_x)
        row = int((cy - L.top) // step_y)
        if col < 0 or row < 0 or col >= L.cols:
            return None
        if (cx - L.x0) - col * step_x > L.card_w or (cy - L.top) - row * step_y > L.card_h:
            return None
        idx = row * L.cols + col
        return idx if idx < len(self.items) else None

    def _set_hover(self, idx: Optional[int]) -> None:
        if idx == self.hover_index:
            return
        old, self.hover_index = self.hover_index, idx
        self._set_card_state(old)
        self._set_card_state(idx)
        self.gallery.configure(cursor="hand2" if idx is not None else "")
        self.tooltip.hide()

    def _on_gallery_motion(self, event: tk.Event) -> None:
        idx = self._hit_test(event.x, event.y)
        changed = idx != self.hover_index
        self._set_hover(idx)
        if changed and idx is not None:
            item = self.items[idx]
            lines = []
            if self._truncate(item.title, self.L.S, "b") != item.title:
                lines.append(item.title)
            if item.kind == "album" and self._truncate(item.artist, self.L.S, "s") != item.artist:
                lines.append(item.artist)
            if item.problem:
                lines.append(friendly_reason(item.problem))
            elif not item.artwork_exists:
                lines.append("No artwork yet")
            if lines:
                self.tooltip.schedule("\n".join(lines), event.x_root, event.y_root)

    def _on_gallery_click(self, event: tk.Event) -> None:
        self.gallery.focus_set()
        self.tooltip.hide()
        idx = self._hit_test(event.x, event.y)
        if idx is not None:
            self.select_index(idx, scroll=False)

    def _on_gallery_double(self, event: tk.Event) -> None:
        idx = self._hit_test(event.x, event.y)
        if idx is not None:
            self.select_index(idx, scroll=False)
            self.open_picker()

    def _on_gallery_context(self, event: tk.Event) -> None:
        idx = self._hit_test(event.x, event.y)
        if idx is None:
            return
        self.select_index(idx, scroll=False)
        item = self.items[idx]
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Search online…", command=self.open_picker)
        menu.add_command(label="Choose image file…", command=self.select_manual_image)
        menu.add_command(label=f"Paste image  ({MOD_LABEL}V)", command=self.paste_image)
        if not item.artwork_exists:
            menu.add_command(label="Auto-fetch", command=self.fetch_selected_item)
        if item.kind == "album":
            menu.add_command(label="Edit album tags…", command=self.edit_selected_tags)
        menu.add_separator()
        menu.add_command(label="Open folder", command=self.open_selected_folder)
        menu.add_command(label="Copy folder path", command=self.copy_selected_path)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _drop_target_index(self, event: Any) -> Optional[int]:
        try:
            x = int(event.x_root) - self.gallery.winfo_rootx()
            y = int(event.y_root) - self.gallery.winfo_rooty()
        except Exception:
            return None
        return self._hit_test(x, y)

    def _on_gallery_drag(self, event: Any, inside: bool) -> None:
        self._set_hover(self._drop_target_index(event) if inside else None)

    def _on_gallery_drop(self, event: Any) -> None:
        idx = self._drop_target_index(event)
        if idx is not None:
            self.select_index(idx, scroll=False)
        self.handle_drop_data(getattr(event, "data", ""))

    def select_index(self, idx: int, scroll: bool = True) -> None:
        if not self.items:
            return
        idx = max(0, min(len(self.items) - 1, idx))
        self.select_item(self.items[idx].id)
        if scroll:
            self._scroll_to_index(idx)

    def move_selection(self, delta: int) -> str:
        if not self.items:
            return "break"
        current = self.index_by_id.get(self.selected_item_id or -1)
        if current is None:
            self.select_index(0)
        else:
            self.select_index(current + delta)
        return "break"

    # ------------------------------------------------------------------
    # Music root, device status, settings
    # ------------------------------------------------------------------
    def _update_root_card(self) -> None:
        value = self.music_root_var.get().strip()
        if not value:
            self.root_name_label.configure(text="No folder chosen")
            self.root_path_label.configure(text="")
            self.scan_age_label.configure(text="")
            return
        path = Path(value).expanduser()
        self.root_name_label.configure(text=path.name or str(path))
        text = str(path)
        try:
            text = "~/" + path.relative_to(Path.home()).as_posix()
        except ValueError:
            pass
        if len(text) > 34:
            parts = Path(text).parts
            text = "…/" + "/".join(parts[-2:]) if len(parts) > 2 else text
        self.root_path_label.configure(text=text)
        last = int(self.db.get_meta("last_scan", "0") or 0)
        self.scan_age_label.configure(text=f"Scanned {humanize_age(last)}" if last else "Not scanned yet")

    def _poll_device(self) -> None:
        value = self.music_root_var.get().strip()
        if not value:
            self.device_dot.set_color(C["faint"])
            self.device_label.configure(text="MUSIC FOLDER", fg=C["faint"])
        elif Path(value).expanduser().is_dir():
            self.device_dot.set_color(C["ok"])
            self.device_label.configure(text="MUSIC FOLDER", fg=C["faint"])
        else:
            self.device_dot.set_color(C["bad"])
            self.device_label.configure(text="NOT CONNECTED", fg=C["bad"])
        self._update_root_card()
        self.root.after(3000, self._poll_device)

    def choose_music_root(self) -> None:
        initial = self.music_root_var.get().strip() or str(Path.home())
        chosen = filedialog.askdirectory(title="Select the music folder", initialdir=initial)
        if not chosen:
            return
        changed = os.path.normpath(chosen) != os.path.normpath(self.music_root_var.get().strip() or ".")
        self.music_root_var.set(chosen)
        self._persist_basic_config()
        self._update_root_card()
        self._poll_device_once()
        if changed or self.db.counts()["total"] == 0:
            self.selected_item_id = None
            self.show_detail(None)
            self.scan_library()

    def _poll_device_once(self) -> None:
        value = self.music_root_var.get().strip()
        ok = bool(value) and Path(value).expanduser().is_dir()
        self.device_dot.set_color(C["ok"] if ok else (C["bad"] if value else C["faint"]))

    def output_size(self) -> int:
        try:
            value = int(self.output_size_var.get().strip())
        except ValueError:
            return DEFAULT_OUTPUT_SIZE
        return value if 64 <= value <= 2000 else DEFAULT_OUTPUT_SIZE

    def _persist_basic_config(self) -> None:
        self.config["music_root"] = self.music_root_var.get().strip()
        self.config["output_name"] = self.output_name
        self.config["output_size"] = self.output_size()
        self.config["media_backup"] = self.media_backup_var.get()
        self.config["media_backup_path"] = self.media_backup_path_var.get().strip()
        self.config["ffmpeg_path"] = self.ffmpeg_path_var.get().strip()
        self.config["page_size"] = int(self.config.get("page_size") or DEFAULT_PAGE_SIZE)
        self.config["zoom"] = self.zoom
        self.config["health_thorough"] = bool(self.health_thorough_var.get())
        self.config["kind_filter"] = self.kind_filter
        self.config["status_filter"] = self.status_filter
        try:
            save_gui_config(self.config)
        except Exception:
            pass

    def _mark_settings_dirty(self) -> None:
        if not self._settings_loading:
            self.settings_state.configure(text="Unsaved changes", fg=C["warn"])

    def _toggle_secret(self) -> None:
        showing = self.secret_toggle.cget("text") == "Hide"
        self.secret_entry.set_show("•" if showing else "")
        self.secret_toggle.configure(text="Show" if showing else "Hide")

    def _load_credentials_into_ui(self) -> None:
        self._settings_loading = True
        creds = engine.load_credentials()
        self.api_lastfm_key_var.set(creds.lastfm_api_key)
        self.api_discogs_token_var.set(creds.discogs_token)
        self.api_fanart_key_var.set(creds.fanart_api_key)
        self._known_root = self.music_root_var.get().strip()
        self.root.after_idle(lambda: setattr(self, "_settings_loading", False))

    def _refresh_settings_info(self) -> None:
        creds = engine.load_credentials()
        states = {
            "lastfm": (creds.has_lastfm, "enabled" if creds.has_lastfm else "no key, skipped"),
            "fanart": (creds.has_fanart, "enabled" if creds.has_fanart else "no key, skipped"),
            "discogs": (creds.has_discogs, "enabled" if creds.has_discogs else "no token, skipped"),
            "mb": (True, "always on, no key needed"),
            "deezer": (True, "always on, no key needed"),
            "adb": (True, "always on, public key"),
        }
        for key, (on, text) in states.items():
            dot, label = self.source_rows[key]
            dot.set_color(C["ok"] if on else C["faint"])
            label.configure(text=text)
        self.cache_label.configure(text=f"{thumbnail_root()}  ·  {format_bytes(self.thumbs.disk_usage())}")

    def save_settings(self) -> None:
        raw = self.output_size_var.get().strip()
        if not raw.isdigit() or not 64 <= int(raw) <= 2000:
            messagebox.showerror(APP_NAME, "Artwork size must be a whole number between 64 and 2000.")
            return
        creds = engine.Credentials(
            lastfm_api_key=self.api_lastfm_key_var.get().strip(),
            fanart_api_key=self.api_fanart_key_var.get().strip(),
            discogs_token=self.api_discogs_token_var.get().strip(),
        )
        engine.save_credentials(creds)
        self.output_name = self.output_seg.value
        self._persist_basic_config()
        self.settings_state.configure(text="Saved", fg=C["ok"])
        self._refresh_settings_info()
        self.toast.show("Settings saved", "ok")
        new_root = self.music_root_var.get().strip()
        if os.path.normpath(new_root or ".") != os.path.normpath(self._known_root or "."):
            self._known_root = new_root
            self._update_root_card()
            if self._valid_music_root(silent=True):
                self.scan_library()

    def _valid_music_root(self, *, silent: bool = False) -> Optional[Path]:
        value = self.music_root_var.get().strip()
        if not value:
            if not silent:
                self.toast.show("Choose your music folder first.", "warn")
            return None
        path = Path(value).expanduser()
        if not path.is_dir():
            if not silent:
                messagebox.showerror(APP_NAME, f"The music folder isn't available:\n{path}\n\n"
                                               "Is the iPod connected and mounted?")
            return None
        return path.resolve()

    def item_folder(self, item: LibraryItem) -> Optional[Path]:
        root = self._valid_music_root(silent=True)
        return root / Path(item.relative_path) if root else None

    def physical_items_for(self, item: LibraryItem, *, missing_only: bool = False) -> list[LibraryItem]:
        """Return the real filesystem rows represented by a virtual item."""
        if item.kind == "artist" and item.group_paths:
            paths = item.group_missing_paths if missing_only else item.group_paths
            return self.db.items_for_paths(paths, kind="artist")
        return [item]

    def artwork_path(self, item: LibraryItem) -> Optional[Path]:
        root = self._valid_music_root(silent=True)
        if not root:
            return None
        rel = item.artwork_relative_path or item.relative_path
        return root / Path(rel) / (item.artwork_name or self.output_name or "folder.jpg")

    # ------------------------------------------------------------------
    # Scan / database
    # ------------------------------------------------------------------
    def scan_library(self) -> None:
        root = self._valid_music_root()
        if not root:
            return
        if not self.operation_lock.acquire(blocking=False):
            self.toast.show("Another task is still running. Wait for it or cancel it first.", "warn")
            return
        self._persist_basic_config()
        self._known_root = self.music_root_var.get().strip()
        self.cancel_event.clear()
        self._set_busy("scan", "Scanning library…")
        self.log_app(f"Scan started: {root}")

        def worker() -> None:
            try:
                scanner = FastLibraryScanner(
                    self.db, root, self.output_name,
                    lambda i, total, name: self.ui_queue.put(("progress", (i, total, f"Scanning {name}"))),
                    self.cancel_event,
                )
                self.ui_queue.put(("scan_done", scanner.scan()))
            except Exception as exc:
                self.ui_queue.put(("error", f"Library scan failed:\n{exc}"))
            finally:
                self.operation_lock.release()

        threading.Thread(target=worker, name="library-scan", daemon=True).start()

    def rebuild_database(self) -> None:
        if self.operation_lock.locked():
            self.toast.show("Wait for the current task to finish first.", "warn")
            return
        if not messagebox.askyesno(APP_NAME, "Rebuild the local artwork database?\n\n"
                                             "This does not touch any artwork on the music device."):
            return
        self.db.clear()
        self.thumbs.clear()
        self.photo_cache.clear()
        self._thumb_errors.clear()
        self.selected_item_id = None
        self.show_detail(None)
        self.refresh_counts()
        self.reload_gallery(reset_scroll=True)
        self.set_view("library")
        self.scan_library()

    def clear_thumbnail_cache(self) -> None:
        self.thumbs.clear()
        self.photo_cache.clear()
        self._thumb_errors.clear()
        self.reload_gallery(reset_scroll=False)
        self._refresh_settings_info()
        self.status_var.set("Thumbnail cache cleared")

    # ------------------------------------------------------------------
    # Selection + detail panel
    # ------------------------------------------------------------------
    def select_item(self, item_id: int) -> None:
        item = self.db.get_item(item_id)
        if not item:
            return
        old_idx = self.index_by_id.get(self.selected_item_id or -1)
        self.selected_item_id = item_id
        self._set_card_state(old_idx)
        self._set_card_state(self.index_by_id.get(item_id))
        if self.pending_item_id is not None and self.pending_item_id != item_id:
            self.clear_pending(redraw=False)
        self.show_detail(item)

    def show_detail(self, item: Optional[LibraryItem]) -> None:
        if item is None or self._detail_item is None or item.id != self._detail_item.id:
            self.detail_sf.canvas.yview_moveto(0)
        self._detail_item = item
        if item is None:
            self.d_title.configure(text="Nothing selected")
            self.d_sub.configure(text="Pick an artist or album to see its artwork, replace it "
                                      "or search online.")
            self.detail_body.pack_forget()
            self.current_preview = None
            self._preview_token = None
            self._draw_preview()
            return
        if not self.detail_body.winfo_ismapped():
            self.detail_body.pack(fill="x")
        self.d_title.configure(text=item.title)
        if item.kind == "album":
            self.d_sub.configure(text=item.artist)
            self.edit_tags_btn.pack(fill="x", pady=(7, 0))
        else:
            n = self.album_counts.get(item.artist, 0)
            suffix = f"{len(item.group_paths)} folders" if len(item.group_paths) > 1 else "library"
            self.d_sub.configure(text=f"{n} album" + ("" if n == 1 else "s") + f" in {suffix}")
            self.edit_tags_btn.pack_forget()
        color = {"Existing": C["ok"], "Missing": C["warn"], "Problem": C["bad"]}[item.status]
        self.chip_status.configure(text=item.status_label.upper(), fg=color, bg=blend(C["panel"], color, 0.16))
        self.chip_kind.configure(text=item.kind.upper())
        if item.kind == "artist" and item.group_paths and item.group_missing_paths:
            missing_n = len(item.group_missing_paths)
            total_n = len(item.group_paths)
            if item.artwork_relative_path:
                self.d_meta.configure(text=f"Artwork missing in {missing_n} of {total_n} artist folders.")
            else:
                self.d_meta.configure(text=f"No artist artwork yet. Missing in all {total_n} folders.")
        elif item.artwork_exists:
            self.d_meta.configure(text=f"{item.artwork_name}  ·  {format_bytes(item.artwork_size)}")
        else:
            self.d_meta.configure(text=f"No artwork file yet. New artwork is saved as {self.output_name}.")
        if item.kind == "artist" and len(item.group_paths) > 1:
            self.d_path.configure(text="Virtual artist identity · " + "  |  ".join(item.group_paths))
        else:
            self.d_path.configure(text=item.relative_path)
        if item.problem:
            self.problem_label.configure(text=friendly_reason(item.problem))
            self.problem_box.pack(fill="x", pady=(12, 0), after=self.d_path)
        else:
            self.problem_box.pack_forget()
        if item.artwork_exists:
            self.autofetch_btn.grid_remove()
            self.open_folder_btn.grid(row=0, column=0, columnspan=2, sticky="ew", padx=0)
            self.detail_hint.configure(text="Tip: drop an image straight onto any card to replace its artwork. "
                                            "Double-click a card to search online.")
        else:
            self.autofetch_btn.grid()
            self.open_folder_btn.grid(row=0, column=1, columnspan=1, sticky="ew", padx=(4, 0))
            self.detail_hint.configure(text="Auto-fetch uses the integrated artwork engine and its matching rules. "
                                            "Search online lets you choose the image yourself.")
        self._load_current_preview(item)

    def _load_current_preview(self, item: LibraryItem) -> None:
        self.current_preview = None
        token = (item.id, item.artwork_mtime_ns)
        self._preview_token = token
        self._draw_preview()
        path = self.artwork_path(item)
        if (not item.artwork_exists and not item.artwork_relative_path) or not path:
            return

        def work() -> Any:
            with Image.open(path) as img:
                img.load()
                dims = img.size
                rgb = to_rgb(img)
            rgb.thumbnail((640, 640), Image.Resampling.LANCZOS)
            return rgb, dims, path.stat().st_size

        self.thumbs.run_device_task(work, lambda result: self.ui_queue.put(("preview", (token, result))))

    def _on_preview(self, token: Any, result: Any) -> None:
        if token != self._preview_token or self._detail_item is None:
            return
        item = self._detail_item
        if isinstance(result, Exception):
            self.d_meta.configure(text=f"{item.artwork_name}  ·  could not be read ({result})")
            self.current_preview = None
        else:
            img, (w, h), size = result
            self.current_preview = img
            shape = "" if w == h else "  ·  not square"
            extra = ""
            if item.kind == "artist" and item.group_missing_paths:
                extra = f"  ·  missing in {len(item.group_missing_paths)}/{len(item.group_paths)} folders"
            self.d_meta.configure(text=f"{item.artwork_name}  ·  {w}×{h}  ·  {format_bytes(size)}{shape}{extra}")
        self._draw_preview()

    def _draw_preview(self) -> None:
        c = self.preview
        c.delete("all")
        P = self.preview_size
        bg = C["panel"]
        item = self._detail_item
        if item is not None and self.pending_image is not None and self.pending_item_id == item.id:
            half = (P - 20) // 2
            if self.current_preview is not None:
                left = ImageTk.PhotoImage(rounded_fit(self.current_preview, half, bg, 10))
            else:
                variant = "loading" if item.artwork_exists else ("problem" if item.problem else "missing")
                left = self._placeholder(item.kind, variant, half, bg)
            new = anchor_crop_square(self.pending_image, self.pending_anchor)
            right = ImageTk.PhotoImage(rounded_fit(new, half, bg, 10))
            self.detail_photo, self.detail_photo2 = left, right  # type: ignore[assignment]
            c.configure(width=P, height=half + 28)
            c.create_image(0, 0, image=left, anchor="nw")
            c.create_image(half + 20, 0, image=right, anchor="nw")
            c.create_text(half + 10, half / 2, text="›", fill=C["muted"], font=self.F.title)
            c.create_text(0, half + 8, text="Current", anchor="nw", fill=C["muted"], font=self.F.small)
            size = self.output_size()
            c.create_text(half + 20, half + 8, text=f"New  ·  {size}×{size}", anchor="nw",
                          fill=C["accent_hi"], font=self.F.small_b)
            return
        if item is not None and not item.artwork_exists:
            # No artwork to look at: keep the placeholder compact so the actions stay in view.
            small = P // 2
            # Shrink the canvas horizontally as well. Keeping a 268px-wide canvas
            # around a 134px placeholder produced an empty framed strip to the right.
            c.configure(width=small, height=small)
            self.detail_photo = self._placeholder(item.kind, "problem" if item.problem else "missing", small, bg)
            c.create_image(0, 0, image=self.detail_photo, anchor="nw")
            return
        c.configure(width=P, height=P)
        if item is None:
            self.detail_photo = self._placeholder("album", "loading", P, bg)
        elif self.current_preview is not None:
            self.detail_photo = ImageTk.PhotoImage(rounded_fit(self.current_preview, P, bg, 14))
        else:
            variant = "loading" if item.artwork_exists else ("problem" if item.problem else "missing")
            self.detail_photo = self._placeholder(item.kind, variant, P, bg)
        c.create_image(0, 0, image=self.detail_photo, anchor="nw")
        if item is None:
            c.create_text(P / 2, P / 2, text="No selection", fill=C["faint"], font=self.F.body)

    # ------------------------------------------------------------------
    # New artwork: file, paste, drop, URL, picker
    # ------------------------------------------------------------------
    def _require_selection(self) -> Optional[int]:
        if self.selected_item_id is None:
            self.toast.show("Select an artist or album first.", "warn")
            return None
        return self.selected_item_id

    def select_manual_image(self) -> None:
        if self._require_selection() is None:
            return
        filename = filedialog.askopenfilename(
            title="Choose an artwork image",
            filetypes=[("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.gif"), ("All files", "*.*")],
        )
        if filename:
            self.load_pending_from_path(Path(filename))

    def load_pending_from_path(self, path: Path) -> None:
        if not path.is_file():
            self.toast.show(f"File not found: {path.name}", "error")
            return
        if path.suffix.casefold() not in SUPPORTED_MANUAL_IMAGE_EXTS:
            self.toast.show(f"Unsupported image type: {path.suffix or 'none'}", "error")
            return
        try:
            img = load_image(path)
        except Exception as exc:
            self.toast.show(f"Could not open image: {exc}", "error")
            return
        self.set_pending(img, f"File  ·  {path.name}")

    def load_pending_from_url(self, url: str) -> None:
        item_id = self._require_selection()
        if item_id is None:
            return
        self.status_var.set("Downloading image…")

        def worker() -> None:
            try:
                img = download_image(url)
                host = urlparse(url).netloc or "web"
                self.ui_queue.put(("pending_image", (item_id, img, f"Link  ·  {host}")))
            except Exception as exc:
                self.ui_queue.put(("toast", (f"Could not download that image: {exc}", "error")))
                self.ui_queue.put(("status", "Ready"))

        threading.Thread(target=worker, name="url-download", daemon=True).start()

    def paste_image(self) -> None:
        if self._require_selection() is None:
            return
        data: Any = None
        try:
            from PIL import ImageGrab
            data = ImageGrab.grabclipboard()
        except Exception:
            data = None
        if isinstance(data, Image.Image):
            self.set_pending(to_rgb(data), "Pasted from clipboard")
            return
        if isinstance(data, list):
            for name in data:
                path = Path(str(name))
                if path.suffix.casefold() in SUPPORTED_MANUAL_IMAGE_EXTS and path.is_file():
                    self.load_pending_from_path(path)
                    return
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            text = ""
        if is_url(text):
            self.load_pending_from_url(text)
        elif text and Path(text).expanduser().is_file():
            self.load_pending_from_path(Path(text).expanduser())
        else:
            self.toast.show("The clipboard doesn't hold an image, an image file or an image link.", "warn")

    def handle_drop_data(self, data: str) -> None:
        if self._require_selection() is None:
            return
        try:
            parts = [str(p).strip() for p in self.root.tk.splitlist(data or "")]
        except tk.TclError:
            parts = [(data or "").strip()]
        for part in parts:
            candidate = part
            if candidate.startswith("file://"):
                candidate = unquote(urlparse(candidate).path)
                if os.name == "nt" and re.match(r"^/[A-Za-z]:", candidate):
                    candidate = candidate[1:]
            if candidate and os.path.isfile(candidate):
                self.load_pending_from_path(Path(candidate))
                return
        for part in parts + [(data or "").strip()]:
            if is_url(part):
                self.load_pending_from_url(part)
                return
        self.toast.show("That drop didn't contain an image file or an image link.", "warn")

    def _on_drop_zone(self, event: Any) -> None:
        self.handle_drop_data(getattr(event, "data", ""))

    def set_pending(self, img: Image.Image, label: str, item_id: Optional[int] = None) -> None:
        item_id = item_id if item_id is not None else self.selected_item_id
        if item_id is None:
            self.toast.show("Select an artist or album first.", "warn")
            return
        if item_id != self.selected_item_id:
            self.select_item(item_id)
        item = self._detail_item
        if item is None:
            return
        self.pending_image = img
        self.pending_label = label
        self.pending_item_id = item_id
        self.pending_anchor = 0.5
        w, h = img.size
        size = self.output_size()
        name = item.artwork_name if item.artwork_exists else self.output_name
        info = f"{label}\n{w}×{h} source, saved as {size}×{size} {name}"
        if min(w, h) < size:
            info += "\nSmaller than the output size, so it will be upscaled and may look soft."
        self.pending_info.configure(text=info, fg=C["warn"] if min(w, h) < size else C["muted"])
        if w != h:
            opts = [(0.0, "Top"), (0.5, "Center"), (1.0, "Bottom")] if h > w else \
                [(0.0, "Left"), (0.5, "Center"), (1.0, "Right")]
            self.crop_seg.set_options(opts, 0.5)
            self.crop_row.pack(fill="x", pady=(10, 0), after=self.pending_info)
        else:
            self.crop_row.pack_forget()
        self.pending_box.pack(fill="x", after=self.drop_zone)
        self.pending_bar.grid()
        self.root.after_idle(self._reveal_pending)
        self.drop_zone.set_text("Drop another image to swap", f"or click to browse  ·  {MOD_LABEL}V to paste")
        self.status_var.set(f"New artwork ready for {item.title}. Press Save to write it.")
        self._draw_preview()

    def _reveal_pending(self) -> None:
        sf = self.detail_sf
        try:
            self.root.update_idletasks()
            y = self.pending_box.winfo_rooty() - sf.inner.winfo_rooty()
            h = self.pending_box.winfo_height()
            view = sf.canvas.winfo_height()
            total = max(1, sf.inner.winfo_reqheight())
            if y + h > sf.canvas.canvasy(0) + view:
                sf.canvas.yview_moveto(max(0, y + h - view + 16) / total)
        except tk.TclError:
            pass

    def _on_crop_anchor(self, value: float) -> None:
        self.pending_anchor = float(value)
        self._draw_preview()

    def clear_pending(self, redraw: bool = True) -> None:
        self.pending_image = None
        self.pending_item_id = None
        self.pending_label = ""
        self.pending_box.pack_forget()
        self.pending_bar.grid_remove()
        self.drop_zone.set_text("Drop an image here", f"or click to browse  ·  {MOD_LABEL}V to paste")
        if self.status_var.get().startswith("New artwork ready"):
            self.status_var.set("Ready")
        if redraw:
            self._draw_preview()

    def save_manual_artwork(self) -> None:
        if self.pending_image is None or self.pending_item_id is None:
            if self.selected_item_id is not None:
                self.toast.show("Choose, drop or paste an image first.", "warn")
            return
        item = self.db.get_item(self.pending_item_id)
        root = self._valid_music_root(silent=True)
        if not item or not root:
            return
        targets = self.physical_items_for(item)
        if not targets:
            messagebox.showerror(APP_NAME, "No physical library folders were found for this item.")
            return
        missing_folders = [root / Path(t.relative_path) for t in targets if not (root / Path(t.relative_path)).is_dir()]
        if missing_folders:
            messagebox.showerror(APP_NAME, "One or more music folders aren't available. Is the iPod connected?\n\n" +
                                 "\n".join(str(p) for p in missing_folders[:4]))
            return

        img, anchor, size = self.pending_image, self.pending_anchor, self.output_size()
        self.save_btn.state(["disabled"])
        if len(targets) > 1:
            self.status_var.set(f"Saving artwork to {len(targets)} artist folders...")
        else:
            self.status_var.set("Saving artwork...")

        def work() -> Any:
            square = anchor_crop_square(img, anchor)
            saved: list[tuple[int, str, str, tuple[int, int]]] = []
            failed: list[str] = []
            for physical in targets:
                folder = root / Path(physical.relative_path)
                output_name = physical.artwork_name if physical.artwork_exists else self.output_name
                output_path = folder / output_name
                try:
                    engine.save_square_jpeg(square, output_path, size)
                    saved.append((physical.id, physical.relative_path, output_name,
                                  image_file_signature(output_path)))
                except Exception as exc:
                    failed.append(f"{physical.relative_path}: {exc}")
            return {"saved": saved, "failed": failed}

        self.thumbs.run_device_task(
            work,
            lambda result: self.ui_queue.put(("saved", (item.id, result))),
        )

    def _on_saved(self, virtual_item_id: int, result: Any) -> None:
        self.save_btn.state(["!disabled"])
        old_item = self.db.get_item(virtual_item_id)
        if isinstance(result, Exception) or old_item is None:
            messagebox.showerror(APP_NAME, f"Could not save artwork:\n{result}")
            self.status_var.set("Save failed")
            return
        saved = list(result.get("saved", [])) if isinstance(result, dict) else []
        failed = list(result.get("failed", [])) if isinstance(result, dict) else []
        if not saved:
            messagebox.showerror(APP_NAME, "Could not save artwork.\n\n" + ("\n".join(failed[:6]) or "Unknown error"))
            self.status_var.set("Save failed")
            return

        updates: list[tuple[int, dict[str, Any]]] = []
        for item_id, rel, output_name, signature in saved:
            mtime_ns, size = signature
            updates.append((item_id, {
                "artwork_name": output_name,
                "artwork_exists": True,
                "artwork_mtime_ns": mtime_ns,
                "artwork_size": size,
                "problem": "",
            }))
            self.log_app(f"Saved {output_name} for {rel}")
        self.db.set_item_states_bulk(updates)

        if self.pending_item_id == virtual_item_id:
            self.clear_pending(redraw=False)
        self.photo_cache.pop(virtual_item_id, None)
        self.refresh_counts()
        self.reload_gallery(reset_scroll=False)
        if self.selected_item_id == virtual_item_id:
            self.show_detail(self.db.get_item(virtual_item_id))

        title = old_item.title
        if failed:
            for line in failed:
                self.log_app("Artwork save failed: " + line)
            self.status_var.set(f"Saved artwork for {title}; {len(failed)} folder(s) failed")
            self.toast.show(f"Artwork saved, but {len(failed)} folder(s) failed. See Activity log.", "warn", ms=5500)
        else:
            suffix = f" to {len(saved)} folders" if len(saved) > 1 else ""
            self.status_var.set(f"Saved artwork for {title}{suffix}")
            self.toast.show(f"Artwork saved for {title}{suffix}", "ok")

    # ------------------------------------------------------------------
    # Auto-fetch through the existing engine
    # ------------------------------------------------------------------
    def _engine_args(self, root: Path) -> Any:
        args = engine.build_parser().parse_args([])
        args.mode = "both"
        args.music_root = str(root)
        args.output_name = self.output_name or "folder.jpg"
        args.max_size = self.output_size()
        args.min_source_size = self.output_size()
        args.interactive = False
        args.overwrite = False
        args.retry_not_found = True  # the GUI's fetch action should retry old not-found entries
        args.ignore_cache = False
        return args

    def fetch_missing(self) -> None:
        root = self._valid_music_root()
        if not root:
            return
        search = self.search_var.get().strip()
        items = self.db.missing_items(self.kind_filter, search)
        if not items:
            self.toast.show("Nothing is missing in this view.", "ok")
            return
        noun = {"All": "artists and albums", "Artists": "artists", "Albums": "albums"}[self.kind_filter]
        scope = f" matching '{search}'" if search else ""
        if not messagebox.askyesno(APP_NAME, f"Fetch artwork for {len(items)} missing {noun}{scope}?\n\n"
                                             "Existing artwork is never overwritten. You can cancel at any time."):
            return
        self._fetch_items(items, root)

    def fetch_selected_item(self) -> None:
        if self._require_selection() is None:
            return
        root = self._valid_music_root()
        item = self.db.get_item(self.selected_item_id or -1)
        if not root or not item:
            return
        if item.artwork_exists:
            self.toast.show("This one already has artwork. Use Search online to replace it.", "info")
            return
        self._fetch_items([item], root)

    def _fetch_items(self, items: list[LibraryItem], root: Path) -> None:
        if not self.operation_lock.acquire(blocking=False):
            self.toast.show("Another task is still running. Wait for it or cancel it first.", "warn")
            return
        self._persist_basic_config()
        self.cancel_event.clear()
        total = len(items)
        self._set_busy("fetch", f"Fetching artwork for {total} item" + ("" if total == 1 else "s") + "…")
        self.log_app(f"Fetch started for {total} item(s)")

        # Read Tk-backed settings on the UI thread. Tk variables are not safe to
        # access from the worker on every Tk build (notably some Linux/macOS builds).
        args = self._engine_args(root)

        def worker() -> None:
            creds = engine.load_credentials()
            session = engine.requests.Session()
            limiter = engine.RateLimiter(args)
            artist_cache_path = root / engine.ARTIST_CACHE_FILENAME
            album_cache_path = root / engine.ALBUM_CACHE_FILENAME
            artist_cache = engine.load_cache(artist_cache_path, "artists")
            album_cache = engine.load_cache(album_cache_path, "albums")
            changed = failed = done = 0
            try:
                for index, item in enumerate(items, start=1):
                    if self.cancel_event.is_set():
                        break
                    self.ui_queue.put(("progress", (index - 1, total, f"Fetching {item.title}")))
                    # A virtual merged artist can represent several physical folders.
                    # Only folders that actually miss artwork are fetched; albums stay one-to-one.
                    targets = self.physical_items_for(item, missing_only=(item.kind == "artist"))
                    if not targets:
                        targets = self.physical_items_for(item)
                    updates: list[tuple[int, dict[str, Any]]] = []
                    item_changed = False
                    item_failed = False

                    # If a merged artist already has artwork in one physical folder,
                    # reuse that exact image for its missing aliases before going
                    # online. This is faster, works offline and guarantees Rockbox
                    # sees consistent artist artwork in every real folder.
                    if item.kind == "artist" and item.group_paths and targets:
                        all_physical = self.physical_items_for(item)
                        source_item = next((p for p in all_physical if p.artwork_exists), None)
                        if source_item is not None:
                            source_path = (root / Path(source_item.relative_path)
                                           / (source_item.artwork_name or args.output_name))
                            if source_path.is_file():
                                remaining: list[LibraryItem] = []
                                for physical in targets:
                                    folder = root / Path(physical.relative_path)
                                    try:
                                        destination = folder / args.output_name
                                        if not destination.exists():
                                            shutil.copy2(source_path, destination)
                                        exists, art_name, mtime_ns, art_size = detect_artwork(folder, args.output_name)
                                        if not exists:
                                            remaining.append(physical)
                                            continue
                                        updates.append((physical.id, {
                                            "artwork_name": art_name,
                                            "artwork_exists": True,
                                            "artwork_mtime_ns": mtime_ns,
                                            "artwork_size": art_size,
                                            "problem": "",
                                        }))
                                        item_changed = True
                                        self.log_app(
                                            f"Copied existing artist artwork: {source_item.relative_path} -> "
                                            f"{physical.relative_path}/{art_name}"
                                        )
                                    except Exception as exc:
                                        # A copy failure is not final: let the normal
                                        # provider pipeline try this folder next.
                                        self.log_app(
                                            f"Could not copy existing artwork to {physical.relative_path}: {exc}; "
                                            "trying online sources"
                                        )
                                        remaining.append(physical)
                                targets = remaining

                    for physical in targets:
                        if self.cancel_event.is_set():
                            break
                        folder = root / Path(physical.relative_path)
                        try:
                            if item.kind == "artist":
                                ok, source = engine.process_artist_folder(folder, args, creds, session, limiter, artist_cache)
                            else:
                                ok, source = engine.process_album_folder(folder, args, creds, session, limiter, album_cache)
                            exists, art_name, mtime_ns, art_size = detect_artwork(folder, args.output_name)
                            if exists:
                                updates.append((physical.id, {
                                    "artwork_name": art_name, "artwork_exists": True,
                                    "artwork_mtime_ns": mtime_ns, "artwork_size": art_size, "problem": "",
                                }))
                                item_changed = item_changed or bool(ok)
                            else:
                                reason = source if source not in {"exists", "not-found"} else "Artwork not found"
                                updates.append((physical.id, {"artwork_exists": False, "problem": reason}))
                                item_failed = True
                        except Exception as exc:
                            updates.append((physical.id, {"artwork_exists": False, "problem": str(exc)}))
                            item_failed = True
                    self.db.set_item_states_bulk(updates, invalidate=False)
                    # Patch only this virtual card instead of rebuilding the whole
                    # canonical library. The UI can therefore update live during a
                    # long batch without bringing back the old per-item slowdown.
                    self.db.refresh_cached_item(item.id)
                    if item_failed:
                        failed += 1
                    elif item_changed:
                        changed += 1
                    done = index
                    self.ui_queue.put(("item_done", item.id))
                    self.ui_queue.put(("progress", (index, total, f"Fetched {item.title}")))
            finally:
                try:
                    engine.save_cache(artist_cache_path, artist_cache)
                    engine.save_cache(album_cache_path, album_cache)
                except Exception as exc:
                    print(f"Could not save fetch cache: {exc}")
                session.close()
                self.ui_queue.put(("fetch_done", {"changed": changed, "failed": failed, "total": total,
                                                  "done": done, "cancelled": self.cancel_event.is_set()}))
                self.operation_lock.release()

        threading.Thread(target=worker, name="artwork-fetch", daemon=True).start()

    def cancel_operation(self) -> None:
        self.cancel_event.set()
        self.cancel_btn.state(["disabled"])
        self.status_var.set("Stopping after the current item…")

    # ------------------------------------------------------------------
    # Folder / clipboard helpers
    # ------------------------------------------------------------------
    def open_selected_folder(self) -> None:
        if self._require_selection() is None:
            return
        item = self.db.get_item(self.selected_item_id or -1)
        folder = self.item_folder(item) if item else None
        if not folder or not folder.exists():
            self.toast.show("That folder isn't available. Is the iPod connected?", "error")
            return
        try:
            open_in_file_manager(folder)
        except Exception as exc:
            self.toast.show(f"Could not open folder: {exc}", "error")

    def copy_selected_path(self) -> None:
        item = self.db.get_item(self.selected_item_id or -1)
        folder = self.item_folder(item) if item else None
        if folder:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(folder))
            self.toast.show("Folder path copied", "ok")

    # ------------------------------------------------------------------
    # Busy state, activity log, background queue
    # ------------------------------------------------------------------
    def _set_busy(self, kind: str, text: str) -> None:
        self._busy_kind = kind
        self.status_var.set(text)
        self.progress.configure(value=0, maximum=100)
        self.progress_label.configure(text="")
        self.progress_label.grid(row=0, column=1, padx=(12, 8))
        self.progress.grid(row=0, column=2)
        self.cancel_btn.state(["!disabled"])
        self.cancel_btn.grid(row=0, column=3, padx=(10, 0))
        for btn in (self.scan_btn, self.fetch_btn, self.autofetch_btn):
            btn.state(["disabled"])

    def _set_progress(self, i: int, total: int, text: str) -> None:
        self.progress.configure(maximum=max(1, total), value=i)
        self.progress_label.configure(text=f"{i} / {total}")
        if not self.cancel_event.is_set():
            self.status_var.set(text)

    def _end_busy(self, text: str) -> None:
        self._busy_kind = ""
        for w in (self.progress, self.progress_label, self.cancel_btn):
            w.grid_remove()
        for btn in (self.scan_btn, self.fetch_btn, self.autofetch_btn):
            btn.state(["!disabled"])
        self.status_var.set(text)

    def _install_log_tee(self) -> None:
        self._orig_stdout = sys.stdout
        sys.stdout = LogTee(sys.stdout, lambda line: self.ui_queue.put(("log", line)))

    def log_app(self, text: str) -> None:
        self.ui_queue.put(("log", f"» {time.strftime('%H:%M:%S')}  {text}"))

    def _append_log(self, lines: list[str]) -> None:
        t = self.log_text
        at_bottom = t.yview()[1] > 0.98
        t.configure(state="normal")
        for line in lines:
            low = line.casefold()
            if line.startswith("»"):
                tag = "app"
            elif line.startswith("[") or line.startswith("===") or line.startswith("---"):
                tag = "head"
            elif "saved:" in low or "selected:" in low:
                tag = "ok"
            elif "error" in low or "failed" in low or "rejected" in low:
                tag = "bad"
            elif "skip" in low or "no confident" in low or "not found" in low:
                tag = "warn"
            else:
                tag = ""
            t.insert("end", line + "\n", tag)
        overflow = int(t.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if overflow > 0:
            t.delete("1.0", f"{overflow + 1}.0")
        t.configure(state="disabled")
        if at_bottom:
            t.see("end")

    def _copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get("1.0", "end-1c"))
        self.status_var.set("Activity log copied")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _process_ui_queue(self) -> None:
        logs: list[str] = []
        try:
            for _ in range(500):
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    logs.append(str(payload))
                elif kind == "thumb":
                    self._on_thumb(*payload)
                elif kind == "preview":
                    self._on_preview(*payload)
                elif kind == "progress":
                    self._set_progress(*payload)
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "toast":
                    self.toast.show(*payload)
                elif kind == "pending_image":
                    item_id, img, label = payload
                    self.set_pending(img, label, item_id)
                elif kind == "saved":
                    self._on_saved(*payload)
                elif kind == "ui_callback":
                    callback, args = payload
                    callback(*args)
                elif kind == "item_done":
                    self.update_item_in_gallery(int(payload))
                    self.refresh_counts()
                    if self.selected_item_id == payload and self.pending_item_id != payload:
                        self.show_detail(self.db.get_item(int(payload)))
                elif kind == "error":
                    self._end_busy("Ready")
                    messagebox.showerror(APP_NAME, str(payload))
                elif kind == "scan_done":
                    self._on_scan_done(dict(payload))
                elif kind == "health_done":
                    self._on_health_done(dict(payload))
                elif kind == "fetch_done":
                    self._on_fetch_done(dict(payload))
        except queue.Empty:
            pass
        finally:
            if logs:
                self._append_log(logs)
            self.root.after(40, self._process_ui_queue)

    def _on_scan_done(self, result: dict[str, int]) -> None:
        if result.get("cancelled"):
            text = "Scan cancelled. Folders that weren't reached keep their previous status."
        else:
            # Report the same virtual/canonical counts shown in the sidebar.
            # Physical duplicate artist folders can otherwise make the scan text
            # disagree with the library view.
            shown = self.db.counts()
            text = (f"Scan complete: {shown['artists']} artists, {shown['albums']} albums, "
                    f"{shown['missing']} missing")
            self.db.set_meta("health_dirty", "1")
        self._end_busy(text)
        self.log_app(text)
        self.refresh_counts()
        self.reload_gallery(reset_scroll=False)
        if self.selected_item_id is not None:
            item = self.db.get_item(self.selected_item_id)
            if item is None:
                self.selected_item_id = None
            if self.pending_item_id is None:
                self.show_detail(item)

    def _on_health_done(self, result: dict[str, Any]) -> None:
        if result.get("cancelled"):
            text = "Health analysis cancelled. Previous results were kept."
        else:
            text = f"Health analysis complete: {result.get('all', 0)} issue" + ("" if result.get('all', 0) == 1 else "s")
        self._end_busy(text)
        self.log_app(text)
        self.refresh_counts()
        self.refresh_health_view()
        self.reload_gallery(reset_scroll=False)
        self.toast.show(text, "ok" if not result.get("all", 0) else "warn", ms=4500)

    def _on_fetch_done(self, result: dict[str, Any]) -> None:
        saved, failed = result["changed"], result["failed"]
        if result.get("cancelled"):
            text = f"Fetch cancelled after {result['done']} of {result['total']}: {saved} saved, {failed} unresolved"
        else:
            text = f"Fetch complete: {saved} saved, {failed} unresolved"
        self._end_busy(text)
        self.log_app(text)
        self.refresh_counts()
        self.reload_gallery(reset_scroll=False)
        if self.selected_item_id is not None and self.pending_item_id is None:
            self.show_detail(self.db.get_item(self.selected_item_id))
        self.toast.show(text, "ok" if not failed else "warn", ms=5000)

    def on_close(self) -> None:
        if self._busy_kind in ("rockbox_database", "media"):
            messagebox.showinfo(APP_NAME, "Wait for the database or media operation to finish before quitting. Media operations can be cancelled in their dialog.")
            return
        if self.operation_lock.locked():
            if not messagebox.askyesno(APP_NAME, "A scan or fetch is still running. Quit anyway?"):
                return
            self.cancel_event.set()
        try:
            if self.root.state() == "normal":
                self.config["geometry"] = self.root.winfo_geometry()
        except tk.TclError:
            pass
        self._persist_basic_config()
        sys.stdout = getattr(self, "_orig_stdout", sys.stdout)
        self.thumbs.shutdown()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Online candidate picker
    # ------------------------------------------------------------------
    def open_picker(self) -> None:
        if self._require_selection() is None:
            return
        item = self.db.get_item(self.selected_item_id or -1)
        if not item:
            return
        picker = getattr(self, "_picker", None)
        if picker is not None and picker.alive():
            picker.close()
        self._picker = PickerDialog(self, item)


# ----------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------
def enable_windows_dpi_awareness() -> None:
    """Without this, Tk renders blurry on scaled Windows displays."""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:
        pass


def create_root() -> tk.Tk:
    enable_windows_dpi_awareness()
    if DND_AVAILABLE and TkinterDnD is not None:
        try:
            return TkinterDnD.Tk()  # type: ignore[return-value]
        except Exception:
            pass
    return tk.Tk()


def main() -> int:
    if not engine.ensure_dependencies():
        try:
            tmp = tk.Tk()
            tmp.withdraw()
            messagebox.showerror(APP_NAME, "Missing dependency 'mutagen'.\n\nInstall with:\n"
                                           "pip install mutagen pillow requests")
            tmp.destroy()
        except Exception:
            pass
        return 2
    root = create_root()
    ArtworkApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
