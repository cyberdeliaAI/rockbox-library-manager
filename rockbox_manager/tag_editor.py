# SPDX-License-Identifier: GPL-2.0-only
"""The album tag editor dialog, with online year/genre suggestions and a review step."""
from __future__ import annotations

import threading
import tkinter as tk
from collections import Counter
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

from . import artwork_engine as engine
from . import sources, tags
from .ui_theme import C, blend
from .widgets import ScrollFrame

if TYPE_CHECKING:
    from .gui import ArtworkApp


def ask_review(parent: tk.Misc, F: SimpleNamespace, title: str, lines: list[str], warnings: list[str],
               confirm: str = "Save tags") -> bool:
    """Modal before/after review. Returns True when the person confirms."""
    win = tk.Toplevel(parent)
    win.title("Review tag changes")
    win.configure(bg=C["panel"])
    win.transient(parent)
    win.minsize(560, 300)
    frame = tk.Frame(win, bg=C["panel"], padx=20, pady=18)
    frame.pack(fill="both", expand=True)
    tk.Label(frame, text=title, bg=C["panel"], fg=C["text"], font=F.title, anchor="w",
             justify="left", wraplength=640).pack(fill="x")
    tk.Label(frame, text="Before → after. Identical changes are grouped; tracks that already have "
             "the new value are left untouched.", bg=C["panel"], fg=C["muted"], font=F.small,
             anchor="w", justify="left", wraplength=640).pack(fill="x", pady=(2, 10))
    result = {"ok": False}

    def finish(ok: bool) -> None:
        result["ok"] = ok
        win.destroy()

    buttons = tk.Frame(frame, bg=C["panel"])
    buttons.pack(side="bottom", fill="x", pady=(14, 0))
    ttk.Button(buttons, text="Back", style="Ghost.TButton", command=lambda: finish(False)).pack(side="right")
    ttk.Button(buttons, text=confirm, style="Accent.TButton", command=lambda: finish(True)).pack(
        side="right", padx=(0, 7))
    for warning in warnings:
        bg = blend(C["panel"], C["warn"], 0.09)
        tk.Label(frame, text=warning, bg=bg, fg=C["text"], font=F.small, anchor="w", justify="left",
                 wraplength=640, padx=10, pady=6).pack(side="bottom", fill="x", pady=(6, 0))
    text = tk.Text(frame, height=min(14, max(4, len(lines))), wrap="word", bg=C["input"], fg=C["text"],
                   font=F.small, relief="flat", bd=0, padx=10, pady=8, highlightthickness=0)
    text.insert("end", "\n".join(f"• {line}" for line in lines))
    text.configure(state="disabled")
    text.pack(fill="both", expand=True)
    win.bind("<Escape>", lambda _e: finish(False))
    win.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    win.grab_set()
    win.wait_window()
    return result["ok"]


class TagEditorDialog:
    """Album-level tag editor with explicit destructive actions.

    Reading tags happens off the UI thread. A blank value is never interpreted
    as deletion: removing a tag requires checking the dedicated Clear box. Every
    value a file holds is read, because Rockbox keeps one value per tag and the
    database update refuses files with several.
    """

    FIELDS = tags.EDIT_FIELDS

    def __init__(self, app: ArtworkApp, folder: Path, artist: str, album: str) -> None:
        self.app = app
        self.folder = folder
        self.artist, self.album = artist, album
        self.files: list[Path] = []
        self.file_values: dict[Path, dict[str, list[str]]] = {}
        self.lookup_token: Optional[object] = None
        self.pending_sources = 0
        self.suggestion_count = 0

        self.win = tk.Toplevel(app.root)
        self.win.title("Edit album tags")
        self.win.configure(bg=C["panel"])
        self.win.transient(app.root)
        self.win.grab_set()
        self.win.geometry("1060x640")
        self.win.minsize(900, 560)
        self.win.bind("<Destroy>", self._on_destroy)
        F = self.F = app.F

        outer = tk.Frame(self.win, bg=C["panel"], padx=20, pady=18)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="Edit album tags", bg=C["panel"], fg=C["text"],
                 font=F.h1, anchor="w").pack(fill="x")
        tk.Label(outer, text=f"{artist} - {album}", bg=C["panel"], fg=C["muted"],
                 font=F.body, anchor="w").pack(fill="x", pady=(1, 2))
        self.file_count_label = tk.Label(outer, text="Reading audio files - only checked fields will be changed",
                                         bg=C["panel"], fg=C["faint"], font=F.small, anchor="w")
        self.file_count_label.pack(fill="x", pady=(0, 14))

        buttons = tk.Frame(outer, bg=C["panel"])
        buttons.pack(side="bottom", fill="x", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", style="Ghost.TButton", command=self.win.destroy).pack(side="right")
        self.save_btn = ttk.Button(buttons, text="Review and save…", style="Accent.TButton", command=self.save)
        self.save_btn.pack(side="right", padx=(0, 7))
        self.save_btn.state(["disabled"])

        body = tk.Frame(outer, bg=C["panel"])
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3, uniform="tag-editor")
        body.columnconfigure(1, weight=2, uniform="tag-editor")
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg=C["panel"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self._build_fields(left)
        self._build_suggestions(body)

        threading.Thread(target=self._load_worker, name="tag-editor-read", daemon=True).start()

    # -- layout -----------------------------------------------------------
    def _build_fields(self, parent: tk.Frame) -> None:
        F = self.F
        self.vars: dict[str, tk.StringVar] = {}
        self.change_vars: dict[str, tk.BooleanVar] = {}
        self.clear_vars: dict[str, tk.BooleanVar] = {}
        self.entries: dict[str, ttk.Entry] = {}
        self.change_checks: dict[str, ttk.Checkbutton] = {}
        self.clear_checks: dict[str, ttk.Checkbutton] = {}
        self.hints: dict[str, tk.Label] = {}

        fields = tk.Frame(parent, bg=C["panel"])
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)

        for row, (key, label) in enumerate(self.FIELDS):
            change = tk.BooleanVar(value=False)
            clear = tk.BooleanVar(value=False)
            value = tk.StringVar(value="")
            self.change_vars[key] = change
            self.clear_vars[key] = clear
            self.vars[key] = value

            cb = ttk.Checkbutton(fields, variable=change, command=lambda k=key: self._toggle_field(k))
            cb.grid(row=row, column=0, sticky="nw", pady=7)
            cb.state(["disabled"])
            self.change_checks[key] = cb

            label_frame = tk.Frame(fields, bg=C["panel"])
            label_frame.grid(row=row, column=1, sticky="ew", pady=7)
            label_frame.columnconfigure(1, weight=1)
            tk.Label(label_frame, text=label, bg=C["panel"], fg=C["text"], font=F.small_b,
                     width=14, anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 8))
            entry = ttk.Entry(label_frame, textvariable=value, state="disabled")
            entry.grid(row=0, column=1, sticky="ew")
            self.entries[key] = entry

            clear_cb = ttk.Checkbutton(label_frame, text="Clear tag", variable=clear,
                                       command=lambda k=key: self._toggle_clear(k))
            clear_cb.grid(row=0, column=2, sticky="e", padx=(10, 0))
            clear_cb.state(["disabled"])
            self.clear_checks[key] = clear_cb

            hint = tk.Label(label_frame, text="reading tags...", bg=C["panel"], fg=C["faint"],
                            font=F.tiny, anchor="w", justify="left", wraplength=470)
            hint.grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, 0))
            self.hints[key] = hint

        note_bg = blend(C["panel"], C["warn"], 0.09)
        note = tk.Frame(parent, bg=note_bg, padx=11, pady=9,
                        highlightthickness=1, highlightbackground=blend(C["panel"], C["warn"], 0.25))
        note.pack(fill="x", pady=(14, 0))
        tk.Label(note, text="Safe bulk editing", bg=note_bg, fg=C["text"], font=F.small_b,
                 anchor="w").pack(fill="x")
        tk.Label(note, text="A blank field is never saved and never deletes a tag. To remove metadata, "
                 "enable that field and explicitly choose Clear tag. A saved field gets exactly one value "
                 "on every track, as Rockbox's database keeps one per tag. Artist can be mixed on "
                 "compilations, so change it only when every track should share the same artist.",
                 bg=note_bg, fg=C["muted"], font=F.small, anchor="w", justify="left",
                 wraplength=560).pack(fill="x", pady=(2, 0))

        self.status = tk.Label(parent, text="Reading tags from the album...",
                               bg=C["panel"], fg=C["muted"], font=F.small, anchor="w",
                               justify="left", wraplength=560)
        self.status.pack(fill="x", pady=(12, 0))

    def _build_suggestions(self, parent: tk.Frame) -> None:
        F = self.F
        panel = tk.Frame(parent, bg=C["card"], padx=12, pady=12,
                         highlightthickness=1, highlightbackground=C["border"])
        panel.grid(row=0, column=1, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        head = tk.Frame(panel, bg=C["card"])
        head.grid(row=0, column=0, sticky="ew")
        tk.Label(head, text="Online suggestions", bg=C["card"], fg=C["text"], font=F.body_b,
                 anchor="w").pack(side="left")
        self.lookup_btn = ttk.Button(head, text="Find year and genre", style="SmallAccent.TButton",
                                     command=self.find_suggestions)
        self.lookup_btn.pack(side="right")
        self.suggest_status = tk.Label(
            panel, text="Searches MusicBrainz, Deezer, Apple Music and TheAudioDB, plus Discogs and "
            "Last.fm with a key in Settings. Click a year or genre to put it in the form; nothing is "
            "saved until you review it.", bg=C["card"], fg=C["muted"], font=F.small, anchor="w",
            justify="left", wraplength=340)
        self.suggest_status.grid(row=1, column=0, sticky="ew", pady=(6, 8))
        self.suggest_scroll = ScrollFrame(panel, C["card"], self.app._register_scrollable)
        self.suggest_scroll.grid(row=2, column=0, sticky="nsew")
        panel.bind("<Configure>", lambda e: self.suggest_status.configure(wraplength=max(200, e.width - 30)))

    # -- lifecycle --------------------------------------------------------
    def _alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self.win:
            self.lookup_token = None
            self.app._scrollables.pop(str(self.suggest_scroll.canvas), None)

    def _load_worker(self) -> None:
        keys = [key for key, _label in self.FIELDS]
        values: dict[Path, dict[str, list[str]]] = {}
        errors: list[str] = []
        try:
            files = list(engine.iter_audio_files(self.folder))
        except Exception as exc:
            files = []
            errors.append(f"Could not scan album folder: {exc}")
        for path in files:
            try:
                values[path] = tags.read_values(path, keys)
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
        self.app.ui_queue.put(("ui_callback", (self._finish_loading, (files, values, errors))))

    def _finish_loading(self, files: list[Path], values: dict[Path, dict[str, list[str]]],
                        errors: list[str]) -> None:
        if not self._alive():
            return
        self.files = files
        self.file_values = values
        if not files:
            from tkinter import messagebox
            messagebox.showwarning("Edit album tags", "No supported audio files were found in this album folder.",
                                   parent=self.win)
            self.win.destroy()
            return
        self.file_count_label.configure(
            text=f"{len(files)} audio file" + ("" if len(files) == 1 else "s") + " - only checked fields will be changed"
        )
        for key, _label in self.FIELDS:
            per_file = [tuple(v.get(key, [])) for v in values.values()]
            uniques = set(per_file)
            several = [t for t in per_file if len(t) > 1]
            value, color = "", C["faint"]
            if len(uniques) == 1 and len(per_file[0]) == 1:
                value, hint = per_file[0][0], "current value"
            elif not any(per_file):
                hint = "not set"
            else:
                hint = "mixed across tracks"
            if several:
                common = Counter(several).most_common(1)[0][0]
                value = value or common[0]
                hint = (f"{len(several)} track{'' if len(several) == 1 else 's'} {'has' if len(several) == 1 else 'have'} "
                        f"several values ({tags.show(list(common))}). Rockbox keeps one per track and the "
                        "database update refuses several: tick this field to save a single value.")
                color = C["warn"]
            if key == "genre" and any(tags.combined_genre(g) for t in per_file for g in t):
                hint += ("  Combined text such as “Pop;Rock” is listed by Rockbox as a genre of its own; "
                         "keep one main genre.")
                color = C["warn"]
            self.vars[key].set(value)
            self.hints[key].configure(text=hint, fg=color)
            self.change_checks[key].state(["!disabled"])
        self.save_btn.state(["!disabled"])
        if errors:
            self.status.configure(text=f"Read {len(values)} file(s); {len(errors)} failed and will be skipped. "
                                       "See Activity log.", fg=C["warn"])
            for line in errors:
                self.app.log_app("Tag read failed: " + line)
        else:
            self.status.configure(text="Tags loaded. Choose only the fields you want to change.", fg=C["muted"])

    # -- fields -----------------------------------------------------------
    def _toggle_field(self, key: str) -> None:
        enabled = self.change_vars[key].get()
        if not enabled:
            self.clear_vars[key].set(False)
            self.entries[key].configure(state="disabled")
            self.clear_checks[key].state(["disabled"])
            return
        self.clear_checks[key].state(["!disabled"])
        if self.clear_vars[key].get():
            self.entries[key].configure(state="disabled")
        else:
            self.entries[key].configure(state="normal")
            self.entries[key].focus_set()

    def _toggle_clear(self, key: str) -> None:
        if self.clear_vars[key].get():
            self.change_vars[key].set(True)
            self.entries[key].configure(state="disabled")
            self.hints[key].configure(text="tag will be removed from every track", fg=C["faint"])
        else:
            if self.change_vars[key].get():
                self.entries[key].configure(state="normal")
            self.hints[key].configure(text="enter the new value", fg=C["faint"])

    # -- online suggestions -------------------------------------------------
    def find_suggestions(self) -> None:
        artist = (self.vars["albumartist"].get() or self.vars["artist"].get() or self.artist).strip()
        album = (self.vars["album"].get() or self.album).strip()
        for child in self.suggest_scroll.inner.winfo_children():
            child.destroy()
        if not artist or not album:
            self.suggest_status.configure(text="Enter an album artist and an album first.", fg=C["warn"])
            return
        keys = engine.load_credentials().keys()
        lookup = sources.Lookup(lambda: keys)
        names = [n for n in sources.TAG_SOURCES if n in lookup.enabled("albums")]
        token = object()
        self.lookup_token = token
        self.pending_sources = len(names)
        self.suggestion_count = 0
        self.suggest_status.configure(
            text=f"Searching {', '.join(lookup.label(n) for n in names)} for “{album}” by {artist}…",
            fg=C["muted"])

        def work(name: str) -> None:
            try:
                results, error = lookup.album(name, artist, album), ""
            except Exception as exc:
                results, error = [], str(exc)
            self.app.ui_queue.put(("ui_callback", (self._add_suggestions,
                                                   (token, lookup.label(name), results, error))))

        for name in names:
            threading.Thread(target=work, args=(name,), name=f"suggest-{name}", daemon=True).start()

    def _add_suggestions(self, token: object, label: str, results: list[dict], error: str) -> None:
        if not self._alive() or token is not self.lookup_token:
            return
        self.pending_sources -= 1
        inner, F = self.suggest_scroll.inner, self.F
        if error:
            tk.Label(inner, text=error, bg=C["card"], fg=C["faint"], font=F.tiny, anchor="w",
                     justify="left", wraplength=330).pack(fill="x", pady=(0, 4))
        for result in results[:3]:
            if not result.get("year") and not result.get("genres"):
                continue
            self.suggestion_count += 1
            row = tk.Frame(inner, bg=C["card"], pady=5)
            row.pack(fill="x")
            tk.Label(row, text=f"{label} · {result.get('title') or ''}", bg=C["card"], fg=C["text"],
                     font=F.small_b, anchor="w", justify="left", wraplength=330).pack(fill="x")
            meta = " · ".join(x for x in (result.get("artist"), f"match {result.get('score', 0):.0%}",
                                          result.get("note")) if x)
            tk.Label(row, text=meta, bg=C["card"], fg=C["faint"], font=F.tiny, anchor="w",
                     justify="left", wraplength=330).pack(fill="x")
            chips: list[tuple[str, str, str]] = []
            if result.get("year"):
                chips.append((str(result["year"]), "date", str(result["year"])))
            chips += [(genre, "genre", genre) for genre in result.get("genres", [])[:6]]
            line = None
            for index, (text, key, value) in enumerate(chips):
                if index % 3 == 0:
                    line = tk.Frame(row, bg=C["card"])
                    line.pack(fill="x", pady=(4, 0))
                chip = tk.Label(line, text=text, bg=C["accent_lo"] if key == "date" else C["input"],
                                fg=C["text"], font=F.small, padx=8, pady=2, cursor="hand2")
                chip.pack(side="left", padx=(0, 5))
                chip.bind("<Button-1>", lambda _e, k=key, v=value, s=label: self.use_suggestion(k, v, s))
            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", pady=(3, 3))
        if self.pending_sources <= 0:
            if self.suggestion_count:
                self.suggest_status.configure(
                    text="Click a year or genre to put it in the form. MusicBrainz and Discogs give the first "
                         "release year; Deezer and Apple Music the date of their edition.", fg=C["muted"])
            else:
                self.suggest_status.configure(text="No suggestions found. Check the spelling of album artist "
                                                   "and album, then search again.", fg=C["warn"])

    def use_suggestion(self, key: str, value: str, source: str) -> None:
        if self.change_checks[key].instate(["disabled"]):
            self.status.configure(text="Wait until the tags are loaded.", fg=C["warn"])
            return
        self.clear_vars[key].set(False)
        self.change_vars[key].set(True)
        self._toggle_field(key)
        self.vars[key].set(value)
        self.hints[key].configure(text=f"from {source}; saved only after you review it", fg=C["accent_hi"])

    # -- saving -------------------------------------------------------------
    def save(self) -> None:
        edits: dict[str, tuple[str, str]] = {}
        blank_fields: list[str] = []
        labels = dict(self.FIELDS)
        for key, _label in self.FIELDS:
            if not self.change_vars[key].get():
                continue
            if self.clear_vars[key].get():
                edits[key] = ("clear", "")
                continue
            value = self.vars[key].get().strip()
            if not value:
                blank_fields.append(labels[key])
            else:
                edits[key] = ("set", value)

        if blank_fields:
            self.status.configure(
                text="Blank values are not saved. Enter a value or choose Clear tag for: " + ", ".join(blank_fields),
                fg=C["bad"],
            )
            return
        if not edits:
            self.status.configure(text="Choose at least one field to change.", fg=C["warn"])
            return
        if edits.get("date", ("", ""))[0] == "set" and not tags.valid_year(edits["date"][1]):
            self.status.configure(text="Year must be YYYY, YYYY-MM or YYYY-MM-DD, the forms the Rockbox "
                                       "database update can read.", fg=C["bad"])
            return

        plans = tags.plan_edits(self.file_values, edits)
        if not plans:
            self.status.configure(text="Nothing to save: every track already has these values.", fg=C["warn"])
            return
        warnings = []
        if "artist" in edits:
            warnings.append("ARTIST changes on every track. This can be wrong for compilations or albums "
                            "with guest-track artists.")
        clears = [labels[k] for k, (mode, _value) in edits.items() if mode == "clear"]
        if clears:
            warnings.append("These tags will be removed from every track: " + ", ".join(clears) + ".")
        skipped = len(self.files) - len(self.file_values)
        if skipped:
            warnings.append(f"{skipped} file(s) could not be read and will be skipped.")
        title = f"Save tags to {len(plans)} of {len(self.files)} track" + ("" if len(self.files) == 1 else "s") + "?"
        confirmed = ask_review(self.win, self.F, title, tags.review_lines(plans), warnings)
        if not self._alive():
            return
        self.win.grab_set()
        if not confirmed:
            return

        if not self.app.operation_lock.acquire(blocking=False):
            self.status.configure(text="Another operation is still running. Wait for it to finish.", fg=C["warn"])
            return
        self.save_btn.state(["disabled"])
        self.status.configure(text="Saving tags...", fg=C["muted"])

        def worker() -> None:
            changed = 0
            failed: list[str] = []
            try:
                for plan in plans:
                    try:
                        changed += tags.write_edits(plan.path, edits)
                    except Exception as exc:
                        failed.append(f"{plan.path.name}: {exc}")
            finally:
                self.app.operation_lock.release()
            self.app.ui_queue.put(("ui_callback", (self._finish_save, (edits, changed, failed))))

        threading.Thread(target=worker, name="tag-editor-save", daemon=True).start()

    def _finish_save(self, edits: dict[str, tuple[str, str]], changed: int, failed: list[str]) -> None:
        if changed:
            self.app.db.set_meta("health_dirty", "1")
        if not self._alive():
            return
        if failed:
            self.save_btn.state(["!disabled"])
            self.status.configure(text=f"Saved {changed}; {len(failed)} failed. See Activity log.", fg=C["bad"])
            for line in failed:
                self.app.log_app("Tag edit failed: " + line)
            return
        changed_fields = ", ".join(edits.keys())
        self.app.log_app(f"Tags updated in {self.folder}: {changed_fields} ({changed} tracks)")
        self.app.toast.show(f"Tags saved to {changed} track" + ("" if changed == 1 else "s") +
                            ". Use Settings > Rockbox database to update the iPod index.", "ok", ms=7000)
        self.win.destroy()
