# SPDX-License-Identifier: GPL-2.0-only
"""On-demand, read-only album details using the app's worker/UI queue pattern."""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from .album_details import AlbumDetails, duration_text, read_album
from .sources import strip_accents
from .track_checks import position
from .ui_theme import C


class AlbumDialog:
    def __init__(self, app, folder, artist, album):
        self.app, self.folder = app, folder
        self.cancel = threading.Event()
        self.running = False
        self.data = AlbumDetails()
        self.win = tk.Toplevel(app.root)
        self.win.title(f"Album details — {album}")
        self.win.geometry("1100x680")
        self.win.minsize(850, 530)
        self.win.configure(bg=C["panel"])
        self.win.transient(app.root)
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda _: self.close())
        body = tk.Frame(self.win, bg=C["panel"], padx=18, pady=18)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=album, font=app.F.title, bg=C["panel"], fg=C["text"], anchor="w").pack(fill="x")
        tk.Label(body, text=artist, font=app.F.body, bg=C["panel"], fg=C["muted"], anchor="w").pack(fill="x")
        self.summary = tk.Label(body, text="", font=app.F.small, bg=C["panel"], fg=C["muted"], anchor="w")
        self.summary.pack(fill="x", pady=8)
        footer = tk.Frame(body, bg=C["panel"])
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        self.status = tk.Label(footer, text="", bg=C["panel"], fg=C["muted"], font=app.F.small, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.refresh_btn = ttk.Button(footer, text="Reload details", command=self.load)
        self.refresh_btn.pack(side="right", padx=(8, 0))
        self.close_btn = ttk.Button(footer, text="Close", command=self.close)
        self.close_btn.pack(side="right")
        self.notes = tk.Text(body, height=5, bg=C["input"], fg=C["text"], font=app.F.small,
                             wrap="word", relief="flat", padx=8, pady=8, state="disabled")
        self.notes.pack(side="bottom", fill="x", pady=(10, 0))
        controls = tk.Frame(body, bg=C["panel"])
        controls.pack(fill="x", pady=(0, 8))
        self.search_var, self.filter_var = tk.StringVar(), tk.StringVar(value="All tracks")
        tk.Label(controls, text="Search", bg=C["panel"], fg=C["text"], font=app.F.small).pack(side="left", padx=(0, 6))
        ttk.Entry(controls, textvariable=self.search_var).pack(side="left", fill="x", expand=True)
        style = ttk.Style(self.win)
        style.configure("Album.TCombobox", background=C["input"], arrowcolor=C["text"], bordercolor=C["border"])
        style.map("Album.TCombobox", fieldbackground=[("readonly", C["input"])],
                  foreground=[("readonly", C["text"])], selectbackground=[("readonly", C["input"])],
                  selectforeground=[("readonly", C["text"])])
        self.filter_box = ttk.Combobox(controls, textvariable=self.filter_var, state="readonly", width=23,
                                     style="Album.TCombobox",
                                     values=("All tracks", "Missing title", "Missing genre", "Possible duplicates", "Unreadable tracks"))
        self.filter_box.pack(side="left", padx=(8, 0))
        self.search_var.trace_add("write", lambda *_: self.render())
        self.filter_var.trace_add("write", lambda *_: self.render())
        frame = tk.Frame(body, bg=C["panel"])
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        columns = ("disc", "track", "title", "artist", "duration", "format", "genre", "year")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse",
                                 style="Health.Treeview")
        for key, label, width in zip(columns, ("Disc", "Track", "Title", "Artist", "Duration", "Format", "Genre", "Year"),
                                     (50, 50, 230, 160, 70, 170, 140, 75)):
            self.tree.heading(key, text=label)
            minimum = max(50, app.F.small_b.measure(label) + 28)
            self.tree.column(key, width=max(width, minimum), minwidth=minimum,
                             stretch=key in ("title", "artist", "format", "genre"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.tree.tag_configure("error", foreground=C["bad"])
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.show_notes())
        self.load()

    def alive(self):
        return bool(self.win.winfo_exists())

    def close(self):
        self.cancel.set()
        self.win.destroy()

    def load(self):
        if self.running or not self.alive():
            return
        if not self.app.operation_lock.acquire(blocking=False):
            self.status.configure(text="Another operation is running. Try Reload details when it finishes.")
            return
        self.running = True
        self.cancel.clear()
        self.refresh_btn.state(["disabled"])
        self.status.configure(text="Reading this album…")

        def work():
            try:
                result = read_album(self.folder, self.cancel)
            except Exception as exc:
                result = exc
            finally:
                self.app.operation_lock.release()
            self.app.ui_queue.put(("ui_callback", (self.finish, (result,))))

        threading.Thread(target=work, name="album-details", daemon=True).start()

    def finish(self, result):
        self.running = False
        if not self.alive():
            return
        self.refresh_btn.state(["!disabled"])
        if isinstance(result, Exception):
            self.status.configure(text=f"Could not read album: {result}", fg=C["bad"])
            return
        if result.cancelled:
            self.status.configure(text="Read cancelled.")
            return
        self.data = result
        self.folder = result.folder or self.folder
        durations = [t.duration for t in result.tracks if t.duration is not None]
        discs = {position(t.value("discnumber")) for t in result.tracks}
        disc_text = f" · {len(discs)} disc" + ("s" if len(discs) != 1 else "") if discs and 0 not in discs else ""
        duration = duration_text(sum(durations)) if durations else "Duration unknown"
        partial = " (known tracks only)" if len(durations) != len(result.tracks) else ""
        self.summary.configure(text=f"{len(result.tracks)} tracks{disc_text} · {duration}{partial}")
        self.render()
        self.show_notes()

    def render(self):
        if not hasattr(self, "tree") or not self.alive():
            return
        for key in self.tree.get_children():
            self.tree.delete(key)
        duplicate_paths = {p for issue in self.data.issues if issue["type"] == "duplicate_track" for p in issue["paths"]}
        words = strip_accents(self.search_var.get()).casefold().split()
        count = 0
        for index, track in enumerate(self.data.tracks):
            keep = {"All tracks": True, "Missing title": not track.value("title"),
                    "Missing genre": not track.value("genre"), "Possible duplicates": track.path in duplicate_paths,
                    "Unreadable tracks": bool(track.error)}[self.filter_var.get()]
            blob = strip_accents(" ".join([str(track.path), track.format, *[v for values in track.values.values() for v in values]])).casefold()
            if not keep or not all(word in blob for word in words):
                continue
            count += 1
            self.tree.insert("", "end", iid=str(index), tags=("error",) if track.error else (),
                             values=(track.value("discnumber") or "—", track.value("tracknumber") or "—",
                                     track.value("title") or track.path.name, track.value("artist"),
                                     duration_text(track.duration), track.format, track.value("genre"), track.value("date")))
        failed = sum(bool(t.error) for t in self.data.tracks) + len(self.data.errors)
        self.status.configure(text=f"Showing {count} of {len(self.data.tracks)} tracks · Read-only"
                                   + (f" · {failed} read error(s)" if failed else ""),
                              fg=C["warn"] if failed else C["muted"])

    def show_notes(self):
        lines = ["Read-only view. Metadata matches do not prove identical audio; numbering gaps may be intentional."]
        selection = self.tree.selection()
        if selection:
            track = self.data.tracks[int(selection[0])]
            lines += [str(track.path.relative_to(self.folder)), "Composer: " + (track.value("composer") or "—")]
            if track.error:
                lines.append("Could not read: " + track.error)
            lines += [issue["details"] for issue in self.data.issues if track.path in issue["paths"]]
        else:
            lines += [issue["details"] for issue in self.data.issues]
        lines += self.data.errors[:10]
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", "\n".join(lines))
        self.notes.configure(state="disabled")
