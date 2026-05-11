#!/usr/bin/env bash
# Relay WRIT-FM's local Icecast stream to Caster.fm.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

CASTER_AUDIO_URL="${CASTER_AUDIO_URL:-http://localhost:8000/stream}"
CASTER_AUDIO_HEALTH_URL="${CASTER_AUDIO_HEALTH_URL:-http://localhost:8000/status-json.xsl}"
CASTER_HOST="${CASTER_HOST:-sapircast.caster.fm}"
CASTER_PORT="${CASTER_PORT:-12990}"
CASTER_MOUNT="${CASTER_MOUNT:-/wt6w8}"
CASTER_SOURCE_USER="${CASTER_SOURCE_USER:-source}"
CASTER_BITRATE="${CASTER_BITRATE:-96k}"
CASTER_CODEC="${CASTER_CODEC:-libopus}"
CASTER_SAMPLE_RATE="${CASTER_SAMPLE_RATE:-48000}"
CASTER_STREAM_NAME="${CASTER_STREAM_NAME:-WRIT-FM}"
CASTER_STREAM_GENRE="${CASTER_STREAM_GENRE:-AI Radio}"
CASTER_STREAM_DESCRIPTION="${CASTER_STREAM_DESCRIPTION:-The frequency between frequencies}"

if [[ -z "${CASTER_SOURCE_PASSWORD:-}" ]]; then
    echo "CASTER_SOURCE_PASSWORD is required" >&2
    echo "Example:" >&2
    echo "  CASTER_SOURCE_PASSWORD=... ./writ start caster" >&2
    exit 2
fi

if ! curl -sf --max-time 5 "$CASTER_AUDIO_HEALTH_URL" >/dev/null; then
    echo "Local audio stream is not reachable: $CASTER_AUDIO_URL" >&2
    echo "Start WRIT-FM first: ./writ start" >&2
    exit 1
fi

mount="${CASTER_MOUNT#/}"
target="icecast://${CASTER_SOURCE_USER}@${CASTER_HOST}:${CASTER_PORT}/${mount}"

if ! nc -z "$CASTER_HOST" "$CASTER_PORT" >/dev/null 2>&1; then
    if [[ -z "${CASTER_PRIVATE_TOKEN:-}" ]]; then
        echo "Caster server is offline: ${CASTER_HOST}:${CASTER_PORT}" >&2
        echo "Start it from the Caster.fm dashboard, or provide CASTER_PRIVATE_TOKEN." >&2
        exit 1
    fi
    echo "Caster server is offline; starting it via API..."
    curl -fsS -X POST -H "Accept: application/json" \
        "https://hub.cloud.caster.fm/private/server/start?token=${CASTER_PRIVATE_TOKEN}" >/dev/null
    for _ in {1..20}; do
        nc -z "$CASTER_HOST" "$CASTER_PORT" >/dev/null 2>&1 && break
        sleep 3
    done
    if ! nc -z "$CASTER_HOST" "$CASTER_PORT" >/dev/null 2>&1; then
        echo "Caster server did not open: ${CASTER_HOST}:${CASTER_PORT}" >&2
        exit 1
    fi
fi

echo "Relaying $CASTER_AUDIO_URL -> ${CASTER_HOST}:${CASTER_PORT}/${mount}"
echo "Codec: $CASTER_CODEC  Bitrate: $CASTER_BITRATE  Sample rate: $CASTER_SAMPLE_RATE"

cd "$PROJECT_ROOT"
exec ffmpeg -hide_banner -loglevel info \
    -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 \
    -re -i "$CASTER_AUDIO_URL" \
    -vn -c:a "$CASTER_CODEC" -b:a "$CASTER_BITRATE" -ar "$CASTER_SAMPLE_RATE" -ac 2 \
    -content_type application/ogg \
    -ice_name "$CASTER_STREAM_NAME" \
    -ice_genre "$CASTER_STREAM_GENRE" \
    -ice_description "$CASTER_STREAM_DESCRIPTION" \
    -password "$CASTER_SOURCE_PASSWORD" \
    -f ogg "$target"
