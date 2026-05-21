#!/usr/bin/env python3
"""Fail-open Slack notifications for RGNRD-FM pipeline agents.

Requires SLACK_BOT_TOKEN and SLACK_CHANNEL_ID in environment.
All functions swallow errors silently — never crashes the pipeline.
"""

from __future__ import annotations

import os
from typing import Any

try:
    from slack_sdk import WebClient as _WebClient
    _client = _WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
    _SLACK_AVAILABLE = True
except ImportError:
    _client = None  # type: ignore[assignment]
    _SLACK_AVAILABLE = False


def _post(text: str, blocks: list[dict] | None = None) -> None:
    if not _SLACK_AVAILABLE or not _client:
        return
    channel = os.environ.get("SLACK_CHANNEL_ID", "")
    if not channel:
        return
    try:
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        _client.chat_postMessage(**kwargs)
    except Exception:
        pass


def notify_batch_complete(results: list[dict]) -> None:
    """Post a single nightly batch summary with artists of the day."""
    ok = [r for r in results if not r.get("error") and r.get("segments", 0) >= 0]
    fail = [r for r in results if r.get("error")]

    lines = ["*RGNRD-FM — Tonight's lineup is ready*"]

    for r in ok:
        block = r.get("block", "?")
        artists = r.get("artists", [])
        artist_str = ", ".join(artists) if artists else "—"
        lines.append(f"> *{block}:* {artist_str}")

    if fail:
        for r in fail:
            lines.append(f"> ✗ {r['block']}: {r['error']}")

    _post("\n".join(lines))


def notify_error(source: str, message: str) -> None:
    """Post an error alert from any pipeline agent."""
    _post(f"⚠️ *RGNRD-FM error* [{source}]\n> {message[:400]}")


def notify_break_airing(show_id: str, segment_stem: str) -> None:
    """Post when feeder starts playing a talk WAV."""
    _post(f"\U0001f3a4 Now airing: {segment_stem} — {show_id}")
