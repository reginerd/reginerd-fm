#!/usr/bin/env python3
"""researcher.py — Build Obsidian wiki notes for artists in a manifest.

Data sources: Last.fm artist.getInfo + track.getInfo, Wikipedia, Obsidian vault grep.
Uses Qwen14B (Ollama) to synthesize into structured wiki notes.
Skip-if-fresh: regenerates only if wiki file is missing or older than 30 days.

Output: ~/life-os/life-os/Notes/Wiki/Artists/{Artist}.md

Usage:
    uv run python mac/agents/researcher.py --manifest output/manifests/prime_time_2026-05-22.json
    uv run python mac/agents/researcher.py --artist "Gorillaz"
    uv run python mac/agents/researcher.py --manifest ... --force
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))

from content_generator.blog_context import find_writeup as _find_writeup

WIKI_DIR = Path.home() / "life-os" / "life-os" / "Notes" / "Wiki" / "Artists"
VAULT_ROOT = Path.home() / "life-os" / "life-os"
LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"
WIKI_STALE_DAYS = 30


def _safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name.strip())


def _lastfm_call(method: str, **kwargs) -> dict | None:
    api_key = os.environ.get("LASTFM_API_KEY", "").strip()
    if not api_key:
        return None
    params = {"method": method, "api_key": api_key, "format": "json", **kwargs}
    url = LASTFM_API_BASE + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _fetch_artist_info(artist: str) -> str:
    data = _lastfm_call("artist.getInfo", artist=artist, autocorrect="1", lang="en")
    if not data:
        return ""
    bio = data.get("artist", {}).get("bio", {})
    content = bio.get("content", "") or bio.get("summary", "")
    # Strip Last.fm links (<a href...>)
    content = re.sub(r"<a[^>]*>.*?</a>", "", content, flags=re.DOTALL)
    content = re.sub(r"\s+", " ", content).strip()
    similar = data.get("artist", {}).get("similar", {}).get("artist", [])
    if isinstance(similar, list):
        names = [s.get("name", "") for s in similar[:5] if s.get("name")]
        if names:
            content += f"\n\nSimilar artists: {', '.join(names)}"
    return content[:3000]


def _fetch_track_info(artist: str, track: str) -> str:
    data = _lastfm_call("track.getInfo", artist=artist, track=track, autocorrect="1", lang="en")
    if not data:
        return ""
    wiki = data.get("track", {}).get("wiki", {})
    content = wiki.get("content", "") or wiki.get("summary", "")
    content = re.sub(r"<a[^>]*>.*?</a>", "", content, flags=re.DOTALL)
    content = re.sub(r"\s+", " ", content).strip()
    listeners = data.get("track", {}).get("listeners", "")
    playcount = data.get("track", {}).get("playcount", "")
    if listeners or playcount:
        content += f"\n\nListeners: {listeners} | Plays: {playcount}"
    return content[:2000]


def _fetch_wikipedia(artist: str, search_term: str | None = None) -> str:
    """Fetch Wikipedia intro for an artist or custom search term.

    When search_term is provided it is used as the page title query instead of
    artist, which is useful for album lookups (e.g. "Illmatic Nas").
    """
    query_title = search_term if search_term is not None else artist
    try:
        url = (
            "https://en.wikipedia.org/w/api.php?"
            + urllib.parse.urlencode({
                "action": "query",
                "prop": "extracts",
                "exintro": "1",
                "explaintext": "1",
                "redirects": "1",
                "titles": query_title,
                "format": "json",
            })
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            extract = page.get("extract", "")
            if extract and page.get("pageid", -1) != -1:
                return extract[:3000]
    except Exception:
        pass
    return ""


def _grep_vault(artist: str) -> str:
    """Search Obsidian vault for personal notes mentioning this artist."""
    try:
        result = subprocess.run(
            ["grep", "-r", "-l", "--include=*.md", "-i", artist, str(VAULT_ROOT)],
            capture_output=True, text=True, timeout=10
        )
        files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
        # Exclude wiki files themselves and scripts
        files = [
            f for f in files
            if "Notes/Wiki" not in f and "Scripts" not in f and "reginerd-fm" not in f.lower()
        ][:5]
        if not files:
            return ""
        snippets = []
        for fpath in files:
            try:
                content = Path(fpath).read_text(encoding="utf-8", errors="ignore")
                # Grab lines mentioning the artist (case-insensitive)
                matching = [
                    line.strip() for line in content.splitlines()
                    if artist.lower() in line.lower() and line.strip()
                ][:5]
                if matching:
                    rel = Path(fpath).relative_to(VAULT_ROOT)
                    snippets.append(f"From {rel}:\n" + "\n".join(matching))
            except Exception:
                continue
        return "\n\n".join(snippets)[:2000]
    except Exception:
        return ""


def _fetch_album_info(artist: str, album: str) -> str:
    """Fetch Last.fm album wiki content/summary."""
    data = _lastfm_call("album.getInfo", artist=artist, album=album, autocorrect="1", lang="en")
    if not data:
        return ""
    wiki = data.get("album", {}).get("wiki", {})
    content = wiki.get("content", "") or wiki.get("summary", "")
    if not content:
        return ""
    content = re.sub(r"<a[^>]*>.*?</a>", "", content, flags=re.DOTALL)
    content = re.sub(r"\s+", " ", content).strip()
    return content[:2000]


def _fetch_all_plex_tracks() -> list[dict]:
    """Query Plex for all tracks in the music library. Returns list of dicts."""
    import yaml as _yaml
    mac_cfg_path = PROJECT_ROOT / "mac" / "config.yaml"
    mac_cfg = _yaml.safe_load(mac_cfg_path.read_text()) if mac_cfg_path.exists() else {}
    plex_cfg = mac_cfg.get("plex", {})
    host = plex_cfg.get("host", "192.168.1.27")
    port = plex_cfg.get("port", 32400)
    token_env = plex_cfg.get("token_env", "PLEX_TOKEN")
    section = plex_cfg.get("library_section", 1)
    token = os.environ.get(token_env, "").strip()
    if not token:
        print(f"[researcher] ERROR: {token_env} not set", file=sys.stderr)
        return []

    import xml.etree.ElementTree as ET
    url = (
        f"http://{host}:{port}/library/sections/{section}/all"
        f"?type=10&X-Plex-Container-Size=10000&X-Plex-Token={token}"
    )
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        print(f"[researcher] Plex query failed: {e}", file=sys.stderr)
        return []

    tracks = []
    for track in root.iter("Track"):
        artist = track.get("grandparentTitle", "").strip()
        if not artist:
            continue
        tracks.append({
            "artist": artist,
            "album": track.get("parentTitle", "").strip(),
            "title": track.get("title", "").strip(),
            "year": track.get("parentYear", "").strip() or None,
        })
    return tracks


def _ollama_synthesize_album(
    artist: str, album: str, tracks: list[str], sources: dict[str, str]
) -> str:
    """Synthesize album wiki note body using Ollama."""
    import yaml as _yaml
    mac_cfg_path = PROJECT_ROOT / "mac" / "config.yaml"
    mac_cfg = _yaml.safe_load(mac_cfg_path.read_text()) if mac_cfg_path.exists() else {}
    llm = mac_cfg.get("llm", {})
    base_url = llm.get("base_url", "http://localhost:11434/v1")
    model = llm.get("model", "qwen3:14b")

    track_list = ", ".join(f'"{t}"' for t in tracks[:10]) if tracks else "unknown tracks"

    source_block = ""
    if sources.get("lastfm"):
        source_block += f"\n\n## Last.fm\n{sources['lastfm']}"
    if sources.get("wikipedia"):
        source_block += f"\n\n## Wikipedia\n{sources['wikipedia']}"
    if sources.get("tracks"):
        source_block += f"\n\n## Track info\n{sources['tracks']}"
    if sources.get("vault"):
        source_block += f"\n\n## Reggie's personal notes\n{sources['vault']}"

    blog_writeup = _find_writeup(f"{artist} {album}")
    if not blog_writeup:
        blog_writeup = _find_writeup(album)
    if blog_writeup:
        source_block += f"\n\n## Reggie's write-up\n{blog_writeup}"

    has_personal = bool(sources.get("vault") or blog_writeup)

    prompt = f"""You are writing an Obsidian wiki note for RGNRD-FM, a personal 24/7 radio station.
The DJ is reginerd — Bay Area developer, music fan, plain-spoken.
Artist: {artist}
Album: {album}
Tracks: {track_list}

Using only the source material below, write the wiki note body in these exact sections:

## About
2-3 paragraphs on what this album is, when it came out, what it sounds like, and why it matters.

## Production & Sound
1-2 paragraphs on the producers, sonic palette, instruments, and production style.

## Track Highlights
Bullet list of standout tracks with a one-line note on each.

## Cultural Moment
1 paragraph on where this album landed culturally — what was happening, who it spoke to.

{"## Reggie's Take" + chr(10) + "Based on Reggie's personal notes and write-ups — paraphrase what he's said about this album. Use his voice." if has_personal else ""}

Rules:
- Write in plain prose. No markdown headers beyond the sections above.
- Do not make up facts not present in the source material.
- CRITICAL: Only include "## Reggie's Take" if SOURCE MATERIAL contains "## Reggie's personal notes" or "## Reggie's write-up". If absent, omit it entirely. Do not invent Reggie's opinion.

SOURCE MATERIAL:{source_block}

Write the wiki note body now (sections only, no frontmatter):"""

    try:
        ollama_base = base_url.rstrip("/").removesuffix("/v1")
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": {"temperature": 0.5, "num_predict": 1200},
        }).encode()
        req_obj = urllib.request.Request(
            f"{ollama_base}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_obj, timeout=180) as resp:
            result = json.loads(resp.read())
        body = result["message"]["content"].strip()
        if not has_personal:
            body = re.sub(
                r"\n*##\s+Reggie[''`]?s Take\b.*",
                "",
                body,
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()
        return body
    except Exception as e:
        print(f"[researcher] Ollama album synthesis failed: {e}", file=sys.stderr)
        return ""


def research_album(
    artist: str,
    album: str,
    tracks: list[str],
    year: str | None = None,
    force: bool = False,
) -> "Path | None":
    """Research one album and write its wiki file. Returns path or None."""
    album_dir = WIKI_DIR.parent / "Albums" / _safe_filename(artist)
    album_dir.mkdir(parents=True, exist_ok=True)
    wiki_path = album_dir / f"{_safe_filename(album)}.md"

    if not force and _wiki_is_fresh(wiki_path):
        print(f"[researcher] {artist} / {album} — album wiki fresh, skipping")
        return wiki_path

    print(f"[researcher] Researching album: {artist} — {album}...")

    sources: dict[str, str] = {}

    lastfm_info = _fetch_album_info(artist, album)
    if lastfm_info:
        sources["lastfm"] = lastfm_info

    wiki_text = _fetch_wikipedia(artist, search_term=f"{album} {artist}")
    if wiki_text:
        sources["wikipedia"] = wiki_text

    track_infos = []
    for track in tracks[:5]:
        info = _fetch_track_info(artist, track)
        if info:
            track_infos.append(f"### {track}\n{info}")
    if track_infos:
        sources["tracks"] = "\n\n".join(track_infos)

    vault_notes = _grep_vault(f"{artist} {album}")
    if not vault_notes:
        vault_notes = _grep_vault(album)
    if vault_notes:
        sources["vault"] = vault_notes

    if not any(sources.values()):
        print(f"[researcher] No source data for album {artist} / {album} — skipping")
        return None

    body = _ollama_synthesize_album(artist, album, tracks, sources)
    if not body:
        print(f"[researcher] Album synthesis empty for {artist} / {album}", file=sys.stderr)
        return None

    today = date.today().isoformat()
    artist_slug = re.sub(r"[^\w]", "-", artist.lower().strip()).strip("-")
    source_count = sum(1 for v in sources.values() if v)
    year_line = f"year: {year}\n" if year else ""
    frontmatter = (
        f"---\n"
        f"tags: [wiki, album, {artist_slug}]\n"
        f"date: {today}\n"
        f"artist: {artist}\n"
        f"album: {album}\n"
        f"{year_line}"
        f"sources: {source_count}\n"
        f"---\n\n"
    )

    wiki_path.write_text(frontmatter + body + "\n", encoding="utf-8")
    print(f"[researcher] {artist} / {album} → {wiki_path}")
    return wiki_path


def _ollama_synthesize(artist: str, tracks: list[str], sources: dict[str, str]) -> str:
    """Synthesize wiki note body from raw source text using Ollama."""
    import yaml as _yaml
    mac_cfg_path = PROJECT_ROOT / "mac" / "config.yaml"
    mac_cfg = _yaml.safe_load(mac_cfg_path.read_text()) if mac_cfg_path.exists() else {}
    llm = mac_cfg.get("llm", {})
    base_url = llm.get("base_url", "http://localhost:11434/v1")
    model = llm.get("model", "qwen3:14b")

    track_list = ", ".join(f'"{t}"' for t in tracks[:5]) if tracks else "unknown tracks"

    source_block = ""
    if sources.get("lastfm"):
        source_block += f"\n\n## Last.fm\n{sources['lastfm']}"
    if sources.get("wikipedia"):
        source_block += f"\n\n## Wikipedia\n{sources['wikipedia']}"
    if sources.get("tracks"):
        source_block += f"\n\n## Track info\n{sources['tracks']}"
    if sources.get("vault"):
        source_block += f"\n\n## Reggie's personal notes\n{sources['vault']}"
    blog_writeup = _find_writeup(artist)
    if blog_writeup:
        source_block += f"\n\n## Reggie's write-up\n{blog_writeup}"
    if sources.get("audio"):
        source_block += f"\n\n## Audio Analysis (measured)\n{sources['audio']}"

    prompt = f"""You are writing an Obsidian wiki note for RGNRD-FM, a personal 24/7 radio station.
The DJ is reginerd — Bay Area developer, music fan, plain-spoken.
Artist: {artist}
Tracks in today's playlist: {track_list}

Using only the source material below, write the wiki note body in these exact sections:

## Bio
2-3 paragraphs covering the artist's career, origin, era, and musical identity. No bullet points.

## Discography Highlights
Bullet list of key albums with years and a one-line description of each era.

## DJ Talking Points
Bullet list of 4-6 specific anecdotes, facts, or cultural moments reginerd can weave into radio breaks.
Focus on the surprising, the personal, the specific — not generic praise.

## Track Notes
For each track in today's playlist, one paragraph: what it's about, notable production or lyrics, cultural moment.

## Audio Profile
If ## Audio Analysis is in the source material: write 1-2 sentences using the actual BPM range and energy numbers. Characterize the sound (warm, bright, punchy, mellow) based on the brightness value (>3000Hz = bright, <2000Hz = warm). Do not invent numbers not present in the source.

{"## Reggie's Take" + chr(10) + "Based on Reggie's personal notes and write-ups below — paraphrase what he's said about this artist. Use his voice." if (sources.get("vault") or blog_writeup) else ""}

Rules:
- Write in plain prose. No markdown headers beyond the section headers above.
- Do not make up facts not present in the source material.
- Keep each section tight — this is reference material for a DJ, not a Wikipedia article.
- If source material is thin, write what you know and note the gap.
- CRITICAL: Only include the section "## Reggie's Take" if the SOURCE MATERIAL below contains a section explicitly labeled "## Reggie's personal notes" or "## Reggie's write-up". If those sections are absent from the source material, do NOT write "## Reggie's Take" — omit it entirely. Do not invent, infer, or speculate about Reggie's opinion.

SOURCE MATERIAL:{source_block}

Write the wiki note body now (sections only, no frontmatter):"""

    try:
        ollama_base = base_url.rstrip("/").removesuffix("/v1")
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": {"temperature": 0.5, "num_predict": 1200},
        }).encode()
        req_obj = urllib.request.Request(
            f"{ollama_base}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_obj, timeout=180) as resp:
            result = json.loads(resp.read())
        body = result["message"]["content"].strip()
        has_personal = bool(sources.get("vault") or blog_writeup)
        if not has_personal:
            body = re.sub(
                r"\n*##\s+Reggie[''`]?s Take\b.*",
                "",
                body,
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()
        return body
    except Exception as e:
        print(f"[researcher] Ollama synthesis failed: {e}", file=sys.stderr)
        return ""


def _wiki_is_fresh(wiki_path: Path) -> bool:
    if not wiki_path.exists():
        return False
    age_days = (datetime.now() - datetime.fromtimestamp(wiki_path.stat().st_mtime)).days
    return age_days < WIKI_STALE_DAYS


def research_artist(artist: str, tracks: list[str], force: bool = False) -> Path | None:
    """Research one artist and write/update their wiki file. Returns path or None."""
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    wiki_path = WIKI_DIR / f"{_safe_filename(artist)}.md"

    if not force and _wiki_is_fresh(wiki_path):
        print(f"[researcher] {artist} — wiki fresh, skipping")
        return wiki_path

    print(f"[researcher] Researching {artist}...")

    sources: dict[str, str] = {}

    lastfm_bio = _fetch_artist_info(artist)
    if lastfm_bio:
        sources["lastfm"] = lastfm_bio

    wiki_text = _fetch_wikipedia(artist)
    if wiki_text:
        sources["wikipedia"] = wiki_text

    track_infos = []
    for track in tracks[:5]:
        info = _fetch_track_info(artist, track)
        if info:
            track_infos.append(f"### {track}\n{info}")
    if track_infos:
        sources["tracks"] = "\n\n".join(track_infos)

    vault_notes = _grep_vault(artist)
    if vault_notes:
        sources["vault"] = vault_notes

    # Audio profile from analyzed library tracks
    try:
        from content_generator.audio_features import get_artist_features
        audio = get_artist_features(artist)
        if audio:
            bpms = [t["bpm"] for t in audio if t.get("bpm")]
            energies = [t["energy"] for t in audio if t.get("energy")]
            if bpms:
                avg_energy = sum(energies) / len(energies) if energies else 0.0
                sources["audio"] = (
                    f"BPM range: {min(bpms):.0f}–{max(bpms):.0f}. "
                    f"Avg energy: {avg_energy:.3f}. "
                    f"Based on {len(audio)} tracks in Reggie's library."
                )
    except Exception:
        pass

    if not any(sources.values()):
        print(f"[researcher] No source data found for {artist} — skipping")
        return None

    body = _ollama_synthesize(artist, tracks, sources)
    if not body:
        print(f"[researcher] Synthesis returned empty for {artist}", file=sys.stderr)
        return None

    today = date.today().isoformat()
    source_count = sum(1 for v in sources.values() if v)
    frontmatter = (
        f"---\n"
        f"tags: [wiki, artist, music]\n"
        f"artist: {artist}\n"
        f"updated: {today}\n"
        f"sources: {source_count}\n"
        f"---\n\n"
    )

    wiki_path.write_text(frontmatter + body + "\n", encoding="utf-8")
    print(f"[researcher] {artist} → {wiki_path}")


    return wiki_path


def run_for_manifest(manifest_path: Path, force: bool = False) -> list[Path]:
    """Research all unique artists in a manifest. Returns list of written paths."""
    manifest = json.loads(manifest_path.read_text())
    tracks_by_artist: dict[str, list[str]] = {}
    for t in manifest.get("tracks", []):
        artist = t.get("artist", "").strip()
        title = t.get("title", "").strip()
        if artist:
            tracks_by_artist.setdefault(artist, []).append(title)

    results = []
    for artist, track_titles in tracks_by_artist.items():
        try:
            path = research_artist(artist, track_titles, force=force)
            if path:
                results.append(path)
        except Exception as e:
            print(f"[researcher] ERROR {artist}: {e}", file=sys.stderr)
            try:
                from agents.slack_notifier import notify_error
                notify_error("researcher", f"{artist}: {e}")
            except Exception:
                pass
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Obsidian wiki notes for artists")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest", help="Path to curator manifest JSON")
    group.add_argument("--artist", help="Single artist name")
    group.add_argument("--all-library", action="store_true", help="Research all artists+albums in Plex library")
    parser.add_argument("--force", action="store_true", help="Ignore 30-day freshness cache")
    args = parser.parse_args()

    if args.manifest:
        paths = run_for_manifest(Path(args.manifest), force=args.force)
        print(f"[researcher] Done — {len(paths)} wiki notes written")
    elif args.all_library:
        tracks = _fetch_all_plex_tracks()
        if not tracks:
            print("[researcher] No tracks returned from Plex", file=sys.stderr)
            sys.exit(1)

        # Build artists dict: artist -> [titles]
        artists_dict: dict[str, list[str]] = {}
        # Build albums dict: (artist, album) -> (titles, year)
        albums_dict: dict[tuple[str, str], tuple[list[str], str | None]] = {}

        for t in tracks:
            artist = t["artist"]
            title = t["title"]
            album = t["album"]
            year = t.get("year")
            if artist:
                artists_dict.setdefault(artist, []).append(title)
                if album:
                    key = (artist, album)
                    if key not in albums_dict:
                        albums_dict[key] = ([], year)
                    albums_dict[key][0].append(title)

        print(f"[researcher] Library: {len(artists_dict)} artists, {len(albums_dict)} albums")

        artist_notes = 0
        for artist, track_titles in artists_dict.items():
            try:
                path = research_artist(artist, track_titles, force=args.force)
                if path:
                    artist_notes += 1
            except Exception as e:
                print(f"[researcher] ERROR artist {artist}: {e}", file=sys.stderr)

        album_notes = 0
        for (artist, album), (track_titles, year) in albums_dict.items():
            try:
                path = research_album(artist, album, track_titles, year=year, force=args.force)
                if path:
                    album_notes += 1
            except Exception as e:
                print(f"[researcher] ERROR album {artist}/{album}: {e}", file=sys.stderr)

        print(f"[researcher] Done — {artist_notes} artist notes, {album_notes} album notes written")
    else:
        path = research_artist(args.artist, [], force=args.force)
        if path:
            print(f"[researcher] Done — {path}")
        else:
            print("[researcher] No output produced", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
