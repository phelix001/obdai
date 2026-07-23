#!/usr/bin/env bash
# Launcher for the interactive OBD2 chat assistant.
#   ./2run.sh                   # auto-detects the adapter (USB or Bluetooth)
#   ./2run.sh --simulate        # no hardware, demo data
#   ./2run.sh --provider openai
#   ./2run.sh --port /dev/rfcomm0    # skip detection, use this port
#   ./2run.sh --vehicle "2015 Honda Accord 2.4"
#
# In-chat photo commands: /pic  /photos  /snap  /phone  /help
#   (a bare /pic takes the newest image from ~/Dropbox/Camera Uploads, ~/Pictures,
#    ~/Downloads or ./photos — override with OBD_PHOTO_DIRS=/some/dir:/another)
#
# Adapter not found?  venv/bin/python obd_connect.py
# Photo sources not working?  venv/bin/python obd_images.py
cd "$(dirname "$0")" || exit 1
exec venv/bin/python obd_chat.py "$@"
