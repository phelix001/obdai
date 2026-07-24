#!/usr/bin/env python3
"""Tests for the transport-agnostic handlers (obd_api) and the Android door (obd_bridge).

Spec asserted: the obd_api / obd_bridge docstring contract — the same handlers back both
front doors (desktop FastAPI, Android Chaquopy bridge); handlers take plain Python and
return JSON-serializable dicts; photos cross as raw bytes / base64 so no multipart
parsing exists on device; a turn is start+poll (a synchronous JS bridge can't stream);
and `obd_bridge.call` always returns a JSON string and never raises into Kotlin.

The model is stubbed, so these run offline with no API key and no network.

Run:  venv/bin/python -m pytest test_obd_bridge.py -q
"""

import base64
import io
import json
import types

import pytest
from PIL import Image

import obd_api
import obd_bridge
import obd_chat
import obd_diagnose


def _jpg(size=(800, 600)):
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 90, 160)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def booted(monkeypatch, tmp_path):
    """A booted simulated session with the model stubbed."""
    monkeypatch.setattr(obd_diagnose, "build_engine",
                        lambda provider: types.SimpleNamespace(
                            name="Claude", model="test", client=None))

    def fake_turn(engine, reader, system, history, interval, messages,
                  expected_vin4=None, on_tool=None, vehicle=None):
        if on_tool:
            on_tool("read_current")
            on_tool("read_trouble_codes")
        messages.append({"role": "assistant",
                         "content": [{"type": "text", "text": "stub reply"}]})
        return "stub reply"

    monkeypatch.setattr(obd_chat, "run_turn", fake_turn)
    monkeypatch.setattr(obd_chat, "_sessions_dir", lambda: str(tmp_path))
    obd_api.boot(provider="claude", simulate=True, sim_car="honda")
    return obd_api


# --------------------------------------------------------------------------- #
# obd_api handlers
# --------------------------------------------------------------------------- #
def test_status_before_boot_is_not_booted():
    # Fresh module state (no adapter chosen yet) → the UI shows the connect screen.
    obd_api.S.clear()
    assert obd_api.status() == {"booted": False}


def test_boot_identifies_the_car(booted):
    s = booted.status()
    assert s["booted"] is True
    assert s["vehicle"] == "2010 Honda Accord 2.4"
    assert s["simulated"] is True and s["adapter"] == "simulated"
    assert s["busy"] is False


def test_upload_takes_raw_bytes_no_multipart(booted):
    r = booted.upload("dash.jpg", _jpg())
    assert r["ok"] and r["pending"] == 1
    assert r["attachment"]["thumb"].startswith("data:image/jpeg;base64,")
    assert booted.status()["pending"]


def test_upload_rejects_non_image(booted):
    assert "error" in booted.upload("notes.txt", b"hello")


def test_pending_clear(booted):
    booted.upload("a.jpg", _jpg())
    assert booted.pending_clear()["cleared"] == 1
    assert booted.status()["pending"] == []


def test_vehicle_set_and_get(booted):
    assert booted.vehicle_get()["vehicle"] == "2010 Honda Accord 2.4"
    r = booted.vehicle_set(vehicle="2013 VW Golf GTI")
    assert r["ok"] and booted.status()["vehicle"] == "2013 VW Golf GTI"


def test_vehicle_set_by_vin(booted):
    r = booted.vehicle_set(vin="WAUHFAFL9AN064693")
    assert r["ok"] and "Audi" in r["vehicle"]


def test_vehicle_set_rejects_bad_vin(booted):
    assert "error" in booted.vehicle_set(vin="TOOSHORT")


def test_chat_start_poll_streams_tools_then_reply(booted):
    booted.upload("d.jpg", _jpg())
    assert booted.chat_start("what is this?") == {"ok": True}
    tools, result = [], None
    for _ in range(200):
        p = booted.poll(timeout=0.1)
        tools += [e["name"] for e in p["events"] if e["type"] == "tool"]
        if p["done"]:
            result = p["result"]
            break
    assert tools == ["read_current", "read_trouble_codes"]
    assert result["reply"] == "stub reply"
    assert result["images_sent"] == 1
    assert booted.status()["pending"] == []      # attachment consumed


def test_chat_sync_wrapper(booted):
    r = booted.chat("hello")
    assert r["reply"] == "stub reply" and r["tools"]


def test_empty_message_rejected(booted):
    assert "error" in booted.chat_start("   ")


def test_new_session_resets(booted):
    booted.chat("hi")
    assert booted.status()["turns"] == 1
    booted.new_session()
    assert booted.status()["turns"] == 0


# --------------------------------------------------------------------------- #
# obd_bridge — the Kotlin-facing door
# --------------------------------------------------------------------------- #
def _call(action, **payload):
    out = obd_bridge.call(action, json.dumps(payload))
    assert isinstance(out, str)          # Kotlin only ever gets a JSON string
    return json.loads(out)


def test_bridge_actions_listed():
    for a in ("boot", "status", "upload", "chat_start", "poll", "vehicle_set"):
        assert a in obd_bridge.actions()


def test_bridge_status_roundtrip(booted):
    assert _call("status")["vehicle"] == "2010 Honda Accord 2.4"


def test_bridge_upload_takes_base64(booted):
    r = _call("upload", filename="d.jpg", data_b64=base64.b64encode(_jpg()).decode())
    assert r["ok"] and r["pending"] == 1


def test_bridge_full_turn(booted):
    # bridge poll is non-blocking by contract; the JS client sleeps between polls,
    # so the test does too (a tight spin would starve the worker thread).
    import time
    assert _call("chat_start", text="hi")["ok"] is True
    for _ in range(200):
        p = _call("poll")
        if p["done"]:
            assert p["result"]["reply"] == "stub reply"
            break
        time.sleep(0.05)
    else:
        pytest.fail("turn never completed")


def test_bridge_unknown_action_is_reported_not_raised():
    r = _call("does_not_exist")
    assert "unknown action" in r["error"] and r["actions"]


def test_bridge_bad_payload_is_reported():
    r = json.loads(obd_bridge.call("status", "not json"))
    assert "bad payload" in r["error"]


def test_bridge_never_raises_into_kotlin(monkeypatch, booted):
    monkeypatch.setattr(obd_api, "status",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _call("status")
    assert "RuntimeError" in r["error"] and "trace" in r


def test_bridge_serves_the_ui_without_a_server():
    html = obd_bridge.ui_html()
    assert "OBDAI" in html and "AndroidBridge" in html   # the page knows both transports
