# -*- coding: utf-8 -*-
"""
obd_mode06_ref.py -- Authoritative reference data for decoding OBD-II Service $06
(Mode 06, "On-Board Monitoring Test Results for specific monitored systems"),
per SAE J1979 / J1979-2 over ISO 15765 (CAN).

Mode 06 reports, for each standardized monitor (OBDMID), a set of test results.
Every test result carries a "Unit and Scaling ID" (UASID) byte that tells the
scan tool how to convert the raw 16-bit test value / min-limit / max-limit words
into engineering units. This module provides:

  * MID_NAMES : OBDMID (int) -> human-readable monitor name
  * UAS       : UASID  (int) -> {"mult", "offset", "unit", "signed"}
  * scale()   : apply a UASID conversion to a raw 16-bit word

Namespaces note: OBDMIDs and UASIDs are two SEPARATE numbering spaces. The same
integer (e.g. 0x41) means different things depending on context -- as an OBDMID
it is "O2 Sensor Heater Monitor B1S1"; as a UASID it is "0.01 microampere". This
module keeps them in two separate dicts, which is correct.

------------------------------------------------------------------------------
SOURCES (verified 2026-07-20)
------------------------------------------------------------------------------
The UAS table and the OBDMID list were taken from the SAE J1979 standard as
implemented by the widely-used, reputable open-source python-OBD library, and
cross-checked against multiple independent implementations via GitHub code
search and general OBD references:

  * python-OBD, obd/UnitsAndScaling.py  (UAS_IDS table -- J1979 Appendix scaling)
    https://github.com/brendan-w/python-OBD/blob/master/obd/UnitsAndScaling.py
  * python-OBD, obd/commands.py  (Mode 06 OBDMID definitions, cmds 0600-06B1)
    https://github.com/brendan-w/python-OBD/blob/master/obd/commands.py
  * Wikipedia, "OBD-II PIDs" (Service 06 overview / service-mode summary)
    https://en.wikipedia.org/wiki/OBD-II_PIDs
  * SAE J1979 / J1979-DA (Digital Annex) "E/E Diagnostic Test Modes" -- the
    normative standard these implementations transcribe.
  * Cross-confirmation: GitHub code search returned dozens of independent repos
    pairing "Catalyst Monitor Bank 1" with request 0621 and "O2 Sensor Heater
    Monitor" with request 0641, agreeing with python-OBD.

------------------------------------------------------------------------------
IMPORTANT CORRECTION vs. this file's original spec
------------------------------------------------------------------------------
The task brief listed "O2 sensor heater monitors (0x21-0x28)" and "Catalyst
monitors (0x41 = Bank 1, 0x42 = Bank 2)". Per the actual SAE J1979 OBDMID
assignments, those two blocks are SWAPPED:

    0x21-0x24  ->  Catalyst Monitor Bank 1..4          (NOT O2 heaters)
    0x41-0x50  ->  O2 Sensor Heater Monitor B1..4 S1..4 (NOT catalyst)

This module uses the CORRECT J1979 values, because Mode 06 data drives what a
mechanic sees: a vehicle that answers MID 0x41 is genuinely reporting an O2
heater result, and labeling it "catalyst" would mislead. The catalyst monitor
lives at 0x21 (Bank 1) / 0x22 (Bank 2).

Everything here is pure standard library. No external dependencies.
"""

# ---------------------------------------------------------------------------
# 1) OBDMID (Monitor ID) -> name
#    Source: python-OBD commands.py (requests 0600..06B1), i.e. MID = low byte.
#    Ranges: [01-20] O2 sensor monitors, [21-40] catalyst/EGR/VVT/EVAP,
#            [41-60] O2 heater / heated-catalyst / secondary air,
#            [61-80] (heated cat, sec air), [81-A0] fuel/boost/NOx,
#            [A1-C0] misfire / PM filter.
# ---------------------------------------------------------------------------
MID_NAMES = {
    # --- "Supported MIDs" enumeration PIDs (bitmaps, not a monitor themselves) -
    0x00: "Supported MIDs [01-20]",
    0x20: "Supported MIDs [21-40]",
    0x40: "Supported MIDs [41-60]",
    0x60: "Supported MIDs [61-80]",
    0x80: "Supported MIDs [81-A0]",
    0xA0: "Supported MIDs [A1-C0]",

    # --- O2 Sensor Monitors (0x01-0x10): Bank 1..4, Sensor 1..4 -------------
    0x01: "O2 Sensor Monitor B1S1",
    0x02: "O2 Sensor Monitor B1S2",
    0x03: "O2 Sensor Monitor B1S3",
    0x04: "O2 Sensor Monitor B1S4",
    0x05: "O2 Sensor Monitor B2S1",
    0x06: "O2 Sensor Monitor B2S2",
    0x07: "O2 Sensor Monitor B2S3",
    0x08: "O2 Sensor Monitor B2S4",
    0x09: "O2 Sensor Monitor B3S1",
    0x0A: "O2 Sensor Monitor B3S2",
    0x0B: "O2 Sensor Monitor B3S3",
    0x0C: "O2 Sensor Monitor B3S4",
    0x0D: "O2 Sensor Monitor B4S1",
    0x0E: "O2 Sensor Monitor B4S2",
    0x0F: "O2 Sensor Monitor B4S3",
    0x10: "O2 Sensor Monitor B4S4",

    # --- Catalyst Monitors (0x21-0x24): Bank 1..4 --------------------------
    0x21: "Catalyst Monitor Bank 1",
    0x22: "Catalyst Monitor Bank 2",
    0x23: "Catalyst Monitor Bank 3",
    0x24: "Catalyst Monitor Bank 4",

    # --- EGR / VVT Monitors (0x31-0x38) ------------------------------------
    0x31: "EGR Monitor Bank 1",
    0x32: "EGR Monitor Bank 2",
    0x33: "EGR Monitor Bank 3",
    0x34: "EGR Monitor Bank 4",
    0x35: "VVT Monitor Bank 1",
    0x36: "VVT Monitor Bank 2",
    0x37: "VVT Monitor Bank 3",
    0x38: "VVT Monitor Bank 4",

    # --- EVAP Monitors (0x39-0x3D) -----------------------------------------
    0x39: 'EVAP Monitor (Cap Off / 0.150")',
    0x3A: 'EVAP Monitor (0.090")',
    0x3B: 'EVAP Monitor (0.040")',
    0x3C: 'EVAP Monitor (0.020")',
    0x3D: "Purge Flow Monitor",

    # --- O2 Sensor Heater Monitors (0x41-0x50): Bank 1..4, Sensor 1..4 -----
    0x41: "O2 Sensor Heater Monitor B1S1",
    0x42: "O2 Sensor Heater Monitor B1S2",
    0x43: "O2 Sensor Heater Monitor B1S3",
    0x44: "O2 Sensor Heater Monitor B1S4",
    0x45: "O2 Sensor Heater Monitor B2S1",
    0x46: "O2 Sensor Heater Monitor B2S2",
    0x47: "O2 Sensor Heater Monitor B2S3",
    0x48: "O2 Sensor Heater Monitor B2S4",
    0x49: "O2 Sensor Heater Monitor B3S1",
    0x4A: "O2 Sensor Heater Monitor B3S2",
    0x4B: "O2 Sensor Heater Monitor B3S3",
    0x4C: "O2 Sensor Heater Monitor B3S4",
    0x4D: "O2 Sensor Heater Monitor B4S1",
    0x4E: "O2 Sensor Heater Monitor B4S2",
    0x4F: "O2 Sensor Heater Monitor B4S3",
    0x50: "O2 Sensor Heater Monitor B4S4",

    # --- Heated Catalyst Monitors (0x61-0x64) ------------------------------
    0x61: "Heated Catalyst Monitor Bank 1",
    0x62: "Heated Catalyst Monitor Bank 2",
    0x63: "Heated Catalyst Monitor Bank 3",
    0x64: "Heated Catalyst Monitor Bank 4",

    # --- Secondary Air Monitors (0x71-0x74) --------------------------------
    0x71: "Secondary Air Monitor 1",
    0x72: "Secondary Air Monitor 2",
    0x73: "Secondary Air Monitor 3",
    0x74: "Secondary Air Monitor 4",

    # --- Fuel System Monitors (0x81-0x84) ----------------------------------
    0x81: "Fuel System Monitor Bank 1",
    0x82: "Fuel System Monitor Bank 2",
    0x83: "Fuel System Monitor Bank 3",
    0x84: "Fuel System Monitor Bank 4",

    # --- Boost Pressure Control Monitors (0x85-0x86) -----------------------
    0x85: "Boost Pressure Control Monitor Bank 1",
    0x86: "Boost Pressure Control Monitor Bank 2",

    # --- NOx Adsorber / NOx Catalyst Monitors (0x90-0x99) ------------------
    0x90: "NOx Absorber Monitor Bank 1",
    0x91: "NOx Absorber Monitor Bank 2",
    0x98: "NOx Catalyst Monitor Bank 1",
    0x99: "NOx Catalyst Monitor Bank 2",

    # --- Misfire Monitors (0xA1-0xAD) --------------------------------------
    0xA1: "Misfire Monitor General Data",
    0xA2: "Misfire Cylinder 1 Data",
    0xA3: "Misfire Cylinder 2 Data",
    0xA4: "Misfire Cylinder 3 Data",
    0xA5: "Misfire Cylinder 4 Data",
    0xA6: "Misfire Cylinder 5 Data",
    0xA7: "Misfire Cylinder 6 Data",
    0xA8: "Misfire Cylinder 7 Data",
    0xA9: "Misfire Cylinder 8 Data",
    0xAA: "Misfire Cylinder 9 Data",
    0xAB: "Misfire Cylinder 10 Data",
    0xAC: "Misfire Cylinder 11 Data",
    0xAD: "Misfire Cylinder 12 Data",

    # --- PM Filter (DPF) Monitors (0xB0-0xB1) ------------------------------
    0xB0: "PM Filter Monitor Bank 1",
    0xB1: "PM Filter Monitor Bank 2",
}


# ---------------------------------------------------------------------------
# 2) UAS (Unit and Scaling ID) table
#    Source: python-OBD UnitsAndScaling.py (SAE J1979 Appendix).
#    Each entry: {"mult": float, "offset": float, "unit": str, "signed": bool}
#      value = raw_interpreted * mult + offset
#    IDs >= 0x80 are the signed (two's-complement) variants of the low IDs.
# ---------------------------------------------------------------------------
UAS = {
    # ---- unsigned --------------------------------------------------------
    0x01: {"mult": 1.0,          "offset": 0.0,     "unit": "",     "signed": False},
    0x02: {"mult": 0.1,          "offset": 0.0,     "unit": "",     "signed": False},
    0x03: {"mult": 0.01,         "offset": 0.0,     "unit": "",     "signed": False},
    0x04: {"mult": 0.001,        "offset": 0.0,     "unit": "",     "signed": False},
    0x05: {"mult": 0.0000305,    "offset": 0.0,     "unit": "",     "signed": False},
    0x06: {"mult": 0.000305,     "offset": 0.0,     "unit": "",     "signed": False},
    0x07: {"mult": 0.25,         "offset": 0.0,     "unit": "rpm",  "signed": False},
    0x08: {"mult": 0.01,         "offset": 0.0,     "unit": "km/h", "signed": False},
    0x09: {"mult": 1.0,          "offset": 0.0,     "unit": "km/h", "signed": False},
    0x0A: {"mult": 0.122,        "offset": 0.0,     "unit": "mV",   "signed": False},
    0x0B: {"mult": 0.001,        "offset": 0.0,     "unit": "V",    "signed": False},
    0x0C: {"mult": 0.01,         "offset": 0.0,     "unit": "V",    "signed": False},
    0x0D: {"mult": 0.00390625,   "offset": 0.0,     "unit": "mA",   "signed": False},
    0x0E: {"mult": 0.001,        "offset": 0.0,     "unit": "A",    "signed": False},
    0x0F: {"mult": 0.01,         "offset": 0.0,     "unit": "A",    "signed": False},
    0x10: {"mult": 1.0,          "offset": 0.0,     "unit": "ms",   "signed": False},
    0x11: {"mult": 100.0,        "offset": 0.0,     "unit": "ms",   "signed": False},
    0x12: {"mult": 1.0,          "offset": 0.0,     "unit": "s",    "signed": False},
    0x13: {"mult": 1.0,          "offset": 0.0,     "unit": "mOhm", "signed": False},
    0x14: {"mult": 1.0,          "offset": 0.0,     "unit": "Ohm",  "signed": False},
    0x15: {"mult": 1.0,          "offset": 0.0,     "unit": "kOhm", "signed": False},
    0x16: {"mult": 0.1,          "offset": -40.0,   "unit": "°C",   "signed": False},
    0x17: {"mult": 0.01,         "offset": 0.0,     "unit": "kPa",  "signed": False},
    0x18: {"mult": 0.0117,       "offset": 0.0,     "unit": "kPa",  "signed": False},
    0x19: {"mult": 0.079,        "offset": 0.0,     "unit": "kPa",  "signed": False},
    0x1A: {"mult": 1.0,          "offset": 0.0,     "unit": "kPa",  "signed": False},
    0x1B: {"mult": 10.0,         "offset": 0.0,     "unit": "kPa",  "signed": False},
    0x1C: {"mult": 0.01,         "offset": 0.0,     "unit": "°",    "signed": False},
    0x1D: {"mult": 0.5,          "offset": 0.0,     "unit": "°",    "signed": False},
    0x1E: {"mult": 0.0000305,    "offset": 0.0,     "unit": "ratio","signed": False},
    0x1F: {"mult": 0.05,         "offset": 0.0,     "unit": "ratio","signed": False},
    0x20: {"mult": 0.00390625,   "offset": 0.0,     "unit": "ratio","signed": False},
    0x21: {"mult": 1.0,          "offset": 0.0,     "unit": "mHz",  "signed": False},
    0x22: {"mult": 1.0,          "offset": 0.0,     "unit": "Hz",   "signed": False},
    0x23: {"mult": 1.0,          "offset": 0.0,     "unit": "kHz",  "signed": False},
    0x24: {"mult": 1.0,          "offset": 0.0,     "unit": "",     "signed": False},
    0x25: {"mult": 1.0,          "offset": 0.0,     "unit": "km",   "signed": False},
    0x26: {"mult": 0.1,          "offset": 0.0,     "unit": "mV/ms","signed": False},
    0x27: {"mult": 0.01,         "offset": 0.0,     "unit": "g/s",  "signed": False},
    0x28: {"mult": 1.0,          "offset": 0.0,     "unit": "g/s",  "signed": False},
    0x29: {"mult": 0.25,         "offset": 0.0,     "unit": "Pa/s", "signed": False},
    0x2A: {"mult": 0.001,        "offset": 0.0,     "unit": "kg/h", "signed": False},
    0x2B: {"mult": 1.0,          "offset": 0.0,     "unit": "",     "signed": False},
    0x2C: {"mult": 0.01,         "offset": 0.0,     "unit": "g",    "signed": False},
    0x2D: {"mult": 0.01,         "offset": 0.0,     "unit": "mg",   "signed": False},
    # 0x2E is a J1979 "bit/boolean" scaling (value is 0 => false, non-0 => true).
    # It has no meaningful multiplier; we expose mult=1 so scale() returns the raw
    # word unchanged and callers can test truthiness. Unit tagged accordingly.
    0x2E: {"mult": 1.0,          "offset": 0.0,     "unit": "(bit)","signed": False},
    0x2F: {"mult": 0.01,         "offset": 0.0,     "unit": "%",    "signed": False},
    0x30: {"mult": 0.001526,     "offset": 0.0,     "unit": "%",    "signed": False},
    0x31: {"mult": 0.001,        "offset": 0.0,     "unit": "L",    "signed": False},
    0x32: {"mult": 0.0000305,    "offset": 0.0,     "unit": "in",   "signed": False},
    0x33: {"mult": 0.00024414,   "offset": 0.0,     "unit": "ratio","signed": False},
    0x34: {"mult": 1.0,          "offset": 0.0,     "unit": "min",  "signed": False},
    0x35: {"mult": 10.0,         "offset": 0.0,     "unit": "ms",   "signed": False},
    0x36: {"mult": 0.01,         "offset": 0.0,     "unit": "g",    "signed": False},
    0x37: {"mult": 0.1,          "offset": 0.0,     "unit": "g",    "signed": False},
    0x38: {"mult": 1.0,          "offset": 0.0,     "unit": "g",    "signed": False},
    0x39: {"mult": 0.01,         "offset": -327.68, "unit": "%",    "signed": False},
    0x3A: {"mult": 0.001,        "offset": 0.0,     "unit": "g",    "signed": False},
    0x3B: {"mult": 0.0001,       "offset": 0.0,     "unit": "g",    "signed": False},
    0x3C: {"mult": 0.1,          "offset": 0.0,     "unit": "µs",   "signed": False},
    0x3D: {"mult": 0.01,         "offset": 0.0,     "unit": "mA",   "signed": False},
    0x3E: {"mult": 0.00006103516,"offset": 0.0,     "unit": "mm²",  "signed": False},
    0x3F: {"mult": 0.01,         "offset": 0.0,     "unit": "L",    "signed": False},
    0x40: {"mult": 1.0,          "offset": 0.0,     "unit": "ppm",  "signed": False},
    0x41: {"mult": 0.01,         "offset": 0.0,     "unit": "µA",   "signed": False},

    # ---- signed (two's-complement) ---------------------------------------
    0x81: {"mult": 1.0,          "offset": 0.0,     "unit": "",     "signed": True},
    0x82: {"mult": 0.1,          "offset": 0.0,     "unit": "",     "signed": True},
    0x83: {"mult": 0.01,         "offset": 0.0,     "unit": "",     "signed": True},
    0x84: {"mult": 0.001,        "offset": 0.0,     "unit": "",     "signed": True},
    0x85: {"mult": 0.0000305,    "offset": 0.0,     "unit": "",     "signed": True},
    0x86: {"mult": 0.000305,     "offset": 0.0,     "unit": "",     "signed": True},
    0x87: {"mult": 1.0,          "offset": 0.0,     "unit": "ppm",  "signed": True},
    0x8A: {"mult": 0.122,        "offset": 0.0,     "unit": "mV",   "signed": True},
    0x8B: {"mult": 0.001,        "offset": 0.0,     "unit": "V",    "signed": True},
    0x8C: {"mult": 0.01,         "offset": 0.0,     "unit": "V",    "signed": True},
    0x8D: {"mult": 0.00390625,   "offset": 0.0,     "unit": "mA",   "signed": True},
    0x8E: {"mult": 0.001,        "offset": 0.0,     "unit": "A",    "signed": True},
    0x90: {"mult": 1.0,          "offset": 0.0,     "unit": "ms",   "signed": True},
    0x96: {"mult": 0.1,          "offset": 0.0,     "unit": "°C",   "signed": True},
    0x99: {"mult": 0.1,          "offset": 0.0,     "unit": "kPa",  "signed": True},
    0x9C: {"mult": 0.01,         "offset": 0.0,     "unit": "°",    "signed": True},
    0x9D: {"mult": 0.5,          "offset": 0.0,     "unit": "°",    "signed": True},
    0xA8: {"mult": 1.0,          "offset": 0.0,     "unit": "g/s",  "signed": True},
    0xA9: {"mult": 0.25,         "offset": 0.0,     "unit": "Pa/s", "signed": True},
    0xAD: {"mult": 0.01,         "offset": 0.0,     "unit": "mg",   "signed": True},
    0xAE: {"mult": 0.1,          "offset": 0.0,     "unit": "mg",   "signed": True},
    0xAF: {"mult": 0.01,         "offset": 0.0,     "unit": "%",    "signed": True},
    0xB0: {"mult": 0.003052,     "offset": 0.0,     "unit": "%",    "signed": True},
    0xB1: {"mult": 2.0,          "offset": 0.0,     "unit": "mV/s", "signed": True},
    0xFC: {"mult": 0.01,         "offset": 0.0,     "unit": "kPa",  "signed": True},
    0xFD: {"mult": 0.001,        "offset": 0.0,     "unit": "kPa",  "signed": True},
    0xFE: {"mult": 0.25,         "offset": 0.0,     "unit": "Pa",   "signed": True},
}


# ---------------------------------------------------------------------------
# 3) scale(): convert a raw 16-bit test word using a UASID
# ---------------------------------------------------------------------------
def scale(uas_id, raw):
    """Scale a raw 16-bit Mode 06 word per its Unit and Scaling ID.

    Args:
        uas_id: the UASID byte (int) accompanying the test value.
        raw:    the 16-bit UNSIGNED word exactly as transmitted (0..65535).

    Returns:
        (value: float, unit: str, signed: bool)

    Behavior:
        * If UAS[uas_id]["signed"] is True, `raw` is reinterpreted as a signed
          16-bit two's-complement value (raw >= 32768 -> raw - 65536) BEFORE
          scaling.
        * value = raw_interpreted * mult + offset
        * Unknown uas_id -> (float(raw), "", False). Never raises.
    """
    spec = UAS.get(uas_id)
    if spec is None:
        return (float(raw), "", False)

    signed = spec["signed"]
    raw_interpreted = raw - 65536 if (signed and raw >= 32768) else raw
    value = raw_interpreted * spec["mult"] + spec["offset"]
    return (float(value), spec["unit"], signed)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("MID_NAMES entries:", len(MID_NAMES))
    print("UAS entries:      ", len(UAS))

    # (a) unsigned voltage UAS 0x0C (0.01 V) on raw 0x1000 = 4096 -> 40.96 V
    val, unit, signed = scale(0x0C, 0x1000)
    assert abs(val - 40.96) < 1e-9, val
    assert unit == "V" and signed is False
    print("scale(0x0C, 0x1000) =", (round(val, 4), unit, signed))

    # (b) signed percent UAS 0xAF (0.01 %) on raw 0xFFFF -> -1 -> -0.01 %
    val, unit, signed = scale(0xAF, 0xFFFF)
    assert abs(val - (-0.01)) < 1e-9, val
    assert unit == "%" and signed is True
    print("scale(0xAF, 0xFFFF) =", (round(val, 4), unit, signed))

    # (b2) signed kPa UAS 0x99 (0.1 kPa) on raw 0x8000 = -32768 -> -3276.8 kPa
    val, unit, signed = scale(0x99, 0x8000)
    assert abs(val - (-3276.8)) < 1e-6, val
    assert unit == "kPa" and signed is True
    print("scale(0x99, 0x8000) =", (round(val, 4), unit, signed))

    # (c) temperature UAS 0x16 (0.1 C, offset -40) on raw 0x0640 = 1600 -> 120 C
    val, unit, signed = scale(0x16, 0x0640)
    assert abs(val - 120.0) < 1e-9, val
    print("scale(0x16, 0x0640) =", (round(val, 4), unit, signed))

    # (d) unknown UASID -> raw passthrough, never raises
    val, unit, signed = scale(0x77, 1234)
    assert val == 1234.0 and unit == "" and signed is False
    print("scale(0x77, 1234)   =", (val, unit, signed))

    # (e) catalyst monitor present. NOTE: per SAE J1979 the catalyst Bank 1
    #     monitor is OBDMID 0x21 (NOT 0x41 -- 0x41 is O2 Sensor Heater B1S1).
    assert MID_NAMES.get(0x21) == "Catalyst Monitor Bank 1"
    assert MID_NAMES.get(0x22) == "Catalyst Monitor Bank 2"
    print("MID_NAMES.get(0x21) =", MID_NAMES.get(0x21))
    print("MID_NAMES.get(0x22) =", MID_NAMES.get(0x22))
    print("MID_NAMES.get(0x41) =", MID_NAMES.get(0x41),
          "(0x41 is the O2 heater monitor per J1979, not catalyst)")

    print("All self-tests passed.")
