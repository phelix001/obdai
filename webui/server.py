#!/usr/bin/env python3
"""OBDAI chat — desktop web server (FastAPI).

This is the *desktop* front door. It is a thin shell: every route delegates to
`obd_api`, which holds the session and all the logic. The Android app uses a
different front door (`obd_bridge.py`, called from Kotlin via Chaquopy) onto the
same handlers — FastAPI can't run on device because pydantic-core (Rust) has no
Chaquopy build, so keeping the logic out of here is what lets both platforms share it.

    python webui/server.py --simulate      # try it with no hardware
    python webui/server.py                 # real ELM327 (USB / Bluetooth / WiFi)
    # then open http://localhost:8000

Keys come from .env exactly like the CLI — nothing is hard-coded and nothing is sent
anywhere except your chosen AI provider.
"""

import argparse
import json
import os
import sys

# Import the OBDAI core from the repo root regardless of where we're launched.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

import obd_api
import obd_connect
import obd_diagnose

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="OBDAI")

# Kept as an alias so existing callers/tests that poke at server.S still work.
S = obd_api.S


def boot(args):
    """Open the adapter and identify the car (delegates to obd_api.boot)."""
    load_dotenv()
    return obd_api.boot(
        provider=args.provider, vehicle=args.vehicle,
        simulate=args.simulate, sim_car=getattr(args, "sim_car", "audi"),
        port=getattr(args, "port_dev", None), baud=args.baud,
        history=args.history)


def _maybe_error(result, code=400):
    """obd_api reports failures as {'error': ...}; surface those as HTTP errors."""
    if isinstance(result, dict) and result.get("error"):
        return JSONResponse(result, status_code=code)
    return result


# --------------------------------------------------------------------------- #
# Routes — all thin
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/status")
def status():
    return obd_api.status()


@app.get("/api/vehicle")
def get_vehicle():
    return obd_api.vehicle_get()


@app.post("/api/vehicle")
def set_vehicle(vehicle: str = Form(None), vin: str = Form(None)):
    return _maybe_error(obd_api.vehicle_set(vehicle, vin))


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    return _maybe_error(obd_api.upload(file.filename or "photo.jpg", data))


@app.post("/api/pending/clear")
def clear_pending():
    return obd_api.pending_clear()


@app.post("/api/chat")
def chat(text: str = Form("")):
    return _maybe_error(obd_api.chat(text))


@app.post("/api/chat/start")
def chat_start(text: str = Form("")):
    """Begin a turn on a worker thread; drive it with /api/chat/poll.

    The UI uses start+poll on BOTH platforms so the page has one code path — on
    Android the same two calls go through the Kotlin bridge instead of HTTP.
    """
    return _maybe_error(obd_api.chat_start(text))


@app.post("/api/chat/poll")
def chat_poll():
    return obd_api.poll()


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(text: str = Form("")):
    """Streaming variant for the desktop UI: a `tool` event as each read fires, then
    the final `reply`. Built on the same start/poll queue the Android bridge uses."""
    started = obd_api.chat_start(text)
    if started.get("error"):
        return JSONResponse(started, status_code=400)

    def gen():
        while True:
            p = obd_api.poll(timeout=0.25)
            for e in p["events"]:
                if e.get("type") == "tool":
                    yield _sse("tool", e["name"])
            if p["done"]:
                yield _sse("reply", p["result"])
                return

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/new")
def new_session():
    return obd_api.new_session()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_arg_parser():
    ap = argparse.ArgumentParser(description="OBDAI chat — web UI")
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
    print(f"\nOBDAI — {where} [{S['engine'].name}]")
    print(f"Open  http://localhost:{args.port}  on this device"
          f"  (or http://<phone-ip>:{args.port} from another device on your network).\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except obd_connect.ObdConnectionError as e:
        print(f"\nOBD adapter problem:\n{e}")
        sys.exit(1)
