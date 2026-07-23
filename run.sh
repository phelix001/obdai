#!/usr/bin/env bash
# Launcher for the adaptive OBD2 diagnostic assistant.
#   ./run.sh                 # auto-detects the adapter (USB or Bluetooth)
#   ./run.sh --simulate      # no hardware, demo data
#   ./run.sh --vehicle "2015 Honda Accord 2.4"
#   ./run.sh --port /dev/rfcomm0     # skip detection, use this port
#
# Adapter not found? Diagnose it with:  venv/bin/python obd_connect.py
cd "$(dirname "$0")" || exit 1
exec venv/bin/python obd_diagnose.py "$@"
