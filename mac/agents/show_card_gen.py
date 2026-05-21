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
import random
import re
import sys
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

# Last.fm tag → energy tier
_HIGH_ENERGY_TAGS = {"upbeat", "energetic", "party", "dance", "hype", "trap", "drill",
                     "hip-hop", "dancehall", "funk", "soul", "r&b", "neo soul"}
_LOW_ENERGY_TAGS = {"chillout", "chill", "mellow", "ambient", "slow", "late night",
                    "acoustic", "ballad", "quiet", "soft"}


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


def _load_full_tracklist(block: str) -> list[tuple[str, str, Path]]:
    """Return sorted (artist, title, path) tuples from the music bumpers pool."""
    block_dir = BUMPERS_DIR / block
    if not block_dir.exists():
        return []
    tracks = []
    for f in block_dir.iterdir():
        if (f.is_file() or f.is_symlink()) and f.suffix.lower() in AUDIO_SUFFIXES:
            artist, title = _parse_bumper_name(f.stem)
            tracks.append((artist, title, f))
    return sorted(tracks, key=lambda x: (x[0].lower(), x[1].lower()))


def _energy_tier(tags: list[str]) -> int:
    """Return 0=low, 1=medium, 2=high based on Last.fm tags."""
    tag_set = {t.lower() for t in tags}
    if tag_set & _HIGH_ENERGY_TAGS:
        return 2
    if tag_set & _LOW_ENERGY_TAGS:
        return 0
    return 1


def _load_sequenced_tracklist(block: str, manifest: dict) -> list[dict]:
    """Return tracks from bumper pool, sequenced by genre + energy arc."""
    full = _load_full_tracklist(block)
    if not full:
        return []

    # Build a lookup: (artist, title) → manifest metadata (lastfm_tags, plex_rating)
    manifest_meta: dict[tuple[str, str], dict] = {}
    for t in manifest.get("tracks", []):
        key = (t.get("artist", "").lower(), t.get("title", "").lower())
        manifest_meta[key] = t

    enriched = []
    for artist, title, path in full:
        key = (artist.lower(), title.lower())
        meta = manifest_meta.get(key, {})
        tags = meta.get("lastfm_tags", [])
        rating = meta.get("plex_rating", 5)
        enriched.append({
            "artist": artist,
            "title": title,
            "path": path,
            "tags": tags,
            "rating": rating or 5,
            "energy": _energy_tier(tags),
        })

    # Interleave across energy tiers for the arc: low → med → high → med → low
    # Priority artists get a +1.5 rating boost so they appear earlier and more often,
    # but are still distributed throughout the show rather than front-loaded.
    def _sort_key(t: dict) -> tuple:
        is_priority = t["artist"].lower() in PRIORITY_ARTISTS
        boost = 1.5 if is_priority else 0.0
        return (-(t["rating"] + boost),)

    low = sorted([t for t in enriched if t["energy"] == 0], key=_sort_key)
    med = sorted([t for t in enriched if t["energy"] == 1], key=_sort_key)
    high = sorted([t for t in enriched if t["energy"] == 2], key=_sort_key)

    # Build a single ordered pool: alternate low/med/high by proportion
    distinct_tiers = [t for t in (low, med, high) if t]
    if len(distinct_tiers) == 1:
        # All tracks in same tier — just use rating order directly
        pool = distinct_tiers[0][:]
    else:
        # Interleave: one from each non-empty tier in arc order, cycling
        arc = []
        iters = {0: iter(low), 1: iter(med), 2: iter(high)}
        nexts = {}
        for k, it in iters.items():
            try:
                nexts[k] = next(it)
            except StopIteration:
                nexts[k] = None
        # Arc pattern: low, med, high, med, low — mapped to tier keys
        pattern = [0, 1, 2, 1, 0]
        p_idx = 0
        while any(v is not None for v in nexts.values()):
            k = pattern[p_idx % len(pattern)]
            p_idx += 1
            if nexts.get(k) is None:
                continue
            arc.append(nexts[k])
            try:
                nexts[k] = next(iters[k])
            except StopIteration:
                nexts[k] = None
        pool = arc

    # Apply artist spacing: no same artist within 3 consecutive tracks
    sequenced: list[dict] = []
    remaining = pool[:]
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
    # These must play immediately after their intro and must NOT appear in the general pool.
    setlist: dict[str, dict] = {}  # (artist.lower, title.lower) → track dict
    for s in active_scripts:
        if s["type"] == "track_intro" and s.get("artist") and s.get("track"):
            key = (s["artist"].lower(), s["track"].lower())
            setlist[key] = s

    sequenced = _load_sequenced_tracklist(block, manifest)
    budget = _block_track_budget(block, len(active_scripts))

    # Remove setlist tracks from the general pool so they only play after their intro
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
        # For track_intro: play the introduced song first, then 2 more from pool
        if script["type"] == "track_intro" and script.get("artist") and script.get("track"):
            entries.append({
                "type": "music",
                "artist": script["artist"],
                "title": script["track"],
                "path": None,
                "tags": [],
                "rating": 0,
                "energy": 1,
            })
            fill = 2
        else:
            fill = 3
        for _ in range(fill):
            if bumper_idx < len(pool):
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
    full_tracks = _load_full_tracklist(block)
    wav_status = f"{rendered}/{total} rendered" + (" ✓" if rendered == total and total > 0 else "")

    lines = [
        "---",
        f"tags: [reginerd-fm, show-card, radio, {slug}]",
        f"date: {target_date.isoformat()}",
        f"block: {block}",
        "---",
        "",
        f"# {label} — {target_date.strftime('%A, %B %-d, %Y')}",
        f"{time_str} · {len(full_tracks)} tracks in pool · {total} breaks · {wav_status}",
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

    # Full library appendix
    if full_tracks:
        lines.append(f"## Full Library ({len(full_tracks)} tracks)")
        lines.append("")
        current_artist = None
        for artist, title, _ in full_tracks:
            if artist != current_artist:
                if current_artist is not None:
                    lines.append("")
                lines.append(f"**{artist}**")
                current_artist = artist
            lines.append(f"- {title}" if title else "- _(unknown title)_")
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
            full_tracks = _load_full_tracklist(block)
            wav_cell = f"{rendered}/{total}" + (" ✓" if rendered == total and total > 0 else "")
            link = f"[[{target_date.isoformat()}-{slug}\\|{label}]]"
            index_lines.append(f"| {link} | {time_str} | {len(full_tracks)} | {total} | {wav_cell} |")

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
