#!/usr/bin/env python3
"""
obd_display.py — Compact, in-place-updating terminal display for live OBD2 data.

During a live capture many sensor values change several times per second. A naive
scrolling printout is unreadable. This module renders a COMPACT, HORIZONTAL block
that is redrawn on top of itself (never scrolls). Each signal gets one aligned row
showing: current value, unit, min…max, avg, and a unicode-block sparkline of its
recent history — so a big value range is visible at a glance.

Pure Python 3 standard library only (ANSI escape codes, no curses, no pip deps).

Public interface (do not rename — the main program is written against it):
    LiveMonitor(signals, history=48)
    LiveMonitor.set_status(text)
    LiveMonitor.update(sample)
    LiveMonitor.render()
    LiveMonitor.summary()
    LiveMonitor.render_summary_table()
"""

import sys
import math
import shutil
from collections import deque

# Unicode block glyphs, low -> high, used to draw the sparklines.
_SPARK_GLYPHS = "▁▂▃▄▅▆▇█"

# ANSI escape helpers.
_ESC = "\x1b["
_CLEAR_LINE = _ESC + "2K"          # erase entire current line
_RESET = _ESC + "0m"
_DIM = _ESC + "2m"
_BOLD = _ESC + "1m"
_CYAN = _ESC + "36m"
_YELLOW = _ESC + "33m"


def _cursor_up(n):
    """Return the ANSI sequence to move the cursor up n lines (no-op for n<=0)."""
    return (_ESC + str(n) + "A") if n > 0 else ""


class LiveMonitor:
    """Live, in-place terminal monitor for a fixed set of numeric signals."""

    def __init__(self, signals, history=48):
        # Keep signal metadata in stable order; buffers are per-signal rolling deques.
        self.signals = list(signals)
        self.history = max(1, int(history))
        self.buffers = {sig["key"]: deque(maxlen=self.history) for sig in self.signals}
        self.status = None
        # Number of lines the previous frame occupied (for the cursor-up redraw).
        self._last_lines = 0
        # Cache the tty check once; None output must never contain color codes.
        self._isatty = bool(getattr(sys.stdout, "isatty", lambda: False)())

    # ------------------------------------------------------------------ inputs

    def set_status(self, text):
        """Set (or clear) the optional single header line shown above the table."""
        self.status = text

    def update(self, sample):
        """Append one tick of data to each signal's rolling buffer.

        `sample` is a dict of key -> float. A key that is missing, or whose value
        is None / non-finite, is recorded as a gap (None) so the column stays
        present but the tick does not affect min/max/avg.
        """
        sample = sample or {}
        for sig in self.signals:
            key = sig["key"]
            val = sample.get(key, None)
            if val is None:
                self.buffers[key].append(None)
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                self.buffers[key].append(None)
                continue
            if not math.isfinite(fval):
                self.buffers[key].append(None)
                continue
            self.buffers[key].append(fval)

    # -------------------------------------------------------------- statistics

    @staticmethod
    def _reals(buf):
        """Return the list of real (non-None) samples from a buffer."""
        return [v for v in buf if v is not None]

    def _stats(self, key):
        """Return (min, max, avg, last, n) for a signal, guarding empty buffers."""
        reals = self._reals(self.buffers[key])
        n = len(reals)
        if n == 0:
            return (None, None, None, None, 0)
        lo = min(reals)
        hi = max(reals)
        avg = sum(reals) / n
        last = reals[-1]
        return (lo, hi, avg, last, n)

    def _trend(self, key):
        """Coarse trend string over the buffer: rising/falling/swinging/flat."""
        reals = self._reals(self.buffers[key])
        n = len(reals)
        if n < 6:
            return "flat"
        third = max(1, n // 3)
        first = reals[:third]
        last = reals[-third:]
        first_avg = sum(first) / len(first)
        last_avg = sum(last) / len(last)

        mean = sum(reals) / n
        variance = sum((v - mean) ** 2 for v in reals) / n
        std = math.sqrt(variance)
        span = max(reals) - min(reals)

        # Net direction is "clear" if the shift between thirds is a meaningful
        # fraction of the overall spread (and the spread itself is non-trivial).
        delta = last_avg - first_avg
        if span > 1e-9 and abs(delta) >= 0.25 * span:
            return "rising" if delta > 0 else "falling"
        # No clear net direction: call it swinging if it's noisy relative to level.
        level = max(abs(mean), 1e-9)
        if span > 1e-9 and std >= 0.15 * level and std >= 0.15 * span:
            return "swinging"
        return "flat"

    # ---------------------------------------------------------------- sparkline

    def _sparkline(self, key, width):
        """Build a unicode-block sparkline for the last `width` samples of a signal.

        Scaled between this signal's own min and max over its buffer. Gaps render
        as a space. A ~zero range renders as a flat mid-level line.
        """
        if width <= 0:
            return ""
        buf = list(self.buffers[key])
        if not buf:
            return " " * width
        window = buf[-width:]
        reals = [v for v in window if v is not None]
        if not reals:
            return " " * len(window)
        lo = min(reals)
        hi = max(reals)
        rng = hi - lo
        out = []
        n_glyphs = len(_SPARK_GLYPHS)
        for v in window:
            if v is None:
                out.append(" ")
                continue
            if rng < 1e-12:
                # Flat signal: draw a low-mid bar so a steady line is still visible.
                out.append(_SPARK_GLYPHS[n_glyphs // 2])
            else:
                frac = (v - lo) / rng
                idx = int(round(frac * (n_glyphs - 1)))
                idx = max(0, min(n_glyphs - 1, idx))
                out.append(_SPARK_GLYPHS[idx])
        return "".join(out)

    # ---------------------------------------------------------------- rendering

    def _fmt_num(self, sig, value):
        """Format a number with the signal's fmt, or a dash placeholder if None."""
        if value is None:
            return "—"
        try:
            return sig["fmt"].format(value)
        except (KeyError, ValueError, IndexError):
            return str(value)

    def _color(self, text, code):
        """Wrap text in an ANSI color only when stdout is a tty."""
        if not self._isatty:
            return text
        return code + text + _RESET

    # Fixed column widths. The sparkline is the flex column and is sized per
    # render() from the live terminal width.
    _LABEL_W = 22
    _CUR_W = 9
    _UNIT_W = 4
    _RANGE_W = 21   # "min…max" field
    _AVG_W = 11     # "avg NNNN" field
    # Visible width of everything before the sparkline (fields + separators):
    #   label(22) "  " cur(9) " " unit(4) " " range(21) "  " avg(11) "  "
    _FIXED_W = _LABEL_W + 2 + _CUR_W + 1 + _UNIT_W + 1 + _RANGE_W + 2 + _AVG_W + 2
    _SPARK_CAP = 48   # never draw more glyphs than this many

    def _assemble(self, segments, max_w):
        """Assemble (text, color_code) segments into a line whose VISIBLE width
        (characters, excluding ANSI codes) never exceeds max_w.

        Segments are truncated from the end as needed. Color codes are applied
        only when stdout is a tty; the ANSI bytes never count toward the width.
        """
        out = []
        used = 0
        for text, code in segments:
            if used >= max_w:
                break
            remaining = max_w - used
            if len(text) > remaining:
                text = text[:remaining]
            used += len(text)
            if text and code and self._isatty:
                out.append(code + text + _RESET)
            else:
                out.append(text)
        return "".join(out)

    def _spark_width(self, columns):
        """Sparkline glyph count for the current terminal width (may be 0)."""
        budget = (columns - 1) - self._FIXED_W
        return max(0, min(self.history, budget, self._SPARK_CAP))

    def _row(self, sig, columns):
        """Build one aligned display row as a fit-to-width string.

        Returns a string whose visible width is <= columns-1 (no wrapping). The
        sparkline (the flex column) is sized to the terminal; if the fixed
        columns alone already overflow a very narrow terminal, the whole line
        is truncated to columns-1.
        """
        key = sig["key"]
        lo, hi, avg, last, n = self._stats(key)

        label_field = sig["label"][: self._LABEL_W].ljust(self._LABEL_W)
        cur_field = self._fmt_num(sig, last).rjust(self._CUR_W)
        unit_field = (sig.get("unit") or "")[: self._UNIT_W].ljust(self._UNIT_W)
        range_str = "{}…{}".format(self._fmt_num(sig, lo), self._fmt_num(sig, hi))
        range_field = range_str.rjust(self._RANGE_W)
        avg_field = ("avg " + self._fmt_num(sig, avg)).rjust(self._AVG_W)
        spark = self._sparkline(key, self._spark_width(columns))

        # (text, color) segments. Separators carry no color. _assemble enforces
        # the visible-width cap and drops color bytes from the width accounting.
        segments = [
            (label_field, _DIM),
            ("  ", None),
            (cur_field, _BOLD),
            (" ", None),
            (unit_field, None),
            (" ", None),
            (range_field, _DIM),
            ("  ", None),
            (avg_field, _DIM),
            ("  ", None),
            (spark, _CYAN),
        ]
        return self._assemble(segments, columns - 1)

    def render(self):
        """Return a string that redraws the whole block in place.

        Every emitted line is kept within the LIVE terminal width (queried each
        call via shutil.get_terminal_size) so NO line can ever wrap. Because a
        logical line then always occupies exactly one physical row, the ESC[<n>A
        cursor-up (using the tracked logical line count) moves up exactly the
        number of physical rows the previous frame emitted — no drift, no smear.

        First call prints fresh and records the block height. Subsequent calls
        prefix ESC[<n>A, and each line is erased with ESC[2K and rewritten so the
        new frame exactly overwrites the previous one; leftover lines from a
        taller previous frame are cleared too.
        """
        # Query the real terminal width every frame; fall back to 80 cols.
        columns = shutil.get_terminal_size((80, 24)).columns
        columns = max(20, columns)  # sane floor so widths stay positive

        lines = []
        if self.status is not None:
            # Truncate the status text to the visible-width budget so it, too,
            # can never wrap and desync the cursor math.
            status_text = self.status[: columns - 1]
            lines.append(self._color(status_text, _YELLOW) if self._isatty else status_text)
        for sig in self.signals:
            lines.append(self._row(sig, columns))

        n_new = len(lines)

        parts = []
        # Move the cursor back up over the previous frame so we overwrite it.
        if self._last_lines > 0:
            parts.append("\r")
            parts.append(_cursor_up(self._last_lines))

        # If the previous frame was taller than this one, we still need to clear
        # the extra trailing lines so nothing is left behind.
        total_lines = max(n_new, self._last_lines)
        for i in range(total_lines):
            parts.append("\r")
            parts.append(_CLEAR_LINE)
            if i < n_new:
                parts.append(lines[i])
            # Newline after EVERY line, including the last. The cursor then rests
            # `total_lines` rows below the top, which is exactly how far the next
            # frame's _cursor_up must climb — so it lands back on the top row with
            # no drift. (Skipping the final newline was the drift bug.)
            parts.append("\n")

        # We descended `total_lines` newlines; the next frame climbs the same.
        self._last_lines = total_lines
        return "".join(parts)

    # ------------------------------------------------------------------ summary

    def summary(self):
        """Return key -> {label,unit,min,max,avg,last,n,trend} over each buffer."""
        out = {}
        for sig in self.signals:
            key = sig["key"]
            lo, hi, avg, last, n = self._stats(key)
            out[key] = {
                "label": sig["label"],
                "unit": sig.get("unit", ""),
                "min": lo,
                "max": hi,
                "avg": avg,
                "last": last,
                "n": n,
                "trend": self._trend(key),
            }
        return out

    def render_summary_table(self):
        """Return a static, plain-text summary table (no ANSI, no cursor codes)."""
        rows = []
        header = "{lab}  {mn}  {mx}  {av}  {ls}  {n}  {tr}".format(
            lab="SIGNAL".ljust(self._LABEL_W),
            mn="MIN".rjust(9),
            mx="MAX".rjust(9),
            av="AVG".rjust(9),
            ls="LAST".rjust(9),
            n="N".rjust(4),
            tr="TREND",
        )
        rows.append(header)
        rows.append("-" * len(header))

        for sig in self.signals:
            key = sig["key"]
            lo, hi, avg, last, n = self._stats(key)
            trend = self._trend(key)
            unit = sig.get("unit", "")
            unit_suffix = (" " + unit) if unit else ""

            def cell(v):
                s = self._fmt_num(sig, v)
                return (s + unit_suffix) if v is not None else s

            rows.append("{lab}  {mn}  {mx}  {av}  {ls}  {n}  {tr}".format(
                lab=sig["label"][: self._LABEL_W].ljust(self._LABEL_W),
                mn=cell(lo).rjust(9),
                mx=cell(hi).rjust(9),
                av=cell(avg).rjust(9),
                ls=cell(last).rjust(9),
                n=str(n).rjust(4),
                tr=trend,
            ))
        return "\n".join(rows)


# ---------------------------------------------------------------------- demo

def _demo():
    """Animate a fake multi-step live capture, then print the summary table.

    Uses 10 signals over 44 frames, and drives most values HIGH (near their max)
    so the sparklines sit at mostly-full blocks — the worst case for width, which
    is where the wrapping bug showed. The block must stay anchored at every width.
    """
    import time
    import random

    signals = [
        {"key": "rpm",    "label": "Engine RPM",              "unit": "RPM", "fmt": "{:.0f}"},
        {"key": "ltft1",  "label": "LTFT Bank 1",             "unit": "%",   "fmt": "{:+.1f}"},
        {"key": "stft1",  "label": "Short Trim Bank 1",       "unit": "%",   "fmt": "{:+.1f}"},
        {"key": "maf",    "label": "MAF Air Flow",            "unit": "g/s", "fmt": "{:.1f}"},
        {"key": "map",    "label": "Intake Manifold Press",   "unit": "kPa", "fmt": "{:.0f}"},
        {"key": "load",   "label": "Calculated Load",         "unit": "%",   "fmt": "{:.1f}"},
        {"key": "tps",    "label": "Throttle Position",       "unit": "%",   "fmt": "{:.1f}"},
        {"key": "ect",    "label": "Coolant Temp",            "unit": "°C",  "fmt": "{:.0f}"},
        {"key": "o2b1s2", "label": "O2 Bank1 Sensor2",        "unit": "V",   "fmt": "{:.3f}"},
        {"key": "lam",    "label": "Lambda Bank1 Sensor1",    "unit": "λ",   "fmt": "{:.3f}"},
    ]

    mon = LiveMonitor(signals, history=48)

    steps = ["IDLE", "REV UP", "HOLD WOT", "SETTLE"]
    frames = 44
    for i in range(frames):
        phase = i / (frames - 1)                      # 0..1
        # Rise fast, hold high, then settle: keeps most values near max so the
        # sparklines are mostly full blocks (the screenshot's case).
        hump = math.sin(min(1.0, phase * 1.35) * math.pi * 0.5) ** 0.6
        step = steps[min(len(steps) - 1, int(phase * len(steps)))]

        rpm = 900 + hump * 4600 + random.uniform(-50, 50)
        ltft = 5.0 - hump * 7.0 + random.uniform(-0.5, 0.5)   # falls as RPM rises
        stft = math.sin(phase * math.pi * 7) * (1.0 + hump * 3.0) + random.uniform(-0.8, 0.8)
        maf = 3.0 + hump * 36.0 + random.uniform(-0.6, 0.6)
        mapk = 28 + hump * 70 + random.uniform(-2, 2)
        load = 18 + hump * 78 + random.uniform(-2, 2)
        tps = 8 + hump * 88 + random.uniform(-2, 2)
        ect = 78 + hump * 14 + random.uniform(-0.5, 0.5)      # warms toward max
        o2 = 0.60 + math.sin(phase * math.pi * 3) * 0.14 + random.uniform(-0.02, 0.02)
        lam = 1.0 + math.sin(phase * math.pi * 9) * 0.05 + random.uniform(-0.01, 0.01)

        sample = {
            "rpm": rpm, "ltft1": ltft, "stft1": stft, "maf": maf, "map": mapk,
            "load": load, "tps": tps, "ect": ect,
            "o2b1s2": None if (i % 13 == 6) else o2,   # occasional dropped PID
            "lam": lam,
        }

        countdown = frames - 1 - i
        mon.set_status("Live capture — step: {:<8}  frame {:>2}/{:<2}  (t-{}s)".format(
            step, i + 1, frames, max(0, countdown) // 10))
        mon.update(sample)
        sys.stdout.write(mon.render())
        sys.stdout.flush()
        time.sleep(0.08)

    sys.stdout.write("\n\n")
    print("Capture summary:")
    print(mon.render_summary_table())


if __name__ == "__main__":
    _demo()
