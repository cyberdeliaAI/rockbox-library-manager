# SPDX-License-Identifier: GPL-2.0-only
"""Tk listening/history and explicit audio checks using the app's worker queue."""
from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
import json
import os
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import integrity, scrobbling
from .listening import ListeningStore, PAGE_SIZE, preview_log
from .ui_theme import C
from .widgets import ScrollFrame


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def text_box(parent, height=8):
    area = tk.Frame(parent, bg=C["panel"])
    area.pack(fill="both", expand=True)
    text = tk.Text(area, height=height, wrap="word", bg=C["input"], fg=C["text"], state="disabled")
    scroll = ttk.Scrollbar(area, command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    text.pack(fill="both", expand=True)
    return text


def set_text(widget, value):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", value)
    widget.configure(state="disabled")


class ActivityDialog:
    def __init__(self, app, title):
        self.app, self.running = app, False
        self.cancel = threading.Event()
        self.controls, self.scroll_forms = [], []
        self.win = tk.Toplevel(app.root)
        self.win.title(title)
        self.win.configure(bg=C["panel"])
        self.win.transient(app.root)
        self.win.geometry(f"{min(1040, self.win.winfo_screenwidth()-80)}x{min(760, self.win.winfo_screenheight()-80)}")
        self.win.minsize(700, 480)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.body = tk.Frame(self.win, bg=C["panel"], padx=14, pady=14)
        self.body.pack(fill="both", expand=True)
        footer = tk.Frame(self.body, bg=C["panel"])
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        self.status = tk.Label(footer, text="Ready", bg=C["panel"], fg=C["text"], anchor="w", justify="left", wraplength=700)
        self.status.pack(fill="x", pady=4)
        ttk.Button(footer, text="Close", command=self.close).pack(side="right")
        self.cancel_btn = ttk.Button(footer, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="right", padx=8)
        footer.bind("<Configure>", lambda e: self.status.configure(wraplength=max(100, e.width)))

    def alive(self):
        return bool(self.win.winfo_exists())

    def close(self):
        if self.running:
            self.status.configure(text="Wait for completion, or cancel. A request in flight must finish recording its outcome.")
            return
        for form in self.scroll_forms:
            self.app._scrollables.pop(str(form.canvas), None)
        self.win.destroy()

    def button(self, parent, text, command, **pack):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(**pack)
        self.controls.append(button)
        return button

    def progress(self, value):
        self.app.ui_queue.put(("ui_callback", (self.show_progress, (value,))))

    def show_progress(self, value):
        if self.running:
            self.status.configure(text=value)

    def idle(self):
        pass

    def run(self, title, work, success, *, cancellable=True):
        if self.running or not self.app.operation_lock.acquire(blocking=False):
            self.status.configure(text="Another operation is running. Try again when it finishes.")
            return
        self.running = True
        self.cancel.clear()
        self.old_states = [(w, w.instate(["disabled"])) for w in self.controls]
        for w, _ in self.old_states:
            w.state(["disabled"])
        self.cancel_btn.state(["!disabled"] if cancellable else ["disabled"])
        self.status.configure(text=title)
        self.app._set_busy("listening_activity", title)
        self.app.cancel_btn.state(["disabled"])
        def worker():
            try:
                value = work()
            except Exception as exc:
                value = exc
            self.app.ui_queue.put(("ui_callback", (self.finish, (value, success))))
        threading.Thread(target=worker, name="listening-activity", daemon=True).start()

    def finish(self, value, success):
        self.running = False
        self.app.operation_lock.release()
        self.app._end_busy("Ready")
        self.cancel_btn.state(["disabled"])
        for w, disabled in self.old_states:
            w.state(["disabled"] if disabled else ["!disabled"])
        try:
            if isinstance(value, Exception):
                self.status.configure(text=str(value))
            else:
                success(value)
        except Exception as exc:
            self.status.configure(text=str(exc))
        self.idle()


class ListeningDialog(ActivityDialog):
    def __init__(self, app, local_dir):
        super().__init__(app, "Listening history and statistics")
        self.store = ListeningStore(Path(local_dir) / "listening.sqlite3")
        self.credentials_path = Path(local_dir) / "scrobbling.json"
        try:
            self.accounts = scrobbling.load_accounts(self.credentials_path)
            credential_error = ""
        except (ValueError, OSError):
            self.accounts = {}
            credential_error = "Saved scrobbling settings could not be read. Local history remains available; repair the private settings file before reconnecting."
        self.preview, self.plan, self.pending_auth = None, None, None
        self.events, self.page_number, self.total = {}, 0, 0
        notebook = ttk.Notebook(self.body)
        notebook.pack(fill="both", expand=True)
        history, stats, accounts = (tk.Frame(notebook, bg=C["panel"], padx=10, pady=10) for _ in range(3))
        for frame, label in ((history, "History"), (stats, "Statistics"), (accounts, "Scrobbling · optional")):
            notebook.add(frame, text=label)
        self.path, self.offset, self.search = tk.StringVar(), tk.StringVar(), tk.StringVar()
        row = tk.Frame(history, bg=C["panel"])
        row.pack(fill="x")
        self.button(row, "Choose log…", self.choose, side="left")
        entry = ttk.Entry(row, textvariable=self.path)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        self.controls.append(entry)
        row = tk.Frame(history, bg=C["panel"])
        row.pack(fill="x", pady=6)
        tk.Label(row, text="UTC offset (minutes, only if unknown):", bg=C["panel"], fg=C["text"]).pack(side="left")
        offset = ttk.Entry(row, textvariable=self.offset, width=7)
        offset.pack(side="left", padx=6)
        self.controls.append(offset)
        row = tk.Frame(history, bg=C["panel"])
        row.pack(fill="x", pady=4)
        self.button(row, "Preview log", self.preview_log, side="left")
        self.import_btn = self.button(row, "Import reviewed log", self.import_log, side="left", padx=6)
        self.import_btn.state(["disabled"])
        row = tk.Frame(history, bg=C["panel"])
        row.pack(fill="x", pady=6)
        self.button(row, "Local history", self.load, side="left")
        search = ttk.Entry(row, textvariable=self.search, width=18)
        search.pack(side="left", padx=6)
        self.controls.append(search)
        self.button(row, "Search", self.search_history, side="left")
        row = tk.Frame(history, bg=C["panel"])
        row.pack(fill="x", pady=4)
        self.button(row, "Previous", lambda: self.change_page(-1), side="left", padx=6)
        self.button(row, "Next", lambda: self.change_page(1), side="left")
        self.button(row, "Export…", self.export, side="right")
        self.note = tk.Label(history, bg=C["panel"], fg=C["text"], anchor="w", justify="left", wraplength=900)
        self.note.pack(fill="x", pady=5)
        columns = ("utc", "artist", "album", "title", "rating", "duration", "receipt")
        area = tk.Frame(history)
        area.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(area, columns=columns, show="headings", selectmode="extended")
        for name, width in zip(columns, (165, 120, 120, 180, 50, 70, 90)):
            self.tree.heading(name, text=name.title())
            self.tree.column(name, width=width, minwidth=40)
        scroll = ttk.Scrollbar(area, command=self.tree.yview)
        horizontal = ttk.Scrollbar(area, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        scroll.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.controls.append(self.tree)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.selection_changed())
        self.stats_text = text_box(stats)
        form = ScrollFrame(accounts, C["panel"], app._register_scrollable)
        form.pack(fill="both", expand=True)
        self.scroll_forms.append(form)
        account_body = form.inner
        tk.Label(account_body, text="No network request on opening. Connect explicitly, enable manual sending, then review selected local history rows.\n"
                 "Only listened tracks are eligible. Logs and music are never modified or archived.",
                 bg=C["panel"], fg=C["text"], justify="left", wraplength=850).pack(anchor="w", pady=6)
        self.key, self.secret, self.lbz_token = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for label, variable, hidden in (("Last.fm application API key", self.key, False),
                                        ("Matching Last.fm shared secret", self.secret, True),
                                        ("ListenBrainz user token", self.lbz_token, True)):
            tk.Label(account_body, text=label, bg=C["panel"], fg=C["text"]).pack(anchor="w")
            entry = ttk.Entry(account_body, textvariable=variable, show="*" if hidden else "", width=48)
            entry.pack(anchor="w", fill="x", pady=(0, 5))
            self.controls.append(entry)
        auth = tk.Frame(account_body, bg=C["panel"])
        auth.pack(fill="x", pady=6)
        self.button(auth, "Authorize Last.fm…", self.begin_auth, side="left")
        self.button(auth, "Finish Last.fm connection", self.finish_auth, side="left", padx=6)
        self.button(account_body, "Validate and save ListenBrainz token", self.connect_lbz, anchor="w")
        self.account_label = tk.Label(account_body, bg=C["panel"], fg=C["text"], anchor="w", justify="left")
        self.account_label.pack(fill="x", pady=8)
        row = tk.Frame(account_body, bg=C["panel"])
        row.pack(fill="x", pady=6)
        self.service, self.enabled = tk.StringVar(value="lastfm"), tk.BooleanVar(value=False)
        service = ttk.Combobox(row, values=scrobbling.SERVICES, textvariable=self.service, state="readonly", width=16)
        service.pack(side="left")
        enabled = ttk.Checkbutton(row, text="Enable manual scrobbling (off when reopened)", variable=self.enabled, command=self.invalidate_plan)
        enabled.pack(anchor="w", pady=6)
        self.controls.extend((service, enabled))
        self.button(account_body, "Disconnect selected service", self.disconnect, anchor="w")
        actions = tk.Frame(account_body, bg=C["panel"])
        actions.pack(fill="x", pady=8)
        self.review_btn = self.button(actions, "Review selected history rows", self.review, side="left")
        self.send_btn = self.button(actions, "Send reviewed selection…", self.send, side="left", padx=6)
        self.retry_btn = self.button(account_body, "Allow retry of selected unaccepted rows…", self.retry, anchor="w")
        self.review_text = text_box(account_body, height=10)
        if credential_error:
            set_text(self.review_text, credential_error)
        self.service.trace_add("write", lambda *_: self.change_service())
        for variable in (self.path, self.offset):
            variable.trace_add("write", lambda *_: self.invalidate_preview())
        history.bind("<Configure>", lambda e: self.note.configure(wraplength=max(100, e.width - 20)))
        self.idle()
        self.load()

    def idle(self):
        if self.running:
            return
        connected = self.service.get() in self.accounts
        self.account_label.configure(text="Connected: " + (", ".join(k + ": " + v.get("username", "?") for k, v in self.accounts.items()) or "none"))
        active = self.enabled.get() and connected and self.preview is None
        for button in (self.review_btn, self.retry_btn):
            button.state(["!disabled"] if active else ["disabled"])
        self.send_btn.state(["!disabled"] if active and self.plan and self.plan.events else ["disabled"])
        self.import_btn.state(["!disabled"] if self.preview and self.preview.events else ["disabled"])

    def invalidate_preview(self):
        self.preview = None
        self.import_btn.state(["disabled"])
        self.invalidate_plan()

    def invalidate_plan(self):
        self.plan = None
        self.idle()

    def change_service(self):
        self.invalidate_plan()
        self.states = {}
        for key in self.tree.get_children():
            self.tree.set(key, "receipt", "")
        if not self.running and self.preview is None:
            self.load()

    def choose(self):
        path = filedialog.askopenfilename(parent=self.win, title="Choose .scrobbler.log (read-only)")
        if path:
            self.path.set(path)

    def render(self, events, note, states=None):
        self.states = states or {}
        self.events = {t.id: t for t in events}
        self.tree.delete(*self.tree.get_children())
        for t in events:
            state = (states or {}).get(t.id, ("", ""))[0]
            self.tree.insert("", "end", iid=t.id, values=(utc(t.timestamp), t.artist, t.album, t.title, t.rating,
                                                          f"{t.duration//60}:{t.duration%60:02d}", state))
        self.note.configure(text=note)
        self.invalidate_plan()

    def selection_changed(self):
        self.invalidate_plan()
        if not self.running:
            details = [self.states[t.id][1] for t in self.selected() if t.id in self.states]
            if details:
                self.status.configure(text="; ".join(details[:3]))

    def preview_log(self):
        try:
            offset = int(self.offset.get()) if self.offset.get().strip() else None
        except ValueError:
            self.status.configure(text="Enter an integer UTC offset in minutes, such as +60 or -300.")
            return
        self.preview = None
        path = self.path.get()
        def done(value):
            self.preview = value
            self.render(value.events[:PAGE_SIZE], f"LOG PREVIEW: {len(value.events)} unique events; {value.invalid} invalid lines; "
                        f"{value.duplicates} repeated events. Showing first {PAGE_SIZE}. Nothing imported.\n"
                        + ("A fixed historical offset applies to the whole log; review daylight-saving changes before importing." if value.fixed_offset else "Times are shown in UTC."))
        self.run("Reading log preview…", lambda: preview_log(path, offset, self.cancel), done)

    def import_log(self):
        plan = self.preview
        if not plan or not messagebox.askyesno("Import listening history", f"Import {len(plan.events)} reviewed events locally?\n"
                "Repeated exact events are ignored. The source log is left unchanged.", parent=self.win):
            return
        def done(count):
            self.preview = None
            set_text(self.review_text, f"Imported {count} new events. Existing exact events were retained.")
            self.load()
        self.run("Importing reviewed log…", lambda: self.store.import_preview(plan, self.cancel), done)

    def search_history(self):
        self.page_number = 0
        self.load()

    def change_page(self, step):
        page = self.page_number + step
        if 0 <= page and (page == 0 or page * PAGE_SIZE < self.total):
            self.page_number = page
            self.load()

    def load(self):
        query, page = self.search.get(), self.page_number
        service = self.service.get()
        creds = self.accounts.get(service)
        def work():
            rows, total = self.store.page(query, page)
            return rows, total, self.store.statistics(), scrobbling.receipts(self.store, service, creds) if creds else {}
        def done(value):
            rows, self.total, stats, states = value
            self.preview = None
            self.render(rows, f"LOCAL HISTORY · {self.total} matching events · page {page+1} · timestamps UTC · L=listened, S=skipped", states)
            parts = [f"Listened: {stats['listened']} · Skipped: {stats['skipped']} · Sessions: {stats['sessions']}",
                     f"Estimated listened time: {stats['seconds']//3600} h {(stats['seconds']%3600)//60} min",
                     "Time sums full durations of L entries, not measured elapsed listening. Sessions use a 20-minute gap after estimated track end."]
            for name in ("artists", "albums", "tracks"):
                parts += ["\nTop " + name] + [f"{i}. {' — '.join(map(str, row[:-1]))}: {row[-1]}" for i, row in enumerate(stats[name], 1)]
            set_text(self.stats_text, "\n".join(parts))
            self.status.configure(text="Local history loaded. No network request was made.")
        self.run("Loading local history…", work, done)

    def selected(self):
        return [self.events[key] for key in self.tree.selection() if key in self.events]

    def export(self):
        path = filedialog.asksaveasfilename(parent=self.win, title="Export local history to a new file", defaultextension=".json",
                                           filetypes=[("JSON", "*.json"), ("CSV", "*.csv")])
        if path:
            self.run("Exporting local history…", lambda: self.store.export(path), lambda _: self.status.configure(text="History exported to a new file."))

    def begin_auth(self):
        key, secret = self.key.get().strip(), self.secret.get().strip()
        self.pending_auth = None
        def done(value):
            token, url = value
            self.pending_auth = (key, secret, token)
            webbrowser.open(url)
            self.status.configure(text="Authorize in the Last.fm browser page, then choose Finish Last.fm connection.")
        self.run("Requesting Last.fm authorization…", lambda: scrobbling.auth_token(key, secret), done, cancellable=False)

    def finish_auth(self):
        if not self.pending_auth:
            self.status.configure(text="Choose Authorize Last.fm first.")
            return
        pending = self.pending_auth
        def work():
            values = scrobbling.finish_auth(*pending)
            scrobbling.save_account(self.credentials_path, "lastfm", values)
            return values
        def done(values):
            self.accounts["lastfm"] = values
            self.pending_auth = None
            self.secret.set("")
            self.invalidate_plan()
            self.status.configure(text="Last.fm connected. Manual sending remains opt-in.")
        self.run("Finishing Last.fm authorization…", work, done, cancellable=False)

    def connect_lbz(self):
        token = self.lbz_token.get().strip()
        def work():
            values = scrobbling.validate_listenbrainz(token)
            scrobbling.save_account(self.credentials_path, "listenbrainz", values)
            return values
        def done(values):
            self.accounts["listenbrainz"] = values
            self.lbz_token.set("")
            self.invalidate_plan()
            self.status.configure(text="ListenBrainz connected. No listens were sent.")
        self.run("Validating ListenBrainz token…", work, done, cancellable=False)

    def disconnect(self):
        service = self.service.get()
        scrobbling.save_account(self.credentials_path, service, None)
        self.accounts.pop(service, None)
        self.enabled.set(False)
        self.invalidate_plan()
        self.status.configure(text="Local connection removed. History and submission receipts are retained.")

    def review(self):
        events, service = self.selected(), self.service.get()
        credentials = self.accounts.get(service)
        if self.preview or not self.enabled.get() or not credentials or not events:
            self.status.configure(text="Load local history, connect/enable the service, and select rows first.")
            return
        def done(plan):
            if {t.id for t in self.selected()} != {t.id for t in events} or self.service.get() != service:
                self.status.configure(text="Selection changed during review. Review again.")
                return
            self.plan = plan
            set_text(self.review_text, f"Reviewed {service} account: {credentials['username']}\n{len(plan.events)} eligible events.\n\n"
                     + "\n".join(f"{utc(t.timestamp)} · {t.artist} — {t.title}" for t in plan.events)
                     + "\n\nExcluded:\n" + "\n".join(plan.excluded))
            self.status.configure(text="Selection reviewed. Nothing sent; choose Send reviewed selection to confirm.")
        self.plan = None
        self.run("Reviewing selected events…", lambda: scrobbling.prepare(self.store, events, service, credentials), done)

    def send(self):
        plan = self.plan
        if not plan or not plan.events or not self.enabled.get():
            return
        credentials = self.accounts.get(plan.service, {})
        if not messagebox.askyesno("Send reviewed listening data", f"Send {len(plan.events)} listens to {plan.service} "
                f"account {credentials.get('username', '?')}?\nArtist, album, title, duration and UTC timestamps will leave this computer.\n"
                "Unverified requests are held for manual review; no automatic retries.", parent=self.win):
            return
        self.plan = None
        def done(summary):
            set_text(self.review_text, "Submission result:\n" + "\n".join(f"{k}: {v}" for k, v in summary.items())
                     + "\nUnknown means the service may have accepted the request. Check your online profile before allowing a retry.")
            self.load()
        self.run("Sending explicitly reviewed listens…", lambda: scrobbling.submit(self.store, plan, credentials,
                 confirmed=True, cancel=self.cancel, progress=self.progress), done)

    def retry(self):
        if not self.enabled.get() or self.preview:
            return
        ids, service = [t.id for t in self.selected()], self.service.get()
        credentials = self.accounts.get(service)
        if ids and credentials and messagebox.askyesno("Allow retry", "First verify the service profile and wait for any request to finish.\n"
                "Clearing unaccepted receipts can cause duplicates if the service actually accepted an unverified request.\n"
                "Allow another reviewed attempt for these rows? Accepted receipts remain protected.", parent=self.win):
            self.run("Allowing explicit retry…", lambda: scrobbling.allow_retry(self.store, ids, service, credentials), lambda _: self.load())


class IntegrityDialog(ActivityDialog):
    def __init__(self, app, root, ffmpeg=""):
        super().__init__(app, "Audio integrity · full decode")
        self.ffmpeg, self.result = ffmpeg, None
        tk.Label(self.body, text="Read every audio stream with FFmpeg. This can take a long time. No repair, normalization or music writes.\n"
                 "A successful decode checks readability, not mastering quality or whether a file was originally lossless.",
                 bg=C["panel"], fg=C["text"], justify="left", wraplength=900).pack(anchor="w", pady=8)
        self.path = tk.StringVar(value=str(root) if root else "")
        row = tk.Frame(self.body, bg=C["panel"])
        row.pack(fill="x", pady=8)
        entry = ttk.Entry(row, textvariable=self.path)
        entry.pack(side="left", fill="x", expand=True)
        self.controls.append(entry)
        self.button(row, "Choose folder…", self.choose, side="left", padx=8)
        self.scan_btn = self.button(row, "Start full decode", self.scan, side="left")
        self.button(self.body, "Export report…", self.export_report, anchor="w", pady=5)
        self.text = text_box(self.body)

    def choose(self):
        path = filedialog.askdirectory(parent=self.win, title="Choose music folder for read-only full decoding")
        if path:
            self.path.set(path)

    def scan(self):
        path = self.path.get()
        self.result = None
        def done(result):
            self.result = result
            counts = Counter(t.status for t in result.items)
            self.status.configure(text=("Cancelled; partial results. " if result.cancelled else "Complete. ")
                                  + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
            set_text(self.text, "Showing at most 1,000 entries; export the report for every result.\n\n"
                     + "\n\n".join(f"{t.status.upper()} · {t.path}\n{t.detail}" for t in result.items[:1000])
                     or "No supported audio files found.")
        self.run("Checking audio without modifying files…", lambda: integrity.scan(path, self.ffmpeg, self.cancel, self.progress), done)

    def export_report(self):
        if self.result is None:
            return
        path = filedialog.asksaveasfilename(parent=self.win, title="Export integrity report to a new file",
                                           defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        result = self.result
        def work():
            with Path(path).open("x", encoding="utf-8") as fp:
                json.dump({"cancelled": result.cancelled, "items": [asdict(t) for t in result.items]}, fp, ensure_ascii=False, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
        self.run("Exporting integrity report…", work, lambda _: self.status.configure(text="Integrity report exported."))
