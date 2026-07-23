"""OBD2 diagnostic-mode readers that work on any J1979-compliant vehicle.

Covers the modes the main tool didn't yet read:
  Mode 01 PID 01  - readiness monitors (which self-tests are complete) + MIL/DTC count
  Mode 02         - freeze frame (sensor snapshot captured when a DTC set)
  Mode 06         - on-board monitoring test results (the catalyst monitor lives here)
  Mode 07         - pending DTCs (this / last drive cycle)
  Mode 09 PID 02  - VIN
  Mode 0A         - permanent DTCs (can't be cleared until the monitor passes)

Everything is standard-protocol, not manufacturer-specific, so it applies to all vehicles.
Mode 06 pass/fail is computed from raw test-value vs raw limits, so it is correct even when
the unit-and-scaling table can't name the physical unit.
"""

import re

# Authoritative Mode-06 unit/scaling + monitor names (built separately). Optional:
# if absent, we fall back to raw values + a small built-in name table, and pass/fail
# (the diagnostically important part) is still correct.
try:
    import obd_mode06_ref as _ref
except Exception:
    _ref = None

# Minimal built-in monitor names (used only when the reference module is
# unavailable). Standard SAE J1979 OBDMID assignments — catalyst is 0x21/0x22,
# O2 sensor heaters are 0x41+ (NOT the other way around).
_FALLBACK_MID_NAMES = {
    0x01: "O2 Sensor Monitor B1S1", 0x02: "O2 Sensor Monitor B1S2",
    0x03: "O2 Sensor Monitor B1S3", 0x04: "O2 Sensor Monitor B1S4",
    0x05: "O2 Sensor Monitor B2S1", 0x06: "O2 Sensor Monitor B2S2",
    0x07: "O2 Sensor Monitor B2S3", 0x08: "O2 Sensor Monitor B2S4",
    0x21: "Catalyst Monitor Bank 1", 0x22: "Catalyst Monitor Bank 2",
    0x31: "EGR Monitor Bank 1", 0x32: "EGR Monitor Bank 2",
    0x39: "EVAP Monitor", 0x3B: "NMHC Catalyst Monitor",
    0x41: "O2 Sensor Heater Monitor B1S1", 0x42: "O2 Sensor Heater Monitor B1S2",
    0x45: "O2 Sensor Heater Monitor B2S1", 0x46: "O2 Sensor Heater Monitor B2S2",
    0xA2: "Misfire Cyl 1 Data", 0xA3: "Misfire Cyl 2 Data",
    0xA4: "Misfire Cyl 3 Data", 0xA5: "Misfire Cyl 4 Data",
}

_MID_BASES = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0)


# --------------------------------------------------------------------------- #
# Frame parsing (used by the hardware reader's .raw())
# --------------------------------------------------------------------------- #
def parse_obd_bytes(resp):
    """Turn a raw ELM327 text response into a flat list of data bytes, or None.

    Handles single-frame lines and ISO-TP multi-frame ("0:.. 1:.. 2:..") responses
    with headers off. Best-effort and adapter-tolerant."""
    if resp is None:
        return None
    up = resp.upper()
    if "NO DATA" in up or "UNABLE TO CONNECT" in up or "BUS INIT" in up and "ERROR" in up:
        return None
    lines = [ln.strip() for ln in up.replace("\r", "\n").split("\n")]
    multiframe = any(re.match(r"^[0-9A-F]:", ln.replace(" ", "")) for ln in lines)

    out = []
    for ln in lines:
        ln = ln.replace(" ", "")
        if not ln or ln in ("OK", ">", "SEARCHING", "SEARCHING..."):
            continue
        if "NODATA" in ln or "STOPPED" in ln:
            continue
        if multiframe:
            m = re.match(r"^([0-9A-F]):(.*)$", ln)
            if m:
                ln = m.group(2)
            elif len(ln) <= 3:  # ISO-TP total-length prefix line
                continue
        if not re.fullmatch(r"[0-9A-F]+", ln) or len(ln) % 2:
            continue
        out += [int(ln[i:i + 2], 16) for i in range(0, len(ln), 2)]
    return out or None


def _find(data, resp_byte, second=None):
    """Index of the response mode byte (e.g. 0x46) in a byte list, or None.
    If `second` is given, also require the following byte to match."""
    if not data:
        return None
    for i, b in enumerate(data):
        if b == resp_byte and (second is None or (i + 1 < len(data) and data[i + 1] == second)):
            return i
    return None


def _mid_name(mid):
    if _ref is not None:
        n = getattr(_ref, "MID_NAMES", {}).get(mid)
        if n:
            return n
    return _FALLBACK_MID_NAMES.get(mid, f"Monitor 0x{mid:02X}")


def _scale(uas, raw):
    if _ref is not None and hasattr(_ref, "scale"):
        try:
            value, unit, _signed = _ref.scale(uas, raw)
            return value, unit
        except Exception:
            pass
    return float(raw), ""


# --------------------------------------------------------------------------- #
# Mode 06 — on-board monitoring test results
# --------------------------------------------------------------------------- #
def mode06_supported_mids(reader):
    supported = set()
    for base in _MID_BASES:
        data = reader.raw(f"06{base:02X}")
        idx = _find(data, 0x46)
        if idx is None or len(data) < idx + 6:
            continue
        bm = data[idx + 2:idx + 6]
        bits = (bm[0] << 24) | (bm[1] << 16) | (bm[2] << 8) | bm[3]
        for i in range(32):
            if bits & (1 << (31 - i)):
                supported.add(base + i + 1)
    # Drop the range-boundary MIDs (they are "next range supported" flags, not tests).
    return sorted(m for m in supported if m not in _MID_BASES)


def read_mode06(reader):
    """Return a list of test records:
    {mid, mid_name, tid, uas, value, unit, min, max, min_u, max_u, passed}."""
    results = []
    for mid in mode06_supported_mids(reader):
        data = reader.raw(f"06{mid:02X}")
        idx = _find(data, 0x46)
        if idx is None:
            continue
        i = idx + 1
        while i + 9 <= len(data) and data[i] == mid:
            tid = data[i + 1]
            uas = data[i + 2]
            tv = (data[i + 3] << 8) | data[i + 4]
            mn = (data[i + 5] << 8) | data[i + 6]
            mx = (data[i + 7] << 8) | data[i + 8]
            v, unit = _scale(uas, tv)
            mn_v, _ = _scale(uas, mn)
            mx_v, _ = _scale(uas, mx)
            results.append({
                "mid": mid, "mid_name": _mid_name(mid), "tid": tid, "uas": uas,
                "value": v, "unit": unit, "min": mn_v, "max": mx_v,
                "min_raw": mn, "max_raw": mx, "passed": mn_v <= v <= mx_v,
            })
            i += 9
    return results


# --------------------------------------------------------------------------- #
# Mode 01 PID 01 — readiness monitors + MIL/DTC count
# --------------------------------------------------------------------------- #
_CONTINUOUS = [("Misfire", 0), ("Fuel System", 1), ("Comprehensive Components", 2)]
_NONCONT = [
    ("Catalyst", 0), ("Heated Catalyst", 1), ("Evaporative System", 2),
    ("Secondary Air System", 3), ("A/C Refrigerant", 4), ("O2 Sensor", 5),
    ("O2 Sensor Heater", 6), ("EGR/VVT System", 7),
]


def read_readiness(reader):
    data = reader.raw("0101")
    idx = _find(data, 0x41, 0x01)
    if idx is None or len(data) < idx + 6:
        return None
    a, b, c, d = data[idx + 2], data[idx + 3], data[idx + 4], data[idx + 5]
    monitors = []
    for name, bit in _CONTINUOUS:
        if b & (1 << bit):
            monitors.append((name, not (b & (1 << (bit + 4)))))  # complete if incomplete-bit clear
    for name, bit in _NONCONT:
        if c & (1 << bit):
            monitors.append((name, not (d & (1 << bit))))
    return {
        "mil_on": bool(a & 0x80),
        "dtc_count": a & 0x7F,
        "monitors": monitors,  # list of (name, complete_bool)
    }


# --------------------------------------------------------------------------- #
# DTC list modes: 07 (pending), 0A (permanent)
# --------------------------------------------------------------------------- #
def _decode_dtcs(data, resp_byte):
    idx = _find(data, resp_byte)
    if idx is None:
        return []
    codes = []
    i = idx + 1
    while i + 1 < len(data):
        hi, lo = data[i], data[i + 1]
        i += 2
        if hi == 0 and lo == 0:
            continue
        letter = "PCBU"[(hi & 0xC0) >> 6]
        codes.append(f"{letter}{(hi & 0x3F) >> 4:X}{hi & 0x0F:X}{lo >> 4:X}{lo & 0x0F:X}")
    return codes


def read_pending_dtcs(reader):
    return _decode_dtcs(reader.raw("07"), 0x47)


def read_permanent_dtcs(reader):
    return _decode_dtcs(reader.raw("0A"), 0x4A)


# --------------------------------------------------------------------------- #
# Mode 09 PID 02 — VIN
# --------------------------------------------------------------------------- #
def read_vin(reader):
    data = reader.raw("0902")
    idx = _find(data, 0x49, 0x02)
    if idx is None:
        return None
    payload = data[idx + 2:]
    if payload and payload[0] in (0x01, 0x02, 0x03, 0x04, 0x05):  # message-count byte
        payload = payload[1:]
    chars = [chr(b) for b in payload if 32 <= b < 127]
    vin = "".join(chars).strip()
    return vin or None


# --------------------------------------------------------------------------- #
# Mode 02 — freeze frame (snapshot at the moment a DTC set)
# --------------------------------------------------------------------------- #
def read_freeze_frame(reader, signals):
    """`signals`: list of (pid_hex, label, unit, fmt, formula) to pull from frame 0."""
    out = []
    for pid, label, unit, fmt, formula in signals:
        data = reader.raw(f"02{pid}00")  # PID at freeze-frame 0
        idx = _find(data, 0x42)
        if idx is None:
            continue
        body = data[idx + 3:]  # skip 42, PID, frame#
        try:
            out.append(f"{label}: {fmt.format(formula(body))} {unit}".strip())
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
def collect_monitors(reader, freeze_signals=None):
    """Read all diagnostic modes. Returns (text_block, data) where data is a dict
    with keys: readiness, pending, permanent, mode06, freeze, vin."""
    out = []
    data = {"readiness": None, "pending": [], "permanent": [],
            "mode06": [], "freeze": [], "vin": None}

    readiness = read_readiness(reader)
    data["readiness"] = readiness
    if readiness:
        out.append("== Readiness monitors ==")
        out.append(f"MIL (check-engine light): {'ON' if readiness['mil_on'] else 'off'}   "
                   f"Stored DTC count: {readiness['dtc_count']}")
        for name, complete in readiness["monitors"]:
            out.append(f"  {name:<26} {'complete' if complete else 'NOT complete'}")

    pending = read_pending_dtcs(reader)
    data["pending"] = pending
    out.append("")
    out.append("== Pending codes (Mode 07) == " + (", ".join(pending) if pending else "none"))
    permanent = read_permanent_dtcs(reader)
    data["permanent"] = permanent
    out.append("== Permanent codes (Mode 0A) == " + (", ".join(permanent) if permanent else "none")
               + ("   (won't clear until the monitor re-passes)" if permanent else ""))

    m06 = read_mode06(reader)
    data["mode06"] = m06
    out.append("")
    if m06:
        out.append("== On-board monitor tests (Mode 06) ==")
        out.append(f"  {'Monitor':<26} {'value':>10}  {'limits':>21}  result")
        for r in m06:
            u = f" {r['unit']}" if r["unit"] else ""
            val = f"{r['value']:.4g}{u}"
            # raw 0x0000 / 0xFFFF are the "no lower / no upper bound" sentinels.
            lo = "—" if r["min_raw"] == 0x0000 else f"{r['min']:.4g}"
            hi = "—" if r["max_raw"] == 0xFFFF else f"{r['max']:.4g}"
            lim = f"{lo} .. {hi}"
            out.append(f"  {r['mid_name']:<26} {val:>10}  {lim:>21}  "
                       f"{'PASS' if r['passed'] else 'FAIL'}")
    else:
        out.append("== On-board monitor tests (Mode 06) == not reported by this ECU")

    if freeze_signals:
        ff = read_freeze_frame(reader, freeze_signals)
        data["freeze"] = ff
        if ff:
            out.append("")
            out.append("== Freeze frame (conditions when a code set) ==")
            out.extend("  " + line for line in ff)

    vin = read_vin(reader)
    data["vin"] = vin
    if vin:
        out.append("")
        out.append(f"VIN: {vin}")

    return "\n".join(out), data
