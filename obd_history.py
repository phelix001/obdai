"""Per-vehicle run history — save each diagnosis and compare across visits.

Records are appended as JSON lines to history/<slug>.jsonl, keyed by VIN when
available (so the same physical car is tracked even if the description changes),
otherwise by a slug of the vehicle description. Works for any vehicle.
"""

import json
import os
import re


def _history_dir(script_dir):
    d = os.path.join(script_dir, "history")
    os.makedirs(d, exist_ok=True)
    return d


def _slug(vehicle, vin):
    if vin:
        return re.sub(r"[^A-Za-z0-9]", "", vin)[:32] or "unknown"
    s = re.sub(r"[^A-Za-z0-9]+", "_", vehicle.strip().lower()).strip("_")
    return s[:48] or "unknown"


def _path(script_dir, vehicle, vin):
    return os.path.join(_history_dir(script_dir), _slug(vehicle, vin) + ".jsonl")


def load_history(script_dir, vehicle, vin):
    """Return prior run records (chronological). Empty list if none."""
    path = _path(script_dir, vehicle, vin)
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def append_run(script_dir, record):
    """Append one run record. Returns the file path."""
    path = _path(script_dir, record.get("vehicle", ""), record.get("vin"))
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
    return path


def format_history_console(records, limit=6):
    """Compact table of recent visits for the terminal."""
    if not records:
        return ""
    rows = records[-limit:]
    out = ["== Prior visits (this vehicle) =="]
    out.append(f"  {'date':<19} {'LTFT':>7} {'STFT':>7} {'λ B1S1':>7}  {'catalyst':<10} {'codes':<12} problem")
    for r in rows:
        m = r.get("metrics") or {}
        ltft = "—" if m.get("ltft_b1") is None else f"{m['ltft_b1']:+.1f}%"
        stft = "—" if m.get("stft_b1") is None else f"{m['stft_b1']:+.1f}%"
        lam = "—" if m.get("lambda_b1s1") is None else f"{m['lambda_b1s1']:.3f}"
        cat = r.get("catalyst")
        cat_s = "—" if not cat else ("PASS" if cat.get("passed") else "FAIL")
        codes = ",".join(r.get("dtcs") or []) or "none"
        prob = (r.get("most_likely_problem") or "").split(".")[0][:40]
        date = (r.get("ts") or "")[:19].replace("T", " ")
        out.append(f"  {date:<19} {ltft:>7} {stft:>7} {lam:>7}  {cat_s:<10} {codes:<12} {prob}")
    return "\n".join(out)


def format_history_for_ai(records, limit=4):
    """Short text block fed to the AI so it can comment on trends across visits."""
    if not records:
        return ""
    rows = records[-limit:]
    lines = ["Prior visits on record for this vehicle (oldest first) — use these to judge "
             "whether a previous repair worked or a problem is progressing:"]
    for r in rows:
        m = r.get("metrics") or {}
        bits = []
        if m.get("ltft_b1") is not None:
            bits.append(f"LTFT {m['ltft_b1']:+.1f}%")
        if m.get("stft_b1") is not None:
            bits.append(f"STFT {m['stft_b1']:+.1f}%")
        if m.get("lambda_b1s1") is not None:
            bits.append(f"lambda {m['lambda_b1s1']:.3f}")
        cat = r.get("catalyst")
        if cat:
            bits.append(f"catalyst monitor {'PASS' if cat.get('passed') else 'FAIL'}")
        codes = ", ".join(r.get("dtcs") or []) or "no codes"
        date = (r.get("ts") or "")[:10]
        prob = r.get("most_likely_problem") or ""
        lines.append(f"  - {date}: {', '.join(bits)}; codes: {codes}. Diagnosed: {prob}")
    return "\n".join(lines)
