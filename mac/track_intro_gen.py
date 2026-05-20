#!/usr/bin/env python3
"""
Track intro pre-generator for RGNRD-FM.

Generates short "Next up: [song] by [artist] off the album [album]" WAV clips
for each music bumper. feeder.py inserts these before the track plays.
Intros are cached; this script only generates missing ones.

Usage:
    uv run python mac/track_intro_gen.py                 # All shows
    uv run python mac/track_intro_gen.py --show prime_time
    uv run python mac/track_intro_gen.py --status
    uv run python mac/track_intro_gen.py --force          # Regenerate all
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

from station_config import load_station_config  # noqa: E402

STATION = load_station_config()
BUMPER_DIR = STATION.bumper_dir
INTRO_DIR = STATION.intro_dir
AUDIO_SUFFIXES = {".flac", ".mp3", ".wav", ".m4a"}

DEFAULT_VOICE = "reginerd_clone"


def get_intro_path(bumper_path: Path, show_id: str) -> Path:
    """Return the expected intro WAV path for a bumper (may not exist yet)."""
    return INTRO_DIR / show_id / (bumper_path.stem + "_intro.wav")


def get_bumper_metadata(bumper_path: Path) -> dict:
    """Read track metadata from JSON sidecar, or parse from filename as fallback."""
    json_path = bumper_path.with_suffix(".json")
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text())
            if isinstance(data, dict):
                return {
                    "title": str(data.get("title", "") or ""),
                    "artist": str(data.get("artist", "") or ""),
                    "album": str(data.get("album", "") or ""),
                }
        except Exception:
            pass

    # Fallback: parse Artist__Track_Name filename
    stem = bumper_path.stem
    if "__" in stem:
        artist_raw, track_raw = stem.split("__", 1)
        artist = artist_raw.replace("_", " ").strip()
        title = re.sub(r"_+", " ", track_raw).strip()
    else:
        artist = ""
        title = re.sub(r"_+", " ", stem).strip()
    return {"title": title, "artist": artist, "album": ""}


def build_intro_text(meta: dict) -> str:
    """Build the DJ intro phrase from track metadata."""
    title = meta.get("title", "").strip()
    artist = meta.get("artist", "").strip()
    album = meta.get("album", "").strip()

    if not title:
        return ""

    # Skip album when missing or identical to title (common for singles)
    include_album = bool(album) and album.lower() != title.lower()

    if artist and include_album:
        return f"Next up: {title}, by {artist}, off the album {album}."
    elif artist:
        return f"Next up: {title}, by {artist}."
    elif include_album:
        return f"Next up: {title}, off the album {album}."
    else:
        return f"Next up: {title}."


def generate_intro(bumper_path: Path, show_id: str, voice: str = DEFAULT_VOICE, force: bool = False) -> bool:
    """Generate a track intro WAV for a bumper. Skips if already exists unless force=True."""
    intro_path = get_intro_path(bumper_path, show_id)

    if intro_path.exists() and not force:
        return True

    meta = get_bumper_metadata(bumper_path)
    text = build_intro_text(meta)
    if not text:
        return False

    intro_path.parent.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(PROJECT_ROOT / "mac" / "content_generator"))
    from helpers import render_single_voice  # noqa: E402

    print(f"  Generating intro: {text}")
    return render_single_voice(text, intro_path, voice)


def get_show_voice(show_id: str) -> str:
    """Look up the configured host voice for a show."""
    try:
        from schedule import load_schedule  # noqa: E402
        schedule = load_schedule(STATION.schedule_path)
        show = schedule.shows.get(show_id)
        if show:
            return show.voices.get("host", DEFAULT_VOICE)
    except Exception:
        pass
    return DEFAULT_VOICE


def generate_for_show(show_id: str, force: bool = False) -> tuple[int, int]:
    """Generate intros for all bumpers in a show. Returns (generated, skipped)."""
    show_dir = BUMPER_DIR / show_id
    if not show_dir.exists():
        print(f"  No bumper directory for show: {show_id}")
        return 0, 0

    bumpers = [
        f for f in show_dir.iterdir()
        if f.suffix.lower() in AUDIO_SUFFIXES
        and not (f.is_symlink() and not f.exists())
    ]

    if not bumpers:
        print(f"  {show_id}: no bumpers found")
        return 0, 0

    voice = get_show_voice(show_id)
    generated = 0
    skipped = 0

    print(f"  {show_id}: {len(bumpers)} bumpers, voice={voice}")
    for bumper in sorted(bumpers):
        intro_path = get_intro_path(bumper, show_id)
        if intro_path.exists() and not force:
            skipped += 1
            continue
        ok = generate_intro(bumper, show_id, voice=voice, force=force)
        if ok:
            generated += 1
        else:
            print(f"    FAILED: {bumper.name}")

    return generated, skipped


def cmd_status() -> None:
    if not BUMPER_DIR.exists():
        print("No music_bumpers directory found.")
        return
    print("Track Intro Status")
    print("-" * 50)
    for show_dir in sorted(BUMPER_DIR.iterdir()):
        if not show_dir.is_dir():
            continue
        show_id = show_dir.name
        bumpers = [
            f for f in show_dir.iterdir()
            if f.suffix.lower() in AUDIO_SUFFIXES
            and not (f.is_symlink() and not f.exists())
        ]
        intro_dir = INTRO_DIR / show_id
        intros = len(list(intro_dir.glob("*_intro.wav"))) if intro_dir.exists() else 0
        pct = f"{intros}/{len(bumpers)}" if bumpers else "0/0"
        print(f"  {show_id:<20} {pct} intros generated")


def main() -> int:
    parser = argparse.ArgumentParser(description="RGNRD-FM Track Intro Generator")
    parser.add_argument("--show", metavar="SHOW_ID", help="Generate intros for one show")
    parser.add_argument("--all", action="store_true", help="Generate intros for all shows")
    parser.add_argument("--status", action="store_true", help="Show intro coverage stats")
    parser.add_argument("--force", action="store_true", help="Regenerate even if intro exists")
    args = parser.parse_args()

    if args.status:
        cmd_status()
        return 0

    if args.show:
        gen, skip = generate_for_show(args.show, force=args.force)
        print(f"\n{args.show}: {gen} generated, {skip} already existed")
        return 0

    if args.all or not args.show:
        if not BUMPER_DIR.exists():
            print("No music_bumpers directory found.")
            return 1
        total_gen = total_skip = 0
        for show_dir in sorted(BUMPER_DIR.iterdir()):
            if show_dir.is_dir():
                gen, skip = generate_for_show(show_dir.name, force=args.force)
                total_gen += gen
                total_skip += skip
        print(f"\nTotal: {total_gen} generated, {total_skip} already existed")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
