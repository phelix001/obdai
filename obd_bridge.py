#!/usr/bin/env python3
"""Chaquopy entry point — the Android app's door into the Python core.

There is no HTTP server on device: FastAPI needs pydantic v2 -> pydantic-core (Rust),
which Chaquopy can't package. Instead the WebView's JavaScript calls Kotlin
(`@JavascriptInterface`), and Kotlin calls one function here:

    Kotlin:   val out = py.getModule("obd_bridge").callAttr("call", action, payloadJson)
    JS:       AndroidBridge.call("status", "{}")   ->   '{"vehicle": ...}'

Everything crosses as JSON strings (the only type that survives JS -> Kotlin -> Python
cleanly). Photos cross as base64 in `upload`, which also means no multipart parsing
anywhere — handy, since Python 3.13 removed `cgi`.

All actions map onto obd_api, the same handlers the desktop FastAPI server uses, so the
two platforms can't drift.
"""

import base64
import json
import traceback

import obd_api

# action -> handler(payload dict) -> dict
_ACTIONS = {
    "boot":          lambda p: obd_api.boot(**p),
    "status":        lambda p: obd_api.status(),
    "vehicle_get":   lambda p: obd_api.vehicle_get(),
    "vehicle_set":   lambda p: obd_api.vehicle_set(p.get("vehicle"), p.get("vin")),
    "pending_clear": lambda p: obd_api.pending_clear(),
    "new_session":   lambda p: obd_api.new_session(),
    "chat_start":    lambda p: obd_api.chat_start(p.get("text", "")),
    "poll":          lambda p: obd_api.poll(),
    "chat":          lambda p: obd_api.chat(p.get("text", "")),
    "upload":        lambda p: obd_api.upload(
                         p.get("filename", "photo.jpg"),
                         base64.b64decode(p.get("data_b64", ""))),
}


def call(action, payload_json="{}"):
    """Dispatch one action. Always returns a JSON string — never raises into Kotlin."""
    try:
        payload = json.loads(payload_json or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
    except Exception as e:
        return json.dumps({"error": f"bad payload: {e}"})

    handler = _ACTIONS.get(action)
    if handler is None:
        return json.dumps({"error": f"unknown action: {action}",
                           "actions": sorted(_ACTIONS)})
    try:
        return json.dumps(handler(payload))
    except Exception as e:
        # A crash here would kill the WebView's call; hand back a readable error.
        return json.dumps({"error": f"{type(e).__name__}: {e}",
                           "trace": traceback.format_exc()[-800:]})


def actions():
    """Names the Kotlin side can dispatch (handy for a smoke test)."""
    return sorted(_ACTIONS)


def ui_html():
    """The chat page, so Kotlin can load it into the WebView without a server."""
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "index.html")
    with open(path, encoding="utf-8") as f:
        return f.read()
