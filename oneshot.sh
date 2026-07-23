#!/usr/bin/env bash
# OBDAI OneShot — guided one-shot diagnosis (alias of run.sh)
cd "$(dirname "$0")" || exit 1
exec ./run.sh "$@"
