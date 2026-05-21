#!/usr/bin/env python3
"""Small Discogs release lookup client for now-playing metadata."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass


DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "").strip()
DISCOGS_USER_AGENT = os.environ.get("DISCOGS_USER_AGENT", "RGNRD-FM/0.1")
HAS_CREDENTIALS = bool(DISCOGS_TOKEN)


@dataclass(frozen=True)
class DiscogsResult:
    release_id: int | None
    title: str
    artist: str
    year: int | None
    url: str | None
    thumb_url: str | None
    label: str | None
    format: str | None


def _request_json(url: str, timeout: float = 6.0) -> dict:
    headers = {"User-Agent": DISCOGS_USER_AGENT}
    if DISCOGS_TOKEN:
        headers["Authorization"] = f"Discogs token={DISCOGS_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _first_text(value) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    if isinstance(value, str) and value:
        return value
    return None


def _public_url(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    path = value if value.startswith("/") else f"/{value}"
    return f"https://www.discogs.com{path}"


def search_discogs(track_name: str, vibe: str | None = None) -> DiscogsResult | None:
    """Search Discogs for the best release match for a track display name."""
    if not HAS_CREDENTIALS or not track_name:
        return None

    query = track_name if not vibe else f"{track_name} {vibe}"
    params = urllib.parse.urlencode({
        "q": query,
        "type": "release",
        "per_page": 5,
    })
    data = _request_json(f"https://api.discogs.com/database/search?{params}")
    results = data.get("results") or []
    if not results:
        return None

    item = results[0]
    title = str(item.get("title") or track_name)
    artist = title.split(" - ", 1)[0] if " - " in title else ""
    return DiscogsResult(
        release_id=item.get("id"),
        title=title,
        artist=artist,
        year=item.get("year"),
        url=_public_url(item.get("uri")),
        thumb_url=item.get("thumb"),
        label=_first_text(item.get("label")),
        format=_first_text(item.get("format")),
    )
