#!/usr/bin/env python3
"""Last.fm listening context for REGINERD-FM segment personalization.

Syncs recent plays, top tracks/artists, and loved tracks from Last.fm.
Caches to output/runtime/lastfm_context.json with a 1-hour TTL.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_API_BASE = "https://ws.audioscrobbler.com/2.0/"
_CACHE_TTL = int(os.environ.get("LASTFM_CACHE_TTL", "3600"))
_CACHE_PATH = PROJECT_ROOT / "output" / "runtime" / "lastfm_context.json"
# Shared life-os cache written by the 1am lastfm-sync Hermes job (6h TTL).
_SHARED_CACHE = Path("~/life-os/data/lastfm_context.json").expanduser()
_SHARED_CACHE_TTL = 6 * 3600


def _api_call(method: str, api_key: str, username: str, **kwargs) -> dict | None:
    params = {
        "method": method,
        "user": username,
        "api_key": api_key,
        "format": "json",
        **kwargs,
    }
    url = _API_BASE + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _fetch_recent_tracks(api_key: str, username: str, limit: int = 50) -> list[dict]:
    data = _api_call("user.getRecentTracks", api_key, username, limit=limit, extended=0)
    if not data:
        return []
    tracks = data.get("recenttracks", {}).get("track", [])
    if not isinstance(tracks, list):
        tracks = [tracks]
    result = []
    for t in tracks:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        if t.get("@attr", {}).get("nowplaying"):
            continue
        artist = t.get("artist", {})
        album = t.get("album", {})
        result.append({
            "track": t.get("name", ""),
            "artist": artist.get("#text", "") if isinstance(artist, dict) else str(artist),
            "album": album.get("#text", "") if isinstance(album, dict) else str(album),
        })
    return result


def _fetch_top_tracks(api_key: str, username: str, period: str, limit: int = 10) -> list[dict]:
    data = _api_call("user.getTopTracks", api_key, username, period=period, limit=limit)
    if not data:
        return []
    tracks = data.get("toptracks", {}).get("track", [])
    if not isinstance(tracks, list):
        tracks = [tracks]
    result = []
    for t in tracks:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        artist = t.get("artist", {})
        result.append({
            "track": t.get("name", ""),
            "artist": artist.get("name", "") if isinstance(artist, dict) else str(artist),
            "playcount": t.get("playcount", ""),
        })
    return result


def _fetch_top_artists(api_key: str, username: str, period: str, limit: int = 10) -> list[dict]:
    data = _api_call("user.getTopArtists", api_key, username, period=period, limit=limit)
    if not data:
        return []
    artists = data.get("topartists", {}).get("artist", [])
    if not isinstance(artists, list):
        artists = [artists]
    result = []
    for a in artists:
        if not isinstance(a, dict) or not a.get("name"):
            continue
        result.append({
            "artist": a.get("name", ""),
            "playcount": a.get("playcount", ""),
        })
    return result


def _fetch_loved_tracks(api_key: str, username: str, limit: int = 20) -> list[dict]:
    data = _api_call("user.getLovedTracks", api_key, username, limit=limit)
    if not data:
        return []
    tracks = data.get("lovedtracks", {}).get("track", [])
    if not isinstance(tracks, list):
        tracks = [tracks]
    result = []
    for t in tracks:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        artist = t.get("artist", {})
        result.append({
            "track": t.get("name", ""),
            "artist": artist.get("name", "") if isinstance(artist, dict) else str(artist),
        })
    return result


def sync(api_key: str | None = None, username: str | None = None) -> dict | None:
    """Fetch from Last.fm and write cache. Returns the context dict."""
    api_key = api_key or os.environ.get("LASTFM_API_KEY", "").strip()
    username = username or os.environ.get("LASTFM_USERNAME", "").strip()
    if not api_key or not username:
        return None

    context = {
        "synced_at": datetime.now().isoformat(timespec="seconds"),
        "username": username,
        "recent_tracks": _fetch_recent_tracks(api_key, username, limit=50),
        "top_tracks_week": _fetch_top_tracks(api_key, username, period="7day", limit=10),
        "top_tracks_month": _fetch_top_tracks(api_key, username, period="1month", limit=10),
        "top_artists_week": _fetch_top_artists(api_key, username, period="7day", limit=8),
        "top_artists_month": _fetch_top_artists(api_key, username, period="1month", limit=8),
        "loved_tracks": _fetch_loved_tracks(api_key, username, limit=20),
    }

    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(context, indent=2, ensure_ascii=False) + "\n")
    return context


def load(force_sync: bool = False) -> dict | None:
    """Load context from cache, syncing if stale or missing.

    Primary source: ~/life-os/data/lastfm_context.json (written by life-os 1am Hermes job).
    Fallback: local output/runtime/lastfm_context.json, then live API sync.
    """
    if not force_sync:
        # Try shared life-os cache first
        if _SHARED_CACHE.exists():
            try:
                ctx = json.loads(_SHARED_CACHE.read_text())
                synced_at = ctx.get("synced_at", "")
                if synced_at:
                    age = time.time() - datetime.fromisoformat(synced_at).timestamp()
                    if age < _SHARED_CACHE_TTL:
                        return ctx
            except Exception:
                pass
        # Fall back to local cache
        if _CACHE_PATH.exists():
            try:
                ctx = json.loads(_CACHE_PATH.read_text())
                synced_at = ctx.get("synced_at", "")
                if synced_at:
                    age = time.time() - datetime.fromisoformat(synced_at).timestamp()
                    if age < _CACHE_TTL:
                        return ctx
            except Exception:
                pass
    return sync()


def format_for_prompt(context: dict | None = None) -> str:
    """Format Last.fm context as a prompt injection block."""
    ctx = context or load()
    if not ctx:
        return ""

    lines = [
        "YOUR RECENT LISTENING (weave in naturally — 'been on repeat', "
        "'can\\'t stop playing this one', 'been revisiting', 'just heard this yesterday'):"
    ]

    week_tracks = ctx.get("top_tracks_week", [])
    if week_tracks:
        names = [f"{t['track']} by {t['artist']}" for t in week_tracks[:5] if t.get("track")]
        if names:
            lines.append(f"On repeat this week: {', '.join(names)}")

    recent = ctx.get("recent_tracks", [])
    if recent:
        seen: set[str] = set()
        unique: list[str] = []
        for t in recent:
            key = f"{t.get('track','').lower()}|{t.get('artist','').lower()}"
            if key not in seen and t.get("track"):
                seen.add(key)
                unique.append(f"{t['track']} by {t['artist']}")
            if len(unique) >= 8:
                break
        if unique:
            lines.append(f"Recently played: {', '.join(unique[:6])}")

    week_artists = ctx.get("top_artists_week", [])
    if week_artists:
        names = [a["artist"] for a in week_artists[:5] if a.get("artist")]
        if names:
            lines.append(f"Most played artists this week: {', '.join(names)}")

    loved = ctx.get("loved_tracks", [])
    if loved:
        names = [f"{t['track']} by {t['artist']}" for t in loved[:4] if t.get("track")]
        if names:
            lines.append(f"Recently loved: {', '.join(names)}")

    return "\n".join(lines) if len(lines) > 1 else ""


if __name__ == "__main__":
    import sys
    force = "--sync" in sys.argv
    ctx = load(force_sync=force)
    if not ctx:
        print("Failed to load Last.fm context (check LASTFM_API_KEY and LASTFM_USERNAME)")
        sys.exit(1)
    print(f"Synced at: {ctx['synced_at']}")
    print(f"Username: {ctx['username']}")
    print(f"Recent tracks: {len(ctx.get('recent_tracks', []))}")
    print(f"Top tracks (week): {len(ctx.get('top_tracks_week', []))}")
    print(f"Loved tracks: {len(ctx.get('loved_tracks', []))}")
    print()
    print(format_for_prompt(ctx))
