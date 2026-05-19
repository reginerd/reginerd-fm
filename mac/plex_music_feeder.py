#!/usr/bin/env python3
"""
plex_music_feeder.py — Stock RGNRD-FM music queue from Plex.

Queries the Plex library by genre and symlinks tracks from the NAS mount
into output/music_bumpers/{show_id}/ for feeder.py to pick up.
No file copying — symlinks only.

Usage:
    uv run python mac/plex_music_feeder.py --all --full
    uv run python mac/plex_music_feeder.py --all --min 20
    uv run python mac/plex_music_feeder.py --show prime_time --count 10
    uv run python mac/plex_music_feeder.py --status
    uv run python mac/plex_music_feeder.py --genres
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

import yaml

from station_config import load_station_config

STATION = load_station_config()
BUMPER_DIR = STATION.bumper_dir

CONFIG_PATH = PROJECT_ROOT / "mac" / "config.yaml"
GENRES_CONFIG_PATH = PROJECT_ROOT / "config" / "genres.yaml"

AUDIO_SUFFIXES = {".flac", ".mp3", ".wav", ".m4a"}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config not found: {CONFIG_PATH}\n"
            "Copy mac/config.yaml.example → mac/config.yaml and fill in your values."
        )
    return yaml.safe_load(CONFIG_PATH.read_text())


def load_genres_config() -> dict:
    return yaml.safe_load(GENRES_CONFIG_PATH.read_text())


def _safe_filename(text: str) -> str:
    return re.sub(r"[^\w\-.]", "_", text)


class PlexClient:
    def __init__(self, host: str, port: int, token: str, section: int, nas_mount: str, music_prefix: str):
        self.base_url = f"http://{host}:{port}"
        self.token = token
        self.section = section
        self.nas_mount = Path(nas_mount)
        # music_prefix is the path prefix Plex uses on the NAS server
        # e.g. /volume1/music on a Synology DS220+
        self.music_prefix = music_prefix.rstrip("/")
        self._genre_cache: dict[str, int] | None = None

    def _get(self, path: str) -> ET.Element:
        sep = "&" if "?" in path else "?"
        url = f"{self.base_url}{path}{sep}X-Plex-Token={self.token}"
        req = Request(url, headers={"Accept": "application/xml"})
        with urlopen(req, timeout=15) as resp:
            return ET.fromstring(resp.read())

    def get_genres(self) -> dict[str, int]:
        if self._genre_cache is not None:
            return self._genre_cache
        root = self._get(f"/library/sections/{self.section}/genre")
        genres: dict[str, int] = {}
        for tag in root.iter("Directory"):
            title = tag.get("title", "")
            key = tag.get("key", "")
            if title and key:
                try:
                    genres[title] = int(key)
                except ValueError:
                    pass
        self._genre_cache = genres
        return genres

    def get_tracks_by_genre(self, genre_name: str, limit: int = 500) -> list[dict]:
        """Return track dicts with title, artist, album, year, file (local path)."""
        genres = self.get_genres()
        genre_id = genres.get(genre_name)
        if genre_id is None:
            available = ", ".join(sorted(genres.keys()))
            raise ValueError(f"Genre '{genre_name}' not found in Plex. Available: {available}")

        root = self._get(
            f"/library/sections/{self.section}/all"
            f"?type=10&genre={genre_id}&X-Plex-Container-Size={limit}"
        )

        tracks = []
        for track in root.iter("Track"):
            part = track.find(".//Part")
            if part is None:
                continue
            plex_path = part.get("file", "")
            if not plex_path:
                continue

            local_path = self._plex_to_local(plex_path)
            tracks.append({
                "title": track.get("title", "unknown"),
                "artist": track.get("grandparentTitle", "unknown"),
                "album": track.get("parentTitle", ""),
                "year": track.get("parentYear", ""),
                "genre": genre_name,
                "file": local_path,
                "plex_path": plex_path,
            })

        return tracks

    def _plex_to_local(self, plex_path: str) -> Path:
        """Map a Plex server path to the local NAS mount path.

        Plex stores the path as it sees it on the server (e.g. /volume1/music/…).
        We strip the music_prefix and join with the local nas_mount.
        """
        if self.music_prefix and plex_path.startswith(self.music_prefix):
            relative = plex_path[len(self.music_prefix):].lstrip("/")
        else:
            # Fallback: use the full path joined under nas_mount
            relative = plex_path.lstrip("/")
        return self.nas_mount / relative


def stock_show(
    client: PlexClient,
    show_id: str,
    genres: list[str],
    min_tracks: int,
    count: int | None,
    full: bool = False,
) -> int:
    """Symlink tracks for a show. Returns number of new symlinks created."""
    show_dir = BUMPER_DIR / show_id
    show_dir.mkdir(parents=True, exist_ok=True)

    existing = [
        f for f in show_dir.iterdir()
        if f.suffix.lower() in AUDIO_SUFFIXES
    ]

    # Prune broken symlinks silently
    broken = [f for f in existing if f.is_symlink() and not f.exists()]
    for b in broken:
        b.unlink()
    existing = [f for f in existing if not (f.is_symlink() and not f.exists())]

    if not full:
        need = count if count is not None else max(0, min_tracks - len(existing))
        if need <= 0:
            print(f"  {show_id}: {len(existing)} tracks stocked (min={min_tracks}) — skipping")
            return 0

    print(f"  {show_id}: {len(existing)} existing, syncing all [{', '.join(genres)}]" if full else
          f"  {show_id}: {len(existing)} existing, need {need} more [{', '.join(genres)}]")

    candidates: list[dict] = []
    for genre in genres:
        try:
            tracks = client.get_tracks_by_genre(genre, limit=10000)
            candidates.extend(tracks)
        except ValueError as e:
            print(f"    Warning: {e}")

    if not candidates:
        print(f"    No tracks found for {show_id}")
        return 0

    existing_targets = {str(f.resolve()) for f in existing if f.is_symlink()}
    candidates = [t for t in candidates if str(t["file"].resolve()) not in existing_targets]

    if full:
        need = len(candidates)
    else:
        random.shuffle(candidates)

    added = 0
    for track in candidates[:need]:
        src: Path = track["file"]
        if not src.exists():
            continue

        link_name = (
            f"{_safe_filename(track['artist'])}__{_safe_filename(track['title'])}{src.suffix}"
        )
        link_path = show_dir / link_name
        if link_path.exists() or link_path.is_symlink():
            continue

        link_path.symlink_to(src)
        print(f"    + {track['artist']} — {track['title']}")
        added += 1

    print(f"  {show_id}: added {added} tracks ({len(existing) + added} total)")
    return added


def cmd_status(genres_config: dict) -> None:
    show_genres = genres_config.get("show_genres", {})
    print("RGNRD-FM Music Queue Status")
    print("-" * 40)
    for show_id in show_genres:
        show_dir = BUMPER_DIR / show_id
        if not show_dir.exists():
            print(f"  {show_id:<20} 0 tracks")
            continue
        all_files = [f for f in show_dir.iterdir() if f.suffix.lower() in AUDIO_SUFFIXES]
        valid = [f for f in all_files if not f.is_symlink() or f.exists()]
        broken = [f for f in all_files if f.is_symlink() and not f.exists()]
        note = f" ({len(broken)} broken)" if broken else ""
        print(f"  {show_id:<20} {len(valid)} tracks{note}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stock RGNRD-FM music queue from Plex",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--show", metavar="SHOW_ID", help="Stock a specific show block")
    group.add_argument("--all", action="store_true", help="Stock all show blocks")
    group.add_argument("--status", action="store_true", help="Show queue status (no Plex needed)")
    group.add_argument("--genres", action="store_true", help="List available Plex genres")
    parser.add_argument("--full", action="store_true", help="Sync all available Plex tracks (no limit)")
    parser.add_argument("--min", type=int, default=20, help="Minimum tracks per show (default 20)")
    parser.add_argument("--count", type=int, help="Add exactly N tracks regardless of current count")
    args = parser.parse_args()

    genres_config = load_genres_config()

    if args.status:
        cmd_status(genres_config)
        return 0

    try:
        cfg = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1

    plex_cfg = cfg.get("plex", {})
    token = os.environ.get("PLEX_TOKEN") or str(plex_cfg.get("token", ""))
    if not token:
        print("Error: PLEX_TOKEN not set. Add to .env or mac/config.yaml")
        return 1

    client = PlexClient(
        host=str(plex_cfg.get("host", "192.168.1.27")),
        port=int(plex_cfg.get("port", 32400)),
        token=token,
        section=int(plex_cfg.get("library_section", 1)),
        nas_mount=str(cfg.get("nas_mount", "/Volumes/music")),
        music_prefix=str(plex_cfg.get("music_prefix", "/volume1/music")),
    )

    if args.genres:
        print("Available Plex genres:")
        try:
            genres = client.get_genres()
        except Exception as e:
            print(f"Error: {e}")
            return 1
        for name, gid in sorted(genres.items()):
            print(f"  {name} (id={gid})")
        return 0

    show_genres: dict[str, list[str]] = genres_config.get("show_genres", {})

    if args.show:
        if args.show not in show_genres:
            print(f"Error: unknown show '{args.show}'. Known: {', '.join(show_genres)}")
            return 1
        stock_show(client, args.show, show_genres[args.show], args.min, args.count, full=args.full)
        return 0

    if args.all:
        total = 0
        for show_id, genres in show_genres.items():
            total += stock_show(client, show_id, genres, args.min, args.count, full=args.full)
        print(f"\nTotal new tracks added: {total}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
