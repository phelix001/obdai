#!/usr/bin/env python3
"""OBDAI CarChat — mobile web UI.

A small local web server that puts CarChat behind a phone-friendly chat page:
tap to send, tap the 📎 to attach a photo straight from the camera or gallery —
no slash commands. It reuses the exact same engine as the terminal app
(obd_chat.run_turn, the obd_* core), so the AI has all the same tools (read
sensors / DTCs / monitors / VIN / manufacturer data / live capture) and the same
photo pipeline.

Designed to run in Termux on Android:

    pkg install python
    pip install -r requirements.txt
    python webui/server.py --simulate         # try it with no hardware
    python webui/server.py                     # real ELM327 (USB-OTG / Bluetooth)
    # then open http://localhost:8000 in the phone's browser

Single active session per process (this is a personal, on-the-phone tool, not a
multi-tenant service). Keys come from .env exactly like the CLI — nothing is
hard-coded and nothing is sent anywhere except your chosen AI provider.
"""

import argparse
import base64
import io
import os
import sys
import tempfile

# Import the OBDAI core from the repo root regardless of where we're launched.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

import obd_chat
import obd_connect
import obd_diagnose
import obd_images
import obd_vehicle

try:
    from PIL import Image
except ImportError:
    Image = None

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="OBDAI CarChat")

# The one live session. Populated by boot().
S = {}


# --------------------------------------------------------------------------- #
# Boot: open the reader, identify the car, build the engine
# --------------------------------------------------------------------------- #
def boot(args):
    load_dotenv()
    provider = args.provider or "claude"
    engine = obd_diagnose.build_engine(provider)

    # open_reader reads .port/.baud/.simulate/.sim_car — give it its own namespace
    # so the adapter port never collides with the web server's --port.
    reader_args = argparse.Namespace(
        port=getattr(args, "port_dev", None), baud=args.baud,
        simulate=args.simulate, sim_car=getattr(args, "sim_car", "audi"))
    reader, simulated = obd_diagnose.open_reader(reader_args)
    if reader is None:
        raise obd_connect.ObdConnectionError(
            "No OBD adapter and no --simulate. Plug in an ELM327 (USB-OTG or paired "
            "Bluetooth) and restart, or run with --simulate for a demo.")

    script_dir = os.path.dirname(HERE)
    # Non-interactive identify: the header shows the car and the user can correct
    # it from the UI (the assistant also has read_vin to re-check).
    vehicle, vin = obd_vehicle.resolve(
        reader, script_dir, default=obd_diagnose.DEFAULT_VEHICLE, explicit=args.vehicle,
        known=obd_diagnose.KNOWN_VEHICLE_VINS, interactive=False)

    session = obd_chat.new_session(vehicle, vin[-4:] if vin else None, provider)
    messages = [] if engine.name == "Claude" else \
        [{"role": "system", "content": obd_chat.system_prompt(vehicle)}]

    S.update({
        "engine": engine, "reader": reader, "simulated": simulated,
        "provider": provider, "vehicle": vehicle, "vin": vin,
        "expected_vin4": vin[-4:] if vin else None,
        "system": obd_chat.system_prompt(vehicle),
        "session": session, "messages": messages,
        "media_dir": obd_chat.session_media_dir(session["id"]),
        "pending": [], "history": args.history,
        "interval": 0.12 if simulated else 0.3,
    })


def _persist():
    obd_chat.save_session(S["session"], S["messages"])


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/status")
def status():
    return {
        "vehicle": S["vehicle"],
        "vin": S["vin"],
        "provider": S["engine"].name,
        "simulated": S["simulated"],
        "images": obd_images.count_images(S["messages"]),
        "turns": obd_chat._turn_count(S["messages"]),
        "pending": [_att_view(a) for a in S["pending"]],
    }


def _thumb_data_uri(path, box=200):
    """Small base64 thumbnail for the composer preview."""
    if Image is None:
        with open(path, "rb") as f:
            return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
    with Image.open(path) as im:
        im.thumbnail((box, box))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _att_view(att):
    return {"name": os.path.basename(att.get("source") or att["path"]),
            "desc": obd_images.describe(att),
            "thumb": _thumb_data_uri(att["path"])}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """Receive a photo from the camera/gallery, downscale it, queue it for the
    next message. Returns a thumbnail so the composer can show it."""
    scratch = tempfile.mkdtemp(prefix="obdai_up_")
    raw = os.path.join(scratch, file.filename or "upload.jpg")
    try:
        with open(raw, "wb") as f:
            f.write(await file.read())
        att = obd_images.prepare(raw, S["media_dir"], label=file.filename or "photo")
    except obd_images.ImageError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)
    S["pending"].append(att)
    return {"ok": True, "attachment": _att_view(att),
            "pending": len(S["pending"])}


@app.post("/api/pending/clear")
def clear_pending():
    n = len(S["pending"])
    S["pending"].clear()
    return {"cleared": n}


@app.post("/api/chat")
def chat(text: str = Form("")):
    """Run one turn: the typed text plus any queued photos. Returns the reply and
    the names of any tools the assistant used (so the UI can show 'reading …')."""
    text = (text or "").strip()
    attachments = list(S["pending"])
    if not text and not attachments:
        return JSONResponse({"error": "empty message"}, status_code=400)
    S["pending"].clear()

    S["messages"].append(obd_images.user_turn(text, attachments))
    tools = []
    try:
        reply = obd_chat.run_turn(
            S["engine"], S["reader"], S["system"], S["history"], S["interval"],
            S["messages"], S["expected_vin4"], on_tool=tools.append)
    except obd_connect.ObdConnectionError as e:
        reply = (f"⚠️ Lost the connection to the OBD adapter: {e}\n"
                 "Check the cable / Bluetooth link and the ignition, then try again.")
    _persist()
    return {"reply": reply, "tools": tools,
            "images_sent": len(attachments),
            "turns": obd_chat._turn_count(S["messages"])}


@app.post("/api/new")
def new_session():
    """Start a fresh session on the same car (keeps the reader open)."""
    prov = S["provider"]
    session = obd_chat.new_session(S["vehicle"], S["expected_vin4"], prov)
    S["session"] = session
    S["media_dir"] = obd_chat.session_media_dir(session["id"])
    S["messages"] = [] if S["engine"].name == "Claude" else \
        [{"role": "system", "content": S["system"]}]
    S["pending"] = []
    return {"ok": True, "id": session["id"]}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_arg_parser():
    ap = argparse.ArgumentParser(description="OBDAI CarChat — mobile web UI")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--provider", choices=["claude", "openai"], default=None)
    ap.add_argument("--vehicle", default=None, help="override the vehicle label")
    ap.add_argument("--simulate", action="store_true", help="no hardware — demo data")
    ap.add_argument("--sim-car", choices=sorted(obd_diagnose.SIM_CARS), default="audi")
    ap.add_argument("--serial-port", dest="port_dev", default=None,
                    help="force an adapter port: /dev/rfcomm0 or tcp:192.168.0.10:35000")
    ap.add_argument("--baud", type=int, default=None)
    ap.add_argument("--history", type=int, default=48)
    return ap


def main():
    args = build_arg_parser().parse_args()
    boot(args)
    import uvicorn
    where = "SIMULATE — " + S["vehicle"] if S["simulated"] else S["vehicle"]
    print(f"\nOBDAI CarChat — {where} [{S['engine'].name}]")
    print(f"Open  http://localhost:{args.port}  on this device"
          f"  (or http://<phone-ip>:{args.port} from another device on your network).\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except obd_connect.ObdConnectionError as e:
        print(f"\nOBD adapter problem:\n{e}")
        sys.exit(1)
