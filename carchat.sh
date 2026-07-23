#!/usr/bin/env bash
# OBDAI CarChat — interactive chat assistant (alias of 2run.sh)
cd "$(dirname "$0")" || exit 1
exec ./2run.sh "$@"
