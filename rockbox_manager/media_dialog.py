"""Selectable FLAC and artwork maintenance, separate from the fast library index."""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import media_tools as media


class MediaDialog:
    def __init__(self, app, music_root, local_dir, colors, open_folder):
        self.app, self.music_root, self.local_dir = app, music_root.resolve(), local_dir
        self.library_root = self.music_root
        self.open_folder = open_folder
        self.size = app.output_size()
        self.backups = app.media_backup_var.get()
        self.backup_root = Path(app.media_backup_path_var.get()).expanduser()
        self.ffmpeg = app.ffmpeg_path_var.get()
        self.running = False
        self.candidates = {}
        self.win = tk.Toplevel(app.root)
        self.win.title("Prepare media for PodBox")
        self.win.geometry("1060x730")
        self.win.minsize(900, 650)
        self.win.configure(bg=colors["panel"])
        self.win.transient(app.root)
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        frame = tk.Frame(self.win, bg=colors["panel"], padx=18, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="Prepare media for PodBox", font=app.F.title,
                 bg=colors["panel"], fg=colors["text"], anchor="w").pack(fill="x")
        tk.Label(frame, text=f"FLAC: 16-bit, up to 44.1 kHz. Artwork: {self.size}×{self.size} px (Settings).\n"
                 "Preview a folder or the whole library, then select files to change. Small artwork needs a larger source.",
                 font=app.F.small, bg=colors["panel"], fg=colors["muted"],
                 anchor="w", justify="left", wraplength=840).pack(fill="x", pady=(5, 10))
        backup_text = f"Local backups: {self.backup_root}" if self.backups else "Local backups OFF — selected originals will be replaced."
        tk.Label(frame, text=backup_text, font=app.F.small, bg=colors["panel"],
                 fg=colors["muted"] if self.backups else colors["warn"], anchor="w",
                 wraplength=840, justify="left").pack(fill="x", pady=(0, 8))
        # Reserve the footer before laying out the expanding results list.
        footer = tk.Frame(frame, bg=colors["panel"])
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        self.apply_btn = ttk.Button(footer, text="Apply selected…", style="Accent.TButton", command=self.apply)
        self.apply_btn.pack(side="left")
        self.find_btn = ttk.Button(footer, text="Find better artwork…", command=self.find_artwork)
        self.find_btn.pack(side="left", padx=8)
        self.cancel_btn = ttk.Button(footer, text="Cancel operation", command=app.cancel_event.set)
        self.cancel_btn.pack(side="right", padx=8)
        self.close_btn = ttk.Button(footer, text="Close", command=self.close)
        self.close_btn.pack(side="right")
        self.details = tk.Text(frame, height=5, wrap="word", font=app.F.small, bg=colors["input"],
                               fg=colors["text"], borderwidth=0, padx=8, pady=8, state="disabled")
        self.details.pack(side="bottom", fill="x", pady=(8, 0))
        toolbar = tk.Frame(frame, bg=colors["panel"])
        toolbar.pack(fill="x")
        self.audio_var = tk.BooleanVar(value=True)
        self.artwork_var = tk.BooleanVar(value=True)
        self.fresh_var = tk.BooleanVar(value=False)
        self.controls = []
        scope = tk.Frame(frame, bg=colors["panel"])
        scope.pack(fill="x", before=toolbar, pady=(0, 8))
        self.scope_var = tk.StringVar(value=str(self.music_root))
        self.scope_entry = ttk.Entry(scope, textvariable=self.scope_var, state="readonly")
        self.scope_entry.pack(side="left", fill="x", expand=True)
        for label, command in (("Choose folder…", self.choose_folder), ("Whole library", self.whole_library)):
            button = ttk.Button(scope, text=label, command=command)
            button.pack(side="left", padx=(6, 0))
            self.controls.append(button)
        for text, var in (("FLAC", self.audio_var), ("Artwork", self.artwork_var), ("Ignore cache", self.fresh_var)):
            button = ttk.Checkbutton(toolbar, text=text, variable=var)
            button.pack(side="left", padx=(0, 10))
            self.controls.append(button)
        self.scan_btn = ttk.Button(toolbar, text="Scan media", command=self.scan)
        self.scan_btn.pack(side="right")
        self.controls.append(self.scan_btn)
        select = tk.Frame(frame, bg=colors["panel"])
        select.pack(fill="x", pady=8)
        for label, kind in (("Select FLAC", "flac"), ("Select large artwork", "artwork"), ("Select both", "all"), ("Clear selection", "none")):
            button = ttk.Button(select, text=label, command=lambda k=kind: self.select(k))
            button.pack(side="left", padx=(0, 6))
            self.controls.append(button)
        self.status = tk.Label(frame, text="Ready to scan. Select rows with Ctrl/Shift, or use the selection buttons.",
                               font=app.F.small, bg=colors["panel"], fg=colors["text"],
                               anchor="w", justify="left", wraplength=840)
        self.status.pack(fill="x", pady=(0, 8))
        content = tk.Frame(frame)
        content.pack(fill="both", expand=True)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(content, columns=("kind", "change", "file"), show="headings", selectmode="extended")
        for name, title, width in (("kind", "Type", 105), ("change", "Current → target", 350), ("file", "File", 480)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=70, stretch=name == "file")
        scroll = ttk.Scrollbar(content, command=self.tree.yview)
        horizontal = ttk.Scrollbar(content, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.update_buttons())
        self.cancel_btn.state(["disabled"])
        self.update_buttons()

    def close(self):
        if self.running:
            messagebox.showinfo("Prepare media", "Cancel the operation and wait for it to stop before closing or disconnecting the player.", parent=self.win)
            return
        self.win.destroy()

    def select(self, kind):
        self.tree.selection_set([key for key, c in self.candidates.items()
                                 if c.kind in ("flac", "artwork") and kind in (c.kind, "all")])

    def selected(self):
        return [self.candidates[k] for k in self.tree.selection() if k in self.candidates]

    def update_buttons(self):
        selected = self.selected()
        self.apply_btn.state(["!disabled" if not self.running and any(c.kind != "small_artwork" for c in selected) else "disabled"])
        self.find_btn.state(["!disabled" if not self.running and len(selected) == 1 and selected[0].kind != "flac" else "disabled"])

    def detail(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", text)
        self.details.configure(state="disabled")

    def progress(self, text):
        self.app.ui_queue.put(("ui_callback", (self.show_progress, (text,))))

    def show_progress(self, text):
        self.status.configure(text=text if len(text) <= 160 else text[:157] + "…")

    def run(self, kind, work):
        if self.running or not self.app.operation_lock.acquire(blocking=False):
            return
        self.running = True
        self.app.cancel_event.clear()
        self.app._set_busy("media", kind)
        for button in [*self.controls, self.close_btn]:
            button.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        self.update_buttons()
        self.status.configure(text=kind + "… Keep the player connected.")

        def worker():
            try:
                result, error = work(), None
                if isinstance(result, media.ApplyResult):
                    try:
                        self.refresh_artwork(result.completed)
                    except Exception as exc:
                        result.errors.append(f"Local artwork index needs a Rescan: {exc}")
            except Exception as exc:
                result, error = None, str(exc)
            self.app.ui_queue.put(("ui_callback", (self.finish, (result, error))))
        threading.Thread(target=worker, name="media-tools", daemon=True).start()

    def refresh_artwork(self, completed):
        from .gui import detect_artwork
        indexed_root = self.app.db.get_meta("music_root", "")
        if not indexed_root or Path(indexed_root).resolve() != self.library_root:
            return
        paths = {(self.music_root / p).parent.relative_to(self.library_root).as_posix()
                 for p in completed if Path(p).suffix.lower() == ".jpg"
                 and (self.music_root / p).is_relative_to(self.library_root)}
        updates = []
        for item in self.app.db.items_for_paths(list(paths)):
            exists, name, mtime, size = detect_artwork(self.library_root / item.relative_path, self.app.output_name)
            updates.append((item.id, dict(artwork_name=name, artwork_exists=exists,
                           artwork_mtime_ns=mtime, artwork_size=size, problem="")))
        self.app.db.set_item_states_bulk(updates)

    def finish(self, result, error):
        self.running = False
        self.app.operation_lock.release()
        self.app._end_busy("Ready")
        self.cancel_btn.state(["disabled"])
        for button in [*self.controls, self.close_btn]:
            button.state(["!disabled"])
        if error:
            self.status.configure(text="Operation stopped.")
            self.detail(error)
            messagebox.showerror("Prepare media", error, parent=self.win)
        elif isinstance(result, media.ScanResult):
            self.tree.delete(*self.tree.get_children())
            self.candidates = {str(n): c for n, c in enumerate(result.candidates)}
            for key, c in self.candidates.items():
                label = {"flac": "FLAC", "artwork": "Large artwork", "small_artwork": "Small artwork"}[c.kind]
                self.tree.insert("", "end", iid=key, values=(label, c.description(self.size), c.relative))
            counts = {k: sum(c.kind == k for c in result.candidates) for k in ("flac", "artwork", "small_artwork")}
            self.status.configure(text=f"{'Scan cancelled' if result.cancelled else 'Scan complete'}: {result.checked} files checked ({result.cached} cached). "
                                  f"{counts['flac']} FLAC, {counts['artwork']} large and {counts['small_artwork']} small artwork. {len(result.errors)} errors.")
            self.detail("\n".join(result.errors) or "Select files to convert or resize. Small artwork is listed for replacement, never upscaled.")
        else:
            completed = set(result.completed)
            for key, c in list(self.candidates.items()):
                if c.relative in completed:
                    self.tree.delete(key)
                    del self.candidates[key]
            self.status.configure(text=f"{'Cancelled' if result.cancelled else 'Finished'}: {len(completed)} files replaced; {len(result.errors)} errors. Completed files are kept.")
            backup = f"Originals: {result.backup_folder}" if result.backup_folder else "Local backups were disabled."
            self.detail(backup + "\n" + "\n".join(result.errors) + "\nSafely eject and restart the player. After audio conversion, use Rockbox's Database → Update Now to refresh file properties.")
            self.app.photo_cache.clear()
            self.app.refresh_counts()
            self.app.reload_gallery(reset_scroll=False)
            if self.app.selected_item_id:
                self.app.show_detail(self.app.db.get_item(self.app.selected_item_id))
        self.app.log_app(self.status.cget("text"))
        for error_text in getattr(result, "errors", []):
            self.app.log_app(error_text)
        self.update_buttons()

    def scan(self):
        if not self.audio_var.get() and not self.artwork_var.get():
            return
        audio, artwork, fresh = self.audio_var.get(), self.artwork_var.get(), self.fresh_var.get()
        self.run("Scanning media", lambda: media.scan(self.music_root, self.size, self.local_dir / "media-scan.sqlite3",
                 self.app.cancel_event, self.progress, audio=audio, artwork=artwork, fresh=fresh))

    def set_scope(self, folder):
        if self.running:
            return
        self.music_root = Path(folder).resolve(strict=True)
        self.scope_var.set(str(self.music_root))
        self.tree.delete(*self.tree.get_children())
        self.candidates.clear()
        self.detail("")
        self.status.configure(text="Ready to scan this folder and its subfolders.")
        self.update_buttons()

    def choose_folder(self):
        chosen = filedialog.askdirectory(parent=self.win, title="Choose a folder to inspect (including subfolders)",
                                         initialdir=str(self.music_root))
        if chosen:
            try:
                self.set_scope(chosen)
            except OSError as exc:
                messagebox.showerror("Choose folder", str(exc), parent=self.win)
                return
            self.scan()

    def whole_library(self):
        try:
            self.set_scope(self.library_root)
        except OSError as exc:
            messagebox.showerror("Music folder", str(exc), parent=self.win)

    def apply(self):
        chosen = [c for c in self.selected() if c.kind in ("flac", "artwork")]
        if not chosen:
            return
        if any(c.kind == "flac" for c in chosen):
            try:
                media.find_ffmpeg(self.ffmpeg)
            except media.MediaError as exc:
                messagebox.showerror("FFmpeg", str(exc), parent=self.win)
                return
        backup = f"Verified original copies will be saved to:\n{self.backup_root}" if self.backups else "LOCAL BACKUPS ARE OFF. The originals will be replaced without a recovery copy."
        if not messagebox.askyesno("Replace selected media",
                f"Replace {len(chosen)} selected files in:\n{self.music_root}\n\n{backup}\n\n"
                "FLAC conversion reduces audio resolution to 16-bit and at most 44.1 kHz. Tags and embedded covers are retained. "
                f"Large artwork becomes {self.size}×{self.size}; non-square images are centre cropped. "
                "Small artwork is skipped. Keep the player connected.", parent=self.win):
            return
        self.run("Preparing media", lambda: media.apply(self.music_root, chosen, self.size,
                 self.backup_root if self.backups else None, self.ffmpeg, self.app.cancel_event, self.progress))

    def find_artwork(self):
        selected = self.selected()
        if len(selected) != 1 or selected[0].kind == "flac":
            return
        folder = (self.music_root / selected[0].relative).parent
        relative = folder.relative_to(self.library_root).as_posix() if folder.is_relative_to(self.library_root) else ""
        items = self.app.db.items_for_paths([relative])
        indexed_root = self.app.db.get_meta("music_root", "")
        if not items or not indexed_root or Path(indexed_root).resolve() != self.library_root:
            messagebox.showinfo("Find artwork", "Run the library Rescan first so this artist or album is indexed. You can then use Search online in its artwork panel.", parent=self.win)
            return
        self.close()
        self.app.set_view("library")
        self.app.select_item(items[0].id)
        self.app.open_picker()
