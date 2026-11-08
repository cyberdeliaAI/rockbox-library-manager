# SPDX-License-Identifier: GPL-2.0-only
"""Reviewable EQ refits; export new presets without touching their source."""
from __future__ import annotations

import hashlib
import math
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path

from . import _eq_refit as fit


@dataclass
class EqPlan:
    source: Path
    original: bytes
    output: bytes
    before: int
    after: int
    rate: int
    weighted_error: float
    max_error: float
    rms_error: float
    peak_before: float
    peak_after: float
    frequencies: list[float]
    response_before: list[float]
    response_after: list[float]
    tolerance: float


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Choose a regular EQ preset smaller than 1 MiB.")
    return path.read_bytes()


def preview_refit(path: Path, *, max_bands=5, tolerance=1.0, rate=44100, restarts=12) -> EqPlan:
    if not 1 <= max_bands <= 9 or not 1 <= restarts <= 24:
        raise ValueError("Use 1–9 bands and 1–24 fitting restarts.")
    if rate not in (44100, 48000) or not math.isfinite(tolerance) or not .05 <= tolerance <= 6:
        raise ValueError("Use 44100/48000 Hz and a tolerance between 0.05 and 6 dB.")
    path = Path(path).absolute()
    raw = _read(path)
    text = raw.decode("utf-8-sig")
    bands = [fit.Band(fit.BAND_KINDS[i], fit.DEFAULT_CUTOFFS[i], fit.DEFAULT_Q[i], 0) for i in range(10)]
    seen = set()
    for line in text.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = (v.strip() for v in line.split(":", 1))
        if key not in fit.BAND_KEYS:
            if key.startswith(("eq peak filter", "eq low shelf", "eq high shelf")):
                raise ValueError(f"Unsupported EQ filter: {key}")
            continue
        if key in seen:
            raise ValueError(f"Duplicate EQ setting: {key}")
        seen.add(key)
        if not re.fullmatch(r"\d+\s*,\s*\d+\s*,\s*-?\d+", value):
            raise ValueError(f"Malformed EQ setting: {key}")
        cutoff, q, gain = map(int, value.split(","))
        if not 20 <= cutoff <= 22040 or not 1 <= q <= 64 or not -240 <= gain <= 240:
            raise ValueError(f"EQ setting outside player limits: {key}")
        i = fit.BAND_KEYS.index(key)
        bands[i] = fit.Band(fit.BAND_KINDS[i], cutoff, q, gain)
    if not seen:
        raise ValueError("This file contains no supported EQ filter settings.")
    before = sum(b.active for b in bands)
    frequencies, rng = fit.make_grid(), random.Random(1)
    weights = fit.make_weights(frequencies)
    target = fit.cascade_response_db(bands, frequencies, rate)
    limit = min(24.0, max(6.0, float(fit.np.max(fit.np.abs(target))) + 6.0))
    chosen = bands
    for n in range(1, min(max_bands, before - 1) + 1):
        params, _ = fit.fit_n_bands(target, frequencies, weights, n, rate, limit, restarts, rng)
        candidate = fit.polish(fit.quantise(params), target, frequencies, weights, rate)
        chosen = fit.place_in_slots(candidate)
        if fit.errors(bands, chosen, frequencies, weights, rate)[1] <= tolerance:
            break
    raw_error, weighted, rms = fit.errors(bands, chosen, frequencies, weights, rate)
    response = fit.cascade_response_db(chosen, frequencies, rate)
    replacements = {key: f"{key}: {b.cutoff}, {b.q10}, {b.gain10}"
                    for key, b in zip(fit.BAND_KEYS, chosen)}
    # Keep comments, unrelated settings, enable/precut, BOM and line endings.
    newline = "\r\n" if b"\r\n" in raw else "\n"
    lines = []
    for line in text.splitlines():
        key = line.split(":", 1)[0].strip() if ":" in line else ""
        lines.append(replacements.pop(key, line) if not line.lstrip().startswith("#") else line)
    lines.extend(replacements.values())  # Explicit zero bands prevent inherited settings.
    output = (newline.join(lines) + (newline if raw.endswith(b"\n") else "")).encode("utf-8")
    if raw.startswith(b"\xef\xbb\xbf"):
        output = b"\xef\xbb\xbf" + output
    return EqPlan(path, raw, output, before, sum(b.active for b in chosen), rate, weighted, raw_error, rms,
                  float(max(target)), float(max(response)), frequencies.tolist(), target.tolist(),
                  response.tolist(), tolerance)


def export_refit(plan: EqPlan, destination: Path) -> str:
    if plan.after >= plan.before:
        raise ValueError("No bands were removed; there is no refit to export.")
    if plan.weighted_error > plan.tolerance:
        raise ValueError("The fit exceeds the reviewed tolerance. Adjust it and preview again.")
    if _read(plan.source) != plan.original:
        raise ValueError("The source preset changed. Preview it again.")
    destination = Path(destination).absolute()
    if destination.suffix.casefold() != ".cfg" or destination.resolve() == plan.source.resolve():
        raise ValueError("Export to a new .cfg file; the original is preserved.")
    # Exclusive creation also refuses symlinks and any existing destination.
    with destination.open("xb") as fp:
        fp.write(plan.output)
        fp.flush()
        os.fsync(fp.fileno())
    if destination.read_bytes() != plan.output:
        raise OSError("Export verification failed.")
    return hashlib.sha256(plan.output).hexdigest()
