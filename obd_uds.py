"""Manufacturer-specific data via UDS ReadDataByIdentifier (service 0x22).

Standard OBD2 (Mode 01) only exposes a fixed PID set. Carmakers expose far more
through UDS DIDs — on the Audi CAEB that's real boost pressure, oil temperature,
fuel-rail pressure, misfire counters, lambda control, etc. These DIDs are
vehicle-specific, so they're loaded from a JSON map (audi_caeb_dids.json).

This talks to the ECU over the *same ELM327 serial adapter* the rest of the tool
uses (no python-can/SocketCAN needed): set the header to the engine ECU, then
send `22 <did>` and parse the `62 <did> <data>` response.
"""

import json

import obd_modes  # for parse_obd_bytes()


# --- scaling functions (keys match the "scale" field in the DID JSON) ---
def _rpm_2b(d):            return ((d[0] << 8) | d[1]) / 4.0
def _temp_c_minus_48(d):   return d[0] - 48
def _u16_div_100(d):       return ((d[0] << 8) | d[1]) / 100.0
def _pct_255(d):           return d[0] * 100.0 / 255.0
def _u16_div_128(d):       return ((d[0] << 8) | d[1]) / 128.0
def _u16_div_32768(d):     return ((d[0] << 8) | d[1]) / 32768.0
def _u16_div_10(d):        return ((d[0] << 8) | d[1]) / 10.0
def _u16_raw(d):           return (d[0] << 8) | d[1]
def _ivalve_state(d):      return "High" if d[0] == 1 else "Low" if d[0] == 0 else f"Unknown({d[0]})"

SCALE_MAP = {
    "rpm_2b": _rpm_2b, "temp_c_minus_48": _temp_c_minus_48, "u16_div_100": _u16_div_100,
    "pct_255": _pct_255, "u16_div_128": _u16_div_128, "u16_div_32768": _u16_div_32768,
    "u16_div_10": _u16_div_10, "u16_raw": _u16_raw, "ivalve_state": _ivalve_state,
}


def load_dids(path):
    """Load a DID map: {did_int: {name, size, scale, unit}}."""
    raw = json.load(open(path))
    out = {}
    for k, v in raw.items():
        out[int(k, 16)] = {"name": v["name"], "size": int(v["bytes"]),
                           "scale": v["scale"], "unit": v.get("unit", "")}
    return out


def _parse_did(data, did, meta):
    """Extract and scale one DID value from a 0x62 response, or None."""
    if not data:
        return None
    hi, lo = (did >> 8) & 0xFF, did & 0xFF
    for i in range(len(data) - 2):
        if data[i] == 0x62 and data[i + 1] == hi and data[i + 2] == lo:
            payload = data[i + 3:i + 3 + meta["size"]]
            if len(payload) < meta["size"]:
                return None
            fn = SCALE_MAP.get(meta["scale"])
            try:
                return fn(payload) if fn else int.from_bytes(bytes(payload), "big")
            except Exception:
                return None
    return None


def read_dids(reader, did_map, header="7E0"):
    """Read every DID in the map. Returns list of {name, value, unit} for the
    ones that responded. Sets/restores the ELM327 header on real hardware."""
    has_cmd = hasattr(reader, "cmd")
    if has_cmd:
        try:
            reader.cmd("ATSH " + header)   # physical addressing to the engine ECU
            reader.cmd("1003")             # extended diagnostic session (ignore if refused)
        except Exception:
            pass
    results = []
    for did, meta in did_map.items():
        value = _parse_did(reader.raw(f"22{did:04X}"), did, meta)
        if value is not None:
            results.append({"name": meta["name"], "value": value, "unit": meta["unit"]})
    if has_cmd:
        try:
            reader.cmd("ATSH 7DF")         # restore functional addressing for standard OBD
        except Exception:
            pass
    return results


def format_uds(results):
    if not results:
        return ""
    lines = ["== Manufacturer-specific data (VAG UDS, service 0x22) =="]
    for r in results:
        v = r["value"]
        vs = f"{v:.2f}" if isinstance(v, float) else str(v)
        lines.append(f"  {r['name']}: {vs} {r['unit']}".rstrip())
    return "\n".join(lines)
