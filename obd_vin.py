#!/usr/bin/env python3
"""VIN validation and decode for the chat assistant.

The car reports its VIN over OBD Mode 09 PID 02 (obd_modes.read_vin). This module
turns that raw 17-character string into something a mechanic can trust and act on:

    validate  the ISO 3779 check digit (position 9) and the character set
    decode    the fields every VIN encodes: world manufacturer, model year,
              assembly plant, and — for VAG — the body/engine hints in the VDS
    check     read it off the car, validate, decode, and flag any mismatch
              against the VIN the session started with

Why validate rather than just print it: a flaky adapter or a multi-frame parsing
glitch can drop or mangle a character, and a wrong VIN quietly sends someone to
order the wrong part. The check digit catches almost every single-character error,
so the assistant can say "that read is corrupt, try again" instead of trusting it.

Standard: ISO 3779 (VIN structure) and the North America 49 CFR 565 check digit.
The check digit is mandatory for North-America-market cars (WMI starting 1-5),
advisory elsewhere — decode still works either way.
"""

import re

# I, O, Q are never used in a VIN (they look like 1, 0, 0).
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# Check-digit transliteration: each letter maps to a number (ISO 3779).
_TRANSLIT = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]

# Model-year code (position 10). The cycle repeats every 30 years; for a car this
# old the 1980-2009 range is disambiguated from 2010-2039 by position 7 being a
# digit (pre-2010 passenger cars) vs a letter — handled in decode_model_year.
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"  # maps to 1980.. and 2010..


def normalize(vin):
    """Upper-case, strip, and drop separators/whitespace a scanner might inject."""
    if not vin:
        return ""
    return re.sub(r"[\s\-_.]", "", vin).upper()


def is_wellformed(vin):
    """17 chars, legal alphabet, no I/O/Q. Independent of the check digit."""
    return bool(_VIN_RE.match(normalize(vin)))


def compute_check_digit(vin):
    """The check digit position 9 *should* hold, as a string ('0'-'9' or 'X').

    Returns None if the VIN has a character outside the legal set.
    """
    vin = normalize(vin)
    if len(vin) != 17:
        return None
    total = 0
    for ch, w in zip(vin, _WEIGHTS):
        if ch not in _TRANSLIT:
            return None
        total += _TRANSLIT[ch] * w
    r = total % 11
    return "X" if r == 10 else str(r)


def check_digit_ok(vin):
    """Does the VIN's own position-9 digit match what the other 16 imply?"""
    vin = normalize(vin)
    expected = compute_check_digit(vin)
    return expected is not None and vin[8] == expected


def requires_check_digit(vin):
    """North-America-market VINs (WMI 1-5) must satisfy the check digit; others may not."""
    vin = normalize(vin)
    return bool(vin) and vin[0] in "12345"


# --------------------------------------------------------------------------- #
# Decode
# --------------------------------------------------------------------------- #
# First VIN character -> region / country. Ranges per ISO 3780; only the common
# ones are spelled out, enough to sanity-check "is this even plausibly my car".
_REGION = [
    ("A", "H", "Africa"),
    ("J", "R", "Asia"),
    ("S", "Z", "Europe"),
    ("1", "5", "North America"),
    ("6", "7", "Oceania"),
    ("8", "9", "South America"),
]
_COUNTRY = {
    "W": "Germany", "1": "United States", "4": "United States", "5": "United States",
    "2": "Canada", "3": "Mexico", "J": "Japan", "K": "Korea", "L": "China",
    "S": "United Kingdom", "V": "France/Spain", "Y": "Sweden/Finland",
    "Z": "Italy", "T": "Switzerland/Czech", "U": "Romania/Hungary",
}
# Well-known WMIs relevant to this project (VAG) plus a few common others.
_WMI = {
    "WAU": "Audi (Germany)", "WA1": "Audi SUV (Germany)", "TRU": "Audi (Hungary)",
    "WVW": "Volkswagen (Germany)", "WV1": "VW Commercial", "WV2": "VW Bus/Van",
    "1VW": "Volkswagen (USA)", "3VW": "Volkswagen (Mexico)", "WVG": "VW SUV",
    "WP0": "Porsche (car)", "WP1": "Porsche (SUV)",
    "1HG": "Honda (USA)", "JHM": "Honda (Japan)", "5YJ": "Tesla",
}


def decode_region(vin):
    c = normalize(vin)[:1]
    for lo, hi, name in _REGION:
        if lo <= c <= hi:
            return name
    return "unknown"


def decode_model_year(vin):
    """Model year from position 10. Returns an int, or None.

    The A-Y/1-9 code repeats on a 30-year cycle. Position 7 breaks the tie for
    passenger cars: a digit means 1980-2009, a letter means 2010-2039.
    """
    vin = normalize(vin)
    if len(vin) != 17:
        return None
    code = vin[9]
    if code not in _YEAR_CODES:
        return None
    base = 1980 + _YEAR_CODES.index(code)          # 1980..2009
    pos7_is_letter = vin[6].isalpha()
    return base + 30 if pos7_is_letter else base


def decode(vin):
    """Full structural decode. Never raises — unknown fields come back as None/''.

    Returns a dict: wmi, manufacturer, region, country, vds, check_digit,
    model_year, plant, serial, valid_format, check_digit_ok.
    """
    vin = normalize(vin)
    wmi = vin[:3]
    out = {
        "vin": vin,
        "wmi": wmi,
        "manufacturer": _WMI.get(wmi),
        "region": decode_region(vin) if vin else "unknown",
        "country": _COUNTRY.get(vin[:1]) if vin else None,
        "vds": vin[3:9] if len(vin) >= 9 else "",       # descriptor: body/engine/restraints
        "check_digit": vin[8] if len(vin) >= 9 else "",
        "model_year": decode_model_year(vin),
        "plant": vin[10] if len(vin) >= 11 else "",     # assembly plant code
        "serial": vin[11:] if len(vin) >= 12 else "",
        "valid_format": is_wellformed(vin),
        "check_digit_ok": check_digit_ok(vin) if is_wellformed(vin) else False,
    }
    return out


def validity_note(vin):
    """One-line human verdict on whether a VIN read can be trusted."""
    vin = normalize(vin)
    if not vin:
        return "no VIN was returned by the ECU"
    if not is_wellformed(vin):
        bad = "".join(sorted({c for c in vin if c not in _TRANSLIT and c != "X"}))
        why = f"contains invalid character(s) '{bad}'" if bad else \
              f"is {len(vin)} characters, not 17"
        return f"INVALID — the read '{vin}' {why}; the adapter likely mangled it, read again"
    if check_digit_ok(vin):
        return "valid — check digit confirms the read"
    exp = compute_check_digit(vin)
    if requires_check_digit(vin):
        return (f"CHECK-DIGIT MISMATCH — position 9 is '{vin[8]}' but should be '{exp}'. "
                "One or more characters were misread; do not order parts off this read")
    # Outside North America the check digit is not legally required, but most
    # VAG (and many other) VINs still carry a correct one — so a mismatch is a
    # yellow flag, not a green light. Say so rather than implying it's fine.
    return (f"well-formed, but its check digit ('{vin[8]}') does not match the computed "
            f"'{exp}'. That's legal for some non-US-market VINs, yet VW/Audi normally "
            "compute it correctly — so verify against the dash/door-jamb VIN before ordering parts")


def format_decode(vin, expected_last4=None):
    """Multi-line summary for the assistant/console. `expected_last4` compares
    against the VIN the session was opened with, if any."""
    vin = normalize(vin)
    if not vin:
        return "VIN: no data returned by the ECU (Mode 09 PID 02)."
    d = decode(vin)
    lines = [f"VIN: {vin}", f"  {validity_note(vin)}"]
    if d["manufacturer"]:
        lines.append(f"  Manufacturer: {d['manufacturer']} (WMI {d['wmi']})")
    else:
        lines.append(f"  WMI: {d['wmi']} — {d['region']}"
                     + (f", {d['country']}" if d["country"] else ""))
    if d["model_year"]:
        lines.append(f"  Model year: {d['model_year']} (code '{vin[9]}')")
    lines.append(f"  Assembly plant code: {d['plant']}   Serial: {d['serial']}")
    if expected_last4:
        got4 = vin[-4:]
        if got4 == expected_last4:
            lines.append(f"  Matches this session's vehicle (…{expected_last4}).")
        else:
            lines.append(f"  ⚠ MISMATCH: session started on VIN …{expected_last4}, "
                         f"but the car now reports …{got4}. Different vehicle, or a bad read.")
    return "\n".join(lines)


def read_and_check(reader, expected_last4=None):
    """Read the VIN from the car, validate and decode it. Returns (text, decode-dict|None).

    Any read/parse failure is reported in the text; it never raises for a bad or
    absent VIN — only a dead adapter propagates (handled by the caller).
    """
    import obd_modes
    vin = normalize(obd_modes.read_vin(reader) or "")
    if not vin:
        return ("VIN: the ECU did not return a VIN (Mode 09 PID 02 — No Data). "
                "Some gateways block Mode 09; the VIN on the dash/door jamb is authoritative.",
                None)
    return format_decode(vin, expected_last4), decode(vin)
