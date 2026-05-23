#!/usr/bin/env python3
"""show_card_gen.py — Daily markdown show cards for RGNRD-FM.

Produces:
  ~/life-os/life-os/Projects/reginerd.fm/Show Cards/YYYY-MM-DD.md          (index)
  ~/life-os/life-os/Projects/reginerd.fm/Show Cards/YYYY-MM-DD-morning.md  (per block)
  ...etc

Called automatically at the end of each nightly orchestrator batch.

Usage:
    uv run python mac/agents/show_card_gen.py --date 2026-05-20
    uv run python mac/agents/show_card_gen.py  # defaults to tomorrow
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))

MANIFEST_DIR = PROJECT_ROOT / "output" / "manifests"
BUMPERS_DIR = PROJECT_ROOT / "output" / "music_bumpers"
PLAYLISTS_DIR = PROJECT_ROOT / "output" / "playlists"
VAULT_RGNRD = Path.home() / "life-os" / "life-os" / "Projects" / "reginerd.fm"
SHOW_CARDS_DIR = VAULT_RGNRD / "Show Cards"
SCRIPTS_DIR = VAULT_RGNRD / "Scripts"

# Artists that get a sequencing priority boost — appear earlier and more often
PRIORITY_ARTISTS = {
    "young dolph", "key glock", "kendrick lamar", "tyler the creator",
    "tyler, the creator", "kanye west", "ye",
}

AUDIO_SUFFIXES = {".flac", ".mp3", ".wav", ".aac", ".m4a", ".ogg"}

BLOCK_LABELS = {
    "morning":    "Morning",
    "midday":     "Midday",
    "prime_time": "Prime Time",
    "wind_down":  "Wind Down",
    "late_night": "Late Night",
}

BLOCK_TIMES = {
    "morning":    "6am–10am",
    "midday":     "10am–3pm",
    "prime_time": "3pm–8pm",
    "wind_down":  "8pm–10pm",
    "late_night": "10pm–6am",
}

BLOCK_MINUTES = {
    "morning": 240,
    "midday": 300,
    "prime_time": 300,
    "wind_down": 120,
    "late_night": 480,
}

BLOCK_ORDER = ["morning", "midday", "prime_time", "wind_down", "late_night"]

BLOCK_SLUG = {
    "morning": "morning",
    "midday": "midday",
    "prime_time": "prime-time",
    "wind_down": "wind-down",
    "late_night": "late-night",
}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm: dict = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"')
    return fm, parts[2].strip()


def _first_sentence(body: str) -> str:
    body = body.strip()
    match = re.search(r'[.!?](?:\s|$)', body)
    if match:
        snippet = body[:match.end()].strip()
    else:
        snippet = body[:120].strip()
    if len(snippet) > 120:
        snippet = snippet[:117] + "..."
    return snippet


def _parse_bumper_name(stem: str) -> tuple[str, str]:
    if "__" in stem:
        artist, _, title = stem.partition("__")
    else:
        artist, title = stem, ""
    artist = re.sub(r"\s+", " ", artist.replace("_", " ")).strip()
    title = re.sub(r"\s+", " ", title.replace("_", " ")).strip()
    return artist, title


def _load_scripts_for_block(target_date: date, block: str) -> list[dict]:
    script_dir = SCRIPTS_DIR / target_date.isoformat()
    if not script_dir.exists():
        return []
    scripts = sorted(script_dir.glob(f"*_{block}_*.md"))
    result = []
    for s in scripts:
        text = s.read_text(encoding="utf-8", errors="ignore")
        fm, body = _parse_frontmatter(text)
        seq_match = re.match(r"(\d+)_", s.name)
        result.append({
            "seq": int(seq_match.group(1)) if seq_match else 99,
            "type": fm.get("type", "?"),
            "track": fm.get("track", ""),
            "artist": fm.get("artist", "").strip("[]"),
            "preview": _first_sentence(body),
            "status": fm.get("status", "pending"),
        })
    return sorted(result, key=lambda x: x["seq"])


def _load_full_tracklist(block: str, manifest: dict | None = None) -> list[dict]:
    """Return track dicts from the manifest pool or bumpers directory.

    If manifest has a 'pool' key, use it (preferred).
    Otherwise fall back to the music_bumpers glob (backward compat).

    Returns list of dicts with: artist, title, path (optional),
    bpm, energy, brightness, score, lastfm_tags.
    """
    # Prefer manifest pool
    if manifest is not None and manifest.get("pool"):
        result = []
        for t in manifest["pool"]:
            nas_path = t.get("nas_path") or t.get("plex_file", "")
            path = Path(nas_path) if nas_path else None
            result.append({
                "artist": t.get("artist", ""),
                "title": t.get("title", ""),
                "path": path,
                "bpm": t.get("bpm"),
                "energy": t.get("energy"),
                "brightness": t.get("brightness"),
                "score": t.get("score", 0.0),
                "lastfm_tags": t.get("lastfm_tags", []),
                "plex_rating": t.get("plex_rating", 0.0),
            })
        return sorted(result, key=lambda x: (x["artist"].lower(), x["title"].lower()))

    # Fallback: read from bumpers directory
    block_dir = BUMPERS_DIR / block
    if not block_dir.exists():
        return []
    result = []
    for f in block_dir.iterdir():
        if (f.is_file() or f.is_symlink()) and f.suffix.lower() in AUDIO_SUFFIXES:
            artist, title = _parse_bumper_name(f.stem)
            result.append({
                "artist": artist,
                "title": title,
                "path": f,
                "bpm": None,
                "energy": None,
                "brightness": None,
                "score": 0.0,
                "lastfm_tags": [],
                "plex_rating": 0.0,
            })
    return sorted(result, key=lambda x: (x["artist"].lower(), x["title"].lower()))


def _sinusoidal_arc_sequence(tracks: list[dict], period: int = 16) -> list[dict]:
    """Sequence tracks with DJ-style ebb-and-flow energy arc."""
    if not tracks:
        return []

    energies = [t.get("energy") or 0 for t in tracks]
    e_min = min(energies) if energies else 0
    e_max = max(energies) if energies else 1
    e_span = (e_max - e_min) or 1e-9

    def norm_energy(t: dict) -> float:
        e = t.get("energy")
        if e is None:
            return 0.5
        return (e - e_min) / e_span

    remaining = list(tracks)
    result = []

    for i in range(len(tracks)):
        # Sinusoidal target oscillates between ~0.1 and ~0.9
        target = 0.5 + 0.4 * math.sin(2 * math.pi * i / period)
        best = min(remaining, key=lambda t: abs(norm_energy(t) - target))
        result.append(best)
        remaining.remove(best)

    return result


def _load_sequenced_tracklist(block: str, manifest: dict | None = None) -> list[dict]:
    """Return tracks from pool, sequenced by sinusoidal energy arc."""
    full = _load_full_tracklist(block, manifest)
    if not full:
        return []

    # Build a lookup: (artist, title) → manifest metadata for enrichment
    manifest_meta: dict[tuple[str, str], dict] = {}
    for t in (manifest or {}).get("tracks", []):
        key = (t.get("artist", "").lower(), t.get("title", "").lower())
        manifest_meta[key] = t

    # Enrich with manifest metadata where available (fills in tags/rating for fallback tracks)
    enriched = []
    for t in full:
        key = (t["artist"].lower(), t["title"].lower())
        meta = manifest_meta.get(key, {})
        tags = t.get("lastfm_tags") or meta.get("lastfm_tags", [])
        rating = t.get("plex_rating") or meta.get("plex_rating", 5) or 5
        score = t.get("score") or 0.0
        # Priority artist boost: appear earlier in sequence via score bump
        is_priority = t["artist"].lower() in PRIORITY_ARTISTS
        effective_score = score + (1.5 if is_priority else 0.0)
        enriched.append({
            "artist": t["artist"],
            "title": t["title"],
            "path": t.get("path"),
            "tags": tags,
            "rating": rating,
            "energy": t.get("energy"),
            "score": effective_score,
            "bpm": t.get("bpm"),
            "brightness": t.get("brightness"),
        })

    # Sort by score descending so high-priority tracks get first pick in arc
    enriched.sort(key=lambda t: -t["score"])

    # Apply sinusoidal energy arc
    sequenced_arc = _sinusoidal_arc_sequence(enriched, period=16)

    # Apply artist spacing: no same artist within 3 consecutive tracks
    sequenced: list[dict] = []
    remaining = sequenced_arc[:]
    while remaining:
        placed = False
        for i, candidate in enumerate(remaining):
            recent = [e["artist"].lower() for e in sequenced[-3:]]
            if candidate["artist"].lower() not in recent:
                sequenced.append(candidate)
                remaining.pop(i)
                placed = True
                break
        if not placed:
            sequenced.append(remaining.pop(0))  # force it if spacing impossible

    return sequenced


def _block_track_budget(block: str, n_breaks: int) -> int:
    """Estimate how many music tracks fit in one block airing."""
    try:
        from content_generator.audio_features import get_duration
        block_dir = BUMPERS_DIR / block
        if not block_dir.exists():
            raise ValueError("no dir")
        durations = []
        for f in list(block_dir.iterdir())[:50]:  # sample up to 50 for speed
            if (f.is_file() or f.is_symlink()) and f.suffix.lower() in AUDIO_SUFFIXES:
                d = get_duration(f)
                if d:
                    durations.append(d)
        if not durations:
            raise ValueError("no durations")
        avg_sec = sum(durations) / len(durations)
    except Exception:
        avg_sec = 210  # ~3.5 min fallback

    available_min = BLOCK_MINUTES.get(block, 240)
    music_min = max(0, available_min - (n_breaks * 2))
    return max(1, int((music_min * 60) / avg_sec))


def _generate_run_of_show(block: str, target_date: date, manifest: dict) -> list[dict]:
    """Build interleaved music+talk sequence for the run of show."""
    scripts = _load_scripts_for_block(target_date, block)
    active_scripts = [s for s in scripts if s["type"] != "track_outro"]

    # Identify tracks specifically introduced by track_intro scripts
    setlist: dict[tuple[str, str], dict] = {}
    for s in active_scripts:
        if s["type"] == "track_intro" and s.get("artist") and s.get("track"):
            key = (s["artist"].lower(), s["track"].lower())
            setlist[key] = s

    sequenced = _load_sequenced_tracklist(block, manifest)
    budget = _block_track_budget(block, len(active_scripts))

    # Remove setlist tracks from the general pool
    pool = [
        t for t in sequenced
        if (t["artist"].lower(), t["title"].lower()) not in setlist
    ][:budget]

    bumper_idx = 0
    entries = []

    # Lead-in: fixed 2 tracks
    for _ in range(2):
        if bumper_idx < len(pool):
            entries.append({"type": "music", **pool[bumper_idx]})
            bumper_idx += 1

    # Interleave talk breaks with music
    for script in active_scripts:
        entries.append({"type": "talk", **script})
        if script["type"] == "track_intro" and script.get("artist") and script.get("track"):
            entries.append({
                "type": "music",
                "artist": script["artist"],
                "title": script["track"],
                "path": None,
                "tags": [],
                "rating": 0,
                "energy": None,
            })
            fill = 2
        else:
            fill = 3
        for _ in range(fill):
            if bumper_idx < len(pool):
                entries.append({"type": "music", **pool[bumper_idx]})
                bumper_idx += 1

    # Fill remaining pool tracks for full block coverage
    while bumper_idx < len(pool):
        entries.append({"type": "music", **pool[bumper_idx]})
        bumper_idx += 1

    return entries


def _write_block_show_card(block: str, target_date: date, manifest: dict) -> Path:
    """Write the per-block show card file. Returns output path."""
    date_dir = SHOW_CARDS_DIR / target_date.isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    slug = BLOCK_SLUG.get(block, block)
    output_path = date_dir / f"{target_date.isoformat()}-{slug}.md"

    label = BLOCK_LABELS.get(block, block)
    time_str = BLOCK_TIMES.get(block, "")
    scripts = _load_scripts_for_block(target_date, block)
    rendered = sum(1 for s in scripts if s["status"] == "rendered")
    total = len(scripts)
    wav_status = f"{rendered}/{total} rendered" + (" ✓" if rendered == total and total > 0 else "")

    # Pool summary from manifest
    pool_tracks = manifest.get("pool", [])
    pool_size = len(pool_tracks)
    if pool_size == 0:
        # Fallback to bumpers count
        full_tracks = _load_full_tracklist(block, manifest)
        pool_size = len(full_tracks)

    # Top artists by count in pool
    artist_counts = Counter(
        t.get("artist", "") for t in pool_tracks
    )
    top_artists = ", ".join(f"{a} ({n})" for a, n in artist_counts.most_common(5))
    pool_summary = f"{pool_size} tracks · {top_artists}" if top_artists else f"{pool_size} tracks"

    lines = [
        "---",
        f"tags: [reginerd-fm, show-card, radio, {slug}]",
        f"date: {target_date.isoformat()}",
        f"block: {block}",
        "---",
        "",
        f"# {label} — {target_date.strftime('%A, %B %-d, %Y')}",
        f"{time_str} · {pool_summary} · {total} breaks · {wav_status}",
        "",
    ]

    # Run of show
    run = _generate_run_of_show(block, target_date, manifest)

    # Write playlist JSON for feeder to follow
    if run:
        PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
        playlist_entries = []
        for entry in run:
            if entry["type"] == "music" and entry.get("path"):
                playlist_entries.append({
                    "type": "music",
                    "path": str(entry["path"]),
                    "artist": entry.get("artist", ""),
                    "title": entry.get("title", ""),
                })
            elif entry["type"] == "talk":
                playlist_entries.append({"type": "talk"})
        playlist_path = PLAYLISTS_DIR / f"{block}_{target_date.isoformat()}.json"
        playlist_path.write_text(json.dumps({
            "block": block,
            "date": target_date.isoformat(),
            "entries": playlist_entries,
        }, indent=2))

    if run:
        lines.append("## Run of Show")
        lines.append("")
        for i, entry in enumerate(run, 1):
            if entry["type"] == "music":
                artist = entry.get("artist", "")
                title = entry.get("title", "")
                lines.append(f"{i}. 🎵 {artist} — {title}" if title else f"{i}. 🎵 {artist}")
            else:
                seg_label = entry.get("type", "").replace("_", " ")
                track_note = f" · {entry.get('artist', '')} / {entry.get('track', '')}" if entry.get("track") else ""
                preview = f' — "{entry.get("preview", "")}"' if entry.get("preview") else ""
                lines.append(f"{i}. 🎤 **{seg_label}**{track_note}{preview}")
        lines.append("")

    # Break details
    break_scripts = [s for s in scripts if s["type"] not in ("track_outro",)]
    if break_scripts:
        lines.append(f"## Breaks ({len(break_scripts)})")
        lines.append("")
        for s in break_scripts:
            seg_label = s["type"].replace("_", " ")
            track_note = f" · {s['artist']} / {s['track']}" if s["track"] else ""
            preview = f' — "{s["preview"]}"' if s["preview"] else ""
            status_icon = "✓" if s["status"] == "rendered" else "○"
            lines.append(f"- {status_icon} `{s['seq']:02d}` {seg_label}{track_note}{preview}")
        lines.append("")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def generate_show_card(target_date: date) -> Path:
    """Generate index + per-block show cards. Returns index path."""
    date_dir = SHOW_CARDS_DIR / target_date.isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    index_path = date_dir / f"{target_date.isoformat()}.md"

    # Load manifests
    manifests: dict[str, dict] = {}
    for block in BLOCK_ORDER:
        manifest_path = MANIFEST_DIR / f"{block}_{target_date.isoformat()}.json"
        if manifest_path.exists():
            manifests[block] = json.loads(manifest_path.read_text())

    # Write per-block files
    block_files: dict[str, Path] = {}
    for block in BLOCK_ORDER:
        if block not in manifests:
            continue
        block_files[block] = _write_block_show_card(block, target_date, manifests[block])
        print(f"[show_card_gen] {block_files[block]}")

    # Write index
    date_str = target_date.strftime("%A, %B %-d, %Y")
    index_lines = [
        "---",
        "tags: [reginerd-fm, show-card, radio]",
        f"date: {target_date.isoformat()}",
        "---",
        "",
        f"# RGNRD-FM — {date_str}",
        "",
    ]

    if not manifests:
        index_lines.append("_No manifests found for this date._")
    else:
        index_lines += [
            "| Block | Time | Pool | Breaks | WAVs |",
            "|---|---|---|---|---|",
        ]
        for block in BLOCK_ORDER:
            if block not in manifests:
                continue
            slug = BLOCK_SLUG.get(block, block)
            label = BLOCK_LABELS.get(block, block)
            time_str = BLOCK_TIMES.get(block, "")
            scripts = _load_scripts_for_block(target_date, block)
            rendered = sum(1 for s in scripts if s["status"] == "rendered")
            total = len(scripts)
            pool_tracks = manifests[block].get("pool", [])
            pool_count = len(pool_tracks) if pool_tracks else len(_load_full_tracklist(block, manifests[block]))
            wav_cell = f"{rendered}/{total}" + (" ✓" if rendered == total and total > 0 else "")
            link = f"[[{target_date.isoformat()}-{slug}\\|{label}]]"
            index_lines.append(f"| {link} | {time_str} | {pool_count} | {total} | {wav_cell} |")

    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    print(f"[show_card_gen] index → {index_path}")
    return index_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily RGNRD-FM show cards")
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: tomorrow)")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today() + timedelta(days=1)
    index = generate_show_card(target_date)
    print(index.read_text())


if __name__ == "__main__":
    main()
