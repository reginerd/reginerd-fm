#!/usr/bin/env python3
"""Last.fm artist tag fetching with per-artist disk cache.

Used by plex_music_feeder.py --use-tags to filter track candidates by
Last.fm community tags rather than coarse Plex genre labels.

Cache: output/runtime/artist_tags_cache.json
TTL:   7 days (configurable via LASTFM_TAGS_TTL env var, in seconds)

Within a single process run, tags are deduplicated in memory — 15 tracks
from the same artist result in 1 API call and 14 cache hits. Call
flush_cache() once at the end of the run to persist to disk.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_API_BASE = "https://ws.audioscrobbler.com/2.0/"
_CACHE_PATH = PROJECT_ROOT / "output" / "runtime" / "artist_tags_cache.json"
_CACHE_TTL = int(os.environ.get("LASTFM_TAGS_TTL", str(7 * 24 * 3600)))
_MAX_TAGS = int(os.environ.get("LASTFM_TAGS_MAX", "10"))

# In-memory write-back cache. Loaded lazily from disk on first get_artist_tags()
# call; flushed to disk by flush_cache() at end of a feeder run.
_memory_cache: dict[str, dict] | None = None


def _load_disk_cache() -> dict[str, dict]:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text())
    except Exception:
        return {}


def _ensure_memory_cache() -> dict[str, dict]:
    global _memory_cache
    if _memory_cache is None:
        _memory_cache = _load_disk_cache()
    return _memory_cache


def _is_fresh(entry: dict) -> bool:
    cached_at = entry.get("cached_at", "")
    if not cached_at:
        return False
    try:
        ts = datetime.fromisoformat(cached_at).timestamp()
        return (time.time() - ts) < _CACHE_TTL
    except Exception:
        return False


def _fetch_tags(api_key: str, artist_name: str) -> list[str]:
    """Call artist.getTopTags. Returns lowercase tag list or [] on any failure."""
    params = {
        "method": "artist.getTopTags",
        "artist": artist_name,
        "api_key": api_key,
        "autocorrect": "1",
        "format": "json",
    }
    url = _API_BASE + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []

    if "error" in data:
        return []

    tags = data.get("toptags", {}).get("tag", [])
    if not isinstance(tags, list):
        tags = [tags] if tags else []

    return [t["name"].lower() for t in tags if isinstance(t, dict) and t.get("name")][:_MAX_TAGS]


def get_artist_tags(artist_name: str, api_key: str | None = None) -> list[str]:
    """Return lowercase top Last.fm tags for artist_name. Cached for TTL seconds.

    Returns [] if LASTFM_API_KEY is missing or on any API error (fail-open).
    """
    cache_key = artist_name.lower().strip()
    if not cache_key:
        return []

    cache = _ensure_memory_cache()

    entry = cache.get(cache_key)
    if entry and _is_fresh(entry):
        return entry["tags"]

    resolved_api_key = api_key or os.environ.get("LASTFM_API_KEY", "").strip()
    if not resolved_api_key:
        return []

    tags = _fetch_tags(resolved_api_key, artist_name)

    cache[cache_key] = {
        "tags": tags,
        "cached_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }

    return tags


def flush_cache() -> None:
    """Persist in-memory cache to disk. Call once at end of a feeder run."""
    if _memory_cache is None:
        return
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(_memory_cache, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    import sys

    artist = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "65daysofstatic"
    tags = get_artist_tags(artist)
    flush_cache()
    print(f"{artist}: {tags or '(no tags returned)'}")
