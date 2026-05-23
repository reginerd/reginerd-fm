#!/usr/bin/env python3
"""audio_analyzer.py — Batch audio feature extractor for RGNRD-FM.

Scans all tracks in output/music_bumpers/ across all block subdirs.
Extracts BPM, energy, brightness, and duration via librosa.
Saves results incrementally to output/runtime/audio_features.json.

Usage:
    uv run python mac/audio_analyzer.py           # incremental (skip cached)
    uv run python mac/audio_analyzer.py --force   # reanalyze all
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))

BUMPERS_ROOT = PROJECT_ROOT / "output" / "music_bumpers"
CACHE_PATH = PROJECT_ROOT / "output" / "runtime" / "audio_features.json"
AUDIO_SUFFIXES = {".flac", ".mp3", ".wav", ".m4a"}


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def _parse_metadata(filepath: Path) -> tuple[str, str, str]:
    """Return (artist, album, title) from filename or JSON sidecar.

    Filename pattern: Artist__Title.ext  (double underscore separator)
    Spaces encoded as single underscore within each field.
    """
    stem = filepath.stem
    # Try JSON sidecar first (written by plex_music_feeder.py)
    json_path = filepath.with_suffix(".json")
    if not json_path.exists():
        # Symlink's JSON is next to the symlink, not the target
        json_path = Path(str(filepath) + ".json")
    if json_path.exists():
        try:
            meta = json.loads(json_path.read_text())
            artist = meta.get("artist", "").strip()
            album = meta.get("album", "").strip()
            title = meta.get("title", "").strip()
            if artist and title:
                return artist, album, title
        except Exception:
            pass

    # Fall back to filename parsing: Artist__Title
    if "__" in stem:
        parts = stem.split("__", 1)
        artist_raw = parts[0].replace("_", " ").strip()
        title_raw = parts[1].replace("_", " ").strip()
    else:
        artist_raw = ""
        title_raw = stem.replace("_", " ").strip()

    return artist_raw, "", title_raw


def _analyze(filepath: Path) -> dict | None:
    """Run librosa analysis on one file. Returns feature dict or None on error."""
    try:
        import librosa
        y, sr = librosa.load(str(filepath), sr=None, mono=True, duration=300)
        bpm = float(librosa.feature.rhythm.tempo(y=y, sr=sr)[0])
        energy = float(librosa.feature.rms(y=y).mean())
        brightness = float(librosa.feature.spectral_centroid(y=y, sr=sr).mean())
        duration = float(librosa.get_duration(y=y, sr=sr))
        return {
            "bpm": round(bpm, 1),
            "energy": round(energy, 6),
            "brightness": round(brightness, 1),
            "duration": round(duration, 2),
        }
    except Exception as e:
        print(f"  [error] librosa failed: {e}", file=sys.stderr)
        return None


def collect_files() -> list[Path]:
    """Return sorted list of all audio files across all block subdirs."""
    files: list[Path] = []
    if not BUMPERS_ROOT.exists():
        return files
    for block_dir in sorted(BUMPERS_ROOT.iterdir()):
        if not block_dir.is_dir():
            continue
        for f in sorted(block_dir.iterdir()):
            if f.suffix.lower() in AUDIO_SUFFIXES and (f.is_file() or f.is_symlink()):
                files.append(f)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch audio feature extractor")
    parser.add_argument("--force", action="store_true", help="Reanalyze all tracks regardless of cache")
    args = parser.parse_args()

    cache = _load_cache()
    files = collect_files()
    total = len(files)

    if total == 0:
        print("[audio_analyzer] No audio files found in output/music_bumpers/")
        return

    print(f"[audio_analyzer] Found {total} tracks across all blocks")
    if args.force:
        print("[audio_analyzer] --force: reanalyzing all")

    analyzed = 0
    skipped = 0
    errors = 0
    today = date.today().isoformat()

    for idx, filepath in enumerate(files, 1):
        resolved = str(filepath.resolve())
        try:
            mtime = filepath.stat().st_mtime
        except Exception:
            mtime = 0.0

        # Check cache freshness
        cached = cache.get(resolved, {})
        if not args.force and cached.get("mtime") == mtime and cached.get("bpm") is not None:
            skipped += 1
            continue

        artist, album, title = _parse_metadata(filepath)
        display_label = f"{artist} — {title}" if artist else title

        t_start = __import__("time").perf_counter()
        feats = _analyze(filepath)
        elapsed = __import__("time").perf_counter() - t_start

        if feats is None:
            print(f"[{idx}/{total}] SKIP (error): {display_label}")
            errors += 1
            continue

        entry = {
            "mtime": mtime,
            **feats,
            "artist": artist,
            "album": album,
            "title": title,
            "analyzed_at": today,
        }
        cache[resolved] = entry
        _save_cache(cache)  # persist after every track

        print(f"[{idx}/{total}] {display_label} ({elapsed:.1f}s)")
        analyzed += 1

    print(
        f"\n[audio_analyzer] Done — {analyzed} analyzed, {skipped} skipped (cached), {errors} errors"
    )


if __name__ == "__main__":
    main()
