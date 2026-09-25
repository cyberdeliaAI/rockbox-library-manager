"""Preview, local backup and recovery controls for the device's Rockbox database."""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import rockbox_database as database


class DatabaseDialog:
    def __init__(self, app, music_root: Path, backup_root: Path, colors: dict, open_folder):
        self.app = app
        self.music_root = music_root
        self.device = database.discover_device(music_root)
        self.backup_root = backup_root
        self.open_folder = open_folder
        self.plan = None
        self.running = False
        self.win = tk.Toplevel(app.root)
        self.win.title("Rockbox database")
        self.win.configure(bg=colors["panel"])
        self.win.geometry("840x640")
        self.win.minsize(740, 540)
        self.win.transient(app.root)
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        frame = tk.Frame(self.win, bg=colors["panel"], padx=18, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="Rockbox database", font=app.F.title, bg=colors["panel"],
                 fg=colors["text"], anchor="w").pack(fill="x")
        tk.Label(frame, text=f"Device: {self.device}\nLocal backups: {backup_root}",
                 font=app.F.small, bg=colors["panel"], fg=colors["muted"],
                 anchor="w", justify="left", wraplength=750).pack(fill="x", pady=(5, 10))
        tk.Label(frame, text="Preview changes to artist, album artist, album, genre and year from the music tags.\n"
                 "A verified local backup is required before writing. Play counts, ratings and resume positions are retained.\n"
                 "New or deleted tracks need Rockbox's Update Now. Library-only merges do not change music tags.\n"
                 "Direct database updates are experimental until verified on your player. Eject and reboot after writing.",
                 font=app.F.small, bg=colors["panel"], fg=colors["muted"],
                 anchor="w", justify="left", wraplength=750).pack(fill="x", pady=(0, 12))
        # Keep actions outside the expandable text area so scaling cannot hide them.
        actions = tk.Frame(frame, bg=colors["panel"])
        actions.pack(side="bottom", fill="x", pady=(12, 0))
        self.apply_btn = ttk.Button(actions, text="Back up and apply", style="Accent.TButton", command=self.apply)
        self.apply_btn.pack(side="left")
        self.close_btn = ttk.Button(actions, text="Close", command=self.close)
        self.close_btn.pack(side="right")
        toolbar = tk.Frame(frame, bg=colors["panel"])
        toolbar.pack(fill="x", pady=(0, 10))
        self.buttons = []
        for label, command in (("Preview tag update", self.preview), ("Back up now", self.backup),
                               ("Restore backup…", self.restore), ("Open local backups", self.show_backups)):
            button = ttk.Button(toolbar, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            self.buttons.append(button)
        self.status = tk.Label(frame, text="Create a preview before applying an update.",
                               bg=colors["panel"], fg=colors["text"], font=app.F.small,
                               anchor="w", justify="left", wraplength=750)
        self.status.pack(fill="x", pady=(0, 8))
        content = tk.Frame(frame)
        content.pack(fill="both", expand=True)
        self.text = tk.Text(content, wrap="word", font=app.F.small, bg=colors["input"],
                            fg=colors["text"], borderwidth=0, padx=10, pady=10, state="disabled")
        scrollbar = ttk.Scrollbar(content, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.apply_btn.state(["disabled"])

    def close(self):
        if self.running:
            messagebox.showinfo("Rockbox database", "Wait for the database operation to finish before closing or disconnecting the iPod.", parent=self.win)
            return
        self.win.destroy()

    def show_backups(self):
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.open_folder(self.backup_root)

    def show_text(self, text):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", text)
        self.text.configure(state="disabled")

    def run(self, kind, work):
        if self.running or not self.app.operation_lock.acquire(blocking=False):
            messagebox.showinfo("Rockbox database", "Another operation is still running. Wait for it to finish.", parent=self.win)
            return
        self.running = True
        for button in (*self.buttons, self.apply_btn, self.close_btn):
            button.state(["disabled"])
        self.status.configure(text=f"{kind}… Keep the iPod connected.")
        self.app._set_busy("rockbox_database", kind)
        self.app.cancel_btn.state(["disabled"])

        def worker():
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, str(exc)
            self.app.ui_queue.put(("ui_callback", (self.finish, (kind, result, error))))

        threading.Thread(target=worker, name="rockbox-database", daemon=True).start()

    def finish(self, kind, result, error):
        self.running = False
        self.app.operation_lock.release()
        self.app._end_busy("Ready")
        for button in (*self.buttons, self.close_btn):
            button.state(["!disabled"])
        self.plan = result if isinstance(result, database.UpdatePlan) else None
        if error:
            self.status.configure(text="Operation stopped. See the details below.")
            self.show_text(error)
            self.app.log_app(f"Rockbox database: {error}")
            messagebox.showerror("Rockbox database", error, parent=self.win)
        elif self.plan is not None:
            self.status.configure(text=f"Checked {self.plan.checked_tracks} tracks; {self.plan.changed_tracks} need an update. No database files have been changed.")
            self.show_text("\n\n".join(self.plan.changes) or "The supported tags of all indexed tracks in this music folder are up to date.")
            if self.plan.changed_tracks:
                self.apply_btn.state(["!disabled"])
        else:
            if kind == "Creating local backup":
                text = f"Verified local backup saved:\n{result}"
            else:
                text = (f"{kind} completed. Local safety backup:\n{result}\n\n"
                        "Safely eject the iPod and reboot Rockbox to reload the database.")
            self.status.configure(text="Completed.")
            self.show_text(text)
            self.app.log_app(text)

    def preview(self):
        import json
        try:
            moves = json.loads(self.app.db.get_meta("rockbox_folder_moves", "[]"))
        except (ValueError, TypeError):
            moves = []
        def progress(text):
            self.app.ui_queue.put(("ui_callback", (self.show_progress, (text,))))
        self.run("Preparing preview", lambda: database.preview_update(self.music_root, moves=moves, progress=progress))

    def show_progress(self, text):
        if self.running:
            self.status.configure(text=text)

    def backup(self):
        self.run("Creating local backup", lambda: database.backup_database(self.device, self.backup_root))

    def apply(self):
        plan = self.plan
        if plan is None or not plan.changed_tracks:
            return
        if not messagebox.askyesno("Update Rockbox database",
                                  f"Update {plan.changed_tracks} tracks on {self.device}?\n\n"
                                  f"A verified backup will be saved here first:\n{self.backup_root}\n\n"
                                  "Keep the iPod connected until completion, then safely eject and reboot Rockbox.", parent=self.win):
            return
        self.run("Updating Rockbox database", lambda: database.apply_update(plan, self.backup_root))

    def restore(self):
        selected = filedialog.askopenfilename(parent=self.win, title="Select a local backup manifest",
                                             initialdir=str(self.backup_root), filetypes=[("Backup manifest", "manifest.json")])
        if not selected:
            return
        folder = Path(selected).parent
        if not messagebox.askyesno("Restore Rockbox database",
                                  f"Restore this database backup?\n{folder}\n\nTo: {self.device}\n\n"
                                  "Ratings and play history will return to their state at backup time. "
                                  "The current database will be backed up locally first. Music files and tags are not restored.", parent=self.win):
            return
        self.run("Restoring Rockbox database", lambda: database.restore_backup(self.device, folder, self.backup_root))
