#!/usr/bin/env python3
"""narrator.py — Render Obsidian DJ scripts to WAV via ElevenLabs.

Reads script markdown files written by scriptwriter.py, calls ElevenLabs TTS,
and writes WAVs to the path specified in each script's audio_output frontmatter.
Idempotent: skips scripts with status: rendered.

Usage:
    uv run python mac/agents/narrator.py --date 2026-05-22
    uv run python mac/agents/narrator.py --block prime_time --date 2026-05-22
    uv run python mac/agents/narrator.py --script path/to/script.md
    uv run python mac/agents/narrator.py --date 2026-05-22 --force
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))

SCRIPTS_DIR = Path.home() / "life-os" / "life-os" / "Projects" / "reginerd.fm" / "Scripts"
TTS_CACHE_DIR = PROJECT_ROOT / "output" / "tts_cache"

_CACHEABLE_TYPES = {"track_intro", "track_outro", "deep_dive"}


def _cache_path(segment_type: str, artist: str, title: str, voice: str) -> Path:
    artist_slug = re.sub(r"[^\w]", "_", artist.lower().strip())[:30]
    title_slug = re.sub(r"[^\w]", "_", title.lower().strip())[:30]
    return TTS_CACHE_DIR / segment_type / f"{voice}__{artist_slug}__{title_slug}.mp3"


def _check_tts_cache(segment_type: str, artist: str, title: str, voice: str) -> Path | None:
    p = _cache_path(segment_type, artist, title, voice)
    return p if p.exists() else None


def _store_tts_cache(audio_output: Path, segment_type: str, artist: str, title: str, voice: str) -> None:
    try:
        dest = _cache_path(segment_type, artist, title, voice)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audio_output, dest)
    except Exception:
        pass


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split script into (frontmatter dict, body text)."""
    import yaml
    if not content.startswith("---"):
        return {}, content.strip()
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content.strip()
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except Exception:
        fm = {}
    return fm, parts[2].strip()


def _update_frontmatter_status(script_path: Path, status: str) -> None:
    """Update status field in script frontmatter in-place."""
    content = script_path.read_text(encoding="utf-8")
    today = date.today().isoformat()
    content = re.sub(r"^status:.*$", f"status: {status}", content, flags=re.MULTILINE)
    if status == "rendered":
        if "rendered_at:" in content:
            content = re.sub(r"^rendered_at:.*$", f"rendered_at: {today}", content, flags=re.MULTILINE)
        else:
            content = content.replace(f"status: {status}", f"status: {status}\nrendered_at: {today}", 1)
    script_path.write_text(content, encoding="utf-8")


def render_script(script_path: Path, force: bool = False) -> bool:
    """Render one script to MP3. Returns True on success."""
    content = script_path.read_text(encoding="utf-8", errors="ignore")
    fm, body = _parse_frontmatter(content)

    if not force and fm.get("status") == "rendered":
        print(f"[narrator] {script_path.name} — already rendered, skipping")
        return True

    audio_output_raw = fm.get("audio_output", "")
    if not audio_output_raw:
        print(f"[narrator] {script_path.name} — no audio_output in frontmatter", file=sys.stderr)
        return False

    audio_output = Path(str(audio_output_raw))
    if not audio_output.is_absolute():
        audio_output = PROJECT_ROOT / audio_output

    if not body:
        print(f"[narrator] {script_path.name} — empty body, skipping", file=sys.stderr)
        return False

    segment_type = fm.get("type", "")
    artist = fm.get("artist", "").strip("[]")
    title = fm.get("track", "")
    voice = "reginerd_clone"

    # Check TTS cache for track-specific segments
    if segment_type in _CACHEABLE_TYPES and artist and title and not force:
        cached = _check_tts_cache(segment_type, artist, title, voice)
        if cached:
            audio_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, audio_output)
            _update_frontmatter_status(script_path, "rendered")
            print(f"[narrator] ✓ {audio_output.name} (from cache)")
            return True

    print(f"[narrator] Rendering {script_path.name} → {audio_output.name}")

    try:
        from content_generator.helpers import preprocess_for_tts, render_single_voice
    except ImportError as e:
        print(f"[narrator] Import error: {e}", file=sys.stderr)
        return False

    text = preprocess_for_tts(body, include_cough=False)

    audio_output.parent.mkdir(parents=True, exist_ok=True)
    ok = render_single_voice(text, audio_output, voice)

    if ok:
        _update_frontmatter_status(script_path, "rendered")
        print(f"[narrator] ✓ {audio_output}")
        if segment_type in _CACHEABLE_TYPES and artist and title:
            _store_tts_cache(audio_output, segment_type, artist, title, voice)
    else:
        print(f"[narrator] ✗ TTS failed for {script_path.name}", file=sys.stderr)
        try:
            from agents.slack_notifier import notify_error
            notify_error("narrator", f"TTS failed: {script_path.name}")
        except Exception:
            pass

    return ok


def render_date(target_date: date, block: str | None = None, force: bool = False) -> tuple[int, int]:
    """Render all pending scripts for a date (optionally filtered by block)."""
    script_dir = SCRIPTS_DIR / target_date.isoformat()
    if not script_dir.exists():
        print(f"[narrator] No scripts found for {target_date.isoformat()}")
        return 0, 0

    pattern = f"*_{block}_*.md" if block else "*.md"
    scripts = sorted(script_dir.glob(pattern))
    if not scripts:
        print(f"[narrator] No scripts match pattern '{pattern}' in {script_dir}")
        return 0, 0

    ok_count = 0
    fail_count = 0
    for script_path in scripts:
        try:
            if render_script(script_path, force=force):
                ok_count += 1
            else:
                fail_count += 1
        except Exception as e:
            print(f"[narrator] ERROR {script_path.name}: {e}", file=sys.stderr)
            fail_count += 1

    return ok_count, fail_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Render DJ scripts to WAV via ElevenLabs")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", help="Render all scripts for this date (YYYY-MM-DD)")
    group.add_argument("--script", help="Render a single script file")
    parser.add_argument("--block", help="Filter by block name (use with --date)")
    parser.add_argument("--force", action="store_true", help="Re-render already-rendered scripts")
    args = parser.parse_args()

    if args.script:
        script_path = Path(args.script)
        if not script_path.exists():
            print(f"[narrator] Script not found: {script_path}", file=sys.stderr)
            sys.exit(1)
        ok = render_script(script_path, force=args.force)
        sys.exit(0 if ok else 1)

    target_date = date.fromisoformat(args.date)
    ok, fail = render_date(target_date, block=args.block, force=args.force)
    print(f"[narrator] Done — {ok} rendered, {fail} failed")
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
