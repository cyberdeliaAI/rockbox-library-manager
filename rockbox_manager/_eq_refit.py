# SPDX-License-Identifier: GPL-2.0-only
# Adapted from Anthony Fletcher's PodBox tools/eq_refit/eq_refit.py.
# Source: https://github.com/anthonyfletcher/podbox/blob/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/tools/eq_refit/eq_refit.py
# The original numerical fitting implementation is kept separate from UI and I/O.
import math
import numpy as np

# ---------------------------------------------------------------------------
# Rockbox preset format
# ---------------------------------------------------------------------------

# Band slots, in the order settings_list.c declares them. Slot 0 must be the
# low shelf and slot 9 the high shelf: eq.c picks the coefficient generator
# from the slot index, not from anything in the config file.
LOW_SHELF, HIGH_SHELF, PEAKING = "ls", "hs", "pk"

BAND_KEYS = (
    ["eq low shelf filter"]
    + ["eq peak filter %d" % i for i in range(1, 9)]
    + ["eq high shelf filter"]
)
BAND_KINDS = [LOW_SHELF] + [PEAKING] * 8 + [HIGH_SHELF]
NUM_BANDS = len(BAND_KEYS)

# Stored-value limits, from apps-ipod/screens/settings/eq_settings.h. Cutoff is
# whole Hz; Q and gain are stored multiplied by ten (EQ_USER_DIVISOR).
CUTOFF_MIN, CUTOFF_MAX = 20, 22040
Q_MIN, Q_MAX = 1, 64
GAIN_MIN, GAIN_MAX = -240, 240

# Frequencies the untouched bands keep, so the graphical EQ screen still shows
# a sane spread (settings_list.c, eq_defaults).
DEFAULT_CUTOFFS = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
DEFAULT_Q = [7, 10, 10, 10, 10, 10, 10, 10, 10, 7]


class Band:
    """One EQ band, held in Rockbox's stored units."""

    def __init__(self, kind, cutoff, q10, gain10):
        self.kind = kind
        self.cutoff = int(cutoff)
        self.q10 = int(q10)
        self.gain10 = int(gain10)

    @property
    def active(self):
        return self.gain10 != 0

    def __repr__(self):
        return "%s(%dHz Q%.1f %+.1fdB)" % (
            self.kind, self.cutoff, self.q10 / 10.0, self.gain10 / 10.0)


def biquad(kind, f0, q, gain_db, fs):
    """Cookbook biquad coefficients, normalised so a0 == 1."""
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)

    if kind == PEAKING:
        b = [1 + alpha * a, -2 * cw, 1 - alpha * a]
        aa = [1 + alpha / a, -2 * cw, 1 - alpha / a]
    elif kind == LOW_SHELF:
        two_sqrt_a_alpha = 2.0 * math.sqrt(a) * alpha
        b = [a * ((a + 1) - (a - 1) * cw + two_sqrt_a_alpha),
             2 * a * ((a - 1) - (a + 1) * cw),
             a * ((a + 1) - (a - 1) * cw - two_sqrt_a_alpha)]
        aa = [(a + 1) + (a - 1) * cw + two_sqrt_a_alpha,
              -2 * ((a - 1) + (a + 1) * cw),
              (a + 1) + (a - 1) * cw - two_sqrt_a_alpha]
    elif kind == HIGH_SHELF:
        two_sqrt_a_alpha = 2.0 * math.sqrt(a) * alpha
        b = [a * ((a + 1) + (a - 1) * cw + two_sqrt_a_alpha),
             -2 * a * ((a - 1) + (a + 1) * cw),
             a * ((a + 1) + (a - 1) * cw - two_sqrt_a_alpha)]
        aa = [(a + 1) - (a - 1) * cw + two_sqrt_a_alpha,
              2 * ((a - 1) - (a + 1) * cw),
              (a + 1) - (a - 1) * cw - two_sqrt_a_alpha]
    else:
        raise ValueError(kind)

    return [c / aa[0] for c in b], [c / aa[0] for c in aa]


def band_response_db(kind, f0, q, gain_db, freqs, fs):
    """Magnitude response of one biquad, in dB, at the given frequencies."""
    if gain_db == 0.0:
        return np.zeros_like(freqs)
    b, a = biquad(kind, f0, q, gain_db, fs)
    z = np.exp(-1j * 2.0 * np.pi * freqs / fs)
    num = b[0] + b[1] * z + b[2] * z * z
    den = a[0] + a[1] * z + a[2] * z * z
    return 20.0 * np.log10(np.abs(num / den) + 1e-30)


def cascade_response_db(bands, freqs, fs):
    """Combined response of a band list. Cascaded filters add in dB."""
    total = np.zeros_like(freqs)
    for band in bands:
        if band.active:
            total += band_response_db(
                band.kind, band.cutoff, band.q10 / 10.0,
                band.gain10 / 10.0, freqs, fs)
    return total


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------

def make_grid(points=320, f_lo=20.0, f_hi=20000.0):
    return np.exp(np.linspace(math.log(f_lo), math.log(f_hi), points))


def make_weights(freqs):
    """Per-frequency importance.

    Full weight across 40 Hz - 12 kHz, tapering to 0.3 at the extremes: the
    5G's output stage and typical earbuds do little below 40 Hz or above
    12 kHz, so error spent there buys nothing audible and would otherwise pull
    the fit away from the range that matters.
    """
    w = np.ones_like(freqs)
    lo = freqs < 40.0
    w[lo] = 0.3 + 0.7 * (np.log(freqs[lo] / 20.0) / math.log(40.0 / 20.0))
    hi = freqs > 12000.0
    w[hi] = 1.0 - 0.7 * (np.log(freqs[hi] / 12000.0) / math.log(20000.0 / 12000.0))
    return np.clip(w, 0.3, 1.0)


def slot_layout(n):
    """Which filter kinds an n-band fit uses.

    The shelves anchor the ends of the spectrum, where these presets do most
    of their work; anything left over becomes peaking bands in between.
    """
    if n == 1:
        return [LOW_SHELF]
    if n == 2:
        return [LOW_SHELF, HIGH_SHELF]
    return [LOW_SHELF] + [PEAKING] * (n - 2) + [HIGH_SHELF]


def param_bounds(kind, gain_limit):
    """(min, max) for log10(f), Q and gain of one band."""
    if kind == LOW_SHELF:
        return (math.log10(25.0), math.log10(500.0)), (0.3, 2.0), (-gain_limit, gain_limit)
    if kind == HIGH_SHELF:
        return (math.log10(1500.0), math.log10(19000.0)), (0.3, 2.0), (-gain_limit, gain_limit)
    return (math.log10(35.0), math.log10(16000.0)), (0.3, 4.0), (-gain_limit, gain_limit)


def unpack(x, kinds):
    out = []
    for i, kind in enumerate(kinds):
        lf, q, g = x[3 * i:3 * i + 3]
        out.append((kind, 10.0 ** lf, q, g))
    return out


def response_of(params, freqs, fs):
    total = np.zeros_like(freqs)
    for kind, f0, q, gain in params:
        total += band_response_db(kind, f0, q, gain, freqs, fs)
    return total


def make_objective(target, freqs, weights, kinds, gain_limit, fs):
    flat_lo = []
    flat_hi = []
    for kind in kinds:
        for b in param_bounds(kind, gain_limit):
            flat_lo.append(b[0])
            flat_hi.append(b[1])
    flat_lo = np.array(flat_lo)
    flat_hi = np.array(flat_hi)

    def objective(x):
        # Out-of-bounds costs, rather than hard clipping, so the simplex still
        # gets a gradient pointing back into the valid region.
        penalty = float(np.sum(np.maximum(0.0, flat_lo - x) ** 2)
                        + np.sum(np.maximum(0.0, x - flat_hi) ** 2))
        xc = np.clip(x, flat_lo, flat_hi)
        err = (response_of(unpack(xc, kinds), freqs, fs) - target) * weights
        worst = float(np.max(np.abs(err)))
        rms = float(np.sqrt(np.mean(err * err)))
        return worst + 0.25 * rms + 100.0 * penalty

    return objective, flat_lo, flat_hi


def nelder_mead(f, x0, step, iters=4000, tol=1e-9):
    """Standard Nelder-Mead simplex. Hand-rolled to avoid a scipy dependency."""
    n = len(x0)
    simplex = [np.array(x0, dtype=float)]
    for i in range(n):
        pt = np.array(x0, dtype=float)
        pt[i] += step[i]
        simplex.append(pt)
    vals = [f(p) for p in simplex]

    for _ in range(iters):
        order = np.argsort(vals)
        simplex = [simplex[i] for i in order]
        vals = [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < tol:
            break

        centroid = np.mean(simplex[:-1], axis=0)
        worst = simplex[-1]

        refl = centroid + (centroid - worst)
        f_refl = f(refl)
        if f_refl < vals[0]:
            expand = centroid + 2.0 * (centroid - worst)
            f_expand = f(expand)
            simplex[-1], vals[-1] = ((expand, f_expand) if f_expand < f_refl
                                     else (refl, f_refl))
        elif f_refl < vals[-2]:
            simplex[-1], vals[-1] = refl, f_refl
        else:
            contract = centroid + 0.5 * (worst - centroid)
            f_contract = f(contract)
            if f_contract < vals[-1]:
                simplex[-1], vals[-1] = contract, f_contract
            else:
                best = simplex[0]
                for i in range(1, n + 1):
                    simplex[i] = best + 0.5 * (simplex[i] - best)
                    vals[i] = f(simplex[i])

    order = int(np.argmin(vals))
    return simplex[order], vals[order]


def fit_n_bands(target, freqs, weights, n, fs, gain_limit, restarts, rng):
    """Best n-band fit found, as a list of (kind, f0, Q, gain)."""
    kinds = slot_layout(n)
    objective, flat_lo, flat_hi = make_objective(
        target, freqs, weights, kinds, gain_limit, fs)

    best_x, best_v = None, float("inf")
    for attempt in range(restarts):
        x0 = []
        step = []
        for i, kind in enumerate(kinds):
            (lf_lo, lf_hi), (q_lo, q_hi), _ = param_bounds(kind, gain_limit)
            if attempt == 0:
                # Deterministic opening guess: bands spread evenly in log
                # frequency across their allowed span.
                span = (i + 1.0) / (len(kinds) + 1.0)
                lf = lf_lo + span * (lf_hi - lf_lo)
                q = 0.7
            else:
                lf = rng.uniform(lf_lo, lf_hi)
                q = rng.uniform(q_lo, min(q_hi, 2.0))
            x0 += [lf, q, rng.uniform(-2.0, 2.0) if attempt else 0.0]
            step += [0.35, 0.3, 1.5]

        x, v = nelder_mead(objective, x0, step)
        if v < best_v:
            best_x, best_v = np.clip(x, flat_lo, flat_hi), v

    return unpack(best_x, kinds), kinds


# ---------------------------------------------------------------------------
# Quantisation to Rockbox's stored units
# ---------------------------------------------------------------------------

def quantise(params):
    """Round a fit onto the grid the settings actually store."""
    out = []
    for kind, f0, q, gain in params:
        cutoff = int(min(CUTOFF_MAX, max(CUTOFF_MIN, round(f0))))
        q10 = int(min(Q_MAX, max(Q_MIN, round(q * 10))))
        gain10 = int(min(GAIN_MAX, max(GAIN_MIN, round(gain * 10))))
        out.append(Band(kind, cutoff, q10, gain10))
    return out


def polish(bands, target, freqs, weights, fs):
    """Coordinate search on the quantised grid.

    Rounding to whole Hz and tenths can undo some of what the continuous fit
    won, so nudge each stored value a few steps either way and keep whatever
    lowers the weighted worst-case error.
    """
    def score(bs):
        err = (cascade_response_db(bs, freqs, fs) - target) * weights
        return float(np.max(np.abs(err))) + 0.25 * float(np.sqrt(np.mean(err * err)))

    best = score(bands)
    improved = True
    while improved:
        improved = False
        for band in bands:
            for attr, deltas, lo, hi in (
                ("gain10", (-2, -1, 1, 2), GAIN_MIN, GAIN_MAX),
                ("q10", (-3, -1, 1, 3), Q_MIN, Q_MAX),
                ("cutoff", (-40, -8, -2, 2, 8, 40), CUTOFF_MIN, CUTOFF_MAX),
            ):
                original = getattr(band, attr)
                for delta in deltas:
                    candidate = original + delta
                    if not lo <= candidate <= hi:
                        continue
                    setattr(band, attr, candidate)
                    trial = score(bands)
                    if trial < best - 1e-9:
                        best, original, improved = trial, candidate, True
                    else:
                        setattr(band, attr, original)
    return bands


def place_in_slots(fitted):
    """Map fitted bands onto Rockbox's ten slots.

    Slot 0 takes the low shelf and slot 9 the high shelf because eq.c chooses
    the filter type by slot index. Peaking bands go into slots 1.. in
    ascending frequency order so the graphical EQ screen reads left to right.
    """
    slots = [Band(BAND_KINDS[i], DEFAULT_CUTOFFS[i], DEFAULT_Q[i], 0)
             for i in range(NUM_BANDS)]

    peaks = sorted((b for b in fitted if b.kind == PEAKING),
                   key=lambda b: b.cutoff)
    for band in fitted:
        if band.kind == LOW_SHELF:
            slots[0] = band
        elif band.kind == HIGH_SHELF:
            slots[9] = band
    for i, band in enumerate(peaks):
        slots[1 + i] = band
    return slots


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

PLOT_FREQS = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


def format_comparison(original, fitted, fs):
    freqs = np.array(PLOT_FREQS, dtype=float)
    a = cascade_response_db(original, freqs, fs)
    b = cascade_response_db(fitted, freqs, fs)

    lines = ["    freq     was    now    diff",
             "    ----------------------------"]
    for f, x, y in zip(PLOT_FREQS, a, b):
        label = "%gk" % (f / 1000.0) if f >= 1000 else "%d" % f
        lines.append("    %5s  %+5.1f  %+5.1f  %+5.2f" % (label, x, y, y - x))
    return "\n".join(lines)


def errors(original, fitted, freqs, weights, fs):
    diff = cascade_response_db(fitted, freqs, fs) - cascade_response_db(
        original, freqs, fs)
    weighted = diff * weights
    return float(np.max(np.abs(diff))), float(np.max(np.abs(weighted))), \
        float(np.sqrt(np.mean(weighted ** 2)))

