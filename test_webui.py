#!/usr/bin/env python3
"""Smoke tests for the CarChat web layer (webui/server.py).

Spec asserted: the server docstring contract — the web UI reuses the same engine
(obd_chat.run_turn) and photo pipeline as the CLI; a photo uploaded via /api/upload
is downscaled, queued, and sent with the next /api/chat turn; the session JSON holds
path references, not base64; and status/new/clear behave.

The model itself is stubbed (run_turn) so these run offline with no API key.

Run:  venv/bin/python -m pytest test_webui.py -q
"""

import argparse
import io
import os
import sys
import types

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient

import obd_diagnose
import obd_images
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui"))
import server


def _jpg_bytes(size=(1200, 900), color=(200, 40, 40)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Fake engine so boot() needs no API key; stub the model turn.
    fake_engine = types.SimpleNamespace(name="Claude", model="test", client=None)
    monkeypatch.setattr(obd_diagnose, "build_engine", lambda provider: fake_engine)

    def fake_turn(engine, reader, system, history, interval, messages,
                  expected_vin4=None, on_tool=None, vehicle=None):
        if on_tool:
            on_tool("read_current")
        messages.append({"role": "assistant",
                         "content": [{"type": "text", "text": "stub reply"}]})
        return "stub reply"

    # Patch the obd_chat module itself — obd_api (where the logic now lives) holds a
    # reference to the same module object, so this covers both front doors.
    import obd_chat
    monkeypatch.setattr(obd_chat, "run_turn", fake_turn)
    # keep session + media under tmp so tests don't litter the repo
    monkeypatch.setattr(obd_chat, "_sessions_dir", lambda: str(tmp_path))

    args = argparse.Namespace(provider="claude", vehicle=None, simulate=True,
                              sim_car="honda", baud=None, history=48, port_dev=None)
    server.boot(args)
    return TestClient(server.app)


def test_status_reports_identified_car(client):
    s = client.get("/api/status").json()
    assert s["vehicle"] == "2010 Honda Accord 2.4"
    assert s["simulated"] is True
    assert s["provider"] == "Claude"
    assert s["images"] == 0 and s["turns"] == 0


def test_index_serves_the_ui(client):
    html = client.get("/").text
    assert "OBDAI" in html and "Add a photo" in html


def test_upload_queues_a_downscaled_photo(client):
    r = client.post("/api/upload", files={"file": ("dash.jpg", _jpg_bytes(), "image/jpeg")})
    j = r.json()
    assert j["ok"] and j["pending"] == 1
    assert j["attachment"]["thumb"].startswith("data:image/jpeg;base64,")
    # queued, visible in status
    assert len(client.get("/api/status").json()["pending"]) == 1


def test_upload_rejects_a_non_image(client):
    r = client.post("/api/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    assert "error" in r.json()


def test_chat_sends_queued_photo_and_runs_a_turn(client):
    client.post("/api/upload", files={"file": ("dash.jpg", _jpg_bytes(), "image/jpeg")})
    r = client.post("/api/chat", data={"text": "what is this?"}).json()
    assert r["reply"] == "stub reply"
    assert r["tools"] == ["read_current"]
    assert r["images_sent"] == 1
    # pending cleared after send
    assert client.get("/api/status").json()["pending"] == []


def test_session_json_holds_paths_not_base64(client, tmp_path):
    client.post("/api/upload", files={"file": ("dash.jpg", _jpg_bytes(), "image/jpeg")})
    client.post("/api/chat", data={"text": "look"})
    # the saved session on disk must not embed image bytes
    import glob, json
    files = glob.glob(str(tmp_path / "session_*.json"))
    assert files
    blob = open(files[0]).read()
    assert "base64" not in blob
    assert obd_images.IMAGE_REF_TYPE in blob


def test_empty_message_is_rejected(client):
    assert client.post("/api/chat", data={"text": "  "}).status_code == 400


def test_clear_pending(client):
    client.post("/api/upload", files={"file": ("a.jpg", _jpg_bytes(), "image/jpeg")})
    assert client.post("/api/pending/clear").json()["cleared"] == 1
    assert client.get("/api/status").json()["pending"] == []


def test_new_session_resets(client):
    client.post("/api/chat", data={"text": "hi"})
    assert client.get("/api/status").json()["turns"] == 1
    client.post("/api/new")
    assert client.get("/api/status").json()["turns"] == 0


# --------------------------------------------------------------------------- #
# Phase C: adapter label, vehicle correction, streaming
# --------------------------------------------------------------------------- #
def test_status_reports_adapter(client):
    assert client.get("/api/status").json()["adapter"] == "simulated"


def test_vehicle_correct_by_description(client):
    assert client.get("/api/vehicle").json()["vehicle"] == "2010 Honda Accord 2.4"
    r = client.post("/api/vehicle", data={"vehicle": "2013 VW Golf GTI"}).json()
    assert r["ok"] and r["vehicle"] == "2013 VW Golf GTI"
    assert client.get("/api/status").json()["vehicle"] == "2013 VW Golf GTI"


def test_vehicle_correct_by_vin_uses_known_map(client):
    r = client.post("/api/vehicle", data={"vin": "WAUHFAFL9AN064693"}).json()
    assert r["ok"] and r["vin"] == "WAUHFAFL9AN064693"
    assert "Audi" in r["vehicle"]          # decoded/identified from the VIN


def test_vehicle_rejects_bad_vin(client):
    r = client.post("/api/vehicle", data={"vin": "TOOSHORT"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_chat_stream_emits_tool_then_reply(client):
    client.post("/api/upload", files={"file": ("d.jpg", _jpg_bytes(), "image/jpeg")})
    body = client.post("/api/chat/stream", data={"text": "what is this?"}).text
    # the SSE stream carries a live tool event, then the final reply
    assert "event: tool" in body and "read_current" in body
    assert "event: reply" in body and "stub reply" in body
    # attachment was consumed
    assert client.get("/api/status").json()["pending"] == []


def test_chat_stream_rejects_empty(client):
    assert client.post("/api/chat/stream", data={"text": ""}).status_code == 400
