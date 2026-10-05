# SPDX-License-Identifier: GPL-2.0-only
"""Small custom widgets (tk-based so colours work on macOS, Windows and Linux)."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
from typing import Any, Callable, Optional

from .common import IS_MAC, MOD_LABEL
from .ui_theme import C, blend


def wheel_pixels(event: tk.Event) -> int:
    if getattr(event, "num", None) == 4:
        return -70
    if getattr(event, "num", None) == 5:
        return 70
    delta = int(getattr(event, "delta", 0) or 0)
    if abs(delta) >= 120:
        return int(-delta / 120 * 70)
    return -delta * (10 if IS_MAC else 20)


# ----------------------------------------------------------------------
# Small custom widgets (tk-based so colours work on macOS, Windows and Linux)
# ----------------------------------------------------------------------
def recolor(widget: tk.Misc, bg: str) -> None:
    try:
        widget.configure(bg=bg)
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        if isinstance(child, (tk.Frame, tk.Label, tk.Canvas)) and not getattr(child, "_keep_bg", False):
            recolor(child, bg)


class Dot(tk.Canvas):
    def __init__(self, parent: tk.Misc, color: str, bg: str, size: int = 8) -> None:
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self.oval = self.create_oval(1, 1, size - 1, size - 1, fill=color, outline=color)

    def set_color(self, color: str) -> None:
        self.itemconfigure(self.oval, fill=color, outline=color)


class NavItem(tk.Frame):
    def __init__(self, parent: tk.Misc, F: SimpleNamespace, text: str, command: Callable[[], None],
                 dot: Optional[str] = None, count: bool = True) -> None:
        super().__init__(parent, bg=C["sidebar"], cursor="hand2")
        self.F = F
        self.active = False
        self.hover = False
        self.command = command
        self.bar = tk.Frame(self, width=3, bg=C["sidebar"])
        self.bar._keep_bg = True  # type: ignore[attr-defined]
        self.bar.pack(side="left", fill="y")
        self.dot = Dot(self, dot, C["sidebar"]) if dot else None
        if self.dot:
            self.dot.pack(side="left", padx=(12, 0))
        self.label = tk.Label(self, text=text, bg=C["sidebar"], fg=C["muted"], font=F.body,
                              anchor="w", padx=10 if dot else 15, pady=7)
        self.label.pack(side="left", fill="x", expand=True)
        self.count = tk.Label(self, text="", bg=C["sidebar"], fg=C["faint"], font=F.small, padx=14)
        if count:
            self.count.pack(side="right")
        for w in (self, self.label, self.count, *( [self.dot] if self.dot else [])):
            w.bind("<Button-1>", lambda _e: self.command())
            w.bind("<Enter>", lambda _e: self._set_hover(True))
            w.bind("<Leave>", lambda _e: self._set_hover(False))

    def _set_hover(self, hover: bool) -> None:
        self.hover = hover
        self._paint()

    def set_active(self, active: bool) -> None:
        self.active = active
        self._paint()

    def set_count(self, value: Any) -> None:
        self.count.configure(text=str(value))

    def _paint(self) -> None:
        bg = C["input"] if self.active else (C["card"] if self.hover else C["sidebar"])
        recolor(self, bg)
        self.bar.configure(bg=C["accent"] if self.active else bg)
        self.label.configure(fg=C["text"] if self.active else C["muted"],
                             font=self.F.body_b if self.active else self.F.body)


class Segmented(tk.Frame):
    """A compact segmented control."""

    def __init__(self, parent: tk.Misc, F: SimpleNamespace, options: list[tuple[Any, str]], value: Any,
                 command: Optional[Callable[[Any], None]] = None, font: Any = None) -> None:
        super().__init__(parent, bg=C["border"], padx=1, pady=1)
        self.F = F
        self.command = command
        self.font = font or F.small
        self.value = value
        self.labels: dict[Any, tk.Label] = {}
        self.set_options(options, value)

    def set_options(self, options: list[tuple[Any, str]], value: Any) -> None:
        for lbl in self.labels.values():
            lbl.destroy()
        self.labels = {}
        for i, (val, text) in enumerate(options):
            lbl = tk.Label(self, text=text, font=self.font, padx=12, pady=4, cursor="hand2", bd=0)
            lbl.grid(row=0, column=i, padx=(0 if i == 0 else 1, 0), sticky="nsew")
            lbl.bind("<Button-1>", lambda _e, v=val: self.set(v, notify=True))
            lbl.bind("<Enter>", lambda _e, v=val: self._hover(v, True))
            lbl.bind("<Leave>", lambda _e, v=val: self._hover(v, False))
            self.labels[val] = lbl
        self.set(value)

    def _hover(self, value: Any, on: bool) -> None:
        if value != self.value and value in self.labels:
            self.labels[value].configure(bg=C["hover"] if on else C["input"])

    def set(self, value: Any, notify: bool = False) -> None:
        changed = value != self.value
        self.value = value
        for val, lbl in self.labels.items():
            selected = val == value
            lbl.configure(bg=C["accent_lo"] if selected else C["input"],
                          fg=C["text"] if selected else C["muted"])
        if notify and changed and self.command:
            self.command(value)


class FieldEntry(tk.Frame):
    """Flat entry with a focus ring and an optional placeholder / clear button."""

    def __init__(self, parent: tk.Misc, F: SimpleNamespace, variable: tk.StringVar,
                 placeholder: str = "", show: str = "", icon: bool = False, clearable: bool = False,
                 width: int = 20) -> None:
        super().__init__(parent, bg=C["input"], highlightthickness=1,
                         highlightbackground=C["border"], highlightcolor=C["border"])
        self.var = variable
        if icon:
            glass = tk.Canvas(self, width=16, height=16, bg=C["input"], highlightthickness=0)
            glass.create_oval(2, 2, 11, 11, outline=C["faint"], width=2)
            glass.create_line(10, 10, 14, 14, fill=C["faint"], width=2)
            glass.pack(side="left", padx=(9, 0))
        self.entry = tk.Entry(self, textvariable=variable, bg=C["input"], fg=C["text"],
                              insertbackground=C["text"], relief="flat", bd=0, font=F.body,
                              highlightthickness=0, show=show, width=width,
                              disabledbackground=C["input"], disabledforeground=C["faint"],
                              selectbackground=C["accent"], selectforeground="#ffffff")
        self.entry.pack(side="left", fill="both", expand=True, padx=(8, 8), pady=6)
        self.placeholder = tk.Label(self, text=placeholder, bg=C["input"], fg=C["faint"], font=F.body,
                                    anchor="w", cursor="xterm")
        self.placeholder.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.clear = tk.Label(self, text="✕", bg=C["input"], fg=C["faint"], font=F.small,
                              cursor="hand2", padx=8)
        self.clear.bind("<Button-1>", lambda _e: (self.var.set(""), self.entry.focus_set()))
        self.clearable = clearable
        self.entry.bind("<FocusIn>", lambda _e: self.configure(highlightbackground=C["accent"],
                                                               highlightcolor=C["accent"]))
        self.entry.bind("<FocusOut>", lambda _e: self.configure(highlightbackground=C["border"],
                                                                highlightcolor=C["border"]))
        variable.trace_add("write", lambda *_a: self._sync())
        self.entry.bind("<Configure>", lambda _e: self._sync(), add="+")
        self.after_idle(self._sync)

    def _sync(self) -> None:
        empty = not self.var.get()
        if empty and self.placeholder.cget("text"):
            x = self.entry.winfo_x() or 30
            self.placeholder.place(x=x, rely=0.5, anchor="w")
        else:
            self.placeholder.place_forget()
        if self.clearable:
            if empty:
                self.clear.pack_forget()
            else:
                self.clear.pack(side="right")

    def set_show(self, show: str) -> None:
        self.entry.configure(show=show)


class DropZone(tk.Canvas):
    def __init__(self, parent: tk.Misc, F: SimpleNamespace, on_click: Callable[[], None],
                 height: int = 86) -> None:
        super().__init__(parent, height=height, bg=C["panel"], highlightthickness=0, bd=0, cursor="hand2")
        self.F = F
        self.title = "Drop an image here"
        self.sub = f"or click to browse  ·  {MOD_LABEL}V to paste"
        self.active = False
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Enter>", lambda _e: self.set_active(True))
        self.bind("<Leave>", lambda _e: self.set_active(False))
        self.bind("<Button-1>", lambda _e: on_click())

    def set_text(self, title: str, sub: str) -> None:
        self.title, self.sub = title, sub
        self.redraw()

    def set_active(self, active: bool) -> None:
        self.active = active
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        color = C["accent"] if self.active else C["border_hi"]
        fill = blend(C["panel"], C["accent"], 0.08) if self.active else C["panel"]
        self.create_rectangle(1, 1, w - 2, h - 2, outline=color, dash=(5, 4), width=1, fill=fill)
        self.create_text(w / 2, h / 2 - 9, text=self.title, fill=C["text"], font=self.F.body_b)
        self.create_text(w / 2, h / 2 + 11, text=self.sub, fill=C["muted"], font=self.F.small)


class Tooltip:
    def __init__(self, root: tk.Misc, F: SimpleNamespace) -> None:
        self.root = root
        self.F = F
        self.win: Optional[tk.Toplevel] = None
        self.job: Optional[str] = None

    def schedule(self, text: str, x: int, y: int, delay: int = 550) -> None:
        self.cancel()
        self.job = self.root.after(delay, lambda: self.show(text, x, y))

    def show(self, text: str, x: int, y: int) -> None:
        self.hide()
        win = tk.Toplevel(self.root)
        win.wm_overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(win, text=text, bg=C["input"], fg=C["text"], font=self.F.small, justify="left",
                 padx=9, pady=6, wraplength=320, highlightthickness=1,
                 highlightbackground=C["border_hi"]).pack()
        win.wm_geometry(f"+{x + 14}+{y + 18}")
        self.win = win

    def cancel(self) -> None:
        if self.job:
            self.root.after_cancel(self.job)
            self.job = None

    def hide(self) -> None:
        self.cancel()
        if self.win is not None:
            self.win.destroy()
            self.win = None


class Toast:
    """Small transient message at the bottom of a container instead of a modal popup."""

    COLORS = {"info": "accent", "ok": "ok", "warn": "warn", "error": "bad"}

    def __init__(self, parent: tk.Misc, F: SimpleNamespace) -> None:
        self.parent = parent
        self.frame = tk.Frame(parent, bg=C["input"], highlightthickness=1, highlightbackground=C["border_hi"])
        self.dot = Dot(self.frame, C["accent"], C["input"], 8)
        self.dot.pack(side="left", padx=(12, 0))
        self.label = tk.Label(self.frame, text="", bg=C["input"], fg=C["text"], font=F.body,
                              padx=10, pady=8, wraplength=460, justify="left")
        self.label.pack(side="left")
        self.job: Optional[str] = None

    def show(self, text: str, kind: str = "info", ms: int = 3200) -> None:
        self.dot.set_color(C[self.COLORS.get(kind, "accent")])
        self.label.configure(text=text)
        self.frame.place(relx=0.5, rely=1.0, y=-18, anchor="s")
        self.frame.lift()
        if self.job:
            self.parent.after_cancel(self.job)
        self.job = self.parent.after(ms, self.hide)

    def hide(self) -> None:
        self.frame.place_forget()
        self.job = None


class ScrollFrame(tk.Frame):
    """Vertically scrolling container; wheel events are routed by the app."""

    def __init__(self, parent: tk.Misc, bg: str, register: Callable[[tk.Misc, Callable[[int], None]], None],
                 scrollbar_style: str = "Slim.Vertical.TScrollbar") -> None:
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, yscrollincrement=1)
        self.vsb = ttk.Scrollbar(self, orient="vertical", style=scrollbar_style, command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        register(self.canvas, self.scroll_pixels)

    def _on_inner(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()))

    def _on_canvas(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.window, width=event.width)

    def _on_scroll(self, first: str, last: str) -> None:
        self.vsb.set(first, last)
        needed = not (float(first) <= 0.0 and float(last) >= 1.0)
        if needed != bool(self.vsb.winfo_ismapped()):
            if needed:
                self.vsb.grid()
            else:
                self.vsb.grid_remove()

    def scroll_pixels(self, px: int) -> None:
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(px, "units")
