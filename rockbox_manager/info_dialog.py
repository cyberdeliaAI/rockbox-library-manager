# SPDX-License-Identifier: GPL-2.0-only
"""Explicit background lookup; opening this dialog makes no online request."""
from __future__ import annotations

import re
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk

from .ui_theme import C


class InformationDialog:
    def __init__(self, app, kind, artist, album=""):
        self.app, self.kind, self.artist, self.album = app, kind, artist, album
        self.running, self.url = False, ""
        self.cancel = threading.Event()
        self.win = tk.Toplevel(app.root)
        self.win.title("Artist biography" if kind == "artist" else "Album background")
        self.win.geometry("760x600")
        self.win.minsize(600, 420)
        self.win.configure(bg=C["panel"])
        self.win.transient(app.root)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda _: self.close())
        body = tk.Frame(self.win, bg=C["panel"], padx=18, pady=18)
        body.pack(fill="both", expand=True)
        heading = tk.Label(body, text=album or artist, bg=C["panel"], fg=C["text"], font=app.F.title,
                           anchor="w", wraplength=680, justify="left")
        heading.pack(fill="x")
        self.status = tk.Label(body, text="TheAudioDB · English · No request until you choose Fetch background.",
                               bg=C["panel"], fg=C["muted"], font=app.F.small, anchor="w", wraplength=680)
        self.status.pack(fill="x", pady=10)
        body.bind("<Configure>", lambda event: (heading.configure(wraplength=max(100, event.width - 36)),
                                               self.status.configure(wraplength=max(100, event.width - 36))))
        footer = tk.Frame(body, bg=C["panel"])
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        self.fetch_btn = ttk.Button(footer, text="Fetch background", command=self.load)
        self.fetch_btn.pack(side="left")
        self.source_btn = ttk.Button(footer, text="View source", command=self.open_source, state="disabled")
        self.source_btn.pack(side="left", padx=8)
        self.close_btn = ttk.Button(footer, text="Close", command=self.close)
        self.close_btn.pack(side="right")
        area = tk.Frame(body, bg=C["panel"])
        area.pack(fill="both", expand=True)
        self.text = tk.Text(area, bg=C["input"], fg=C["text"], font=app.F.body, wrap="word",
                            relief="flat", padx=12, pady=12, state="disabled")
        scroll = ttk.Scrollbar(area, orient="vertical", command=self.text.yview)
        scroll.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(fill="both", expand=True)
        self._text("Only artist and album names are sent to TheAudioDB when you request background text. "
                   "Descriptions are cached locally. No music files, artwork or tags are changed.")

    def alive(self):
        return bool(self.win.winfo_exists())

    def _text(self, value):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        self.text.configure(state="disabled")

    def load(self):
        if self.running or not self.alive():
            return
        if not self.app.operation_lock.acquire(blocking=False):
            self.status.configure(text="Another operation is running. Try Fetch background when it finishes.")
            return
        self.running = True
        self.fetch_btn.state(["disabled"])
        self.status.configure(text="Looking up background…")

        def work():
            try:
                result = self.app.online_information.get(self.kind, self.artist, self.album)
            except Exception:
                result = None
            finally:
                self.app.operation_lock.release()
            self.app.ui_queue.put(("ui_callback", (self.finish, (result,))))

        threading.Thread(target=work, name="online-information", daemon=True).start()

    def finish(self, result):
        self.running = False
        if self.cancel.is_set() or not self.alive():
            return
        self.fetch_btn.state(["!disabled"])
        if result is None:
            self.status.configure(text="Could not fetch background. Wait a minute and try again.", fg=C["warn"])
            return
        labels = {"ready": "Background text", "missing": "No exact match found",
                  "ambiguous": "Several exact matches; no description selected", "unavailable": "No English text available"}
        self.status.configure(text="TheAudioDB · " + labels[result["status"]]
                                   + (" · Local cache" if result["cached"] else ""), fg=C["muted"])
        self._text(result["text"] or labels[result["status"]])
        self.url = result["url"]
        self.source_btn.state(["!disabled"] if self.url else ["disabled"])

    def open_source(self):
        if re.fullmatch(r"https://www\.theaudiodb\.com/" + self.kind + r"/[0-9]{1,20}", self.url):
            webbrowser.open(self.url)

    def close(self):
        self.cancel.set()
        self.win.destroy()
