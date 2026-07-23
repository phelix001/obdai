#!/usr/bin/env python3
"""Tests for VIN-driven vehicle identification (obd_vehicle) and the sim identity.

Spec asserted: the obd_vehicle docstring contract — the vehicle is resolved from
the car's VIN (prior visit -> known-VIN shortcut -> VIN decode), never a silent
hardcoded default; an explicit --vehicle wins; the user always gets the last word
and can correct a wrong VIN; a missing/unreadable VIN falls back without crashing;
and the simulator reports whichever identity it was given so the flow can be
exercised on more than one car.

Run:  venv/bin/python -m pytest test_obd_vehicle.py -q
"""

import json
import os

import pytest

import obd_vehicle
import obd_diagnose


AUDI_VIN = "WAUHFAFL9AN064693"
HONDA_VIN = "1HGCP2F49AA000137"
KNOWN = obd_diagnose.KNOWN_VEHICLE_VINS


class DummyReader:
    """A reader whose VIN read is fully controlled."""
    def __init__(self, vin):
        self._vin = vin

    def raw(self, command):
        return None


@pytest.fixture(autouse=True)
def vin_from_reader(monkeypatch):
    """obd_modes.read_vin returns whatever the DummyReader/SimReader carries."""
    import obd_modes

    def fake(reader):
        if isinstance(reader, obd_diagnose.SimReader):
            return reader.vin
        return getattr(reader, "_vin", None)

    monkeypatch.setattr(obd_modes, "read_vin", fake)


def _history(tmp_path, vin, vehicle):
    d = tmp_path / "history"
    d.mkdir(exist_ok=True)
    (d / f"{vin}.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00", "vin": vin, "vehicle": vehicle}) + "\n")
    return str(tmp_path)


# --------------------------------------------------------------------------- #
# suggest_from_vin
# --------------------------------------------------------------------------- #
def test_suggest_prefers_history(tmp_path):
    sd = _history(tmp_path, AUDI_VIN, "2010 Audi A4 2.0T (CAEB engine)")
    assert obd_vehicle.suggest_from_vin(sd, AUDI_VIN, KNOWN) == "2010 Audi A4 2.0T (CAEB engine)"


def test_suggest_uses_known_shortcut_when_no_history(tmp_path):
    assert obd_vehicle.suggest_from_vin(str(tmp_path), HONDA_VIN, KNOWN) == "2010 Honda Accord 2.4"


def test_suggest_falls_back_to_vin_decode(tmp_path):
    # A VIN not in history or the known map still yields year + make.
    out = obd_vehicle.suggest_from_vin(str(tmp_path), HONDA_VIN, known={})
    assert out == "2010 Honda"


def test_suggest_none_for_empty_vin(tmp_path):
    assert obd_vehicle.suggest_from_vin(str(tmp_path), "", KNOWN) is None


def test_history_naming_beats_known_map(tmp_path):
    # If the shop named this VIN something specific, that wins over the generic map.
    sd = _history(tmp_path, HONDA_VIN, "2010 Honda Accord EX-L (owner's car)")
    assert "owner's car" in obd_vehicle.suggest_from_vin(sd, HONDA_VIN, KNOWN)


# --------------------------------------------------------------------------- #
# resolve — non-interactive
# --------------------------------------------------------------------------- #
def test_noninteractive_returns_detected_vehicle(tmp_path):
    v, vin = obd_vehicle.resolve(DummyReader(HONDA_VIN), str(tmp_path),
                                 default="fallback", known=KNOWN, interactive=False)
    assert v == "2010 Honda Accord 2.4"
    assert vin == HONDA_VIN


def test_explicit_vehicle_wins(tmp_path):
    v, vin = obd_vehicle.resolve(DummyReader(AUDI_VIN), str(tmp_path),
                                 default="fallback", explicit="1998 Miata",
                                 known=KNOWN, interactive=False)
    assert v == "1998 Miata"
    assert vin == AUDI_VIN            # VIN still read and returned


def test_no_vin_falls_back_to_default(tmp_path):
    v, vin = obd_vehicle.resolve(DummyReader(None), str(tmp_path),
                                 default="2010 Audi A4", known=KNOWN, interactive=False)
    assert v == "2010 Audi A4"
    assert vin is None


def test_read_vin_never_raises(monkeypatch):
    import obd_modes
    monkeypatch.setattr(obd_modes, "read_vin",
                        lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    assert obd_vehicle.read_vin(DummyReader("x")) == ""


# --------------------------------------------------------------------------- #
# resolve — interactive
# --------------------------------------------------------------------------- #
def _resolve_with(tmp_path, reader, answers, **kw):
    it = iter(answers)
    seen = []
    v, vin = obd_vehicle.resolve(reader, str(tmp_path), default="fallback",
                                 known=KNOWN, ask=lambda p: next(it),
                                 out=lambda *a: seen.append(" ".join(map(str, a))), **kw)
    return v, vin, "\n".join(seen)


def test_confirm_keeps_detected(tmp_path):
    v, vin, _ = _resolve_with(tmp_path, DummyReader(HONDA_VIN), [""])
    assert v == "2010 Honda Accord 2.4"


def test_user_types_a_correction(tmp_path):
    v, vin, _ = _resolve_with(tmp_path, DummyReader(HONDA_VIN), ["2015 Subaru WRX"])
    assert v == "2015 Subaru WRX"
    assert vin == HONDA_VIN


def test_user_fixes_a_wrong_vin(tmp_path):
    sd = _history(tmp_path, AUDI_VIN, "2010 Audi A4 2.0T (CAEB engine)")
    it = iter([f"vin {AUDI_VIN}", ""])
    v, vin = obd_vehicle.resolve(DummyReader(HONDA_VIN), sd, default="fallback",
                                 known=KNOWN, ask=lambda p: next(it), out=lambda *a: None)
    assert vin == AUDI_VIN
    assert v == "2010 Audi A4 2.0T (CAEB engine)"


def test_bare_17char_vin_is_treated_as_a_vin(tmp_path):
    it = iter([AUDI_VIN, ""])
    v, vin = obd_vehicle.resolve(DummyReader(HONDA_VIN), str(tmp_path), default="fallback",
                                 known=KNOWN, ask=lambda p: next(it), out=lambda *a: None)
    assert vin == AUDI_VIN


def test_invalid_vin_entry_reprompts(tmp_path):
    # 'vin GARBAGE' is rejected with a note; then a real correction is accepted.
    v, vin, log = _resolve_with(tmp_path, DummyReader(HONDA_VIN),
                                ["vin NOTAVALIDVIN0", "2012 Mazda 3"])
    assert v == "2012 Mazda 3"
    assert "INVALID" in log or "characters" in log


def test_eof_during_confirm_keeps_detected(tmp_path):
    def raise_eof(_):
        raise EOFError

    v, vin = obd_vehicle.resolve(DummyReader(HONDA_VIN), str(tmp_path), default="fallback",
                                 known=KNOWN, ask=raise_eof, out=lambda *a: None)
    assert v == "2010 Honda Accord 2.4"


# --------------------------------------------------------------------------- #
# Simulator identity
# --------------------------------------------------------------------------- #
def test_sim_reports_its_configured_vin():
    sim = obd_diagnose.SimReader(vin=HONDA_VIN, vehicle="2010 Honda Accord 2.4")
    data = sim.raw("0902")
    vin = "".join(chr(b) for b in data[3:])
    assert vin == HONDA_VIN


def test_sim_defaults_to_the_reference_audi():
    sim = obd_diagnose.SimReader()
    assert sim.vin == obd_diagnose.SIM_CARS["audi"]["vin"]


def test_make_sim_selects_by_sim_car():
    class A:
        simulate = True
        sim_car = "honda"
    sim = obd_diagnose._make_sim(A())
    assert sim.vin == HONDA_VIN
    assert sim.vehicle == "2010 Honda Accord 2.4"


def test_make_sim_defaults_to_audi_when_absent():
    class A:  # no sim_car attribute at all (e.g. the diagnose CLI)
        simulate = True
    sim = obd_diagnose._make_sim(A())
    assert sim.vehicle == obd_diagnose.SIM_CARS["audi"]["vehicle"]


def test_known_vins_are_all_valid_check_digits():
    import obd_vin
    for vin in obd_diagnose.KNOWN_VEHICLE_VINS:
        assert obd_vin.check_digit_ok(vin), f"{vin} has a bad check digit"
