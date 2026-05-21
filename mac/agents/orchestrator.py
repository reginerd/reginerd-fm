#!/usr/bin/env python3
"""orchestrator.py — Nightly content batch coordinator for RGNRD-FM.

Runs the full pipeline for tomorrow's scheduled blocks:
  curator → researcher → scriptwriter → narrator

Usage:
    uv run python mac/agents/orchestrator.py --nightly
    uv run python mac/agents/orchestrator.py --nightly --dry-run
    uv run python mac/agents/orchestrator.py --block prime_time
    uv run python mac/agents/orchestrator.py --block prime_time --date 2026-05-23
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))

SCHEDULE_PATH = PROJECT_ROOT / "config" / "schedule.yaml"
GENRES_CONFIG = PROJECT_ROOT / "config" / "genres.yaml"
BLOCKS_CONFIG = PROJECT_ROOT / "config" / "blocks.yaml"
MANIFEST_DIR = PROJECT_ROOT / "output" / "manifests"
SCRIPTS_DIR = Path.home() / "life-os" / "life-os" / "Projects" / "reginerd.fm" / "Scripts"


def _load_yaml(path: Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _get_tomorrow_airings(target_date: date) -> list[tuple[str, datetime]]:
    """Return [(show_id, airing_start)] for all unique shows on target_date."""
    try:
        from schedule import load_schedule, slot_key
        schedule = load_schedule(SCHEDULE_PATH)
        start_of_day = datetime.combine(target_date, datetime.min.time())
        airings = schedule.next_airings(now=start_of_day, count=20, horizon_hours=25)
        seen: dict[str, datetime] = {}
        result = []
        for show_id, start_dt in airings:
            if start_dt.date() == target_date and show_id not in seen:
                seen[show_id] = start_dt
                result.append((show_id, start_dt))
        return result
    except Exception as e:
        print(f"[orchestrator] Could not read schedule: {e}", file=sys.stderr)
        return []


def _all_rendered(block: str, target_date: date) -> bool:
    """Return True if all scripts for this block/date already have status: rendered."""
    script_dir = SCRIPTS_DIR / target_date.isoformat()
    if not script_dir.exists():
        return False
    scripts = list(script_dir.glob(f"*_{block}_*.md"))
    if not scripts:
        return False
    for s in scripts:
        content = s.read_text(encoding="utf-8", errors="ignore")
        if "status: rendered" not in content:
            return False
    return True


def _load_context(target_date: date) -> dict:
    """Assemble the shared context bundle for all scriptwriters."""
    dt = datetime.combine(target_date, datetime.min.time())
    context: dict = {
        "date": target_date.isoformat(),
        "day_of_week": dt.strftime("%A"),
        "is_friday": dt.weekday() == 4,
        "is_saturday": dt.weekday() == 5,
        "is_sunday": dt.weekday() == 6,
        "weather": "",
        "news": "",
        "lastfm": None,
    }
    try:
        from content_generator import lastfm_context
        context["lastfm"] = lastfm_context.load()
    except Exception as e:
        print(f"[orchestrator] Last.fm context unavailable: {e}", file=sys.stderr)
    try:
        from content_generator.weather_context import load as load_weather, format_for_prompt as fmt_weather
        context["weather"] = fmt_weather(load_weather())
    except Exception as e:
        print(f"[orchestrator] Weather context unavailable: {e}", file=sys.stderr)
    try:
        from content_generator.news_context import load as load_news, format_for_prompt as fmt_news
        context["news"] = fmt_news(load_news())
    except Exception as e:
        print(f"[orchestrator] News context unavailable: {e}", file=sys.stderr)
    return context


def run_block(
    block: str,
    target_date: date,
    slot: str,
    context: dict,
    dry_run: bool = False,
) -> dict:
    """Run the full pipeline for one block. Returns result dict."""
    result: dict = {"block": block, "slot": slot, "segments": 0, "error": None, "artists": []}

    if not dry_run and _all_rendered(block, target_date):
        print(f"[orchestrator] {block} — all scripts already rendered, skipping")
        result["segments"] = -1  # sentinel for "skipped"
        return result

    # Step 1: Curator
    print(f"\n[orchestrator] ── {block} ── curator")
    manifest_path = MANIFEST_DIR / f"{block}_{target_date.isoformat()}.json"
    try:
        from agents.curator import build_manifest
        manifest = build_manifest(block, target_date, dry_run=dry_run)
        if manifest is None:
            result["error"] = "curator returned no manifest"
            return result
        result["artists"] = list({t["artist"] for t in manifest.get("tracks", [])})
    except Exception as e:
        result["error"] = f"curator: {e}"
        return result

    # Step 2: Researcher
    print(f"[orchestrator] ── {block} ── researcher")
    try:
        from agents.researcher import run_for_manifest
        if not dry_run and manifest_path.exists():
            run_for_manifest(manifest_path, force=False)
    except Exception as e:
        print(f"[orchestrator] researcher soft-failed for {block}: {e}", file=sys.stderr)
        # Fail-open: continue without wiki context

    # Step 3: Scriptwriter
    print(f"[orchestrator] ── {block} ── scriptwriter")
    try:
        from agents.scriptwriter import generate_scripts
        scripts = generate_scripts(manifest, slot, context, dry_run=dry_run)
        result["segments"] = len(scripts)
    except Exception as e:
        result["error"] = f"scriptwriter: {e}"
        return result

    # Step 4: Narrator (skip if ElevenLabs not configured or quota hit)
    print(f"[orchestrator] ── {block} ── narrator")
    try:
        from agents.narrator import render_date
        if not dry_run:
            ok, fail = render_date(target_date, block=block, force=False)
            if fail > 0:
                print(f"[orchestrator] narrator: {fail} WAVs failed for {block}", file=sys.stderr)
    except Exception as e:
        print(f"[orchestrator] narrator soft-failed for {block}: {e}", file=sys.stderr)
        # Fail-open: scripts are written even if narrator fails

    return result


def run_nightly(target_date: date | None = None, dry_run: bool = False) -> list[dict]:
    """Run the full pipeline for all blocks scheduled for target_date."""
    if target_date is None:
        target_date = date.today() + timedelta(days=1)

    print(f"[orchestrator] Nightly batch for {target_date.isoformat()}{' (dry-run)' if dry_run else ''}")

    # REG-140: Refresh Plex music queues before curating
    if not dry_run:
        try:
            import subprocess
            print("[orchestrator] Refreshing Plex music queues (plex_music_feeder --all --full)...")
            subprocess.run(
                ["uv", "run", "python", "mac/plex_music_feeder.py", "--all", "--full"],
                cwd=PROJECT_ROOT, capture_output=True, timeout=600
            )
            print("[orchestrator] plex_music_feeder refresh done")
        except Exception as e:
            print(f"[orchestrator] plex_music_feeder refresh failed (non-fatal): {e}", file=sys.stderr)

    context = _load_context(target_date)
    airings = _get_tomorrow_airings(target_date)

    if not airings:
        # Fallback: run all blocks from genres.yaml without schedule slot info
        print("[orchestrator] No schedule found — falling back to genres.yaml blocks")
        genres_cfg = _load_yaml(GENRES_CONFIG)
        blocks = list(genres_cfg.get("show_genres", {}).keys())
        airings = [
            (block, datetime.combine(target_date, datetime.min.time()))
            for block in blocks
        ]

    from schedule import slot_key

    results = []
    for show_id, airing_start in airings:
        slot = slot_key(airing_start)
        try:
            r = run_block(show_id, target_date, slot, context, dry_run=dry_run)
        except Exception as e:
            r = {"block": show_id, "slot": slot, "segments": 0, "error": str(e)}
            print(f"[orchestrator] UNHANDLED ERROR {show_id}: {e}", file=sys.stderr)
        results.append(r)

    # Post-batch steps
    if not dry_run:
        try:
            import subprocess
            subprocess.run(
                ["uv", "run", "python", "mac/track_intro_gen.py", "--new"],
                cwd=PROJECT_ROOT, capture_output=True, timeout=300
            )
            print("[orchestrator] track_intro_gen --new done")
        except Exception as e:
            print(f"[orchestrator] track_intro_gen failed: {e}", file=sys.stderr)

        try:
            from agents.show_card_gen import generate_show_card
            generate_show_card(target_date)
        except Exception as e:
            print(f"[orchestrator] show_card_gen failed: {e}", file=sys.stderr)

        try:
            from agents.slack_notifier import notify_batch_complete
            notify_batch_complete(results)
        except Exception:
            pass

        try:
            from content_generator.ledger import append_event
            ok_blocks = [r for r in results if not r.get("error") and r.get("segments", 0) >= 0]
            append_event({
                "type": "nightly_batch",
                "date": target_date.isoformat(),
                "blocks_ok": len(ok_blocks),
                "blocks_total": len(results),
                "segments_total": sum(r.get("segments", 0) for r in results if not r.get("error")),
            })
        except Exception:
            pass

    # Summary
    print("\n[orchestrator] ── Summary ──")
    for r in results:
        status = "✓" if not r.get("error") else "✗"
        segs = r.get("segments", 0)
        err = r.get("error", "")
        line = f"  {status} {r['block']} ({r['slot']}) {segs} segments"
        if err:
            line += f" — {err}"
        print(line)

    return results


def run_single_block(
    block: str,
    target_date: date | None = None,
    dry_run: bool = False,
) -> dict:
    """Run pipeline for one block only."""
    if target_date is None:
        target_date = date.today() + timedelta(days=1)

    context = _load_context(target_date)

    # Find slot from schedule
    airings = _get_tomorrow_airings(target_date)
    airing_start = None
    for show_id, start_dt in airings:
        if show_id == block:
            airing_start = start_dt
            break

    if airing_start is None:
        airing_start = datetime.combine(target_date, datetime.min.time())
        print(f"[orchestrator] {block} not in tomorrow's schedule, using midnight as slot")

    from schedule import slot_key
    slot = slot_key(airing_start)

    return run_block(block, target_date, slot, context, dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(description="RGNRD-FM nightly content batch")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--nightly", action="store_true", help="Run all blocks for tomorrow")
    group.add_argument("--block", help="Run a single block")
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: tomorrow)")
    parser.add_argument("--dry-run", action="store_true", help="Generate content but don't write to disk")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else None

    if args.nightly:
        results = run_nightly(target_date=target_date, dry_run=args.dry_run)
        errors = [r for r in results if r.get("error")]
        sys.exit(1 if errors else 0)
    else:
        r = run_single_block(args.block, target_date=target_date, dry_run=args.dry_run)
        sys.exit(1 if r.get("error") else 0)


if __name__ == "__main__":
    main()
