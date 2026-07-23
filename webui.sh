#!/usr/bin/env bash
# OBDAI CarChat — mobile web UI. Open http://localhost:8000 after it starts.
#   ./webui.sh --simulate            # no hardware, demo data
#   ./webui.sh                       # real ELM327 (USB-OTG / Bluetooth)
#   ./webui.sh --sim-car honda --provider openai
cd "$(dirname "$0")" || exit 1
exec venv/bin/python webui/server.py "$@"
