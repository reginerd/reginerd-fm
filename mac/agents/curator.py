#!/usr/bin/env python3
"""curator.py — Build a track manifest for a show block.

Loads the flat music library index (output/runtime/music_library.json),
filters by play history, audio features, and blocked artists, scores tracks
using Plex ratings + Last.fm signals + vibe boost, and writes a JSON manifest
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
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))

import yaml

BLOCKS_CONFIG = PROJECT_ROOT / "config" / "blocks.yaml"
MAC_CONFIG = PROJECT_ROOT / "mac" / "config.yaml"
MANIFEST_DIR = PROJECT_ROOT / "output" / "manifests"
BUMPERS_DIR = PROJECT_ROOT / "output" / "music_bumpers"

AUDIO_SUFFIXES = {".flac", ".mp3", ".wav", ".aac", ".m4a", ".ogg"}


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _load_from_library() -> list[dict]:
    lib_path = PROJECT_ROOT / "output" / "runtime" / "music_library.json"
    if not lib_path.exists():
        print("[curator] music_library.json not found — run music_indexer.py first", file=sys.stderr)
        return []
    try:
        return json.loads(lib_path.read_text())
    except Exception as e:
        print(f"[curator] Failed to load music_library.json: {e}", file=sys.stderr)
        return []


def _build_lastfm_lookup(lastfm_ctx: dict) -> tuple[set[str], set[str], set[str]]:
    """Return (loved_keys, top_week_keys, top_month_keys) as 'artist|title' lowercase sets."""
    def _key(artist: str, track: str) -> str:
        return f"{artist.lower().strip()}|{track.lower().strip()}"

    loved = {_key(t.get("artist", ""), t.get("track", "")) for t in lastfm_ctx.get("loved_tracks", [])}
    top_week = {_key(t.get("artist", ""), t.get("track", "")) for t in lastfm_ctx.get("top_tracks_week", [])}
    top_month = {_key(t.get("artist", ""), t.get("track", "")) for t in lastfm_ctx.get("top_tracks_month", [])}
    return loved, top_week, top_month


def _artist_match(artist: str, favorites: list[str]) -> bool:
    a = artist.lower().strip()
    return any(f.lower().strip() == a for f in favorites)


def _brightness_score(brightness: float | None, brightness_range: list | None) -> float:
    """0.0–1.0 bonus based on how centered brightness is within the block's range."""
    if brightness is None or not brightness_range or len(brightness_range) < 2:
        return 0.5  # neutral if no data/config
    lo, hi = brightness_range[0], brightness_range[1]
    if brightness < lo or brightness > hi:
        return 0.0
    center = (lo + hi) / 2
    half = (hi - lo) / 2
    return 1.0 - abs(brightness - center) / half


def _tag_boost(tags: list[str], boost_tags: list[str]) -> float:
    """0.5 per matching tag, capped at 3.0."""
    if not tags or not boost_tags:
        return 0.0
    tag_set = {t.lower() for t in tags}
    boost_set = {t.lower() for t in boost_tags}
    return min(3.0, len(tag_set & boost_set) * 0.5)


def _score_track(
    plex_rating: float,
    loved: bool,
    top_week: bool,
    top_month: bool,
    is_favorite: bool = False,
    brightness: float | None = None,
    brightness_range: list | None = None,
    tags: list[str] | None = None,
    boost_tags: list[str] | None = None,
) -> float:
    score = plex_rating  # already 0-10 from Plex userRating field
    if is_favorite:
        score += 2.0
    if loved:
        score += 3.0
    if top_week:
        score += 2.0
    if top_month:
        score += 1.0
    score += _brightness_score(brightness, brightness_range)
    score += _tag_boost(tags or [], boost_tags or [])
    return score


def _slug(artist: str) -> str:
    return re.sub(r"[^\w]", "_", artist.lower().strip())


def _safe_filename(text: str) -> str:
    return re.sub(r"[^\w\-.]", "_", text)


def _update_symlinks(block: str, pool: list[dict]) -> None:
    """Add new pool tracks to output/music_bumpers/{block}/.

    Only adds — never removes — to avoid breaking active playlist JSONs
    that reference symlink paths from the current or previous day.
    Writes .json sidecars alongside each symlink.
    """
    block_dir = BUMPERS_DIR / block
    block_dir.mkdir(parents=True, exist_ok=True)

    # Build set of target paths for the new pool
    pool_targets: dict[str, dict] = {}  # resolved nas_path → track dict
    for t in pool:
        nas_path = t.get("nas_path", "")
        if nas_path:
            pool_targets[nas_path] = t

    # Build set of existing symlink targets
    existing_targets: set[str] = set()
    for f in block_dir.iterdir():
        if f.suffix.lower() in AUDIO_SUFFIXES and f.is_symlink():
            existing_targets.add(str(f.resolve()))

    # Add new symlinks for pool tracks not yet present
    added = 0
    for nas_path_str, track in pool_targets.items():
        if nas_path_str in existing_targets:
            continue
        src = Path(nas_path_str)
        if not src.exists():
            continue

        link_name = (
            f"{_safe_filename(track['artist'])}__{_safe_filename(track['title'])}{src.suffix}"
        )
        link_path = block_dir / link_name
        if link_path.exists() or link_path.is_symlink():
            continue

        try:
            link_path.symlink_to(src)
        except Exception as e:
            print(f"[curator] symlink failed {link_name}: {e}", file=sys.stderr)
            continue

        json_path = link_path.with_suffix(".json")
        if not json_path.exists():
            json_path.write_text(json.dumps({
                "title": track["title"],
                "artist": track["artist"],
                "album": track.get("album", ""),
                "year": str(track.get("year", "")),
                "display_name": f"{track['artist']} - {track['title']}",
                "bpm": track.get("bpm"),
                "energy": track.get("energy"),
                "brightness": track.get("brightness"),
            }, indent=2))
        added += 1

    if added:
        print(f"[curator] {block}: +{added} symlinks added to music_bumpers")


def build_manifest(
    block: str,
    target_date: date,
    dry_run: bool = False,
) -> dict | None:
    """Build and write manifest for block+date. Returns manifest dict or None on failure."""
    blocks_cfg = _load_yaml(BLOCKS_CONFIG)

    block_cfg = blocks_cfg.get(block, {})
    if not block_cfg:
        print(f"[curator] Block '{block}' not found in blocks.yaml", file=sys.stderr)
        return None

    segments: list[str] = block_cfg.get("segments", [])
    track_slots = [s for s in segments if s in ("track_intro", "track_outro")]
    n_tracks = len(track_slots) if track_slots else 3
    favorite_artists: list[str] = block_cfg.get("favorite_artists", [])
    blocked_artists_raw: list[str] = block_cfg.get("blocked_artists", [])
    blocked_artists = {a.lower().strip() for a in blocked_artists_raw}
    pool_size: int = block_cfg.get("pool_size", 400)
    boost_tags_cfg: list[str] = block_cfg.get("boost_tags", [])
    brightness_range = block_cfg.get("brightness_range")

    # Audio feature filters
    bpm_range: list[int] | None = block_cfg.get("bpm_range")
    energy_min: float | None = block_cfg.get("energy_min")
    energy_max: float | None = block_cfg.get("energy_max")

    # Last.fm tag hard filters (blocked only — allowed has been replaced by boost_tags)
    tag_cfg = block_cfg.get("lastfm_tags", {})
    blocked_tags: set[str] = {t.lower() for t in tag_cfg.get("blocked", [])}

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

    # Load the flat library index
    all_tracks = _load_from_library()
    if not all_tracks:
        print(f"[curator] No tracks in library for block '{block}'", file=sys.stderr)
        return None

    # Deduplicate by nas_path
    seen_paths: set[str] = set()
    unique_tracks: list[dict] = []
    for t in all_tracks:
        p = t.get("nas_path", "")
        if p and p not in seen_paths:
            seen_paths.add(p)
            unique_tracks.append(t)

    # Load votes once for both filtering and scoring
    votes_map: dict[str, int] = {}
    try:
        import sqlite3 as _sqlite3
        _votes_db = Path.home() / ".rgnrd" / "votes.db"
        if _votes_db.exists():
            _conn = _sqlite3.connect(str(_votes_db))
            for _row in _conn.execute("SELECT filepath, SUM(vote) FROM votes GROUP BY filepath"):
                votes_map[_row[0]] = int(_row[1])
            _conn.close()
    except Exception:
        pass

    # Global skit/interlude filter
    _SKIT_RE = re.compile(r'\b(skit|interlude)\b', re.IGNORECASE)
    min_duration: float = block_cfg.get("min_duration", 60.0)
    unique_tracks = [
        t for t in unique_tracks
        if not _SKIT_RE.search(t.get("title", ""))
        and (t.get("duration") is None or t["duration"] >= min_duration)
    ]

    # Filter: blocked artists + recent play history + heavily downvoted
    candidates: list[dict] = []
    for t in unique_tracks:
        if t.get("artist", "").lower().strip() in blocked_artists:
            continue
        if t.get("nas_path", "") in recent_files:
            continue
        if votes_map.get(t.get("nas_path", ""), 0) <= -4:
            continue
        candidates.append(t)

    if not candidates:
        print(f"[curator] All tracks filtered out for block '{block}'", file=sys.stderr)
        return None

    # Audio feature filtering (fail-open: tracks with no features pass through)
    if bpm_range or energy_min is not None or energy_max is not None:
        before_audio = len(candidates)
        audio_filtered: list[dict] = []
        for t in candidates:
            track_bpm = t.get("bpm")
            track_energy = t.get("energy")

            # No features → fail open (include)
            if track_bpm is None and track_energy is None:
                audio_filtered.append(t)
                continue

            if bpm_range and track_bpm is not None:
                if not (bpm_range[0] <= track_bpm <= bpm_range[1]):
                    continue

            if energy_min is not None and track_energy is not None:
                if track_energy < energy_min:
                    continue

            if energy_max is not None and track_energy is not None:
                if track_energy > energy_max:
                    continue

            audio_filtered.append(t)

        candidates = audio_filtered
        after_audio = len(candidates)
        if before_audio != after_audio:
            print(f"[curator] Audio filter: {before_audio} → {after_audio} tracks ({before_audio - after_audio} removed)")

        if not candidates:
            print(f"[curator] All tracks filtered by audio features for block '{block}'", file=sys.stderr)
            return None

    # Last.fm blocked-tag hard filter
    if blocked_tags:
        before_tags = len(candidates)
        tag_filtered: list[dict] = []
        for t in candidates:
            track_tags = {tag.lower() for tag in (t.get("lastfm_tags") or [])}
            if not track_tags:
                # Fail-open: no tags → include
                tag_filtered.append(t)
                continue
            if track_tags & blocked_tags:
                continue
            tag_filtered.append(t)
        candidates = tag_filtered
        after_tags = len(candidates)
        if before_tags != after_tags:
            print(f"[curator] Tag block filter: {before_tags} → {after_tags} tracks ({before_tags - after_tags} removed)")

        if not candidates:
            print(f"[curator] All tracks removed by blocked tags for block '{block}'", file=sys.stderr)
            return None

    # Get play history last_played + play_count for candidates
    if history is not None:
        try:
            file_list = [t["nas_path"] for t in candidates]
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

    # Build cross-show artist cooldown from manifests generated in the last 48 hours
    recent_artist_counts: dict[str, int] = {}
    cutoff = datetime.now() - timedelta(hours=48)
    for mf in MANIFEST_DIR.glob("*.json"):
        try:
            if datetime.fromtimestamp(mf.stat().st_mtime) < cutoff:
                continue
            data = json.loads(mf.read_text())
            for t in data.get("tracks", []):
                a = t.get("artist", "").lower().strip()
                if a:
                    recent_artist_counts[a] = recent_artist_counts.get(a, 0) + 1
        except Exception:
            pass

    # Score and sort
    track_key = lambda t: f"{t.get('artist', '').lower().strip()}|{t.get('title', '').lower().strip()}"
    for t in candidates:
        tk = track_key(t)
        t["lastfm_loved"] = tk in loved
        t["lastfm_top_week"] = tk in top_week
        t["lastfm_top_month"] = tk in top_month
        t["last_played"] = last_played_map.get(t["nas_path"])
        t["play_count"] = play_count_map.get(t["nas_path"], 0)
        recent_appearances = recent_artist_counts.get(t.get("artist", "").lower().strip(), 0)
        net_votes = votes_map.get(t.get("nas_path", ""), 0)
        t["net_votes"] = net_votes
        t["score"] = _score_track(
            t.get("plex_rating", 0.0),
            t["lastfm_loved"],
            t["lastfm_top_week"],
            t["lastfm_top_month"],
            is_favorite=_artist_match(t.get("artist", ""), favorite_artists),
            brightness=t.get("brightness"),
            brightness_range=brightness_range,
            tags=t.get("lastfm_tags", []),
            boost_tags=boost_tags_cfg,
        ) - (recent_appearances * 1.0) + max(-3.0, min(3.0, net_votes * 0.5))

    # Sort: score desc, last_played asc (None = never played → highest priority), random within tier
    def _sort_key(t: dict) -> tuple:
        lp = t.get("last_played") or "0000-00-00"
        jitter = random.random()
        return (-t["score"], lp, jitter)

    candidates.sort(key=_sort_key)

    # Select featured tracks for intros + full vibe pool
    selected = candidates[:n_tracks]
    pool = candidates[:pool_size]

    def _make_entry(t: dict, include_audio: bool = False) -> dict:
        entry: dict = {
            "title": t.get("title", ""),
            "artist": t.get("artist", ""),
            "album": t.get("album", ""),
            "year": t.get("year"),
            "plex_rating": t.get("plex_rating", 0.0),
            "play_count": t.get("play_count", 0),
            "last_played": t.get("last_played"),
            "lastfm_tags": t.get("lastfm_tags", []),
            "lastfm_loved": t.get("lastfm_loved", False),
            "lastfm_top_week": t.get("lastfm_top_week", False),
            "lastfm_top_month": t.get("lastfm_top_month", False),
            "plex_playlists": t.get("plex_playlists", []),
            "score": round(t.get("score", 0.0), 2),
            # plex_file is set to nas_path for backward compat with show_card_gen and feeder
            "plex_file": t.get("nas_path", ""),
            "nas_path": t.get("nas_path", ""),
        }
        if include_audio:
            entry["bpm"] = t.get("bpm")
            entry["energy"] = t.get("energy")
            entry["brightness"] = t.get("brightness")
            entry["duration"] = t.get("duration")
        return entry

    selected_entries = [_make_entry(t, include_audio=True) for t in selected]
    pool_entries = [_make_entry(t, include_audio=True) for t in pool]

    manifest = {
        "block": block,
        "date": target_date.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_tracks": len(selected_entries),
        "segments": segments,
        "tracks": selected_entries,
        "pool": pool_entries,
    }

    if dry_run:
        # Print summary rather than full manifest to avoid flooding stdout
        print(f"[curator] DRY RUN {block}: {len(selected_entries)} featured, {len(pool_entries)} pool tracks")
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return manifest

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MANIFEST_DIR / f"{block}_{target_date.isoformat()}.json"
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"[curator] {block} → {out_path} ({len(selected_entries)} featured, {len(pool_entries)} pool)")

    # Update symlinks in music_bumpers/{block}/
    try:
        _update_symlinks(block, pool)
    except Exception as e:
        print(f"[curator] Warning: symlink update failed for {block}: {e}", file=sys.stderr)

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build track manifests from library index")
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

    # Get block list from blocks.yaml (no longer depends on genres.yaml)
    blocks_cfg = _load_yaml(BLOCKS_CONFIG)
    blocks = list(blocks_cfg.keys()) if args.all else [args.block]

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
