#!/usr/bin/env python3
"""news_context.py — Fetch top headlines for RGNRD-FM context injection.

Sources: NPR News (primary), BBC World Service (fallback), both via RSS.
Caches to output/runtime/news_context.json with a 6-hour TTL.

Usage:
    from content_generator.news_context import load
    news = load()  # -> {"headlines": [...], "fetched_at": "..."}
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_CACHE_PATH = PROJECT_ROOT / "output" / "runtime" / "news_context.json"
_CACHE_TTL = int(os.environ.get("NEWS_CACHE_TTL", str(6 * 3600)))
_MAX_HEADLINES = 8

_FEEDS = [
    ("NPR News", "https://feeds.npr.org/1001/rss.xml"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
]


def _fetch_rss(url: str, max_items: int = _MAX_HEADLINES) -> list[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RGNRD-FM/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            root = ET.fromstring(resp.read())
        items = root.findall(".//item")
        headlines = []
        for item in items[:max_items]:
            title = item.findtext("title", "").strip()
            if title:
                headlines.append(title)
        return headlines
    except Exception:
        return []


def _fetch_headlines() -> list[str]:
    for source_name, url in _FEEDS:
        headlines = _fetch_rss(url)
        if headlines:
            return headlines
    return []


def load() -> dict:
    """Return news context dict, using cache if fresh."""
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _CACHE_PATH.exists():
        try:
            cached = json.loads(_CACHE_PATH.read_text())
            age = time.time() - cached.get("fetched_at_ts", 0)
            if age < _CACHE_TTL:
                return cached
        except Exception:
            pass

    headlines = _fetch_headlines()
    ctx = {
        "headlines": headlines,
        "fetched_at": datetime.utcnow().isoformat(),
        "fetched_at_ts": time.time(),
    }
    try:
        _CACHE_PATH.write_text(json.dumps(ctx, indent=2))
    except Exception:
        pass
    return ctx


def format_for_prompt(ctx: dict | None = None, max_items: int = 5) -> str:
    """Return a compact string suitable for injection into an LLM prompt."""
    if ctx is None:
        ctx = load()
    headlines = ctx.get("headlines", [])
    if not headlines:
        return ""
    lines = ["Today's headlines:"] + [f"- {h}" for h in headlines[:max_items]]
    return "\n".join(lines)


if __name__ == "__main__":
    ctx = load()
    print(format_for_prompt(ctx))
