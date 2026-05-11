#!/usr/bin/env bash
# Relay the configured station's local Icecast stream to YouTube RTMP.
#
# Required:
#   YOUTUBE_STREAM_KEY=... bash mac/youtube_relay.sh
# or:
#   YOUTUBE_RTMP_URL=rtmp://x.rtmp.youtube.com/live2/... bash mac/youtube_relay.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

eval "$(uv run python mac/station_config.py --env)"

YOUTUBE_RTMP_BASE="${YOUTUBE_RTMP_BASE:-rtmp://x.rtmp.youtube.com/live2}"
YOUTUBE_AUDIO_URL="${YOUTUBE_AUDIO_URL:-http://${WRIT_ICECAST_HOST}:${WRIT_ICECAST_PORT}${WRIT_ICECAST_MOUNT}}"
YOUTUBE_AUDIO_HEALTH_URL="${YOUTUBE_AUDIO_HEALTH_URL:-${ICECAST_STATUS_URL}}"
YOUTUBE_VIDEO_SOURCE="${YOUTUBE_VIDEO_SOURCE:-color=c=0x101318:s=1280x720:r=30}"
YOUTUBE_BACKGROUND_IMAGE="${YOUTUBE_BACKGROUND_IMAGE:-}"
YOUTUBE_VIDEO_FILTER="${YOUTUBE_VIDEO_FILTER:-scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,format=yuv420p}"
YOUTUBE_VIDEO_BITRATE="${YOUTUBE_VIDEO_BITRATE:-2500k}"
YOUTUBE_VIDEO_BUFSIZE="${YOUTUBE_VIDEO_BUFSIZE:-5000k}"
YOUTUBE_X264_PARAMS="${YOUTUBE_X264_PARAMS:-nal-hrd=cbr:force-cfr=1}"
YOUTUBE_AUDIO_BITRATE="${YOUTUBE_AUDIO_BITRATE:-160k}"
YOUTUBE_SAMPLE_RATE="${YOUTUBE_SAMPLE_RATE:-44100}"
YOUTUBE_FPS="${YOUTUBE_FPS:-30}"
YOUTUBE_GOP="${YOUTUBE_GOP:-60}"

if [[ -z "${YOUTUBE_RTMP_URL:-}" ]]; then
    if [[ -z "${YOUTUBE_STREAM_KEY:-}" ]]; then
        echo "YOUTUBE_STREAM_KEY or YOUTUBE_RTMP_URL is required" >&2
        exit 2
    fi
    YOUTUBE_RTMP_URL="${YOUTUBE_RTMP_BASE%/}/${YOUTUBE_STREAM_KEY}"
fi

if ! curl -sf --max-time 5 "$YOUTUBE_AUDIO_HEALTH_URL" >/dev/null; then
    echo "Local Icecast health endpoint is not reachable: $YOUTUBE_AUDIO_HEALTH_URL" >&2
    exit 1
fi

if ! ffprobe -hide_banner -loglevel error "$YOUTUBE_AUDIO_URL" >/dev/null; then
    echo "Local station audio is not reachable: $YOUTUBE_AUDIO_URL" >&2
    exit 1
fi

video_input=(-f lavfi -re -i "$YOUTUBE_VIDEO_SOURCE")
if [[ -n "$YOUTUBE_BACKGROUND_IMAGE" ]]; then
    if [[ ! -f "$YOUTUBE_BACKGROUND_IMAGE" ]]; then
        echo "YOUTUBE_BACKGROUND_IMAGE does not exist: $YOUTUBE_BACKGROUND_IMAGE" >&2
        exit 1
    fi
    video_input=(-loop 1 -framerate "$YOUTUBE_FPS" -re -i "$YOUTUBE_BACKGROUND_IMAGE")
fi

echo "Relaying ${WRIT_CALL_SIGN} ${WRIT_ICECAST_MOUNT} -> YouTube RTMP"
echo "Audio: $YOUTUBE_AUDIO_URL"
if [[ -n "$YOUTUBE_BACKGROUND_IMAGE" ]]; then
    echo "Video: $YOUTUBE_BACKGROUND_IMAGE at ${YOUTUBE_FPS}fps, ${YOUTUBE_VIDEO_BITRATE}; audio ${YOUTUBE_AUDIO_BITRATE}"
else
    echo "Video: generated ${YOUTUBE_FPS}fps slate, ${YOUTUBE_VIDEO_BITRATE}; audio ${YOUTUBE_AUDIO_BITRATE}"
fi

exec ffmpeg -hide_banner -loglevel info -nostdin -stats_period 15 \
    "${video_input[@]}" \
    -thread_queue_size 4096 \
    -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 \
    -re -i "$YOUTUBE_AUDIO_URL" \
    -map 0:v:0 -map 1:a:0 \
    -c:v libx264 -preset veryfast -tune stillimage \
    -vf "$YOUTUBE_VIDEO_FILTER" -pix_fmt yuv420p -r "$YOUTUBE_FPS" -g "$YOUTUBE_GOP" \
    -b:v "$YOUTUBE_VIDEO_BITRATE" \
    -minrate "$YOUTUBE_VIDEO_BITRATE" -maxrate "$YOUTUBE_VIDEO_BITRATE" \
    -bufsize "$YOUTUBE_VIDEO_BUFSIZE" -x264-params "$YOUTUBE_X264_PARAMS" \
    -c:a aac -b:a "$YOUTUBE_AUDIO_BITRATE" -ar "$YOUTUBE_SAMPLE_RATE" -ac 2 \
    -f flv "$YOUTUBE_RTMP_URL"
