#!/bin/bash
# RGNRD-FM Operator Daemon — runs the Claude maintenance loop on an interval.
# This replaces the separate talk and bumper stocking daemons.

set -euo pipefail

RADIO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RADIO_DIR"

# Load secrets (.env not sourced by launchd)
[[ -f .env ]] && { set -a; source .env; set +a; }

eval "$(uv run python mac/station_config.py --env)"
INTERVAL_SECONDS="${RGNRD_OPERATOR_INTERVAL_SECONDS:-900}"

ts() { date +%H:%M; }

echo "[operator-daemon $(ts)] Starting ${RGNRD_CALL_SIGN:-REGINERD-FM}. Interval: ${INTERVAL_SECONDS}s"

while true; do
    echo "[operator-daemon $(ts)] Running ${RGNRD_CALL_SIGN:-REGINERD-FM} operator loop..."
    (
        ./run_operator.sh
    ) || echo "[operator-daemon $(ts)] operator pass exited with status $?; continuing"
    echo "[operator-daemon $(ts)] Sleeping ${INTERVAL_SECONDS}s..."
    sleep "$INTERVAL_SECONDS"
done
