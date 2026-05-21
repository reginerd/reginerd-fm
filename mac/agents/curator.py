#!/usr/bin/env python3
"""curator.py — Build a track manifest for a show block.

Queries Plex, filters play history and blocked artists, scores tracks using
Plex ratings + Last.fm personal listening signals, and writes a JSON manifest
for the scriptwriter and researcher to consume.

Output: output/manifests/{block}_{YYYY-MM-DD}.json

Usage:
    uv run python mac/agents/curator.py --block prime_time
    uv run python mac/agents/curator.py --block prime_time --date 2026-05-23
    uv run python mac/agents/curator.py --all --date 2026-05-23
    uv run python mac/agents/curator.py --block prime_time --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))

import yaml

GENRES_CONFIG = PROJECT_ROOT / "config" / "genres.yaml"
BLOCKS_CONFIG = PROJECT_ROOT / "config" / "blocks.yaml"
MAC_CONFIG = PROJECT_ROOT / "mac" / "config.yaml"
MANIFEST_DIR = PROJECT_ROOT / "output" / "manifests"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _plex_get(base_url: str, token: str, path: str) -> ET.Element:
    sep = "&" if "?" in path else "?"
    url = f"{base_url}{path}{sep}X-Plex-Token={token}"
    req = Request(url, headers={"Accept": "application/xml"})
    with urlopen(req, timeout=15) as resp:
        return ET.fromstring(resp.read())


def _fetch_tracks(base_url: str, token: str, section: int, genre_name: str) -> list[dict]:
    """Return raw track dicts from Plex for a genre."""
    root = _plex_get(base_url, token, f"/library/sections/{section}/genre")
    genres: dict[str, int] = {}
    for tag in root.iter("Directory"):
        title = tag.get("title", "")
        key_str = tag.get("key", "")
        if title and key_str:
            try:
                genres[title] = int(key_str)
            except ValueError:
                pass

    genre_id = genres.get(genre_name)
    if genre_id is None:
        return []

    root = _plex_get(
        base_url, token,
        f"/library/sections/{section}/all?type=10&genre={genre_id}&X-Plex-Container-Size=10000"
    )

    tracks = []
    for track in root.iter("Track"):
        artist = track.get("grandparentTitle", "").strip()
        title = track.get("title", "").strip()
        album = track.get("parentTitle", "").strip()
        year_raw = track.get("parentYear", "")
        rating_raw = track.get("userRating", "0")
        playlists = [
            pl.get("title", "") for pl in track.findall(".//Playlist")
            if pl.get("title")
        ]

        part = track.find(".//Part")
        if part is None:
            continue
        file_path = part.get("file", "")
        if not file_path:
            continue

        try:
            year = int(year_raw) if year_raw else None
        except ValueError:
            year = None
        try:
            plex_rating = float(rating_raw)
        except (ValueError, TypeError):
            plex_rating = 0.0

        tracks.append({
            "title": title,
            "artist": artist,
            "album": album,
            "year": year,
            "plex_rating": plex_rating,
            "plex_playlists": playlists,
            "plex_file": file_path,
            "genre": genre_name,
        })
    return tracks


def _build_lastfm_lookup(lastfm_ctx: dict) -> tuple[set[str], set[str], set[str]]:
    """Return (loved_keys, top_week_keys, top_month_keys) as 'artist|title' lowercase sets."""
    def _key(artist: str, track: str) -> str:
        return f"{artist.lower().strip()}|{track.lower().strip()}"

    loved = {_key(t.get("artist", ""), t.get("track", "")) for t in lastfm_ctx.get("loved_tracks", [])}
    top_week = {_key(t.get("artist", ""), t.get("track", "")) for t in lastfm_ctx.get("top_tracks_week", [])}
    top_month = {_key(t.get("artist", ""), t.get("track", "")) for t in lastfm_ctx.get("top_tracks_month", [])}
    return loved, top_week, top_month


def _score_track(
    plex_rating: float,
    loved: bool,
    top_week: bool,
    top_month: bool,
) -> float:
    score = plex_rating  # already 0-10 from Plex userRating field
    if loved:
        score += 3.0
    if top_week:
        score += 2.0
    if top_month:
        score += 1.0
    return score


def _slug(artist: str) -> str:
    return re.sub(r"[^\w]", "_", artist.lower().strip())


def build_manifest(
    block: str,
    target_date: date,
    dry_run: bool = False,
) -> dict | None:
    """Build and write manifest for block+date. Returns manifest dict or None on failure."""
    mac_cfg = _load_yaml(MAC_CONFIG)
    genres_cfg = _load_yaml(GENRES_CONFIG)
    blocks_cfg = _load_yaml(BLOCKS_CONFIG)

    plex_cfg = mac_cfg.get("plex", {})
    base_url = f"http://{plex_cfg.get('host', '192.168.1.27')}:{plex_cfg.get('port', 32400)}"
    token_env = plex_cfg.get("token_env", "PLEX_TOKEN")
    token = os.environ.get(token_env, "")
    section = int(plex_cfg.get("library_section", 1))

    if not token:
        print(f"[curator] ERROR: {token_env} not set", file=sys.stderr)
        return None

    genres_for_block: list[str] = genres_cfg.get("show_genres", {}).get(block, [])
    if not genres_for_block:
        print(f"[curator] No genres configured for block '{block}'", file=sys.stderr)
        return None

    block_cfg = blocks_cfg.get(block, {})
    segments: list[str] = block_cfg.get("segments", [])
    track_slots = [s for s in segments if s in ("track_intro", "track_outro")]
    n_tracks = len(track_slots) if track_slots else 3

    blocked_artists_raw: list[str] = block_cfg.get("blocked_artists", [])
    blocked_artists = {a.lower().strip() for a in blocked_artists_raw}

    # Load Last.fm personal context (1hr cache)
    try:
        from content_generator import lastfm_context
        lastfm_ctx = lastfm_context.load() or {}
    except Exception:
        lastfm_ctx = {}
    loved, top_week, top_month = _build_lastfm_lookup(lastfm_ctx)

    # Load play history
    try:
        from play_history import PlayHistory
        history = PlayHistory()
        recent_files = history.get_recent_filepaths(hours=4)
    except Exception:
        history = None
        recent_files = set()

    # Fetch tracks from Plex across all genres for this block
    all_tracks: list[dict] = []
    for genre in genres_for_block:
        try:
            tracks = _fetch_tracks(base_url, token, section, genre)
            all_tracks.extend(tracks)
        except Exception as e:
            print(f"[curator] Warning: genre '{genre}' failed — {e}", file=sys.stderr)

    if not all_tracks:
        print(f"[curator] No tracks fetched for block '{block}'", file=sys.stderr)
        return None

    # Deduplicate by plex_file
    seen_files: set[str] = set()
    unique_tracks: list[dict] = []
    for t in all_tracks:
        f = t["plex_file"]
        if f not in seen_files:
            seen_files.add(f)
            unique_tracks.append(t)

    # Filter: blocked artists + recent play history
    candidates: list[dict] = []
    for t in unique_tracks:
        if t["artist"].lower().strip() in blocked_artists:
            continue
        if t["plex_file"] in recent_files:
            continue
        candidates.append(t)

    if not candidates:
        print(f"[curator] All tracks filtered out for block '{block}'", file=sys.stderr)
        return None

    # Fetch Last.fm tags (7-day cache)
    try:
        from content_generator.artist_tags import get_artist_tags, flush_cache
        for t in candidates:
            t["lastfm_tags"] = get_artist_tags(t["artist"])
        flush_cache()
    except Exception:
        for t in candidates:
            t["lastfm_tags"] = []

    # Get play history last_played + play_count for candidates
    if history is not None:
        try:
            file_list = [t["plex_file"] for t in candidates]
            last_played_map = history.get_last_played(file_list)
            play_count_map: dict[str, int] = {}
            for f in file_list:
                play_count_map[f] = history.get_play_count(f)
        except Exception:
            last_played_map = {}
            play_count_map = {}
    else:
        last_played_map = {}
        play_count_map = {}

    # Score and sort
    track_key = lambda t: f"{t['artist'].lower().strip()}|{t['title'].lower().strip()}"
    for t in candidates:
        tk = track_key(t)
        t["lastfm_loved"] = tk in loved
        t["lastfm_top_week"] = tk in top_week
        t["lastfm_top_month"] = tk in top_month
        t["last_played"] = last_played_map.get(t["plex_file"])
        t["play_count"] = play_count_map.get(t["plex_file"], 0)
        t["score"] = _score_track(
            t["plex_rating"],
            t["lastfm_loved"],
            t["lastfm_top_week"],
            t["lastfm_top_month"],
        )

    # Sort: score desc, last_played asc (None = never played → highest priority), random within tier
    def _sort_key(t: dict) -> tuple:
        lp = t["last_played"] or "0000-00-00"
        jitter = random.random()
        return (-t["score"], lp, jitter)

    candidates.sort(key=_sort_key)
    selected = candidates[:n_tracks]

    # Build manifest entries (clean output, no internal fields)
    entries = []
    for t in selected:
        entries.append({
            "title": t["title"],
            "artist": t["artist"],
            "album": t["album"],
            "year": t["year"],
            "plex_rating": t["plex_rating"],
            "play_count": t["play_count"],
            "last_played": t["last_played"],
            "lastfm_tags": t["lastfm_tags"],
            "lastfm_loved": t["lastfm_loved"],
            "lastfm_top_week": t["lastfm_top_week"],
            "lastfm_top_month": t["lastfm_top_month"],
            "plex_playlists": t["plex_playlists"],
            "score": round(t["score"], 2),
        })

    manifest = {
        "block": block,
        "date": target_date.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_tracks": len(entries),
        "segments": segments,
        "tracks": entries,
    }

    if dry_run:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return manifest

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MANIFEST_DIR / f"{block}_{target_date.isoformat()}.json"
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"[curator] {block} → {out_path} ({len(entries)} tracks)")

    try:
        from agents.slack_notifier import notify_error
    except Exception:
        notify_error = None  # type: ignore[assignment]

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build track manifests from Plex")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--block", help="Block name (morning, prime_time, etc.)")
    group.add_argument("--all", action="store_true", help="Run all configured blocks")
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: tomorrow)")
    parser.add_argument("--dry-run", action="store_true", help="Print manifest, don't write")
    args = parser.parse_args()

    if args.date:
        target_date = date.fromisoformat(args.date)
    else:
        target_date = date.today() + timedelta(days=1)

    genres_cfg = _load_yaml(GENRES_CONFIG)
    blocks = list(genres_cfg.get("show_genres", {}).keys()) if args.all else [args.block]

    for block in blocks:
        try:
            build_manifest(block, target_date, dry_run=args.dry_run)
        except Exception as e:
            print(f"[curator] ERROR {block}: {e}", file=sys.stderr)
            try:
                from agents.slack_notifier import notify_error
                notify_error("curator", f"{block}: {e}")
            except Exception:
                pass


if __name__ == "__main__":
    main()
