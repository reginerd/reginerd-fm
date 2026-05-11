#!/bin/bash
# WRIT-FM Listener Response Daemon — turns listener messages into on-air segments
# Polls for unread messages every 30 seconds. When found, generates a short
# spoken response and drops it into the talk segment queue.
#
# Typical turnaround: ~2-3 minutes from message to audio in the queue.
# The streamer picks up new segments between its current playback items.

RADIO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RADIO_DIR"
eval "$(uv run python mac/station_config.py --env)"
MESSAGES_FILE="${WRIT_MESSAGES_FILE:-$HOME/.writ/messages.json}"
POLL_INTERVAL=30  # seconds between checks

# Allow Claude CLI to run inside tmux (may be blocked by parent Claude Code session)
unset CLAUDECODE

ts() { date +%H:%M; }

echo "[listener-daemon $(ts)] Starting ${WRIT_CALL_SIGN:-WRIT-FM}. Polling every ${POLL_INTERVAL}s"

while true; do
    # Quick check: any unread messages?
    if [ -f "$MESSAGES_FILE" ]; then
        UNREAD=$(cd "$RADIO_DIR" && uv run python -c '
import json
import sys
from pathlib import Path

try:
    msgs = json.loads(Path(sys.argv[1]).read_text())
    unread = sum(
        1
        for m in msgs
        if isinstance(m, dict)
        and not m.get("read", False)
        and len(str(m.get("message") or "").strip()) >= 2
    )
except Exception:
    unread = 0

print(unread)
' "$MESSAGES_FILE" 2>/dev/null)
    else
        UNREAD=0
    fi

    if [ "$UNREAD" -gt 0 ] 2>/dev/null; then
        echo "[listener-daemon $(ts)] $UNREAD unread message(s) — generating response..."
        uv run python mac/content_generator/listener_response_generator.py
        echo "[listener-daemon $(ts)] Done."
    fi

    sleep $POLL_INTERVAL
done
