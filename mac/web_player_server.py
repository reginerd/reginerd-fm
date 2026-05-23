#!/usr/bin/env python3
"""
RGNRD-FM Web Player Server

FastAPI server on port 8090.
- Serves React player static build
- Proxies Icecast stream (hides internal port)
- Serves Plex album art (hides token)
- Exposes /now-playing JSON
- Accepts /vote (thumbs up/down → votes.db)
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

from station_config import load_station_config  # noqa: E402

STATION = load_station_config()

# Paths
PLAYER_BUILD = PROJECT_ROOT / "output" / "player"
LIBRARY_PATH = PROJECT_ROOT / "output" / "runtime" / "music_library.json"
CURRENT_TRACK_FILE = STATION.current_track_file
NOW_PLAYING_FILE = STATION.now_playing_file
VOTES_DB = Path.home() / ".rgnrd" / "votes.db"

# Plex config
import yaml as _yaml
_mac_cfg = _yaml.safe_load((PROJECT_ROOT / "mac" / "config.yaml").read_text())
_plex_cfg = _mac_cfg.get("plex", {})
PLEX_HOST = f"http://{_plex_cfg.get('host', '192.168.1.27')}:{_plex_cfg.get('port', 32400)}"
PLEX_TOKEN = os.environ.get(_plex_cfg.get("token_env", "PLEX_TOKEN"), "")
PLEX_SECTION = int(_plex_cfg.get("library_section", 1))

ICECAST_STREAM = f"http://{STATION.stream.icecast_host}:{STATION.stream.icecast_port}{STATION.stream.mount}"

app = FastAPI(title="RGNRD-FM Player", docs_url=None, redoc_url=None)

# ---------------------------------------------------------------------------
# votes.db
# ---------------------------------------------------------------------------

def _init_votes_db() -> sqlite3.Connection:
    VOTES_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(VOTES_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY,
            filepath TEXT NOT NULL,
            artist TEXT,
            title TEXT,
            vote INTEGER NOT NULL,
            voted_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_filepath ON votes(filepath)")
    conn.commit()
    return conn


def _get_net_votes(filepath: str) -> int:
    conn = _init_votes_db()
    row = conn.execute(
        "SELECT COALESCE(SUM(vote), 0) FROM votes WHERE filepath = ?", (filepath,)
    ).fetchone()
    conn.close()
    return int(row[0]) if row else 0


def _record_vote(filepath: str, artist: str | None, title: str | None, vote: int) -> int:
    conn = _init_votes_db()
    conn.execute(
        "INSERT INTO votes (filepath, artist, title, vote, voted_at) VALUES (?, ?, ?, ?, ?)",
        (filepath, artist, title, vote, datetime.now().isoformat()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT COALESCE(SUM(vote), 0) FROM votes WHERE filepath = ?", (filepath,)
    ).fetchone()
    conn.close()
    return int(row[0]) if row else 0

# ---------------------------------------------------------------------------
# Library + current track helpers
# ---------------------------------------------------------------------------

_library_cache: list[dict] = []
_library_mtime: float = 0.0


def _load_library() -> list[dict]:
    global _library_cache, _library_mtime
    try:
        mtime = LIBRARY_PATH.stat().st_mtime
        if mtime != _library_mtime:
            _library_cache = json.loads(LIBRARY_PATH.read_text())
            _library_mtime = mtime
    except Exception:
        pass
    return _library_cache


def _track_for_path(filepath: str) -> dict | None:
    for t in _load_library():
        if t.get("nas_path") == filepath:
            return t
    return None


def _current_track_path() -> str:
    try:
        return CURRENT_TRACK_FILE.read_text().strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Plex album art
# ---------------------------------------------------------------------------

_art_cache: dict[str, bytes | None] = {}


def _plex_art_bytes(artist: str, album: str) -> bytes | None:
    cache_key = f"{artist}||{album}"
    if cache_key in _art_cache:
        return _art_cache[cache_key]

    if not PLEX_TOKEN:
        _art_cache[cache_key] = None
        return None

    try:
        # Search for album
        query = urllib.parse.quote(album)
        url = f"{PLEX_HOST}/library/sections/{PLEX_SECTION}/search?type=9&title={query}&X-Plex-Token={PLEX_TOKEN}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        albums = data.get("MediaContainer", {}).get("Metadata", [])
        # Find best match by album + artist
        thumb = None
        for a in albums:
            if artist.lower() in a.get("parentTitle", "").lower() or \
               album.lower() in a.get("title", "").lower():
                thumb = a.get("thumb")
                break
        if not thumb and albums:
            thumb = albums[0].get("thumb")

        if not thumb:
            _art_cache[cache_key] = None
            return None

        art_url = f"{PLEX_HOST}{thumb}?X-Plex-Token={PLEX_TOKEN}"
        with urllib.request.urlopen(art_url, timeout=5) as art_resp:
            data = art_resp.read()
            # Keep cache bounded
            if len(_art_cache) > 200:
                oldest = next(iter(_art_cache))
                del _art_cache[oldest]
            _art_cache[cache_key] = data
            return data
    except Exception:
        _art_cache[cache_key] = None
        return None

# ---------------------------------------------------------------------------
# Lyrics via LRClib
# ---------------------------------------------------------------------------

_lyrics_cache: dict[str, dict] = {}


def _fetch_lyrics(artist: str, title: str, album: str) -> dict:
    cache_key = f"{artist}||{title}"
    if cache_key in _lyrics_cache:
        return _lyrics_cache[cache_key]

    result: dict = {"synced": None, "plain": None, "instrumental": False, "source": None}
    try:
        params = urllib.parse.urlencode({
            "artist_name": artist,
            "track_name": title,
            "album_name": album,
        })
        req = urllib.request.Request(
            f"https://lrclib.net/api/get?{params}",
            headers={"User-Agent": "RGNRD-FM/1.0 (radio.reginerd.tv)"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        result["instrumental"] = bool(data.get("instrumental"))
        result["synced"] = data.get("syncedLyrics") or None
        result["plain"] = data.get("plainLyrics") or None
        if result["synced"] or result["plain"]:
            result["source"] = "lrclib"
    except Exception:
        pass

    if len(_lyrics_cache) > 300:
        oldest = next(iter(_lyrics_cache))
        del _lyrics_cache[oldest]
    _lyrics_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Play count helper
# ---------------------------------------------------------------------------

def _play_count(filepath: str) -> int:
    try:
        import sqlite3 as _sq
        history_db = Path.home() / ".rgnrd" / "history.db"
        if not history_db.exists():
            return 0
        conn = _sq.connect(str(history_db))
        row = conn.execute(
            "SELECT COUNT(*) FROM plays WHERE filepath = ?", (filepath,)
        ).fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/now-playing")
async def now_playing():
    path = _current_track_path()
    track = _track_for_path(path) if path else None

    # Fall back to now_playing.json written by feeder
    if not track and NOW_PLAYING_FILE.exists():
        try:
            np = json.loads(NOW_PLAYING_FILE.read_text())
            return JSONResponse({
                "artist": np.get("artist"),
                "title": np.get("track"),
                "album": None,
                "show": np.get("show"),
                "show_id": np.get("show_id"),
                "art_url": None,
                "filepath": path,
                "net_votes": _get_net_votes(path) if path else 0,
            })
        except Exception:
            pass

    artist = track.get("artist") if track else None
    title = track.get("title") if track else None
    album = track.get("album") if track else None
    year = track.get("year") if track else None
    tags = track.get("lastfm_tags") or [] if track else []
    plex_rating = track.get("plex_rating") if track else None

    art_url = None
    if artist and album:
        art_url = f"/art?artist={urllib.parse.quote(artist)}&album={urllib.parse.quote(album)}"

    # Show info from now_playing.json
    show = None
    show_id = None
    if NOW_PLAYING_FILE.exists():
        try:
            np = json.loads(NOW_PLAYING_FILE.read_text())
            show = np.get("show")
            show_id = np.get("show_id")
        except Exception:
            pass

    return JSONResponse({
        "artist": artist,
        "title": title,
        "album": album,
        "year": year,
        "tags": tags[:6],
        "plex_rating": plex_rating,
        "show": show,
        "show_id": show_id,
        "art_url": art_url,
        "filepath": path,
        "net_votes": _get_net_votes(path) if path else 0,
        "play_count": _play_count(path) if path else 0,
    })


@app.get("/art")
async def art(artist: str = "", album: str = ""):
    if not artist or not album:
        raise HTTPException(404, "artist and album required")
    data = _plex_art_bytes(artist, album)
    if not data:
        raise HTTPException(404, "art not found")
    return Response(content=data, media_type="image/jpeg")


@app.get("/lyrics")
async def lyrics(artist: str = "", title: str = "", album: str = ""):
    if not artist or not title:
        raise HTTPException(400, "artist and title required")
    result = _fetch_lyrics(artist, title, album)
    return JSONResponse(result)


@app.get("/stream")
async def stream_proxy(request: Request):
    """Proxy the Icecast stream."""
    async def _iter():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", ICECAST_STREAM) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=4096):
                    yield chunk

    return StreamingResponse(
        _iter(),
        media_type="audio/ogg",
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Access-Control-Allow-Origin": "*",
        },
    )


class VoteRequest(BaseModel):
    vote: int  # +1 or -1


@app.post("/vote")
async def vote(body: VoteRequest):
    if body.vote not in (1, -1):
        raise HTTPException(400, "vote must be 1 or -1")

    path = _current_track_path()
    if not path:
        raise HTTPException(409, "no track currently playing")

    track = _track_for_path(path)
    artist = track.get("artist") if track else None
    title = track.get("title") if track else None

    net = _record_vote(path, artist, title, body.vote)
    return JSONResponse({"filepath": path, "net_votes": net, "your_vote": body.vote})


@app.get("/votes")
async def get_votes(path: str = ""):
    if not path:
        raise HTTPException(400, "path required")
    return JSONResponse({"filepath": path, "net_votes": _get_net_votes(path)})


# Static files — served last so API routes take priority
if PLAYER_BUILD.exists():
    app.mount("/assets", StaticFiles(directory=str(PLAYER_BUILD / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        index = PLAYER_BUILD / "index.html"
        if index.exists():
            return FileResponse(str(index))
        raise HTTPException(404, "Player not built yet — run: cd player && npm run build")
else:
    @app.get("/")
    async def not_built():
        return JSONResponse({"error": "Player not built yet — run: cd player && npm run build"}, status_code=503)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("RGNRD_PLAYER_PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
