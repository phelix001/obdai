#!/usr/bin/env python3
"""Tests for VIN validation, decode, and the read_vin chat tool (obd_vin + obd_chat).

Spec asserted: the obd_vin docstring contract and ISO 3779 / 49 CFR 565 —
  * a 17-char VIN with no I/O/Q and a self-consistent position-9 check digit
    is reported valid; a mangled read is flagged, never silently trusted;
  * the structural fields (WMI/manufacturer, model year from position 10 with the
    position-7 tie-break, plant, serial) decode per the standard;
  * read_vin reads off the ECU, compares to the session VIN, and a No-Data / dead
    adapter is reported as text — the chat never crashes on a VIN read.

Check-digit fixtures are real, check-digit-valid VINs (verified by the algorithm
itself, which is the published 49 CFR 565 method).

Run:  venv/bin/python -m pytest test_obd_vin.py -q
"""

import pytest

import obd_vin
import obd_chat


# The project car (from history/) — a genuine, check-digit-valid Audi VIN.
AUDI = "WAUHFAFL9AN064693"


# --------------------------------------------------------------------------- #
# Normalize / well-formedness
# --------------------------------------------------------------------------- #
def test_normalize_strips_separators_and_upcases():
    assert obd_vin.normalize(" wau-hfafl9an 064693 ") == AUDI


def test_wellformed_rejects_ioq_and_wrong_length():
    assert obd_vin.is_wellformed(AUDI)
    assert not obd_vin.is_wellformed("WAUHFAFL9ANO64693")   # contains O
    assert not obd_vin.is_wellformed("WAUHFAFL9ANI64693")   # contains I
    assert not obd_vin.is_wellformed("WAUHFAFL9AN06469")    # 16 chars
    assert not obd_vin.is_wellformed("")


# --------------------------------------------------------------------------- #
# Check digit (49 CFR 565)
# --------------------------------------------------------------------------- #
def test_known_vin_passes_its_own_check_digit():
    assert obd_vin.compute_check_digit(AUDI) == AUDI[8]
    assert obd_vin.check_digit_ok(AUDI)


def test_single_character_error_breaks_the_check_digit():
    # Flip one character; the computed check digit no longer matches position 9.
    bad = AUDI[:5] + ("G" if AUDI[5] != "G" else "H") + AUDI[6:]
    assert not obd_vin.check_digit_ok(bad)


def test_check_digit_x_is_handled():
    # Construct a VIN whose check digit computes to 'X' (remainder 10).
    base = "1M8GDM9AXKP042788"      # classic 49 CFR 565 worked example
    assert obd_vin.compute_check_digit(base) == "X"
    assert obd_vin.check_digit_ok(base)


def test_illegal_character_yields_no_check_digit():
    assert obd_vin.compute_check_digit("WAUHFAFL9ANO64693") is None


def test_north_america_requires_check_digit_others_advisory():
    assert obd_vin.requires_check_digit("1HGCM82633A004352")   # USA
    assert not obd_vin.requires_check_digit(AUDI)              # Germany (W)


# --------------------------------------------------------------------------- #
# Decode
# --------------------------------------------------------------------------- #
def test_decode_audi_fields():
    d = obd_vin.decode(AUDI)
    assert d["wmi"] == "WAU"
    assert d["manufacturer"].startswith("Audi")
    assert d["region"] == "Europe"
    assert d["model_year"] == 2010
    assert d["plant"] == "N"
    assert d["serial"] == "064693"
    assert d["valid_format"] and d["check_digit_ok"]


def test_model_year_tiebreak_on_position_7():
    # Position 10 'A' = 1980 or 2010; a *letter* in position 7 means 2010+.
    letter7 = "WAUHFAFL9AN064693"   # pos7 'A' (letter) -> 2010
    digit7 = "1G1JC5444R7252367"    # pos7 '4' (digit)  -> 1994 (code 'R')
    assert obd_vin.decode_model_year(letter7) == 2010
    assert obd_vin.decode_model_year(digit7) == 1994


def test_decode_never_raises_on_junk():
    d = obd_vin.decode("???")
    assert d["valid_format"] is False
    assert d["model_year"] is None


# --------------------------------------------------------------------------- #
# Validity note — the safety message
# --------------------------------------------------------------------------- #
def test_note_valid():
    assert "valid" in obd_vin.validity_note(AUDI).lower()


def test_note_empty():
    assert "no VIN" in obd_vin.validity_note("")


def test_note_too_short_says_read_again():
    note = obd_vin.validity_note("WAUHFAFL9AN")
    assert "INVALID" in note and "read again" in note


def test_note_illegal_char_names_it():
    note = obd_vin.validity_note("WAUHFAFL9ANO64693")
    assert "'O'" in note


def test_note_north_america_mismatch_is_hard_stop():
    # A US VIN with a deliberately wrong check digit.
    us = "1HGCM82633A004352"
    broken = us[:8] + ("0" if us[8] != "0" else "1") + us[9:]
    note = obd_vin.validity_note(broken)
    assert "MISMATCH" in note and "do not order parts" in note


def test_note_nonNA_mismatch_is_a_yellow_flag():
    broken = AUDI[:8] + ("0" if AUDI[8] != "0" else "1") + AUDI[9:]
    note = obd_vin.validity_note(broken)
    assert "does not match" in note and "verify" in note


# --------------------------------------------------------------------------- #
# format_decode + session comparison
# --------------------------------------------------------------------------- #
def test_format_decode_flags_session_match():
    text = obd_vin.format_decode(AUDI, expected_last4="4693")
    assert "Matches this session" in text


def test_format_decode_flags_session_mismatch():
    text = obd_vin.format_decode(AUDI, expected_last4="0000")
    assert "MISMATCH" in text and "…0000" in text


def test_format_decode_no_data():
    assert "no data" in obd_vin.format_decode("").lower()


# --------------------------------------------------------------------------- #
# read_and_check — reads via a stubbed reader, never crashes
# --------------------------------------------------------------------------- #
class FakeReader:
    def __init__(self, vin):
        self._vin = vin


def _patch_read_vin(monkeypatch, value):
    import obd_modes
    monkeypatch.setattr(obd_modes, "read_vin", lambda reader: value)


def test_read_and_check_happy(monkeypatch):
    _patch_read_vin(monkeypatch, AUDI)
    text, d = obd_vin.read_and_check(FakeReader(AUDI), expected_last4="4693")
    assert d["model_year"] == 2010
    assert "Matches this session" in text


def test_read_and_check_no_vin_returned(monkeypatch):
    _patch_read_vin(monkeypatch, None)
    text, d = obd_vin.read_and_check(FakeReader(None))
    assert d is None
    assert "did not return a VIN" in text


def test_read_and_check_mangled_read_is_flagged(monkeypatch):
    _patch_read_vin(monkeypatch, "WAUHFAFL9AN0646")   # dropped chars
    text, d = obd_vin.read_and_check(FakeReader("x"))
    assert "INVALID" in text


# --------------------------------------------------------------------------- #
# The read_vin chat tool
# --------------------------------------------------------------------------- #
def test_read_vin_is_registered_as_a_tool():
    names = [t["name"] for t in obd_chat.TOOL_DEFS]
    assert "read_vin" in names


def test_execute_tool_read_vin_passes_expected_vin4(monkeypatch):
    import obd_modes
    monkeypatch.setattr(obd_modes, "read_vin", lambda reader: AUDI)
    out = obd_chat.execute_tool(FakeReader(AUDI), "read_vin", {}, 48, 0.3,
                                expected_vin4="4693")
    assert "Matches this session" in out
    assert "2010" in out


def test_execute_tool_read_vin_survives_dead_adapter(monkeypatch):
    import obd_modes

    def boom(reader):
        raise obd_chat.obd_connect.ObdConnectionError("link lost")

    monkeypatch.setattr(obd_modes, "read_vin", boom)
    out = obd_chat.execute_tool(FakeReader("x"), "read_vin", {}, 48, 0.3)
    assert "TOOL FAILED" in out and "Do not guess" in out
