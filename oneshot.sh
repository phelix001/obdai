#!/usr/bin/env bash
# OBDAI quick diagnosis (alias of run.sh — diagnosis-first, then chat).
cd "$(dirname "$0")" || exit 1
exec ./run.sh "$@"
