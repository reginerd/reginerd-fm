#!/usr/bin/env python3
"""audio_features.py — Per-track audio analysis cache for RGNRD-FM.

Phase 1: exact duration via mutagen (already installed).
Phase 2: BPM + RMS energy + spectral centroid via librosa (uv add librosa).

Cache stored at output/runtime/audio_features.json keyed by absolute file path.
Symlinks are resolved before lookup so NAS-backed symlinks cache correctly.

Usage:
    from content_generator.audio_features import get_features, scan_block
    feats = get_features(Path("output/music_bumpers/morning/Artist__Title.flac"))
    scan_block("morning")  # batch-analyze all tracks in a block
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TypedDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_CACHE_PATH = PROJECT_ROOT / "output" / "runtime" / "audio_features.json"
_AUDIO_SUFFIXES = {".flac", ".mp3", ".wav", ".m4a", ".ogg"}


class TrackFeatures(TypedDict, total=False):
    duration: float | None
    bpm: float | None
    rms_energy: float | None
    spectral_centroid: float | None
    analyzed_at: float


def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def _duration_via_mutagen(filepath: Path) -> float | None:
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(filepath))
        if audio and audio.info:
            return float(audio.info.length)
    except Exception:
        pass
    return None


def _analyze_via_librosa(filepath: Path) -> dict:
    """Return BPM, rms_energy, spectral_centroid using librosa (optional dep)."""
    try:
        import librosa
        import numpy as np
        # Load only first 90s to keep analysis fast
        y, sr = librosa.load(str(filepath), sr=None, mono=True, duration=90)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        rms = float(np.mean(librosa.feature.rms(y=y)))
        centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))
        return {
            "bpm": round(float(tempo), 1),
            "rms_energy": round(rms, 6),
            "spectral_centroid": round(centroid, 1),
        }
    except ImportError:
        return {}
    except Exception:
        return {}


def get_features(filepath: Path, use_librosa: bool = False) -> TrackFeatures:
    """Return cached features for filepath. Analyzes and caches on first call."""
    resolved = str(filepath.resolve())
    cache = _load_cache()

    entry = cache.get(resolved, {})
    needs_duration = entry.get("duration") is None
    needs_librosa = use_librosa and entry.get("bpm") is None

    if not needs_duration and not needs_librosa:
        return entry  # type: ignore[return-value]

    if needs_duration:
        entry["duration"] = _duration_via_mutagen(filepath)

    if needs_librosa:
        librosa_feats = _analyze_via_librosa(filepath)
        entry.update(librosa_feats)

    entry["analyzed_at"] = time.time()
    cache[resolved] = entry
    _save_cache(cache)
    return entry  # type: ignore[return-value]


def get_duration(filepath: Path) -> float | None:
    """Convenience: return just duration in seconds."""
    return get_features(filepath).get("duration")


def scan_block(block: str, use_librosa: bool = False, verbose: bool = False) -> int:
    """Analyze all tracks in a block's bumper dir. Returns count analyzed."""
    bumper_dir = PROJECT_ROOT / "output" / "music_bumpers" / block
    if not bumper_dir.exists():
        return 0

    cache = _load_cache()
    analyzed = 0

    for f in sorted(bumper_dir.iterdir()):
        if not f.is_file() and not f.is_symlink():
            continue
        if f.suffix.lower() not in _AUDIO_SUFFIXES:
            continue
        resolved = str(f.resolve())
        entry = cache.get(resolved, {})
        needs = entry.get("duration") is None or (use_librosa and entry.get("bpm") is None)
        if not needs:
            continue

        if verbose:
            print(f"  Analyzing {f.name}...")
        if entry.get("duration") is None:
            entry["duration"] = _duration_via_mutagen(f)
        if use_librosa and entry.get("bpm") is None:
            entry.update(_analyze_via_librosa(f))
        entry["analyzed_at"] = time.time()
        cache[resolved] = entry
        analyzed += 1

    _save_cache(cache)
    if verbose:
        print(f"[audio_features] {block}: {analyzed} tracks analyzed, cache saved")
    return analyzed


def scan_all_blocks(use_librosa: bool = False, verbose: bool = True) -> None:
    """Scan all blocks under output/music_bumpers/."""
    bumpers_root = PROJECT_ROOT / "output" / "music_bumpers"
    if not bumpers_root.exists():
        return
    for block_dir in sorted(bumpers_root.iterdir()):
        if block_dir.is_dir():
            n = scan_block(block_dir.name, use_librosa=use_librosa, verbose=verbose)
            if verbose:
                print(f"[audio_features] {block_dir.name}: {n} new")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scan and cache audio features")
    parser.add_argument("--block", help="Scan a specific block (default: all)")
    parser.add_argument("--librosa", action="store_true", help="Also run librosa BPM/energy analysis")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.block:
        n = scan_block(args.block, use_librosa=args.librosa, verbose=True)
        print(f"Done — {n} tracks analyzed")
    else:
        scan_all_blocks(use_librosa=args.librosa, verbose=True)
