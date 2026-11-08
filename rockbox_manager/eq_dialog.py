# SPDX-License-Identifier: GPL-2.0-only
"""Fit and compare EQ curves before exporting a separate preset."""
from __future__ import annotations

import math
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .eq_tools import export_refit, preview_refit
from .ui_theme import C


class EqDialog:
    def __init__(self, app):
        self.app, self.running, self.plan = app, False, None
        self.win = tk.Toplevel(app.root)
        self.win.title("EQ Refit")
        self.win.geometry("900x660")
        self.win.minsize(720, 540)
        self.win.configure(bg=C["panel"])
        self.win.transient(app.root)
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        body = tk.Frame(self.win, bg=C["panel"], padx=18, pady=18)
        body.pack(fill="both", expand=True)
        footer = tk.Frame(body, bg=C["panel"])
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        self.export_btn = ttk.Button(footer, text="Export new preset…", command=self.export)
        self.export_btn.pack(side="left")
        self.export_btn.state(["disabled"])
        ttk.Button(footer, text="Close", command=self.close).pack(side="right")
        tk.Label(body, text="EQ Refit", font=app.F.title, bg=C["panel"], fg=C["text"]).pack(anchor="w")
        tk.Label(body, text="Fit fewer active bands using PodBox's optimiser. The original preset is preserved.\n"
                 "Curves are a numerical approximation at the selected sample rate; listening/device results can differ.",
                 font=app.F.small, bg=C["panel"], fg=C["muted"], justify="left", wraplength=800).pack(fill="x", pady=8)
        self.path = tk.StringVar()
        row = tk.Frame(body, bg=C["panel"])
        row.pack(fill="x", pady=5)
        ttk.Entry(row, textvariable=self.path).pack(side="left", fill="x", expand=True)
        self.browse_btn = ttk.Button(row, text="Choose preset…", command=self.choose)
        self.browse_btn.pack(side="right", padx=(8, 0))
        controls = tk.Frame(body, bg=C["panel"])
        controls.pack(fill="x", pady=8)
        self.bands, self.tolerance, self.rate = tk.StringVar(value="5"), tk.StringVar(value="1.0"), tk.StringVar(value="44100")
        for label, variable, values in (("Maximum bands", self.bands, tuple(map(str, range(1, 10)))),
                                         ("Weighted tolerance (dB)", self.tolerance, ("0.5", "1.0", "2.0")),
                                         ("Sample rate", self.rate, ("44100", "48000"))):
            tk.Label(controls, text=label, bg=C["panel"], fg=C["text"], font=app.F.small).pack(side="left", padx=(0, 5))
            ttk.Combobox(controls, textvariable=variable, values=values, width=7, state="readonly").pack(side="left", padx=(0, 12))
        self.preview_btn = ttk.Button(body, text="Preview refit", command=self.preview)
        self.preview_btn.pack(anchor="w")
        self.status = tk.Label(body, text="Choose a ten-slot Rockbox/PodBox EQ .cfg preset.", font=app.F.small,
                               bg=C["panel"], fg=C["text"], justify="left", anchor="w", wraplength=800)
        self.status.pack(fill="x", pady=10)
        self.canvas = tk.Canvas(body, bg=C["input"], highlightthickness=0, height=260)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _: self.draw())
        for variable in (self.path, self.bands, self.tolerance, self.rate):
            variable.trace_add("write", lambda *_: self.invalidate())

    def close(self):
        if self.running:
            messagebox.showinfo("EQ Refit", "Wait for the fitting operation to finish.", parent=self.win)
            return
        self.win.destroy()

    def alive(self):
        return bool(self.win.winfo_exists())

    def invalidate(self):
        self.plan = None
        self.export_btn.state(["disabled"])
        self.draw()

    def choose(self):
        path = filedialog.askopenfilename(parent=self.win, filetypes=[("EQ preset", "*.cfg")])
        if path:
            self.path.set(path)

    def preview(self):
        if self.running:
            return
        if not self.app.operation_lock.acquire(blocking=False):
            self.status.configure(text="Another operation is running. Try again when it finishes.")
            return
        self.invalidate()
        self.running = True
        self.preview_btn.state(["disabled"])
        self.status.configure(text="Fitting curves… This may take a minute.")
        options = dict(max_bands=int(self.bands.get()), tolerance=float(self.tolerance.get()), rate=int(self.rate.get()))
        path = Path(self.path.get())
        self.app._set_busy("eq_refit", "Fitting EQ preset")
        self.app.cancel_btn.state(["disabled"])

        def work():
            try:
                result = preview_refit(path, **options)
            except Exception as exc:
                result = exc
            self.app.ui_queue.put(("ui_callback", (self.finish, (result, path, options))))
        threading.Thread(target=work, name="eq-refit", daemon=True).start()

    def finish(self, result, path, options):
        self.running = False
        self.app.operation_lock.release()
        self.app._end_busy("Ready")
        self.preview_btn.state(["!disabled"])
        if isinstance(result, Exception):
            self.status.configure(text=str(result))
            return
        if (str(path) != self.path.get() or options != dict(max_bands=int(self.bands.get()),
                tolerance=float(self.tolerance.get()), rate=int(self.rate.get()))):
            self.status.configure(text="Settings changed during fitting. Preview again.")
            return
        self.plan = result
        text = (f"{result.before} → {result.after} active bands at {result.rate} Hz. "
                f"Maximum error {result.max_error:.2f} dB; weighted {result.weighted_error:.2f} dB; "
                f"weighted RMS {result.rms_error:.2f} dB.\n"
                "Original = blue; refit = orange. Enable/precut, comments and other settings are preserved.")
        if result.peak_after > result.peak_before + .5:
            text += f"\nPeak boost rose to {result.peak_after:.1f} dB; review precut/headroom before use."
        if result.weighted_error > result.tolerance:
            text += "\nOver tolerance: export is disabled. Increase the band limit or tolerance and preview again."
        elif result.after < result.before:
            self.export_btn.state(["!disabled"])
        else:
            text += "\nAlready minimal; no refit to export."
        self.status.configure(text=text)
        self.draw()

    def draw(self):
        self.canvas.delete("all")
        if self.plan is None:
            return
        plan, w, h = self.plan, self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 100 or h < 80:
            return
        low = min(-3, math.floor(min(*plan.response_before, *plan.response_after) - 1))
        high = max(3, math.ceil(max(*plan.response_before, *plan.response_after) + 1))
        def x(f):
            return 46 + math.log(f / 20) / math.log(1000) * (w - 62)
        def y(db):
            return 20 + (high - db) / (high - low) * (h - 48)
        for db in (low, 0, high):
            self.canvas.create_line(46, y(db), w - 16, y(db), fill=C["muted"])
            self.canvas.create_text(24, y(db), text=f"{db} dB", fill=C["text"])
        for f in (20, 100, 1000, 10000, 20000):
            self.canvas.create_text(x(f), h - 12, text=str(f), fill=C["text"])
        for values, color in ((plan.response_before, "#69b7ff"), (plan.response_after, "#ffb35b")):
            points = [coordinate for f, db in zip(plan.frequencies, values) for coordinate in (x(f), y(db))]
            self.canvas.create_line(*points, fill=color, width=2)

    def export(self):
        plan = self.plan
        if plan is None:
            return
        path = filedialog.asksaveasfilename(parent=self.win, title="Export a new EQ preset", defaultextension=".cfg",
                    initialfile=plan.source.stem + "-refit.cfg", filetypes=[("EQ preset", "*.cfg")])
        if not path:
            return
        try:
            export_refit(plan, Path(path))
            self.status.configure(text=f"Verified new preset saved: {path}\nOriginal preserved. Precut is unchanged.")
        except Exception as exc:
            messagebox.showerror("EQ Refit", str(exc), parent=self.win)
