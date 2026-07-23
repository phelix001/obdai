#!/usr/bin/env python3
"""Identify the car at the start of a session — from its VIN, not a hardcoded guess.

The old behaviour asserted "2010 Audi A4" for every session because that was the
default `--vehicle`. That is wrong the moment the tool touches a different car:
the assistant is told, as fact, the wrong make/model/engine.

This module resolves the vehicle the way a shop does — read the VIN off the car,
recognise it (from prior visits on record, then a plain VIN decode), show what it
found, and let the user correct it before anything relies on it:

    VIN from the car: WAUHFAFL9AN064693  (valid — check digit confirms the read)
    Detected vehicle: 2010 Audi A4 2.0T (CAEB engine)
    Enter to confirm · type the correct vehicle · 'vin <VIN>' if the VIN is wrong:

Priority of the suggestion: an explicit --vehicle the user passed, then a prior
visit in history/ keyed by this VIN, then a known-VIN shortcut, then a VIN decode
(year + make), and finally the caller's default. The user always gets the last
word — a wrong or unreadable VIN never locks in the wrong car.
"""

import obd_history
import obd_modes
import obd_vin


def read_vin(reader):
    """VIN off the car, normalized. '' if the ECU returns none or the read fails.

    Never raises: identifying the car must not abort the session — if the VIN
    can't be read we fall back to the default and let the user name the car.
    """
    try:
        return obd_vin.normalize(obd_modes.read_vin(reader) or "")
    except Exception:
        return ""


def suggest_from_vin(script_dir, vin, known=None):
    """Best-effort vehicle description for a VIN, or None.

    history (a real prior visit) -> known-VIN shortcut -> structural VIN decode.
    """
    if not vin:
        return None
    records = obd_history.load_history(script_dir, "", vin)
    for rec in reversed(records):                 # most recent naming wins
        if rec.get("vehicle"):
            return rec["vehicle"]
    if known and vin in known:
        return known[vin]
    d = obd_vin.decode(vin)
    parts = []
    if d["model_year"]:
        parts.append(str(d["model_year"]))
    if d["manufacturer"]:
        parts.append(d["manufacturer"].split(" (")[0])     # "Audi (Germany)" -> "Audi"
    elif d["region"] and d["region"] != "unknown":
        parts.append(d["region"])
    return " ".join(parts) or None


def _looks_like_vin(text):
    t = obd_vin.normalize(text)
    return len(t) == 17 and obd_vin.is_wellformed(t)


def resolve(reader, script_dir, default, explicit=None, known=None,
            interactive=True, ask=input, out=print):
    """Resolve (vehicle_description, vin) for a new session.

    `explicit` is an --vehicle the user passed (wins outright as the suggestion).
    `known` maps VIN->description for known-good shortcuts. When `interactive`,
    the user confirms/edits the vehicle and can re-enter a corrected VIN.
    """
    vin = read_vin(reader)
    suggested = explicit or suggest_from_vin(script_dir, vin, known) or default

    if not interactive:
        return suggested, (vin or None)

    if vin:
        out(f"\nVIN from the car: {vin}  ({obd_vin.validity_note(vin)})")
    else:
        out("\nThe ECU did not return a VIN (Mode 09) — the dash/door-jamb VIN is authoritative.")
    out(f"Detected vehicle: {suggested}")

    prompt = "Enter to confirm · type the correct vehicle · 'vin <VIN>' if the VIN is wrong: "
    while True:
        try:
            resp = ask(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            out("")
            break
        if not resp:
            break

        low = resp.lower()
        if low.startswith("vin ") or _looks_like_vin(resp):
            new_vin = obd_vin.normalize(resp[4:] if low.startswith("vin ") else resp)
            if not obd_vin.is_wellformed(new_vin):
                out("  " + obd_vin.validity_note(new_vin))
                continue
            vin = new_vin
            suggested = suggest_from_vin(script_dir, vin, known) or suggested
            out(f"  {vin} → {suggested}  ({obd_vin.validity_note(vin)})")
            prompt = "Enter to confirm, or type the correct vehicle: "
            continue

        # Anything else is the user naming the vehicle directly.
        suggested = resp
        break

    return suggested, (vin or None)
