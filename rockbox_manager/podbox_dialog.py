# SPDX-License-Identifier: GPL-2.0-only
"""Separate, reviewed PodBox library preparation, backups and recovery."""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import podbox_tools as tools
from .ui_theme import C


class PodBoxDialog:
    def __init__(self, app, device, local_dir, open_folder):
        self.app, self.open_folder = app, open_folder
        self.local_dir = Path(local_dir)
        self.backups = self.local_dir / "podbox-library-backups"
        self.running, self.plan = False, None
        self.cancel = threading.Event()
        self.win = tk.Toplevel(app.root)
        self.win.title("PodBox preparation tools")
        self.win.geometry("1000x740")
        self.win.minsize(800, 600)
        self.win.configure(bg=C["panel"])
        self.win.transient(app.root)
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        body = tk.Frame(self.win, bg=C["panel"], padx=18, pady=18)
        body.pack(fill="both", expand=True)
        footer = tk.Frame(body, bg=C["panel"])
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        self.apply_btn = ttk.Button(footer, text="Back up and install preview…", command=self.apply, style="Accent.TButton")
        self.apply_btn.pack(side="left")
        self.apply_btn.state(["disabled"])
        self.cancel_btn = ttk.Button(footer, text="Cancel preview", command=self.cancel.set)
        self.cancel_btn.pack(side="right", padx=8)
        self.cancel_btn.state(["disabled"])
        self.close_btn = ttk.Button(footer, text="Close", command=self.close)
        self.close_btn.pack(side="right")
        tk.Label(body, text="PodBox preparation tools", bg=C["panel"], fg=C["text"], font=app.F.title).pack(anchor="w")
        tk.Label(body, text="PodBox layout 5 on iPod Video/Classic. Preview runs the player's engines against a local library copy.\n"
                 "Music is read-only. Installation requires a verified local backup and an unchanged player.\n"
                 "Retained database paths keep play history, ratings and resume positions; a rebuild can renumber rows.\n"
                 "Soundscan measures part of each track (dBFS, tempo and pitch); it does not set ReplayGain or LUFS tags.",
                 bg=C["panel"], fg=C["muted"], font=app.F.small, justify="left", anchor="w", wraplength=900).pack(fill="x", pady=8)
        self.device_var = tk.StringVar(value=str(device) if device else "")
        row = tk.Frame(body, bg=C["panel"])
        row.pack(fill="x", pady=5)
        self.entry = ttk.Entry(row, textvariable=self.device_var)
        self.entry.pack(side="left", fill="x", expand=True)
        self.choose_btn = ttk.Button(row, text="Choose player root…", command=self.choose)
        self.choose_btn.pack(side="right", padx=(8, 0))
        self.operation = tk.StringVar(value="update")
        operations = tk.Frame(body, bg=C["panel"])
        operations.pack(fill="x", pady=8)
        self.radios = []
        for index, (text, value) in enumerate((("Update / build database", "update"), ("Rebuild database", "rebuild"),
                                             ("Prepare artwork cache", "artwork"), ("Soundscan", "soundscan"))):
            radio = ttk.Radiobutton(operations, text=text, value=value, variable=self.operation)
            radio.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 15), pady=2)
            self.radios.append(radio)
        toolbar = tk.Frame(body, bg=C["panel"])
        toolbar.pack(fill="x", pady=6)
        self.preview_btn = ttk.Button(toolbar, text="Prepare local preview", command=self.preview)
        self.preview_btn.grid(row=0, column=0, sticky="w", pady=3)
        self.restore_btn = ttk.Button(toolbar, text="Restore backup…", command=self.restore)
        self.restore_btn.grid(row=0, column=1, sticky="w", padx=8, pady=3)
        self.recover_btn = ttk.Button(toolbar, text="Recover interrupted installation…", command=self.recover)
        self.recover_btn.grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
        toolbar.columnconfigure(2, weight=1)
        ttk.Button(toolbar, text="Open backups", command=self.show_backups).grid(row=0, column=3, sticky="e", pady=3)
        self.status = tk.Label(body, text=f"Local backups: {self.backups}\nChoose the player root and prepare a preview.",
                               bg=C["panel"], fg=C["text"], font=app.F.small, anchor="w", justify="left", wraplength=900)
        self.status.pack(fill="x", pady=8)
        box = tk.Frame(body)
        box.pack(fill="both", expand=True)
        self.text = tk.Text(box, bg=C["input"], fg=C["text"], font=app.F.small, wrap="word", state="disabled", relief="flat", padx=8, pady=8)
        scroll = ttk.Scrollbar(box, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.device_var.trace_add("write", lambda *_: self.invalidate())
        self.operation.trace_add("write", lambda *_: self.invalidate())

    def invalidate(self):
        if self.plan:
            self.plan.discard()
        self.plan = None
        self.apply_btn.state(["disabled"])

    def alive(self):
        return bool(self.win.winfo_exists())

    def close(self):
        if self.running:
            messagebox.showinfo("PodBox tools", "Wait for completion before closing or disconnecting. A preview can be cancelled.", parent=self.win)
            return
        self.invalidate()
        self.win.destroy()

    def choose(self):
        path = filedialog.askdirectory(parent=self.win, title="Choose the player root containing .rockbox")
        if path:
            self.device_var.set(path)

    def show_backups(self):
        self.backups.mkdir(parents=True, exist_ok=True)
        self.open_folder(self.backups)

    def show_text(self, text):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", text)
        self.text.configure(state="disabled")

    def progress(self, text):
        self.app.ui_queue.put(("ui_callback", (self.show_progress, (text,))))

    def show_progress(self, text):
        if self.running:
            self.status.configure(text=text)

    def run(self, kind, work, *, cancellable=False):
        if self.running or not self.app.operation_lock.acquire(blocking=False):
            self.status.configure(text="Another operation is running. Try again when it finishes.")
            return
        self.running = True
        self.cancel.clear()
        for widget in (self.entry, self.choose_btn, self.preview_btn, self.restore_btn, self.recover_btn,
                       self.apply_btn, self.close_btn, *self.radios):
            widget.state(["disabled"])
        self.cancel_btn.state(["!disabled"] if cancellable else ["disabled"])
        self.status.configure(text=kind + "… Keep the player connected.")
        self.app._set_busy("podbox_tools", kind)
        self.app.cancel_btn.state(["disabled"])
        def worker():
            try:
                result = work()
            except Exception as exc:
                result = exc
            self.app.ui_queue.put(("ui_callback", (self.finish, (kind, result))))
        threading.Thread(target=worker, name="podbox-tools", daemon=True).start()

    def finish(self, kind, result):
        self.running = False
        self.app.operation_lock.release()
        self.app._end_busy("Ready")
        for widget in (self.entry, self.choose_btn, self.preview_btn, self.restore_btn, self.recover_btn,
                       self.close_btn, *self.radios):
            widget.state(["!disabled"])
        self.cancel_btn.state(["disabled"])
        if isinstance(result, Exception):
            self.status.configure(text="Operation stopped. See details below.")
            self.show_text(str(result))
            self.app.log_app(f"PodBox tools: {result}")
        elif isinstance(result, tools.PodBoxPlan):
            self.plan = result
            self.status.configure(text=f"Preview complete: {len(result.changes)} changed library files. The player has not been changed.")
            changes = [f"{'Remove' if p not in result.after else 'Add' if p not in result.before else 'Replace'}: {p}" for p in result.changes]
            self.show_text("\n".join(changes or ["No changes needed."]) + "\n\nNative engine report:\n" + result.output
                           + f"\n\nFrozen FAT mount offset: {result.offset // 60:+d} minutes.\n"
                           "Backups include the complete PodBox library. Music tags and folder moves are not restored.")
            if result.changes:
                self.apply_btn.state(["!disabled"])
        else:
            self.invalidate()
            self.status.configure(text="Completed. Safely eject and reboot PodBox after installing or restoring.")
            self.show_text(str(result))
            self.app.log_app(f"PodBox tools: {kind} completed; {result}")

    def preview(self):
        if self.running:
            return
        self.invalidate()
        device, operation = Path(self.device_var.get()), self.operation.get()
        self.run("Preparing PodBox preview", lambda: tools.preview(device, operation, self.local_dir / "podbox-previews",
                 cancel=self.cancel, progress=self.progress), cancellable=True)

    def apply(self):
        plan = self.plan
        if not plan or not plan.changes:
            return
        if not messagebox.askyesno("Install PodBox preview",
                f"Install this {plan.operation} preview on {plan.device}?\n\n{len(plan.changes)} library files change. "
                f"A verified complete library backup is required here first:\n{self.backups}\n\n"
                "Keep the player connected until completion, then safely eject and reboot PodBox.", parent=self.win):
            return
        self.run("Installing PodBox preview", lambda: tools.apply(plan, self.backups))

    def recover(self):
        device = Path(self.device_var.get())
        if messagebox.askyesno("Recover PodBox installation", f"Recover the interrupted library installation on {device}?\n"
                              "The verified local backup is checked first. Music is not changed.", parent=self.win):
            self.invalidate()
            self.run("Recovering PodBox installation", lambda: tools.recover(device))

    def restore(self):
        path = filedialog.askopenfilename(parent=self.win, title="Choose a PodBox backup manifest",
                      initialdir=str(self.backups), filetypes=[("PodBox backup manifest", "manifest.json")])
        if not path:
            return
        device = Path(self.device_var.get())
        if messagebox.askyesno("Restore PodBox library", f"Restore {Path(path).parent} to {device}?\n\n"
                "Play history, ratings, artwork cache and sound analysis return to the backup state. "
                "Music tags and folder moves are not restored. A local backup of the current library is made first.", parent=self.win):
            self.invalidate()
            self.run("Restoring PodBox library", lambda: tools.restore(device, Path(path).parent, self.backups))
