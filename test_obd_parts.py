#!/usr/bin/env python3
"""Tests for parts/links + the suggest_parts / full_diagnosis chat tools.

Spec asserted: the obd_parts docstring contract — a part name (and optional number)
becomes valid RockAuto / NAPA / YouTube *search* links; the AI never writes store URLs
itself; suggest_parts returns tappable markdown; full_diagnosis reads a baseline, runs
the structured diagnosis, and returns the problem + cheapest fix + parts-with-links. The
one-shot report (obd_diagnose.render_diagnosis) reuses the same link builders.

Run:  venv/bin/python -m pytest test_obd_parts.py -q
"""

import pytest

import obd_parts
import obd_chat


# --------------------------------------------------------------------------- #
# URL builders
# --------------------------------------------------------------------------- #
def test_store_links_by_name():
    L = obd_parts.store_links("PCV valve / oil separator")
    assert L["rockauto"] == ("https://www.rockauto.com/en/partsearch/?partname="
                             "PCV%20valve%20/%20oil%20separator")
    assert L["napa"].startswith("https://www.napaonline.com/en/search?text=PCV%20valve")
    assert "referer=v2" in L["napa"]


def test_store_links_by_part_number_when_present():
    L = obd_parts.store_links("PCV valve", "06H103495AK")
    assert L["rockauto"] == "https://www.rockauto.com/en/partsearch/?partnum=06H103495AK"


def test_youtube_link_and_empty():
    assert obd_parts.youtube_link("2010 Audi A4 PCV replace") == \
        "https://www.youtube.com/results?search_query=2010%20Audi%20A4%20PCV%20replace"
    assert obd_parts.youtube_link("") == ""
    assert obd_parts.youtube_link("   ") == ""


def test_parts_markdown_has_links_and_video():
    md = obd_parts.parts_markdown(
        [{"name": "PCV valve", "part_number": "06H103495AK"},
         {"name": "Intake hose"}],
        video_search="Audi A4 PCV how-to")
    assert "[RockAuto](https://www.rockauto.com/en/partsearch/?partnum=06H103495AK)" in md
    assert "[NAPA](https://www.napaonline.com/en/search?text=Intake%20hose" in md
    assert "[PN 06H103495AK]" in md
    assert "**How-to:** [Audi A4 PCV how-to](https://www.youtube.com/results?search_query=" in md


def test_parts_markdown_empty_is_blank():
    assert obd_parts.parts_markdown([]) == ""
    assert obd_parts.parts_markdown([{"name": ""}]) == ""


def test_parts_markdown_skips_nameless_entries():
    md = obd_parts.parts_markdown([{"name": ""}, {"name": "Coil pack"}])
    assert "Coil pack" in md and md.count("\n- ") == 1   # exactly one top-level part


def test_plain_parts_lines_for_reports():
    lines = obd_parts.plain_parts_lines([{"name": "MAF sensor", "part_number": "06J906461"}])
    assert lines and "RockAuto: https://www.rockauto.com/en/partsearch/?partnum=06J906461" in lines[0]


# --------------------------------------------------------------------------- #
# The one-shot report reuses the same builders (no regression)
# --------------------------------------------------------------------------- #
def test_render_diagnosis_uses_shared_links(capsys):
    import obd_diagnose
    data = {"most_likely_problem": "PCV leak", "estimated_repair_cost": "$150-400",
            "summary": "Lean trims.", "cheapest_fix": "Replace PCV.",
            "parts": [{"name": "PCV valve", "part_number": "06H103495AK", "search_query": "x"}],
            "video_search": "Audi A4 PCV"}
    lines, video = obd_diagnose.render_diagnosis(data, "2010 Audi A4")
    assert video == "https://www.youtube.com/results?search_query=Audi%20A4%20PCV"
    assert any("partnum=06H103495AK" in l for l in lines)


# --------------------------------------------------------------------------- #
# suggest_parts tool
# --------------------------------------------------------------------------- #
def test_suggest_parts_tool_returns_links():
    out = obd_chat.execute_tool(
        reader=None, name="suggest_parts",
        args={"parts": [{"name": "PCV valve", "part_number": "06H103495AK"}],
              "video_search": "Audi A4 PCV replace"},
        history=48, interval=0.3)
    assert "RockAuto" in out and "partnum=06H103495AK" in out
    assert "How-to" in out


def test_suggest_parts_empty():
    out = obd_chat.execute_tool(None, "suggest_parts", {"parts": []}, 48, 0.3)
    assert "No parts" in out


# --------------------------------------------------------------------------- #
# full_diagnosis tool
# --------------------------------------------------------------------------- #
class FakeEngine:
    name = "Claude"

    def __init__(self, data):
        self._data = data

    def structured(self, prompt, schema):
        return self._data


class FakeReader:
    """Enough of a reader for read_baseline: returns fixed values for any signal."""
    def query(self, pid):
        return [128]           # neutral-ish byte for any Mode 01 pid

    def dtcs(self):
        return ["P0420"]

    def fuel_status_text(self):
        return "closed loop"


DIAG = {"most_likely_problem": "PCV valve leak", "estimated_repair_cost": "$150-400",
        "summary": "LTFT high, falls under rev — vacuum-leak signature.",
        "cheapest_fix": "Replace the PCV valve.",
        "parts": [{"name": "PCV valve / oil separator", "part_number": "06H103495AK",
                   "search_query": "2010 Audi A4 PCV"}],
        "video_search": "2010 Audi A4 PCV replacement"}


def test_full_diagnosis_tool_formats_report_with_links(tmp_path, monkeypatch):
    # keep any save side-effects inside tmp
    monkeypatch.chdir(tmp_path)
    out = obd_chat.execute_tool(
        reader=FakeReader(), name="full_diagnosis",
        args={"symptoms": "rough idle", "save": False},
        history=48, interval=0.3,
        engine=FakeEngine(DIAG), vehicle="2010 Audi A4 2.0T")
    assert "Most likely problem:" in out and "PCV valve leak" in out
    assert "Cheapest fix:" in out
    assert "partnum=06H103495AK" in out          # buyable part link
    assert "youtube.com/results" in out          # how-to link


def test_full_diagnosis_needs_engine_and_vehicle():
    out = obd_chat.execute_tool(FakeReader(), "full_diagnosis", {}, 48, 0.3)
    assert "unavailable" in out


def test_full_diagnosis_is_registered():
    names = [t["name"] for t in obd_chat.TOOL_DEFS]
    assert "full_diagnosis" in names and "suggest_parts" in names
