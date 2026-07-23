#!/usr/bin/env bash
# OBDAI — quick diagnosis. Runs a full diagnosis immediately, then lets you keep
# chatting with the AI mechanic. Same one app as ./2run.sh, just diagnosis-first.
#   ./run.sh                 # auto-detects the adapter (USB / Bluetooth / WiFi)
#   ./run.sh --simulate      # no hardware, demo data
#   ./run.sh --port tcp:192.168.0.10:35000   # WiFi adapter
#
# Adapter not found?  venv/bin/python obd_connect.py
# Offline: produce a diagnosis from a saved capture:
#   venv/bin/python obd_diagnose.py --diagnose-file pending_XXXX.json
cd "$(dirname "$0")" || exit 1
exec venv/bin/python obd_chat.py --diagnose "$@"
