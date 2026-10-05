# SPDX-License-Identifier: GPL-2.0-only
"""The online artwork picker: every candidate the artwork engine's providers return."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote_plus

from PIL import Image, ImageTk

from . import artwork_engine as engine
from .common import MOD_LABEL, load_image
from .library import LibraryItem
from .ui_theme import C, blend, rounded_fit, rounded_panel
from .widgets import FieldEntry

if TYPE_CHECKING:
    from .gui import ArtworkApp


PICKER_MAX_EDGE = 1000


class PickerDialog:
    """Shows every candidate the engine's providers return, so the user can choose."""

    TILE = 172

    def __init__(self, app: ArtworkApp, item: LibraryItem) -> None:
        self.app = app
        self.item = item
        F = app.F
        self.F = F
        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop = threading.Event()
        self.token: Any = None
        self.results: list[dict[str, Any]] = []
        self.selected: Optional[int] = None
        self.hover: Optional[int] = None
        self._bg_cache: dict[Any, ImageTk.PhotoImage] = {}
        self._layout = SimpleNamespace(cols=1, x0=16, tw=0, th=0, gap=14)

        win = tk.Toplevel(app.root)
        self.win = win
        win.title(f"Find artwork  ·  {item.title}")
        win.configure(bg=C["bg"])
        win.geometry("940x660")
        win.minsize(720, 480)
        win.transient(app.root)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(2, weight=1)

        head = tk.Frame(win, bg=C["bg"], padx=24)
        head.grid(row=0, column=0, sticky="ew", pady=(20, 4))
        tk.Label(head, text="Find artwork", bg=C["bg"], fg=C["text"], font=F.h1).pack(anchor="w")
        sub = item.title if item.kind == "artist" else f"{item.album}  ·  {item.artist}"
        tk.Label(head, text=f"{item.kind.title()}  ·  {sub}", bg=C["bg"], fg=C["muted"], font=F.small).pack(anchor="w")

        form = tk.Frame(win, bg=C["bg"], padx=24)
        form.grid(row=1, column=0, sticky="ew", pady=(14, 12))
        self.artist_var = tk.StringVar(value=item.artist)
        self.album_var = tk.StringVar(value=item.album)
        col = 0
        for label, var, show in (("Artist", self.artist_var, True), ("Album", self.album_var, item.kind == "album")):
            if not show:
                continue
            box = tk.Frame(form, bg=C["bg"])
            box.grid(row=0, column=col, sticky="ew", padx=(0, 10))
            form.columnconfigure(col, weight=1)
            tk.Label(box, text=label, bg=C["bg"], fg=C["muted"], font=F.small).pack(anchor="w", pady=(0, 3))
            entry = FieldEntry(box, F, var, width=24)
            entry.pack(fill="x")
            entry.entry.bind("<Return>", lambda _e: self.search())
            col += 1
        ttk.Button(form, text="Search", style="Accent.TButton", command=self.search).grid(
            row=0, column=col, sticky="sw")

        area = tk.Frame(win, bg=C["bg"])
        area.grid(row=2, column=0, sticky="nsew", padx=(14, 4))
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(area, bg=C["bg"], highlightthickness=0, bd=0, yscrollincrement=1)
        vsb = ttk.Scrollbar(area, orient="vertical", style="Slim.Vertical.TScrollbar", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        app._register_scrollable(self.canvas, lambda px: self.canvas.yview_scroll(px, "units"))
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<Motion>", self._on_motion)

        tk.Frame(win, bg=C["border"], height=1).grid(row=3, column=0, sticky="ew")
        foot = tk.Frame(win, bg=C["sidebar"], padx=24, pady=12)
        foot.grid(row=4, column=0, sticky="ew")
        foot.columnconfigure(0, weight=1)
        left = tk.Frame(foot, bg=C["sidebar"])
        left.grid(row=0, column=0, sticky="w")
        self.status = tk.Label(left, text="", bg=C["sidebar"], fg=C["muted"], font=F.small, anchor="w",
                               justify="left", wraplength=520)
        self.status.pack(anchor="w")
        link = tk.Label(left, text="Search in your browser ↗", bg=C["sidebar"], fg=C["accent"], font=F.small,
                        cursor="hand2")
        link.pack(anchor="w", pady=(2, 0))
        link.bind("<Button-1>", lambda _e: self.browser_search())
        ttk.Button(foot, text="Cancel", style="Ghost.TButton", command=self.close).grid(row=0, column=1, padx=(0, 8))
        self.use_btn = ttk.Button(foot, text="Use image", style="Accent.TButton", command=self.use_selected)
        self.use_btn.grid(row=0, column=2)
        self.use_btn.state(["disabled"])

        win.bind("<Escape>", lambda _e: self.close())
        win.bind("<Return>", lambda _e: self.use_selected() if self.selected is not None else None)
        self.search()
        self.win.after(50, self._poll)

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def close(self) -> None:
        self.stop.set()
        self.app._scrollables.pop(str(self.canvas), None)
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def set_status(self, text: str, color: str = "muted") -> None:
        self.status.configure(text=text, fg=C[color])

    def browser_search(self) -> None:
        if self.item.kind == "album":
            q = f"{self.artist_var.get().strip()} {self.album_var.get().strip()} album cover"
        else:
            q = f"{self.artist_var.get().strip()} artist photo"
        webbrowser.open(f"https://www.google.com/search?tbm=isch&q={quote_plus(q)}")
        self.set_status("Found one in the browser? Drag the image onto the card in the main window, "
                        f"or copy it and press {MOD_LABEL}V there.")

    def search(self) -> None:
        self.stop.set()
        self.stop = threading.Event()
        stop = self.stop
        token = object()
        self.token = token
        self.results = []
        self.selected = None
        self.use_btn.state(["disabled"])
        self.redraw()
        artist = self.artist_var.get().strip()
        album = self.album_var.get().strip()
        if not artist or (self.item.kind == "album" and not album):
            self.set_status("Enter a name to search for.", "warn")
            return
        root = self.app._valid_music_root(silent=True)
        folder = root / Path(self.item.relative_path) if root else None
        args = self.app._engine_args(root or Path("."))
        args.max_candidates_per_provider = 8
        args.max_album_candidates_per_provider = 4
        args.min_album_match = args.min_artist_match = 0.6  # a person chooses, so show near matches too
        out_size = self.app.output_size()
        kind = self.item.kind
        self.set_status("Looking up sources…")

        def put(msg: str, payload: Any) -> None:
            self.queue.put((msg, (token, payload)))

        def worker() -> None:
            creds = engine.load_credentials()
            session = engine.requests.Session()
            limiter = engine.RateLimiter(args)
            try:
                print(f"\n[Search online: {artist}{' / ' + album if kind == 'album' else ''}]")
                cands: list[Any] = []
                if kind == "album":
                    if folder is not None and folder.is_dir():
                        put("status", "Reading embedded artwork from the audio files…")
                        embedded = engine.find_embedded_album_art(folder, args.max_files_per_album)
                        if embedded:
                            cands.append(embedded)
                    put("status", "Searching Cover Art Archive, Deezer, Apple Music, TheAudioDB"
                                  + (", Last.fm" if creds.has_lastfm else "") + (" and Discogs" if creds.has_discogs else "") + "…")
                    cands += engine.album_candidates(artist, album, creds, args, session, limiter)
                else:
                    put("status", "Searching TheAudioDB, Deezer" + (", Last.fm" if creds.has_lastfm else "")
                                  + (", fanart.tv" if creds.has_fanart else "") + (" and Discogs" if creds.has_discogs else "") + "…")
                    cands = engine.artist_candidates(artist, creds, args, session, limiter)
                cands = engine.unique_candidates(cands)
                total = len(cands)
                for i, c in enumerate(cands, 1):
                    if stop.is_set():
                        break
                    put("status", f"Downloading image {i} of {total}…")
                    try:
                        if c.image_bytes:
                            img = load_image(c.image_bytes)
                        else:
                            if not c.image_url or engine.contains_placeholder_marker(c.image_url):
                                continue
                            limiter.wait("image")
                            img = engine.image_from_bytes(engine.download_image_bytes(session, c.image_url, timeout=45))
                        if engine.image_seems_bad(img, 1, c.source):
                            print(f"  picker: skipped {c.source} placeholder image")
                            continue
                        w, h = img.size
                        score, _reason = engine.score_downloaded_candidate(img, c, args)
                        img.thumbnail((PICKER_MAX_EDGE, PICKER_MAX_EDGE), Image.Resampling.LANCZOS)
                        put("result", {"source": c.source, "img": img, "w": w, "h": h, "score": score,
                                       "small": min(w, h) < out_size})
                    except Exception as exc:
                        print(f"  picker: {c.source} image failed: {exc}")
            except Exception as exc:
                put("error", str(exc))
            finally:
                session.close()
                put("done", None)

        threading.Thread(target=worker, name="artwork-picker", daemon=True).start()

    def _poll(self) -> None:
        if not self.alive():
            return
        changed = False
        try:
            while True:
                msg, (token, payload) = self.queue.get_nowait()
                if token is not self.token:
                    continue
                if msg == "status":
                    self.set_status(payload)
                elif msg == "error":
                    self.set_status(f"Search failed: {payload}", "bad")
                elif msg == "result":
                    chosen = self.results[self.selected] if self.selected is not None else None
                    self.results.append(payload)
                    self.results.sort(key=lambda r: r["score"], reverse=True)
                    if chosen is not None:
                        self.selected = self.results.index(chosen)
                    changed = True
                elif msg == "done":
                    self._finish()
        except queue.Empty:
            pass
        if changed:
            self.redraw()
        self.win.after(60, self._poll)

    def _finish(self) -> None:
        n = len(self.results)
        creds = engine.load_credentials()
        missing = [name for name, on in (("Last.fm", creds.has_lastfm), ("fanart.tv", creds.has_fanart),
                                         ("Discogs", creds.has_discogs)) if not on]
        tip = f"  Keys for {', '.join(missing)} in Settings add more results." if missing else ""
        if n:
            self.set_status(f"{n} image" + ("" if n == 1 else "s") + " found, best match first. "
                            "Pick one and press Use image." + tip)
        else:
            self.set_status("No artwork found. Try another spelling, or search in your browser and "
                            "drop the image on the card." + tip, "warn")

    # -- drawing -----------------------------------------------------------
    def _tile_bg(self, state: str) -> ImageTk.PhotoImage:
        L = self._layout
        key = (state, L.tw, L.th)
        if key not in self._bg_cache:
            border, bw = {"normal": (blend(C["card"], C["border"], 0.55), 1),
                          "hover": (C["border_hi"], 1.5), "selected": (C["accent"], 2.5)}[state]
            self._bg_cache[key] = ImageTk.PhotoImage(
                rounded_panel(int(L.tw), int(L.th), C["card"], C["bg"], 12, border, bw))
        return self._bg_cache[key]

    def _tile_xy(self, i: int) -> tuple[float, float]:
        L = self._layout
        row, col = divmod(i, L.cols)
        return L.x0 + col * (L.tw + L.gap), 12 + row * (L.th + L.gap)

    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        width = max(300, c.winfo_width())
        T, pad = self.TILE, 10
        L = self._layout
        L.tw = T + 2 * pad
        L.th = pad + T + 10 + self.F.body_b.metrics("linespace") + self.F.small.metrics("linespace") + pad
        L.cols = max(1, int((width - 20 + L.gap) // (L.tw + L.gap)))
        L.x0 = max(10, (width - (L.cols * L.tw + (L.cols - 1) * L.gap)) / 2)
        rows = (len(self.results) + L.cols - 1) // L.cols
        c.configure(scrollregion=(0, 0, width, max(c.winfo_height(), 24 + rows * (L.th + L.gap))))
        if not self.results:
            c.create_text(width / 2, 120, text="Searching…" if self.status.cget("fg") != C["warn"] else "",
                          fill=C["faint"], font=self.F.body)
            return
        for i, r in enumerate(self.results):
            x, y = self._tile_xy(i)
            state = "selected" if i == self.selected else ("hover" if i == self.hover else "normal")
            c.create_image(x, y, image=self._tile_bg(state), anchor="nw")
            if "photo" not in r:
                r["photo"] = ImageTk.PhotoImage(rounded_fit(r["img"], T, C["card"], 8))
            c.create_image(x + pad, y + pad, image=r["photo"], anchor="nw")
            ty = y + pad + T + 9
            c.create_text(x + pad, ty, text=r["source"], anchor="nw", fill=C["text"], font=self.F.body_b)
            notes = [f"{r['w']}×{r['h']}"]
            color = C["muted"]
            if i == 0 and len(self.results) > 1:
                notes.append("best match")
                color = C["accent_hi"]
            if r["small"]:
                notes.append("small")
                color = C["warn"]
            elif r["w"] != r["h"]:
                notes.append("will be cropped")
            c.create_text(x + pad, ty + self.F.body_b.metrics("linespace") + 1, text="  ·  ".join(notes),
                          anchor="nw", fill=color, font=self.F.small)

    def _hit(self, event: tk.Event) -> Optional[int]:
        L = self._layout
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for i in range(len(self.results)):
            x, y = self._tile_xy(i)
            if x <= cx <= x + L.tw and y <= cy <= y + L.th:
                return i
        return None

    def _on_motion(self, event: tk.Event) -> None:
        i = self._hit(event)
        if i != self.hover:
            self.hover = i
            self.canvas.configure(cursor="hand2" if i is not None else "")
            self.redraw()

    def _on_click(self, event: tk.Event) -> None:
        i = self._hit(event)
        if i is not None:
            self.selected = i
            self.use_btn.state(["!disabled"])
            self.redraw()

    def _on_double(self, event: tk.Event) -> None:
        if self._hit(event) is not None:
            self.use_selected()

    def use_selected(self) -> None:
        if self.selected is None or self.selected >= len(self.results):
            return
        r = self.results[self.selected]
        self.close()
        self.app.set_pending(r["img"], f"{r['source']}  ·  original {r['w']}×{r['h']}", self.item.id)
