#!/usr/bin/env python3
"""music_indexer.py — Flat library index for RGNRD-FM.

Queries ALL Plex tracks (no genre filter), runs incremental Librosa analysis,
fetches Last.fm tags, and writes output/runtime/music_library.json.

Replaces plex_music_feeder.py --all --full as the nightly music refresh step.

Usage:
    uv run python mac/music_indexer.py               # incremental
    uv run python mac/music_indexer.py --force        # reanalyze all
    uv run python mac/music_indexer.py --dry-run      # print counts only
    uv run python mac/music_indexer.py --skip-librosa # metadata only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))

import yaml

MAC_CONFIG = PROJECT_ROOT / "mac" / "config.yaml"
AUDIO_FEATURES_CACHE = PROJECT_ROOT / "output" / "runtime" / "audio_features.json"
LIBRARY_PATH = PROJECT_ROOT / "output" / "runtime" / "music_library.json"

AUDIO_SUFFIXES = {".flac", ".mp3", ".wav", ".m4a", ".aac", ".ogg"}


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _load_audio_cache() -> dict:
    if AUDIO_FEATURES_CACHE.exists():
        try:
            return json.loads(AUDIO_FEATURES_CACHE.read_text())
        except Exception:
            pass
    return {}


def _save_audio_cache(cache: dict) -> None:
    AUDIO_FEATURES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    AUDIO_FEATURES_CACHE.write_text(json.dumps(cache, indent=2))


def _plex_to_local(plex_path: str, music_prefix: str, nas_mount: Path) -> Path:
    """Map a Plex server path to the local NAS mount path."""
    prefix = music_prefix.rstrip("/")
    if prefix and plex_path.startswith(prefix):
        relative = plex_path[len(prefix):].lstrip("/")
    else:
        relative = plex_path.lstrip("/")
    return nas_mount / relative


def _analyze(filepath: Path) -> dict | None:
    """Run librosa analysis on one file. Returns partial feature dict on error."""
    try:
        import librosa
        import librosa.feature.rhythm
        y, sr = librosa.load(str(filepath), sr=None, mono=True, duration=300)
    except Exception as e:
        print(f"  [error] librosa load failed on {filepath.name}: {e}", file=sys.stderr)
        return None

    result: dict = {}

    try:
        result["duration"] = round(float(librosa.get_duration(y=y, sr=sr)), 2)
    except Exception:
        result["duration"] = None

    try:
        result["energy"] = round(float(librosa.feature.rms(y=y).mean()), 6)
    except Exception:
        result["energy"] = None

    try:
        result["brightness"] = round(float(librosa.feature.spectral_centroid(y=y, sr=sr).mean()), 1)
    except Exception:
        result["brightness"] = None

    try:
        result["bpm"] = round(float(librosa.feature.rhythm.tempo(y=y, sr=sr)[0]), 1)
    except Exception as e:
        print(f"  [warn] tempo failed on {filepath.name}: {e}", file=sys.stderr)
        result["bpm"] = None

    return result


def fetch_all_tracks(base_url: str, token: str, section: int) -> list[dict]:
    """Fetch all tracks from Plex (no genre filter). Returns raw track dicts."""
    url_path = f"/library/sections/{section}/all?type=10&X-Plex-Container-Size=50000"
    sep = "&" if "?" in url_path else "?"
    url = f"{base_url}{url_path}{sep}X-Plex-Token={token}"
    req = Request(url, headers={"Accept": "application/xml"})
    with urlopen(req, timeout=60) as resp:
        root = ET.fromstring(resp.read())

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
        plex_path = part.get("file", "")
        if not plex_path:
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
            "plex_path": plex_path,
        })
    return tracks


def run_indexer(force: bool = False, dry_run: bool = False, skip_librosa: bool = False) -> list[dict]:
    """Main indexing routine. Returns list of indexed track dicts."""
    mac_cfg = _load_yaml(MAC_CONFIG)
    plex_cfg = mac_cfg.get("plex", {})

    base_url = f"http://{plex_cfg.get('host', '192.168.1.27')}:{plex_cfg.get('port', 32400)}"
    token_env = plex_cfg.get("token_env", "PLEX_TOKEN")
    token = os.environ.get(token_env, "")
    section = int(plex_cfg.get("library_section", 1))
    music_prefix = str(plex_cfg.get("music_prefix", "/volume1/Music"))
    nas_mount = Path(str(mac_cfg.get("nas_mount", "/Volumes/Music")))

    if not token:
        print(f"[indexer] ERROR: {token_env} not set", file=sys.stderr)
        sys.exit(1)

    # --- 1. Fetch all tracks from Plex ---
    print("[indexer] Querying Plex for all tracks (no genre filter)...")
    try:
        raw_tracks = fetch_all_tracks(base_url, token, section)
    except Exception as e:
        print(f"[indexer] ERROR: Plex query failed — {e}", file=sys.stderr)
        sys.exit(1)

    total = len(raw_tracks)
    print(f"[indexer] {total} tracks found in Plex")

    if dry_run:
        print(f"[indexer] --dry-run: would index {total} tracks, skipping analysis and writes")
        return []

    # --- 2. Load audio features cache ---
    audio_cache = _load_audio_cache()

    # --- 3. Load artist tags module ---
    try:
        from content_generator.artist_tags import get_artist_tags, flush_cache as flush_tags_cache
        has_tags = True
    except Exception:
        has_tags = False
        flush_tags_cache = None  # type: ignore[assignment]
        print("[indexer] Warning: artist_tags module unavailable — lastfm_tags will be empty", file=sys.stderr)

    # --- 4. Process each track ---
    library: list[dict] = []
    today = date.today().isoformat()
    analyzed_count = 0
    cached_count = 0
    error_count = 0

    for i, raw in enumerate(raw_tracks, 1):
        nas_path = _plex_to_local(raw["plex_path"], music_prefix, nas_mount)
        nas_path_str = str(nas_path)

        # Progress output every 50 tracks
        if i % 50 == 0 or i == total:
            print(f"[indexer] {i}/{total} {raw['artist']} — {raw['title']}")

        # --- Audio features ---
        bpm = None
        energy = None
        brightness = None
        duration = None

        # Always check the cache first — skip_librosa only prevents NEW analysis
        cached = audio_cache.get(nas_path_str, {})
        try:
            mtime = nas_path.stat().st_mtime if nas_path.exists() else 0.0
        except Exception:
            mtime = 0.0

        if not force and cached.get("mtime") == mtime and cached.get("analyzed_at") is not None:
            # Cache hit — use existing values regardless of skip_librosa
            bpm = cached.get("bpm")
            energy = cached.get("energy")
            brightness = cached.get("brightness")
            duration = cached.get("duration")
            cached_count += 1
        elif not skip_librosa and nas_path.exists():
            # Cache miss and analysis allowed — run librosa
            feats = _analyze(nas_path)
            if feats is not None:
                bpm = feats["bpm"]
                energy = feats["energy"]
                brightness = feats["brightness"]
                duration = feats["duration"]

                cache_entry = {
                    "mtime": mtime,
                    "bpm": bpm,
                    "energy": energy,
                    "brightness": brightness,
                    "duration": duration,
                    "artist": raw["artist"],
                    "album": raw["album"],
                    "title": raw["title"],
                    "analyzed_at": today,
                }
                audio_cache[nas_path_str] = cache_entry
                _save_audio_cache(audio_cache)
                analyzed_count += 1
            else:
                error_count += 1
        else:
            # skip_librosa or file missing — use whatever the cache has (may be incomplete)
            bpm = cached.get("bpm")
            energy = cached.get("energy")
            brightness = cached.get("brightness")
            duration = cached.get("duration")

        # --- Last.fm tags ---
        lastfm_tags: list[str] = []
        if has_tags:
            try:
                lastfm_tags = get_artist_tags(raw["artist"]) or []
            except Exception:
                lastfm_tags = []

        library.append({
            "title": raw["title"],
            "artist": raw["artist"],
            "album": raw["album"],
            "year": raw["year"],
            "plex_rating": raw["plex_rating"],
            "plex_playlists": raw["plex_playlists"],
            "nas_path": nas_path_str,
            "bpm": bpm,
            "energy": energy,
            "brightness": brightness,
            "duration": duration,
            "lastfm_tags": lastfm_tags,
            "indexed_at": today,
        })

    # --- 5. Flush Last.fm tag cache ---
    if has_tags and flush_tags_cache is not None:
        try:
            flush_tags_cache()
        except Exception:
            pass

    # --- 6. Write library ---
    LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY_PATH.write_text(json.dumps(library, indent=2, ensure_ascii=False) + "\n")

    with_features = sum(1 for t in library if t.get("bpm") is not None)
    print(
        f"\n[indexer] Done — {total} tracks indexed, {analyzed_count} analyzed, "
        f"{cached_count} cached, {error_count} errors, {with_features} with audio features"
    )
    print(f"[indexer] Library written to {LIBRARY_PATH}")

    return library


def main() -> None:
    parser = argparse.ArgumentParser(description="Index full Plex library with acoustic features")
    parser.add_argument("--force", action="store_true", help="Reanalyze all tracks regardless of cache")
    parser.add_argument("--dry-run", action="store_true", help="Print counts, don't write")
    parser.add_argument("--skip-librosa", action="store_true", help="Index metadata only, skip audio analysis")
    args = parser.parse_args()

    run_indexer(force=args.force, dry_run=args.dry_run, skip_librosa=args.skip_librosa)


if __name__ == "__main__":
    main()
