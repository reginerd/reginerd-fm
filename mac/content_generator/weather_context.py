#!/usr/bin/env python3
"""weather_context.py — Fetch current Bay Area weather for RGNRD-FM context.

Source: wttr.in JSON API (no key required).
Caches to output/runtime/weather_context.json with a 3-hour TTL.

Usage:
    from content_generator.weather_context import load
    weather = load()  # -> {"summary": "Foggy, 58°F", "desc": "...", ...}
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

_CACHE_PATH = PROJECT_ROOT / "output" / "runtime" / "weather_context.json"
_CACHE_TTL = int(os.environ.get("WEATHER_CACHE_TTL", str(3 * 3600)))
_LOCATION = os.environ.get("WEATHER_LOCATION", "San+Francisco,CA")

# wttr.in condition codes → plain-English descriptions
_CONDITION_MAP = {
    "113": "Clear", "116": "Partly cloudy", "119": "Cloudy",
    "122": "Overcast", "143": "Mist", "176": "Patchy rain",
    "179": "Patchy snow", "182": "Patchy sleet", "185": "Freezing drizzle",
    "200": "Thundery outbreaks", "227": "Blowing snow", "230": "Blizzard",
    "248": "Fog", "260": "Freezing fog", "263": "Light drizzle",
    "266": "Drizzle", "281": "Freezing drizzle", "284": "Heavy freezing drizzle",
    "293": "Light rain", "296": "Light rain", "299": "Moderate rain",
    "302": "Moderate rain", "305": "Heavy rain", "308": "Heavy rain",
    "311": "Light freezing rain", "314": "Moderate freezing rain",
    "317": "Light sleet", "320": "Moderate sleet", "323": "Light snow",
    "326": "Light snow", "329": "Moderate snow", "332": "Moderate snow",
    "335": "Heavy snow", "338": "Heavy snow", "350": "Ice pellets",
    "353": "Light rain shower", "356": "Moderate rain shower",
    "359": "Heavy rain shower", "362": "Light sleet shower",
    "365": "Moderate sleet shower", "368": "Light snow shower",
    "371": "Moderate snow shower", "374": "Light ice pellet shower",
    "377": "Moderate ice pellet shower", "386": "Thundery rain",
    "389": "Heavy thundery rain", "392": "Thundery snow",
    "395": "Heavy thundery snow",
}


def _fetch_weather(location: str) -> dict | None:
    try:
        url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "RGNRD-FM/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def load() -> dict:
    """Return weather context dict, using cache if fresh."""
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _CACHE_PATH.exists():
        try:
            cached = json.loads(_CACHE_PATH.read_text())
            age = time.time() - cached.get("fetched_at_ts", 0)
            if age < _CACHE_TTL:
                return cached
        except Exception:
            pass

    data = _fetch_weather(_LOCATION)
    if not data:
        return {"summary": "", "temp_f": None, "condition": "", "fetched_at": "", "fetched_at_ts": 0}

    try:
        current = data["current_condition"][0]
        temp_f = int(current.get("temp_F", 0))
        temp_c = int(current.get("temp_C", 0))
        code = current.get("weatherCode", "")
        condition = _CONDITION_MAP.get(str(code), current.get("weatherDesc", [{}])[0].get("value", ""))
        feels_f = int(current.get("FeelsLikeF", temp_f))
        humidity = current.get("humidity", "")
        wind_mph = current.get("windspeedMiles", "")

        summary = f"{condition}, {temp_f}°F"
        if feels_f != temp_f:
            summary += f" (feels {feels_f}°F)"

        ctx = {
            "summary": summary,
            "temp_f": temp_f,
            "temp_c": temp_c,
            "feels_like_f": feels_f,
            "condition": condition,
            "humidity": humidity,
            "wind_mph": wind_mph,
            "location": _LOCATION,
            "fetched_at": datetime.utcnow().isoformat(),
            "fetched_at_ts": time.time(),
        }
    except (KeyError, IndexError, ValueError):
        ctx = {"summary": "", "temp_f": None, "condition": "", "fetched_at": "", "fetched_at_ts": 0}

    try:
        _CACHE_PATH.write_text(json.dumps(ctx, indent=2))
    except Exception:
        pass
    return ctx


def format_for_prompt(ctx: dict | None = None) -> str:
    """Return a one-line string suitable for injection into an LLM prompt."""
    if ctx is None:
        ctx = load()
    return ctx.get("summary", "")


if __name__ == "__main__":
    ctx = load()
    print(format_for_prompt(ctx))
    print(json.dumps(ctx, indent=2))
