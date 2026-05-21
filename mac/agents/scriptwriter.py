#!/usr/bin/env python3
"""scriptwriter.py — Generate DJ scripts from a curator manifest.

Reads a curator manifest + blocks.yaml segments list and produces one Obsidian
markdown script per segment. Scripts are stored in the Obsidian vault and
referenced by narrator.py for TTS rendering.

Output: ~/life-os/life-os/Projects/reginerd.fm/Scripts/{YYYY-MM-DD}/{NN}_{block}_{type}_{slug}.md

Usage:
    uv run python mac/agents/scriptwriter.py --manifest output/manifests/prime_time_2026-05-22.json --slot 2026-05-22_1500
    uv run python mac/agents/scriptwriter.py --manifest ... --slot ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))

SCRIPTS_DIR = Path.home() / "life-os" / "life-os" / "Projects" / "reginerd.fm" / "Scripts"
WIKI_DIR = Path.home() / "life-os" / "life-os" / "Notes" / "Wiki" / "Artists"
TALK_SEGMENTS_DIR = PROJECT_ROOT / "output" / "talk_segments"
BUMPERS_DIR = PROJECT_ROOT / "output" / "music_bumpers"
BLOCKS_CONFIG = PROJECT_ROOT / "config" / "blocks.yaml"
MAC_CONFIG = PROJECT_ROOT / "mac" / "config.yaml"

_BUMPER_SUFFIXES = {".flac", ".mp3", ".wav", ".m4a", ".ogg"}


def _safe_slug(text: str) -> str:
    return re.sub(r"[^\w]", "_", text.lower().strip())[:30]


def _find_bumper_file(block: str, artist: str, title: str) -> str | None:
    """Case-insensitive search for the bumper file matching artist+title."""
    bumper_dir = BUMPERS_DIR / block
    if not bumper_dir.exists() or not artist or not title:
        return None
    artist_frag = re.sub(r"[^\w]", "_", artist.lower().strip())[:20]
    title_frag = re.sub(r"[^\w]", "_", title.lower().strip())[:20]
    for f in bumper_dir.iterdir():
        if f.suffix.lower() not in _BUMPER_SUFFIXES:
            continue
        stem = re.sub(r"[^\w]", "_", f.stem.lower())
        if artist_frag and title_frag and artist_frag in stem and title_frag in stem:
            return str(f)
    return None


def _load_yaml(path: Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _load_wiki(artist: str) -> str:
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", artist.strip())
    wiki_path = WIKI_DIR / f"{safe_name}.md"
    if not wiki_path.exists():
        return ""
    content = wiki_path.read_text(encoding="utf-8", errors="ignore")
    # Strip frontmatter
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2]
    return content.strip()[:2000]


def _llm_generate(system_prompt: str, user_prompt: str, mac_cfg: dict) -> str:
    llm = mac_cfg.get("llm", {})
    base_url = llm.get("base_url", "http://localhost:11434/v1")
    model = llm.get("model", "qwen2.5:14b")
    temperature = llm.get("temperature", 0.8)
    max_tokens = llm.get("max_tokens", 300)

    # Use native Ollama /api/chat to support think:false for qwen3 models
    ollama_base = base_url.rstrip("/").removesuffix("/v1")
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }).encode()

    req = urllib.request.Request(
        f"{ollama_base}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result["message"]["content"].strip()


def _build_system_prompt(block: str, segment_type: str, context: dict) -> str:
    try:
        from content_generator.persona import build_host_prompt
        base = build_host_prompt("reginerd")
    except Exception:
        base = "You are reginerd, the DJ and voice of RGNRD-FM. Bay Area, direct, no performance."

    day = context.get("day_of_week", "")
    is_friday = context.get("is_friday", False)
    day_note = " It's Friday — energy is higher tonight." if is_friday else ""

    block_moods = {
        "morning": "Morning energy, easing into the day.",
        "midday": "Midday groove, keeping it moving.",
        "prime_time": "Prime time. Fully present. This is when the station is at its best.",
        "wind_down": "Winding down. Contemplative. Making space for the night.",
        "late_night": "Late night. Intimate, slow. The world is asleep.",
    }
    mood = block_moods.get(block, "")

    segment_notes = {
        "show_intro": "Introduce the block. 2-3 sentences. Welcome without being a greeter. Set the vibe.",
        "show_outro": "Close the block. 1-2 sentences. Don't summarize what you just did — leave room.",
        "track_intro": "Introduce the next track. 2-4 sentences max. One interesting thing about the song or artist. Then let it play.",
        "track_outro": "Come back after the track. 1-3 sentences. React to what just played. Can reference the next track if natural.",
        "deep_dive": "Go deeper on the artist or track you just introduced. 5-8 sentences. This is the moment to tell the real story — history, context, what makes it matter.",
        "music_essay": "A short reflection on a theme running through today's playlist. 6-10 sentences. Not a review — a thought.",
        "news_analysis": "Brief commentary on something in the news or world. 3-5 sentences. Only if you have something real to say.",
    }

    weather = context.get("weather", "")
    news = context.get("news", "")
    context_lines = []
    if weather:
        context_lines.append(f"WEATHER: {weather}")
    if news and segment_type in ("show_intro", "news_analysis", "music_essay"):
        context_lines.append(f"NEWS:\n{news}")
    context_block = ("\n" + "\n".join(context_lines)) if context_lines else ""

    return f"""{base}

BLOCK: {block}{' — ' + mood if mood else ''}
DAY: {day}{day_note}{context_block}
SEGMENT TYPE: {segment_type}
TASK: {segment_notes.get(segment_type, 'Write the segment.')}

CRITICAL RULES:
- Write ONLY the spoken words. No stage directions, no [pause] markers, no headers.
- Plain prose. No markdown. No bullet points in the output.
- Write as if speaking to one person who is half-listening. Not a lecture.
- Length: match the task above. Do not pad.
- Never reference being AI or generated."""


def _build_track_user_prompt(
    track: dict,
    segment_type: str,
    context: dict,
    wiki_context: str,
) -> str:
    artist = track.get("artist", "")
    title = track.get("title", "")
    album = track.get("album", "")
    year = track.get("year", "")
    tags = track.get("lastfm_tags", [])
    loved = track.get("lastfm_loved", False)
    top_week = track.get("lastfm_top_week", False)

    personal = []
    if loved:
        personal.append("you've loved this track on Last.fm")
    if top_week:
        personal.append("you've been playing this heavily this week")

    parts = [f"Track: {title} by {artist}"]
    if album:
        parts.append(f"Album: {album}" + (f" ({year})" if year else ""))
    if tags:
        parts.append(f"Tags: {', '.join(tags[:5])}")
    if personal:
        parts.append(f"Personal connection: {'; '.join(personal)}")
    if wiki_context:
        parts.append(f"\nBackground (from your wiki):\n{wiki_context}")

    lastfm_ctx = context.get("lastfm") or {}
    recent = lastfm_ctx.get("recent_tracks", [])
    if recent:
        recent_names = [f"{t.get('track', '')} by {t.get('artist', '')}" for t in recent[:4]]
        parts.append(f"\nRecently in rotation: {', '.join(recent_names)}")

    action = "Introduce" if segment_type == "track_intro" else "Reflect on"
    parts.append(f"\nWrite a {segment_type}: {action} this track.")

    return "\n".join(parts)


def _build_nontrack_user_prompt(
    segment_type: str,
    block: str,
    manifest: dict,
    context: dict,
    preceding_tracks: list[dict],
) -> str:
    tracks = manifest.get("tracks", [])
    track_list = ", ".join(
        f"{t.get('title', '?')} by {t.get('artist', '?')}" for t in tracks[:5]
    )

    lastfm_ctx = context.get("lastfm") or {}
    week_artists = [a.get("artist", "") for a in lastfm_ctx.get("top_artists_week", [])[:4]]

    parts = [f"Block: {block}"]
    if track_list:
        parts.append(f"Today's playlist: {track_list}")
    if week_artists:
        parts.append(f"Your top artists this week: {', '.join(week_artists)}")
    if preceding_tracks:
        prev = preceding_tracks[-1]
        parts.append(f"Just played: {prev.get('title', '?')} by {prev.get('artist', '?')}")

    if segment_type == "show_intro":
        parts.append("Write the show_intro — open the block.")
    elif segment_type == "show_outro":
        parts.append("Write the show_outro — close the block.")
    elif segment_type == "deep_dive" and preceding_tracks:
        prev = preceding_tracks[-1]
        wiki = _load_wiki(prev.get("artist", ""))
        if wiki:
            parts.append(f"\nWiki context for {prev.get('artist', '')}:\n{wiki}")
        parts.append(f"Write a deep_dive going further on {prev.get('artist', '')} or that track.")
    elif segment_type == "music_essay":
        parts.append("Write a music_essay — a brief reflection on a theme from today's playlist.")
    elif segment_type == "news_analysis":
        parts.append("Skip news for now — write a brief thought on the station or today's music instead.")

    return "\n".join(parts)


def _write_script(
    script_dir: Path,
    seq: int,
    block: str,
    segment_type: str,
    track: dict | None,
    body: str,
    audio_output: Path,
    dry_run: bool = False,
) -> Path:
    artist = track.get("artist", "") if track else ""
    title = track.get("title", "") if track else ""
    album = track.get("album", "") if track else ""
    year = track.get("year", "") if track else ""

    slug_parts = [_safe_slug(artist) if artist else "", _safe_slug(title) if title else ""]
    slug = "_".join(p for p in slug_parts if p) or segment_type
    filename = f"{seq:02d}_{block}_{segment_type}_{slug}.md"

    frontmatter_lines = [
        "---",
        f"tags: [radio, script, {block.replace('_', '-')}]",
        f"date: {script_dir.name}",
        f"block: {block}",
        f"type: {segment_type}",
    ]
    if track:
        frontmatter_lines += [
            f'track: "{title}"',
            f'artist: "[[{artist}]]"',
        ]
        if album:
            frontmatter_lines.append(f'album: "{album}"')
        if year:
            frontmatter_lines.append(f"year: {year}")
    frontmatter_lines += [
        f"audio_output: {audio_output}",
        "status: pending",
        "---",
        "",
    ]

    content = "\n".join(frontmatter_lines) + body + "\n"
    script_path = script_dir / filename

    if dry_run:
        print(f"\n--- {filename} ---")
        print(content)
        return script_path

    script_dir.mkdir(parents=True, exist_ok=True)
    script_path.write_text(content, encoding="utf-8")
    return script_path


def generate_scripts(
    manifest: dict,
    slot: str,
    context: dict | None = None,
    dry_run: bool = False,
) -> list[Path]:
    """Generate all scripts for a manifest. Returns list of written script paths."""
    context = context or {}
    mac_cfg = _load_yaml(MAC_CONFIG)
    blocks_cfg = _load_yaml(BLOCKS_CONFIG)

    block = manifest["block"]
    target_date = manifest["date"]
    segments: list[str] = manifest.get("segments", blocks_cfg.get(block, {}).get("segments", []))
    tracks = manifest.get("tracks", [])

    script_dir = SCRIPTS_DIR / target_date
    talk_slot_dir = TALK_SEGMENTS_DIR / block / slot

    written: list[Path] = []
    setlist_entries: list[dict] = []
    track_idx = 0
    preceding_tracks: list[dict] = []

    for seq, segment_type in enumerate(segments):
        is_track_segment = segment_type in ("track_intro", "track_outro")
        track: dict | None = None

        if is_track_segment:
            if track_idx >= len(tracks):
                print(f"[scriptwriter] Not enough tracks for {segment_type} at position {seq}", file=sys.stderr)
                continue
            track = tracks[track_idx]
            if segment_type == "track_intro":
                track_idx += 1

        # Compute audio output path
        if track:
            artist_slug = _safe_slug(track.get("artist", "unknown"))
            title_slug = _safe_slug(track.get("title", "unknown"))
            mp3_name = f"{seq:02d}_{segment_type}_{artist_slug}_{title_slug}.mp3"
        else:
            mp3_name = f"{seq:02d}_{segment_type}.mp3"
        audio_output = talk_slot_dir / mp3_name

        # Build setlist entry — track_intro gets matched to its specific bumper file
        bumper_file: str | None = None
        if segment_type == "track_intro" and track:
            t_artist = track.get("artist", "")
            t_title = track.get("title", "")
            bumper_file = _find_bumper_file(block, t_artist, t_title)
        setlist_entries.append({
            "wav": mp3_name,
            "type": segment_type,
            "artist": track.get("artist", "") if track else "",
            "title": track.get("title", "") if track else "",
            "track_file": bumper_file,
        })

        # Build prompts
        system_prompt = _build_system_prompt(block, segment_type, context)

        if is_track_segment and track:
            wiki_ctx = _load_wiki(track.get("artist", ""))
            user_prompt = _build_track_user_prompt(track, segment_type, context, wiki_ctx)
        else:
            user_prompt = _build_nontrack_user_prompt(
                segment_type, block, manifest, context, preceding_tracks
            )

        # Generate
        try:
            body = _llm_generate(system_prompt, user_prompt, mac_cfg)
        except Exception as e:
            print(f"[scriptwriter] LLM failed for {segment_type}: {e}", file=sys.stderr)
            try:
                from agents.slack_notifier import notify_error
                notify_error("scriptwriter", f"{block} {segment_type}: {e}")
            except Exception:
                pass
            continue

        script_path = _write_script(
            script_dir, seq, block, segment_type, track, body, audio_output, dry_run=dry_run
        )
        written.append(script_path)

        if track:
            preceding_tracks.append(track)
            if segment_type == "track_outro":
                # outro references the same track as the last intro
                track_idx += 1

    if not dry_run and setlist_entries:
        talk_slot_dir.mkdir(parents=True, exist_ok=True)
        setlist_path = talk_slot_dir / "setlist.json"
        setlist_path.write_text(json.dumps(setlist_entries, indent=2))

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate DJ scripts from a curator manifest")
    parser.add_argument("--manifest", required=True, help="Path to curator manifest JSON")
    parser.add_argument("--slot", required=True, help="Airing slot key (YYYY-MM-DD_HHMM)")
    parser.add_argument("--dry-run", action="store_true", help="Print scripts, don't write")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"[scriptwriter] Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text())

    # Minimal context for standalone runs
    now = datetime.now()
    context = {
        "date": manifest.get("date", now.date().isoformat()),
        "day_of_week": now.strftime("%A"),
        "is_friday": now.weekday() == 4,
        "lastfm": None,
    }

    paths = generate_scripts(manifest, args.slot, context, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"[scriptwriter] Done — {len(paths)} scripts written")
        for p in paths:
            print(f"  {p}")


if __name__ == "__main__":
    main()
