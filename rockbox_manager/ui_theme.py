# SPDX-License-Identifier: GPL-2.0-only
"""Colours, fonts, ttk styles and anti-aliased image shapes for the Tk interface."""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from functools import lru_cache
from tkinter import ttk
from types import SimpleNamespace
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageOps

# ----------------------------------------------------------------------
# Theme
# ----------------------------------------------------------------------
C = {
    # Flatter desktop/media-manager palette: less dashboard, more utility app.
    "bg": "#0e1114",
    "sidebar": "#11161b",
    "panel": "#14191f",
    "card": "#181e25",
    "input": "#1a2027",
    "hover": "#202832",
    "border": "#27313b",
    "border_hi": "#3a4856",
    "text": "#edf1f5",
    "muted": "#9aa6b2",
    "faint": "#687582",
    "accent": "#35b8a6",
    "accent_hi": "#58c9b9",
    "accent_lo": "#173a36",
    "ok": "#4fc58b",
    "warn": "#d9a441",
    "bad": "#e06c75",
}


def make_fonts(root: tk.Misc) -> SimpleNamespace:
    base = tkfont.nametofont("TkDefaultFont")
    family = base.actual("family")
    size = int(base.actual("size"))
    sign = -1 if size < 0 else 1
    s = abs(size) or 10

    def f(delta: int, weight: str = "normal") -> tkfont.Font:
        return tkfont.Font(root=root, family=family, size=sign * max(7, s + delta), weight=weight)

    return SimpleNamespace(
        body=f(0), body_b=f(0, "bold"), small=f(-1), small_b=f(-1, "bold"),
        tiny=f(-2), tiny_b=f(-2, "bold"), title=f(2, "bold"), h1=f(5, "bold"),
        mono=tkfont.nametofont("TkFixedFont"),
    )


def apply_style(root: tk.Misc, F: SimpleNamespace) -> None:
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure(".", background=C["bg"], foreground=C["text"], fieldbackground=C["input"],
                 bordercolor=C["border"], lightcolor=C["bg"], darkcolor=C["bg"],
                 troughcolor=C["input"], focuscolor=C["accent"], selectbackground=C["accent"],
                 selectforeground="#ffffff", insertcolor=C["text"], font=F.body)

    def button(name: str, bg: str, hover: str, fg: str, font: Any, padding: Any) -> None:
        st.configure(name, background=bg, foreground=fg, bordercolor=bg, lightcolor=bg,
                     darkcolor=bg, relief="flat", padding=padding, font=font, anchor="center",
                     focuscolor=blend(bg, "#ffffff", 0.35), focusthickness=1)
        st.map(name,
               background=[("disabled", C["input"]), ("pressed", hover), ("active", hover)],
               foreground=[("disabled", C["faint"])],
               bordercolor=[("disabled", C["input"]), ("active", hover)],
               lightcolor=[("disabled", C["input"]), ("active", hover)],
               darkcolor=[("disabled", C["input"]), ("active", hover)])

    button("TButton", C["input"], C["hover"], C["text"], F.body, (12, 6))
    button("Accent.TButton", C["accent"], C["accent_hi"], "#07110f", F.body_b, (12, 6))
    button("Ghost.TButton", C["panel"], C["input"], C["text"], F.body, (10, 6))
    button("Small.TButton", C["input"], C["hover"], C["text"], F.small, (9, 4))
    button("SmallAccent.TButton", C["accent"], C["accent_hi"], "#07110f", F.small_b, (9, 4))
    button("Danger.TButton", C["input"], blend(C["bad"], C["input"], 0.55), C["bad"], F.body, (14, 7))
    button("TMenubutton", C["input"], C["hover"], C["text"], F.body, (12, 6))

    st.layout("Slim.Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    st.configure("Slim.Vertical.TScrollbar", troughcolor=C["bg"], background=C["border"],
                 bordercolor=C["bg"], lightcolor=C["border"], darkcolor=C["border"],
                 gripcount=0, arrowsize=9, relief="flat")
    st.map("Slim.Vertical.TScrollbar",
           background=[("pressed", C["border_hi"]), ("active", C["border_hi"])],
           lightcolor=[("active", C["border_hi"])], darkcolor=[("active", C["border_hi"])])
    st.configure("Panel.Slim.Vertical.TScrollbar", troughcolor=C["panel"], bordercolor=C["panel"])

    st.configure("Accent.Horizontal.TProgressbar", troughcolor=C["input"], background=C["accent"],
                 bordercolor=C["input"], lightcolor=C["accent"], darkcolor=C["accent"], thickness=6)
    st.configure("Horizontal.TScale", background=C["muted"], troughcolor=C["input"],
                 bordercolor=C["bg"], lightcolor=C["muted"], darkcolor=C["muted"], sliderlength=12,
                 gripcount=0, sliderthickness=12, troughrelief="flat", sliderrelief="flat")
    st.map("Horizontal.TScale", background=[("active", C["text"])])
    st.configure("TCheckbutton", background=C["panel"], foreground=C["text"],
                 indicatorbackground=C["input"], indicatorforeground=C["accent"], focuscolor=C["panel"])
    st.map("TCheckbutton", background=[("active", C["panel"])],
           indicatorbackground=[("selected", C["input"])])

    st.configure("Health.Treeview", background=C["bg"], fieldbackground=C["bg"], foreground=C["text"],
                 bordercolor=C["border"], rowheight=30, relief="flat", font=F.small)
    st.configure("Health.Treeview.Heading", background=C["panel"], foreground=C["muted"],
                 bordercolor=C["border"], relief="flat", font=F.small_b, padding=(8, 7))
    st.map("Health.Treeview", background=[("selected", C["accent_lo"])],
           foreground=[("selected", C["text"])])
    st.map("Health.Treeview.Heading", background=[("active", C["hover"])])

    root.option_add("*Menu.background", C["input"])
    root.option_add("*Menu.foreground", C["text"])
    root.option_add("*Menu.activeBackground", C["accent"])
    root.option_add("*Menu.activeForeground", "#ffffff")
    root.option_add("*Menu.relief", "flat")
    root.option_add("*Menu.borderWidth", 0)


# ----------------------------------------------------------------------
# Image shaping (anti-aliased masks, rendered with Pillow)
# ----------------------------------------------------------------------
def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def blend(c1: str, c2: str, t: float) -> str:
    a, b = hex_to_rgb(c1), hex_to_rgb(c2)
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


@lru_cache(maxsize=64)
def shape_mask(width: int, height: int, shape: str, radius: int) -> Image.Image:
    ss = 4
    mask = Image.new("L", (width * ss, height * ss), 0)
    draw = ImageDraw.Draw(mask)
    box = (0, 0, width * ss - 1, height * ss - 1)
    if shape == "circle":
        draw.ellipse(box, fill=255)
    else:
        draw.rounded_rectangle(box, radius=max(0, radius * ss), fill=255)
    return mask.resize((width, height), Image.Resampling.LANCZOS)


def shaped_thumbnail(img: Image.Image, size: int, shape: str, bg: str) -> Image.Image:
    """Crop-to-fill a square thumbnail and mask it as a circle or rounded square."""
    fitted = ImageOps.fit(img, (size, size), Image.Resampling.LANCZOS)
    base = Image.new("RGB", (size, size), bg)
    base.paste(fitted, (0, 0), shape_mask(size, size, shape, max(4, size // 18)))
    return base


def rounded_fit(img: Image.Image, box: int, bg: str, radius: int = 10) -> Image.Image:
    """Fit an image inside a square box without cropping, with rounded corners."""
    fitted = img.copy()
    fitted.thumbnail((box, box), Image.Resampling.LANCZOS)
    if fitted.width < box and fitted.height < box:
        scale = box / max(fitted.width, fitted.height)
        fitted = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.Resampling.LANCZOS,
        )
    base = Image.new("RGB", (box, box), bg)
    x = (box - fitted.width) // 2
    y = (box - fitted.height) // 2
    base.paste(fitted, (x, y), shape_mask(fitted.width, fitted.height, "rounded", radius))
    return base


def rounded_panel(width: int, height: int, fill: str, bg: str, radius: int,
                  border: Optional[str] = None, border_width: float = 0) -> Image.Image:
    """Anti-aliased rounded rectangle, optionally with a border ring."""
    ss = 3
    img = Image.new("RGB", (width * ss, height * ss), bg)
    draw = ImageDraw.Draw(img)
    box = (0, 0, width * ss - 1, height * ss - 1)
    if border and border_width:
        draw.rounded_rectangle(box, radius=radius * ss, fill=border)
        inset = int(border_width * ss)
        draw.rounded_rectangle(
            (inset, inset, width * ss - 1 - inset, height * ss - 1 - inset),
            radius=max(0, (radius - border_width) * ss), fill=fill,
        )
    else:
        draw.rounded_rectangle(box, radius=radius * ss, fill=fill)
    return img.resize((width, height), Image.Resampling.LANCZOS)


def placeholder_art(kind: str, variant: str, size: int, fill: str, glyph: str, bg: str) -> Image.Image:
    """Artwork placeholder: a person for artists, a record for albums."""
    ss = 4
    s = size * ss
    img = Image.new("RGB", (s, s), fill)
    draw = ImageDraw.Draw(img)
    if variant != "loading":
        if kind == "artist":
            head = s * 0.17
            cx, cy = s / 2, s * 0.40
            draw.ellipse((cx - head, cy - head, cx + head, cy + head), fill=glyph)
            draw.ellipse((s * 0.22, s * 0.62, s * 0.78, s * 1.05), fill=glyph)
        else:
            r = s * 0.30
            c = s / 2
            w = max(2, int(s * 0.03))
            draw.ellipse((c - r, c - r, c + r, c + r), outline=glyph, width=w)
            draw.ellipse((c - r * 0.62, c - r * 0.62, c + r * 0.62, c + r * 0.62), outline=glyph, width=max(1, w // 2))
            draw.ellipse((c - r * 0.16, c - r * 0.16, c + r * 0.16, c + r * 0.16), fill=glyph)
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    base = Image.new("RGB", (size, size), bg)
    shape = "circle" if kind == "artist" else "rounded"
    base.paste(img, (0, 0), shape_mask(size, size, shape, max(4, size // 18)))
    return base


def app_icon_image(accent: str) -> Image.Image:
    ss = 4
    s = 64 * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=14 * ss, fill=accent)
    c = s / 2
    for r, fill in ((0.34, "#10131a"), (0.13, accent), (0.045, "#10131a")):
        draw.ellipse((c - s * r, c - s * r, c + s * r, c + s * r), fill=fill)
    return img.resize((64, 64), Image.Resampling.LANCZOS)
