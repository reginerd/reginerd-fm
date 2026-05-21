#!/usr/bin/env python3
"""
RGNRD-FM Now Playing API

HTTP server that exposes current track info, schedule, history, and more.
Runs as a daemon thread inside the streamer process.
"""

import http.server
import json
import os
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from station_config import load_station_config

# Import play history
try:
    from play_history import get_history
    HISTORY_ENABLED = True
except ImportError:
    HISTORY_ENABLED = False

# Import Discogs lookup and QR generation
try:
    from discogs_lookup import HAS_CREDENTIALS as DISCOGS_HAS_CREDS, search_discogs
    DISCOGS_ENABLED = True
except ImportError:
    DISCOGS_ENABLED = False
    DISCOGS_HAS_CREDS = False

try:
    from qr_generator import generate_qr_png, generate_qr_data_url, HAS_QRCODE
    QR_ENABLED = HAS_QRCODE
except ImportError:
    QR_ENABLED = False

# Discogs lookup cache to avoid repeated lookups for the same track
_DISCOGS_CACHE_MAX = 500
_discogs_cache: dict[str, dict | None] = {}
_discogs_last_track: str | None = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATION = load_station_config()
MESSAGES_FILE = STATION.messages_file
LEDGER_PATH = STATION.ledger_path

# Rate limiting for messages
MESSAGE_COOLDOWN = 300  # 5 minutes between messages per IP
last_message_times: dict[str, float] = {}
_messages_lock = threading.Lock()

PORT = int(os.environ.get("RGNRD_NOW_PLAYING_PORT", str(STATION.stream.api_port)))
ICECAST_STATUS_URL = os.environ.get(
    "ICECAST_STATUS_URL",
    STATION.stream.status_url,
)
STATION_PROXY_ENDPOINTS = {
    "now-playing",
    "health",
    "stats",
    "schedule",
    "history",
    "messages",
    "message",
    "diary",
    "discogs",
    "qr",
}

# Shared state — set by start_api_thread()
_track_info: dict = {}
_encoder_getter = None
_listener_fn = None
_api_mode = "stream"

# Server start time for uptime tracking
SERVER_START_TIME = time.time()
TRACKS_PLAYED = 0
TOTAL_LISTENERS_SERVED = 0
LAST_TRACK = None


class NowPlayingHandler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, data, cache_control=None):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        try:
            self.wfile.write(json.dumps(data).encode())
        except BrokenPipeError:
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        station_route = parse_station_route(path)
        if station_route:
            station_id, station_path = station_route
            self._proxy_station_request(station_id, station_path, parsed.query)
        elif self._handle_local_get(path, parsed):
            return
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_local_get(self, path: str, parsed: urllib.parse.ParseResult) -> bool:
        if path in ("/now-playing", "/"):
            data = get_now_playing()
            track_stats_update(data)
            self._send_json(data, "no-cache, no-store, must-revalidate")
            return True
        elif path == "/health":
            self._send_json(get_health_status())
            return True
        elif path == "/stats":
            self._send_json(get_stats())
            return True
        elif path == "/schedule":
            self._send_json(get_schedule_info())
            return True
        elif path == "/history":
            self._send_json(get_play_history())
            return True
        elif path == "/messages":
            self._send_json(get_messages())
            return True
        elif path == "/diary":
            qs = urllib.parse.parse_qs(parsed.query)
            limit = None
            if qs.get("limit"):
                try:
                    limit = max(1, int(qs["limit"][0]))
                except ValueError:
                    limit = None
            self._send_json(get_diary(limit=limit), "public, max-age=60")
            return True
        elif path == "/discogs":
            self._send_json(get_discogs_info())
            return True
        elif path == "/qr":
            qr_bytes = get_qr_code()
            if qr_bytes:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=60")
                self.end_headers()
                try:
                    self.wfile.write(qr_bytes)
                except BrokenPipeError:
                    pass
            else:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(json.dumps({"error": "No Discogs info available"}).encode())
                except BrokenPipeError:
                    pass
            return True
        else:
            return False

    def _proxy_station_request(
        self,
        station_id: str,
        path: str,
        query: str = "",
        method: str = "GET",
        body: bytes | None = None,
        content_type: str | None = None,
    ):
        try:
            station = load_station_config(station_id)
        except KeyError:
            return self._send_error(404, f"Unknown station: {station_id}")

        if station.stream.api_port == PORT:
            parsed = urllib.parse.urlparse(f"{path}?{query}" if query else path)
            if method == "GET" and self._handle_local_get(path, parsed):
                return
            if method == "POST" and path == "/message":
                return self._handle_message_body(body or b"")
            return self._send_error(404, "Unknown endpoint")

        target = f"http://127.0.0.1:{station.stream.api_port}{path}"
        if query:
            target = f"{target}?{query}"

        headers = {}
        if content_type:
            headers["Content-Type"] = content_type

        try:
            request = urllib.request.Request(
                target,
                data=body if method == "POST" else None,
                headers=headers,
                method=method,
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Access-Control-Allow-Origin", "*")
                cache_control = response.headers.get("Cache-Control")
                if cache_control:
                    self.send_header("Cache-Control", cache_control)
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except BrokenPipeError:
                    pass
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass
        except Exception:
            self._send_error(502, f"Station API unavailable: {station_id}")

    def _send_error(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(json.dumps({"error": msg}).encode())
        except BrokenPipeError:
            pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        station_route = parse_station_route(path)
        if station_route:
            station_id, station_path = station_route
            if station_path != "/message":
                self.send_response(404)
                self.end_headers()
                return
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            return self._proxy_station_request(
                station_id,
                station_path,
                parsed.query,
                method="POST",
                body=body,
                content_type=self.headers.get("Content-Type"),
            )

        if path != "/message":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get('Content-Length', 0))
        self._handle_message_body(self.rfile.read(content_length))

    def _handle_message_body(self, body: bytes):
        client_ip = self.client_address[0]
        now = time.time()
        if client_ip in last_message_times and now - last_message_times[client_ip] < MESSAGE_COOLDOWN:
            wait_time = int(MESSAGE_COOLDOWN - (now - last_message_times[client_ip]))
            return self._send_error(429, f"Please wait {wait_time}s")

        try:
            data = json.loads(body.decode('utf-8'))
            message = data.get('message', '').strip()
            if not message or len(message) > 280:
                return self._send_error(400, "Invalid message")

            save_message(message, client_ip)
            last_message_times[client_ip] = now
            self._send_json({"status": "received"})
        except Exception:
            self._send_error(500, "Internal server error")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logging


def parse_station_route(path: str) -> tuple[str, str] | None:
    """Return (station_id, endpoint_path) for public station-prefixed API paths."""
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None

    if parts[0] == "stations":
        if len(parts) < 3:
            return None
        station_id = parts[1].lower()
        endpoint_parts = parts[2:]
    else:
        station_id = parts[0].lower()
        endpoint_parts = parts[1:]

    if endpoint_parts[0] not in STATION_PROXY_ENDPOINTS:
        return None

    return station_id, "/" + "/".join(endpoint_parts)


def check_process(name: str) -> bool:
    """Check if process is running."""
    try:
        return subprocess.run(["pgrep", "-f", name], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def check_url(url: str, timeout: int = 2) -> bool:
    """Check if URL responds."""
    try:
        return urllib.request.urlopen(url, timeout=timeout).status == 200
    except Exception:
        return False


def get_health_status() -> dict:
    """Get comprehensive health status of all components."""
    icecast_ok = check_url(ICECAST_STATUS_URL)
    encoder = _encoder_getter() if _encoder_getter else None
    streamer_attached = _api_mode == "stream"
    streamer_ok = encoder is not None and encoder.poll() is None
    tunnel_ok = check_process("cloudflared")
    healthy = icecast_ok and tunnel_ok and (streamer_ok or not streamer_attached)
    return {
        "status": "healthy" if healthy else "degraded",
        "mode": _api_mode,
        "station_id": STATION.id,
        "station": STATION.call_sign,
        "mount": STATION.stream.mount,
        "timestamp": datetime.now().isoformat(),
        "components": {
            "icecast": {"status": "up" if icecast_ok else "down"},
            "streamer": {
                "status": "up" if streamer_ok else ("not_attached" if not streamer_attached else "down")
            },
            "tunnel": {"status": "up" if tunnel_ok else "down"},
            "api": {"status": "up"},
        },
        "uptime_seconds": int(time.time() - SERVER_START_TIME),
    }


def get_stats() -> dict:
    """Get server statistics."""
    uptime = int(time.time() - SERVER_START_TIME)
    hours = uptime // 3600
    minutes = (uptime % 3600) // 60
    listeners = _listener_fn() if _listener_fn else 0

    return {
        "uptime": f"{hours}h {minutes}m",
        "uptime_seconds": uptime,
        "tracks_played": TRACKS_PLAYED,
        "total_listeners_served": TOTAL_LISTENERS_SERVED,
        "current_listeners": listeners,
        "api_started": datetime.fromtimestamp(SERVER_START_TIME).isoformat(),
    }


def track_stats_update(data: dict):
    """Update track statistics."""
    global TRACKS_PLAYED, TOTAL_LISTENERS_SERVED, LAST_TRACK

    current_track = data.get("track")
    if current_track and current_track != LAST_TRACK:
        TRACKS_PLAYED += 1
        LAST_TRACK = current_track
        # Only count listeners once per track change, not per API hit
        listeners = data.get("listeners", 0)
        if listeners > 0:
            TOTAL_LISTENERS_SERVED += listeners


def get_play_history() -> dict:
    """Get play history from database."""
    if not HISTORY_ENABLED:
        return {"enabled": False, "message": "History tracking not available"}

    try:
        history = get_history()
        return {
            "enabled": True,
            "recent": history.get_recent_plays(50),
            "stats": history.get_stats(),
            "most_played": history.get_most_played(10),
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)}


def save_message(message: str, ip: str):
    """Save a listener message to the queue."""
    MESSAGES_FILE.parent.mkdir(parents=True, exist_ok=True)

    with _messages_lock:
        # Load existing messages
        messages = []
        if MESSAGES_FILE.exists():
            try:
                with open(MESSAGES_FILE) as f:
                    messages = json.load(f)
            except Exception:
                messages = []

        # Add new message
        messages.append({
            "station_id": STATION.id,
            "message": message,
            "ip": ip,
            "timestamp": datetime.now().isoformat(),
            "read": False,
        })

        # Keep only last 100 messages
        messages = messages[-100:]

        # Save
        with open(MESSAGES_FILE, "w") as f:
            json.dump(messages, f, indent=2)


def get_diary(limit: int | None = None) -> dict:
    """Read the operator's diary entries from the station ledger."""
    generated_at = datetime.now().isoformat(timespec="seconds")
    if not LEDGER_PATH.exists():
        return {"generated_at": generated_at, "count": 0, "entries": []}

    entries: list[dict] = []
    try:
        for line in LEDGER_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "diary_entry":
                continue
            entries.append({
                "id": event.get("id"),
                "time": event.get("time"),
                "mode": event.get("mode"),
                "text": event.get("text", ""),
            })
    except OSError:
        return {"generated_at": generated_at, "count": 0, "entries": []}

    entries.sort(key=lambda e: e.get("time") or "", reverse=True)
    if limit is not None and limit > 0:
        entries = entries[:limit]

    return {"generated_at": generated_at, "count": len(entries), "entries": entries}


def get_messages(limit: int = 20) -> list[dict]:
    """Get recent messages."""
    if not MESSAGES_FILE.exists():
        return []

    try:
        with open(MESSAGES_FILE) as f:
            messages = json.load(f)
        # Return newest first, hide IP
        return [
            {"message": m["message"], "timestamp": m["timestamp"], "read": m.get("read", False)}
            for m in reversed(messages[-limit:])
        ]
    except Exception:
        return []


def get_now_playing() -> dict:
    """Read current track info from shared in-memory state."""
    data = dict(_track_info)
    data.setdefault("station_id", STATION.id)
    data.setdefault("station", STATION.call_sign)
    data.setdefault("mount", STATION.stream.mount)
    data["listeners"] = _listener_fn() if _listener_fn else 0
    return data


def get_schedule_info() -> dict:
    """Get current and upcoming show schedule."""
    try:
        from schedule import load_schedule
        schedule_path = STATION.schedule_path
        schedule = load_schedule(schedule_path)
        now = datetime.now()
        current = schedule.resolve(now)

        # Find exact upcoming airings. next_airings() includes the current one first.
        upcoming = []
        for future_show_id, starts_at in schedule.next_airings(now=now, count=5)[1:]:
            future_show = schedule.shows[future_show_id]
            upcoming.append({
                "show_id": future_show.show_id,
                "name": future_show.name,
                "host": future_show.host,
                "topic_focus": future_show.topic_focus,
                "starts_at": starts_at.isoformat(),
                "starts_around": starts_at.strftime("%H:%M"),
            })

        return {
            "current": {
                "show_id": current.show_id,
                "name": current.name,
                "description": current.description,
                "host": current.host,
                "topic_focus": current.topic_focus,
                "segment_types": current.segment_types,
                "bumper_style": current.bumper_style,
            },
            "upcoming": upcoming[:4],
            "timestamp": now.isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


def _qr_data_url_for(discogs_data: dict | None) -> str | None:
    if not QR_ENABLED or not discogs_data or not discogs_data.get("url"):
        return None
    return generate_qr_data_url(discogs_data["url"])


def _evict_discogs_cache() -> None:
    """Drop oldest half of the in-memory Discogs cache when it exceeds max size."""
    if len(_discogs_cache) >= _DISCOGS_CACHE_MAX:
        # dict preserves insertion order; drop the first half
        keys = list(_discogs_cache)
        for k in keys[: len(keys) // 2]:
            del _discogs_cache[k]


def get_discogs_info() -> dict:
    """Get Discogs info for the currently playing track.

    Returns a dict with Discogs release info, or an error/status message.
    For AI-generated bumpers, returns the generation metadata instead.
    Caches results to avoid repeated API calls for the same track.
    """
    global _discogs_cache, _discogs_last_track

    # Get current track
    now_playing = get_now_playing()
    track_name = now_playing.get("track")
    track_type = now_playing.get("type")

    # AI-generated bumper: return generation metadata instead of Discogs
    if track_type == "bumper" and now_playing.get("ai_generated"):
        return {
            "enabled": True,
            "ai_generated": True,
            "track": track_name,
            "caption": now_playing.get("caption"),
            "model": "ACE-Step (music-gen.server)",
            "show": now_playing.get("show"),
        }

    if not DISCOGS_ENABLED:
        return {"enabled": False, "message": "Discogs lookup not available"}

    if not DISCOGS_HAS_CREDS:
        return {
            "enabled": False,
            "message": "Discogs API requires authentication",
            "setup": "Set DISCOGS_TOKEN env var. Get token at https://www.discogs.com/settings/developers"
        }

    vibe = now_playing.get("vibe")

    # Only look up music tracks, not segments or podcasts
    if not track_name or track_type != "music":
        return {"enabled": True, "track": track_name, "discogs": None, "reason": "Not a music track"}

    # Check cache
    if track_name in _discogs_cache:
        cached = _discogs_cache[track_name]
        if cached is None:
            return {"enabled": True, "track": track_name, "discogs": None, "reason": "Not found on Discogs"}
        return {
            "enabled": True,
            "track": track_name,
            "discogs": cached,
            "qr_data_url": _qr_data_url_for(cached),
        }

    # Perform lookup (only if track changed)
    if track_name != _discogs_last_track:
        _discogs_last_track = track_name
        _evict_discogs_cache()
        result = search_discogs(track_name, vibe)

        if result:
            discogs_data = {
                "release_id": result.release_id,
                "title": result.title,
                "artist": result.artist,
                "year": result.year,
                "url": result.url,
                "thumb_url": result.thumb_url,
                "label": result.label,
                "format": result.format,
            }
            _discogs_cache[track_name] = discogs_data
            return {
                "enabled": True,
                "track": track_name,
                "discogs": discogs_data,
                "qr_data_url": _qr_data_url_for(discogs_data),
            }
        else:
            _discogs_cache[track_name] = None
            return {"enabled": True, "track": track_name, "discogs": None, "reason": "Not found on Discogs"}

    # Track hasn't changed, return cached or pending
    if track_name in _discogs_cache:
        cached = _discogs_cache[track_name]
        if cached is None:
            return {"enabled": True, "track": track_name, "discogs": None, "reason": "Not found on Discogs"}
        return {
            "enabled": True,
            "track": track_name,
            "discogs": cached,
            "qr_data_url": _qr_data_url_for(cached),
        }

    return {"enabled": True, "track": track_name, "discogs": None, "reason": "Lookup pending"}


def get_qr_code() -> bytes | None:
    """Get QR code PNG for the current track's Discogs page."""
    if not QR_ENABLED:
        return None
    discogs_data = get_discogs_info().get("discogs")
    if not discogs_data or not discogs_data.get("url"):
        return None
    return generate_qr_png(discogs_data["url"])


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def _set_api_state(track_info: dict, encoder_getter, listener_fn, mode: str) -> None:
    global _track_info, _encoder_getter, _listener_fn, _api_mode, SERVER_START_TIME
    _track_info = track_info
    _encoder_getter = encoder_getter
    _listener_fn = listener_fn
    _api_mode = mode
    SERVER_START_TIME = time.time()


def serve_api_forever(
    track_info: dict | None = None,
    encoder_getter=None,
    listener_fn=None,
) -> None:
    """Run the API as a standalone process.

    This keeps public station-prefixed routes such as
    /rgnrd-fm/now-playing alive even when the stream is stopped.
    """
    _set_api_state(
        track_info or {},
        encoder_getter or (lambda: None),
        listener_fn or (lambda: 0),
        mode="standalone",
    )
    with ReusableTCPServer(("", PORT), NowPlayingHandler) as httpd:
        print(f"Now Playing API listening on port {PORT} for {STATION.call_sign}", flush=True)
        httpd.serve_forever()


def start_api_thread(track_info: dict, encoder_getter, listener_fn) -> threading.Thread:
    """Start the HTTP API server in a daemon thread.

    Args:
        track_info: Mutable dict shared with the streamer (mutated in-place).
        encoder_getter: Callable returning the current encoder subprocess.
        listener_fn: Callable returning the current listener count.
    """
    _set_api_state(track_info, encoder_getter, listener_fn, mode="stream")

    def _serve():
        try:
            with ReusableTCPServer(("", PORT), NowPlayingHandler) as httpd:
                httpd.serve_forever()
        except OSError as e:
            print(f"API server failed to start: {e}", flush=True)

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return t


def main() -> int:
    serve_api_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
