#!/bin/bash
# RGNRD-FM Health Monitor — lightweight stream health check, no Claude.
# Runs every 2 minutes via launchd. Restarts stream components if down.
# Always exits 0 so launchd doesn't back off.

RADIO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RADIO_DIR"

[[ -f .env ]] && { set -a; source .env; set +a; }
eval "$(uv run python mac/station_config.py --env)"

LOG="$HOME/Library/Logs/rgnrd/healthmon.log"
mkdir -p "$(dirname "$LOG")"
ts() { date '+%H:%M:%S'; }

restart_stream() {
    echo "[healthmon $(ts)] restarting stream..." >> "$LOG"
    pkill -f "ezstream.*${RGNRD_RUNTIME_DIR}/radio.xml" 2>/dev/null || true
    tmux send-keys -t "rgnrd:${RGNRD_STATION_ID}-stream" \
        "RGNRD_STATION_ID=${RGNRD_STATION_ID} uv run python mac/feeder.py --start-ezstream" Enter 2>/dev/null || \
        echo "[healthmon $(ts)] WARNING: tmux session not found, stream not restarted" >> "$LOG"
}

ISSUES=()

# Check feeder
if ! pgrep -f "feeder.py" > /dev/null 2>&1; then
    ISSUES+=("feeder DOWN")
    tmux send-keys -t "rgnrd:${RGNRD_STATION_ID}-stream" \
        "RGNRD_STATION_ID=${RGNRD_STATION_ID} uv run python mac/feeder.py" Enter 2>/dev/null || true
fi

# Check ezstream
if ! pgrep -f "ezstream.*radio.xml" > /dev/null 2>&1; then
    ISSUES+=("ezstream DOWN")
    restart_stream
else
    # Check Icecast source
    ICECAST_OK=$(curl -sf --max-time 2 "$ICECAST_STATUS_URL" 2>/dev/null | \
        python3 -c "
import os,sys,json,urllib.parse
try:
    mount=os.environ.get('RGNRD_ICECAST_MOUNT','')
    src=json.load(sys.stdin).get('icestats',{}).get('source',{})
    sources=src if isinstance(src,list) else [src] if src else []
    ok=any(urllib.parse.urlparse(str(s.get('listenurl',''))).path==mount or s.get('mount')==mount for s in sources)
    print('ok' if ok else 'no_source')
except: print('error')
" 2>/dev/null || echo "unreachable")
    if [[ "$ICECAST_OK" != "ok" ]]; then
        ISSUES+=("icecast: $ICECAST_OK")
        restart_stream
    fi
fi

if [[ ${#ISSUES[@]} -eq 0 ]]; then
    echo "[healthmon $(ts)] ok" >> "$LOG"
else
    echo "[healthmon $(ts)] ISSUES: ${ISSUES[*]}" >> "$LOG"
fi

exit 0
