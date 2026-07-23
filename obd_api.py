#!/usr/bin/env python3
"""Transport-agnostic session + handlers for the OBDAI chat UI.

One set of handlers, two front doors:

    desktop   webui/server.py   FastAPI routes  -> these functions
    Android   obd_bridge.py     Chaquopy call   -> these functions

Nothing here knows about HTTP. That's deliberate: FastAPI depends on pydantic v2 ->
pydantic-core (Rust), which Chaquopy cannot package for Android, so the on-device
build has no web server at all — the WebView calls Kotlin, Kotlin calls Python.
Keeping the logic here means both paths run the *same* code.

Handlers take plain Python and return plain dicts (JSON-serializable), so they work
equally well behind a REST route or a JS bridge. Photo uploads take raw bytes rather
than a multipart body, which also sidesteps multipart parsing on device (Python 3.13
removed `cgi`).

A chat turn is start + poll rather than a stream, because a synchronous JS bridge
can't stream: `chat_start()` kicks the turn off on a worker thread and `poll()`
returns tool events as they happen, then the final reply. The desktop SSE endpoint is
built on the same queue.
"""

import argparse
import base64
import io
import os
import queue
import threading

import obd_chat
import obd_connect
import obd_diagnose
import obd_images
import obd_transport
import obd_vehicle
import obd_vin

try:
    from PIL import Image
except ImportError:
    Image = None

HERE = os.path.dirname(os.path.abspath(__file__))

# The one live session (this is a personal, single-user tool, not a service).
S = {}


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #
def boot(provider=None, vehicle=None, simulate=False, sim_car="audi",
         port=None, baud=None, history=48):
    """Open the adapter, identify the car, build the engine. Raises ObdConnectionError."""
    engine = obd_diagnose.build_engine(provider or "claude")

    reader_args = argparse.Namespace(port=port, baud=baud,
                                     simulate=simulate, sim_car=sim_car)
    reader, simulated = obd_diagnose.open_reader(reader_args)
    if reader is None:
        raise obd_connect.ObdConnectionError(
            "No OBD adapter and no simulate mode. Connect an ELM327 (USB-OTG, "
            "Bluetooth, or WiFi) and retry, or start in simulate mode for a demo.")

    veh, vin = obd_vehicle.resolve(
        reader, HERE, default=obd_diagnose.DEFAULT_VEHICLE, explicit=vehicle,
        known=obd_diagnose.KNOWN_VEHICLE_VINS, interactive=False)

    session = obd_chat.new_session(veh, vin[-4:] if vin else None, provider or "claude")
    messages = [] if engine.name == "Claude" else \
        [{"role": "system", "content": obd_chat.system_prompt(veh)}]

    S.clear()
    S.update({
        "engine": engine, "reader": reader, "simulated": simulated,
        "provider": provider or "claude", "vehicle": veh, "vin": vin,
        "expected_vin4": vin[-4:] if vin else None,
        "system": obd_chat.system_prompt(veh),
        "session": session, "messages": messages,
        "media_dir": obd_chat.session_media_dir(session["id"]),
        "pending": [], "history": history,
        "interval": 0.12 if simulated else 0.3,
        "adapter": adapter_label(reader, simulated),
        "events": queue.Queue(), "turn": None, "result": {},
    })
    return status()


def adapter_label(reader, simulated):
    """Short human label for how we're connected (shown in the header)."""
    if simulated:
        return "simulated"
    port = getattr(reader, "port", "") or ""
    try:
        if obd_transport.parse_tcp(port):
            return "WiFi/TCP"
    except Exception:
        pass
    return "Bluetooth" if "rfcomm" in port else "USB"


def _persist():
    obd_chat.save_session(S["session"], S["messages"])


# --------------------------------------------------------------------------- #
# Read-only
# --------------------------------------------------------------------------- #
def status():
    return {
        "vehicle": S["vehicle"],
        "vin": S["vin"],
        "provider": S["engine"].name,
        "simulated": S["simulated"],
        "adapter": S.get("adapter", "unknown"),
        "images": obd_images.count_images(S["messages"]),
        "turns": obd_chat._turn_count(S["messages"]),
        "pending": [attachment_view(a) for a in S["pending"]],
        "busy": bool(S.get("turn") and S["turn"].is_alive()),
    }


def vehicle_get():
    return {"vehicle": S["vehicle"], "vin": S["vin"]}


# --------------------------------------------------------------------------- #
# Photos
# --------------------------------------------------------------------------- #
def thumb_data_uri(path, box=200):
    """Small base64 thumbnail for the composer preview."""
    if Image is None:
        with open(path, "rb") as f:
            return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
    with Image.open(path) as im:
        im.thumbnail((box, box))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def attachment_view(att):
    return {"name": os.path.basename(att.get("source") or att["path"]),
            "desc": obd_images.describe(att),
            "thumb": thumb_data_uri(att["path"])}


def upload(filename, data):
    """Queue a photo for the next message. `data` = raw bytes (no multipart anywhere)."""
    import shutil
    import tempfile
    scratch = tempfile.mkdtemp(prefix="obdai_up_")
    raw = os.path.join(scratch, filename or "upload.jpg")
    try:
        with open(raw, "wb") as f:
            f.write(data)
        att = obd_images.prepare(raw, S["media_dir"], label=filename or "photo")
    except obd_images.ImageError as e:
        return {"error": str(e)}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    S["pending"].append(att)
    return {"ok": True, "attachment": attachment_view(att), "pending": len(S["pending"])}


def pending_clear():
    n = len(S["pending"])
    S["pending"].clear()
    return {"cleared": n}


# --------------------------------------------------------------------------- #
# Vehicle correction
# --------------------------------------------------------------------------- #
def vehicle_set(vehicle=None, vin=None):
    new_vin = S["vin"]
    if vin:
        v = obd_vin.normalize(vin)
        if not obd_vin.is_wellformed(v):
            return {"error": obd_vin.validity_note(v)}
        new_vin = v
        if not vehicle:
            vehicle = obd_vehicle.suggest_from_vin(
                HERE, v, obd_diagnose.KNOWN_VEHICLE_VINS) or S["vehicle"]
    if vehicle:
        S["vehicle"] = vehicle.strip()
    S["vin"] = new_vin
    S["expected_vin4"] = new_vin[-4:] if new_vin else None
    S["system"] = obd_chat.system_prompt(S["vehicle"])
    msgs = S["messages"]
    if S["engine"].name != "Claude" and msgs and msgs[0].get("role") == "system":
        msgs[0]["content"] = S["system"]
    S["session"]["vehicle"] = S["vehicle"]
    S["session"]["vin_last4"] = S["expected_vin4"]
    _persist()
    return {"ok": True, "vehicle": S["vehicle"], "vin": S["vin"]}


# --------------------------------------------------------------------------- #
# Chat: start on a worker thread, poll for tool events then the reply
# --------------------------------------------------------------------------- #
def chat_start(text=""):
    """Begin one turn (typed text + any queued photos). Returns {'ok'} or {'error'}."""
    text = (text or "").strip()
    attachments = list(S["pending"])
    if not text and not attachments:
        return {"error": "empty message"}
    if S.get("turn") and S["turn"].is_alive():
        return {"error": "a turn is already running"}
    S["pending"].clear()
    S["messages"].append(obd_images.user_turn(text, attachments))

    S["events"] = queue.Queue()
    S["result"] = {}
    images_sent = len(attachments)

    def worker():
        try:
            reply = obd_chat.run_turn(
                S["engine"], S["reader"], S["system"], S["history"], S["interval"],
                S["messages"], S["expected_vin4"],
                on_tool=lambda n: S["events"].put({"type": "tool", "name": n}),
                vehicle=S["vehicle"])
            S["result"] = {"reply": reply}
        except obd_connect.ObdConnectionError as e:
            S["result"] = {"reply": f"⚠️ Lost the connection to the OBD adapter: {e}\n"
                                    "Check the cable / Bluetooth link and the ignition, "
                                    "then try again.", "error": "connection"}
        except Exception as e:
            S["result"] = {"reply": f"⚠️ {type(e).__name__}: {e}", "error": "error"}
        finally:
            S["result"]["images_sent"] = images_sent
            S["result"]["turns"] = obd_chat._turn_count(S["messages"])
            try:
                _persist()
            except Exception:
                pass
            S["events"].put({"type": "done"})

    S["turn"] = threading.Thread(target=worker, daemon=True)
    S["turn"].start()
    return {"ok": True}


def poll(timeout=0.0):
    """Drain pending turn events. Returns {'events': [...], 'done': bool, 'result': {...}}.

    Events are `{"type":"tool","name":...}` as each read fires; when the turn finishes a
    `done` event lands and `result` carries the reply.
    """
    events = []
    q = S.get("events")
    done = False
    if q is None:
        return {"events": [], "done": True, "result": S.get("result", {})}
    try:
        first = q.get(timeout=timeout) if timeout else q.get_nowait()
        events.append(first)
    except queue.Empty:
        first = None
    while True:
        try:
            events.append(q.get_nowait())
        except queue.Empty:
            break
    out = []
    for e in events:
        if e.get("type") == "done":
            done = True
        else:
            out.append(e)
    return {"events": out, "done": done,
            "result": S.get("result", {}) if done else {}}


def chat(text=""):
    """Synchronous convenience: run a turn and return the result (used by REST + tests)."""
    started = chat_start(text)
    if started.get("error"):
        return started
    tools = []
    while True:
        p = poll(timeout=0.25)
        tools += [e["name"] for e in p["events"] if e.get("type") == "tool"]
        if p["done"]:
            return {**p["result"], "tools": tools}


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def new_session():
    session = obd_chat.new_session(S["vehicle"], S["expected_vin4"], S["provider"])
    S["session"] = session
    S["media_dir"] = obd_chat.session_media_dir(session["id"])
    S["messages"] = [] if S["engine"].name == "Claude" else \
        [{"role": "system", "content": S["system"]}]
    S["pending"] = []
    S["result"] = {}
    return {"ok": True, "id": session["id"]}
