#!/usr/bin/env python3
"""
WRIT-FM Playlist Feeder for ezstream.

Runs as a daemon alongside ezstream. Builds and updates the playlist file
based on the current show schedule. Sends SIGHUP to ezstream to reload
when the playlist changes.

Also runs the API server and handles file consumption.
"""

import json
import html
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from station_config import apply_station_env, load_station_config  # noqa: E402

if "--station" in sys.argv:
    station_idx = sys.argv.index("--station")
    try:
        os.environ["WRIT_STATION_ID"] = sys.argv[station_idx + 1]
    except IndexError:
        raise SystemExit("--station requires a station id")
    del sys.argv[station_idx:station_idx + 2]

STATION = load_station_config()
PLAYLIST_FILE = STATION.playlist_file
SILENCE_FILE = STATION.silence_file
TALK_DIR = STATION.talk_dir
BUMPER_DIR = STATION.bumper_dir
NOW_PLAYING_DEFAULT = STATION.now_playing_file
CURRENT_TRACK_FILE = STATION.current_track_file


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


# Music-forward defaults: keep talk as occasional hosted breaks inside longer
# music runs. Env vars make live tuning possible without another deploy.
TALK_SEGMENTS_PER_PLAYLIST = _env_int("WRIT_TALK_SEGMENTS_PER_PLAYLIST", 3)
MUSIC_ONLY_TRACK_LIMIT = _env_int("WRIT_MUSIC_ONLY_TRACK_LIMIT", 12)
MUSIC_LEAD_IN_BUMPERS_RANGE = (2, 3)
MUSIC_BUMPERS_AFTER_TALK_RANGE = (3, 4)

from schedule import load_schedule, slot_key, parse_slot_key  # noqa: E402
SCHEDULE_PATH = STATION.schedule_path
ARCHIVE_DIR = STATION.archive_dir

try:
    from play_history import get_history
    HISTORY_ENABLED = True
except ImportError:
    HISTORY_ENABLED = False

# Now-playing paths
NOW_PLAYING_PATHS = [NOW_PLAYING_DEFAULT]
for public_path in STATION.public_now_playing_paths:
    if public_path.parent.exists():
        NOW_PLAYING_PATHS.append(public_path)

ICECAST_STATUS_URL = os.environ.get("ICECAST_STATUS_URL", STATION.stream.status_url)
ICECAST_MOUNT = STATION.stream.mount

running = True


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def sighup_handler(signum, frame):
    """Ignore SIGHUP — we send it to ezstream and don't want to die from it."""
    pass


def signal_handler(signum, frame):
    global running
    log("Feeder shutting down...")
    running = False


def get_show():
    schedule = load_schedule(SCHEDULE_PATH)
    resolved = schedule.resolve()
    airing_start = schedule.airing_start()
    return {
        "show_id": resolved.show_id,
        "show_name": resolved.name,
        "host": resolved.host,
        "topic_focus": resolved.topic_focus,
        "description": resolved.description,
        "slot": slot_key(airing_start),
        "airing_start": airing_start,
    }


def get_talk_segments(show_id: str, slot: str) -> list[Path]:
    """Segments stocked for THIS specific airing — excludes aired/ subfolder."""
    slot_dir = TALK_DIR / show_id / slot
    if not slot_dir.exists():
        return []
    segments = sorted(slot_dir.glob("*.wav"), key=lambda p: p.name)

    # If any files have sequence prefixes (00_, 01_, ...), respect the order
    has_sequence = any(s.name[:3].rstrip("_").isdigit() for s in segments)
    if has_sequence:
        return segments  # already sorted by name = by sequence

    # Otherwise: listener responses first, rest shuffled
    lr = [s for s in segments if "listener_response" in s.name]
    rest = [s for s in segments if "listener_response" not in s.name]
    random.shuffle(rest)
    return lr + rest


def archive_slot(show_id: str, slot: str) -> None:
    """Move a finished slot folder to output/archive/. Atomic rename. No-op if missing."""
    src = TALK_DIR / show_id / slot
    if not src.exists():
        return
    dst_parent = ARCHIVE_DIR / show_id
    dst_parent.mkdir(parents=True, exist_ok=True)
    dst = dst_parent / slot
    # If a previous archive of the same slot exists (rare: retry run), suffix it.
    if dst.exists():
        i = 1
        while (dst_parent / f"{slot}.{i}").exists():
            i += 1
        dst = dst_parent / f"{slot}.{i}"
    try:
        src.rename(dst)
        log(f"  Archived slot: {show_id}/{slot} → archive/")
    except Exception as e:
        log(f"  Archive failed for {show_id}/{slot}: {e}")


def sweep_stale_slots(current_show_id: str, current_slot: str) -> None:
    """At startup, archive any slot folder whose start-time is past and isn't current."""
    if not TALK_DIR.exists():
        return
    now = datetime.now()
    for show_dir in TALK_DIR.iterdir():
        if not show_dir.is_dir():
            continue
        for slot_dir in show_dir.iterdir():
            if not slot_dir.is_dir():
                continue
            try:
                start = parse_slot_key(slot_dir.name)
            except ValueError:
                continue  # not a slot folder — leave it (will be handled by migration)
            if show_dir.name == current_show_id and slot_dir.name == current_slot:
                continue
            if start < now:
                archive_slot(show_dir.name, slot_dir.name)


def get_bumpers(show_id: str) -> list[Path]:
    show_dir = BUMPER_DIR / show_id
    if not show_dir.exists():
        return []
    files = [
        f for f in show_dir.iterdir()
        if f.is_file() and f.suffix.lower() in {".flac", ".mp3", ".wav"}
    ]
    if HISTORY_ENABLED and files:
        try:
            repeat_hours = int(os.environ.get("WRIT_BUMPER_REPEAT_HOURS", "4"))
            recent = get_history().get_recent_filepaths(hours=repeat_hours)
            fresh = [f for f in files if str(f) not in recent]
            if fresh:
                files = fresh
        except Exception as e:
            log(f"  Bumper history filter failed: {e}")
    random.shuffle(files)
    return files


def clean_name(filepath: Path) -> str:
    name = filepath.stem
    types = {
        "listener_response": "Listener Mail", "deep_dive": "Deep Dive",
        "news_analysis": "Signal Report", "interview": "The Interview",
        "panel": "Crosswire", "story": "Story Hour",
        "listener_mailbag": "Listener Hours", "music_essay": "Sonic Essay",
        "station_id": STATION.call_sign, "show_intro": "Show Opening",
        "show_outro": "Show Closing",
    }
    for key, friendly in types.items():
        if key in name.lower():
            return friendly
    return "Transmission"


def get_listener_count() -> int:
    try:
        import urllib.request
        import urllib.parse
        with urllib.request.urlopen(ICECAST_STATUS_URL, timeout=1.5) as resp:
            data = json.load(resp)
        sources = data.get("icestats", {}).get("source", {})
        if isinstance(sources, dict):
            sources = [sources]
        if not isinstance(sources, list):
            return 0

        for source in sources:
            listen_url = str(source.get("listenurl", ""))
            path = urllib.parse.urlparse(listen_url).path
            if source.get("mount") == ICECAST_MOUNT or path == ICECAST_MOUNT:
                return int(source.get("listeners", 0) or 0)

        # Icecast returns a single dict when only one source is active and that
        # source may not include a mount field. Keep the old behaviour there.
        if len(sources) == 1:
            return int(sources[0].get("listeners", 0) or 0)
        return 0
    except Exception:
        return 0


def write_now_playing(info: dict):
    for path in NOW_PLAYING_PATHS:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(json.dumps(info))
            tmp.replace(path)
        except Exception:
            pass


def describe_track(filepath: Path) -> tuple[str, str]:
    """Return (display_name, track_type) for a track path."""
    s = str(filepath)
    if "music_bumpers" in s:
        meta_path = filepath.with_suffix(".json")
        name = "AI Music"
        if meta_path.exists():
            try:
                m = json.loads(meta_path.read_text())
                name = m.get("display_name", name)
            except Exception:
                pass
        return name, "bumper"
    if "talk_segments" in s:
        return clean_name(filepath), "talk"
    if "silence" in filepath.name.lower():
        return "Silence", "silence"
    return clean_name(filepath), "unknown"


def make_bumper_entry(filepath: Path) -> dict:
    meta_path = filepath.with_suffix(".json")
    name = "AI Music"
    if meta_path.exists():
        try:
            m = json.loads(meta_path.read_text())
            name = m.get("display_name", name)
        except Exception:
            pass
    return {"path": str(filepath), "type": "bumper", "name": name}


def append_bumpers(entries: list[dict], bumpers: list[Path], start_idx: int, count: int) -> int:
    """Append up to count bumpers from start_idx and return the next index."""
    bumper_idx = start_idx
    for _ in range(count):
        if bumper_idx >= len(bumpers):
            break
        entries.append(make_bumper_entry(bumpers[bumper_idx]))
        bumper_idx += 1
    return bumper_idx


def record_play(filepath: str, show_id: str):
    """Record a play to history. Called when stream_metadata advances .current_track.txt."""
    if not HISTORY_ENABLED:
        return
    try:
        name, track_type = describe_track(Path(filepath))
        if track_type == "silence":
            return
        get_history().record_play(
            filepath=filepath,
            track_name=name,
            vibe=track_type,
            time_period=show_id,
            listeners=get_listener_count(),
        )
    except Exception as e:
        log(f"  record_play failed: {e}")


def signal_ezstream_reload():
    """Send SIGHUP to ezstream to reload the playlist."""
    if ezstream_proc and ezstream_proc.poll() is None:
        try:
            os.kill(ezstream_proc.pid, signal.SIGHUP)
        except Exception:
            pass


def build_playlist(show_id: str, slot: str) -> list[dict]:
    """Build an ordered playlist for the current (show, slot).

    Reads only files directly in {TALK_DIR}/{show_id}/{slot}/ — the aired/
    subfolder is ignored, so restart mid-slot doesn't replay what already aired.
    Bumpers remain a shared pool under music_bumpers/{show_id}/.
    """
    entries = []
    talks = get_talk_segments(show_id, slot)
    bumpers = get_bumpers(show_id)
    bumper_idx = 0

    if TALK_SEGMENTS_PER_PLAYLIST:
        talks = talks[:TALK_SEGMENTS_PER_PLAYLIST]
    else:
        talks = []

    if not talks and not bumpers:
        # Nothing — use silence
        entries.append({"path": str(SILENCE_FILE), "type": "silence", "name": "Silence"})
        return entries

    if not talks:
        # No talk, stay music-forward and keep silence only as the playlist tail.
        for b in bumpers[:MUSIC_ONLY_TRACK_LIMIT]:
            entries.append(make_bumper_entry(b))
        entries.append({"path": str(SILENCE_FILE), "type": "silence", "name": "Silence"})
        return entries

    # Music-forward flow: lead with music, then use talk as hosted breaks between
    # larger music blocks.
    lead_in = random.randint(*MUSIC_LEAD_IN_BUMPERS_RANGE)
    bumper_idx = append_bumpers(entries, bumpers, bumper_idx, lead_in)

    for talk in talks:
        entries.append({"path": str(talk), "type": "talk", "name": clean_name(talk)})
        n_bumpers = random.randint(*MUSIC_BUMPERS_AFTER_TALK_RANGE)
        bumper_idx = append_bumpers(entries, bumpers, bumper_idx, n_bumpers)

    return entries


def write_playlist(entries: list[dict]):
    """Write the M3U playlist file."""
    PLAYLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAYLIST_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for entry in entries:
            f.write(entry["path"] + "\n")
    tmp.replace(PLAYLIST_FILE)


def run():
    global running, ezstream_proc
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, sighup_handler)

    log(f"=== {STATION.call_sign} Feeder ===")
    log(f"Station: {STATION.id} | Mount: {STATION.stream.mount}")
    log(f"Playlist: {PLAYLIST_FILE}")

    # Ensure silence file exists for fallback
    if not SILENCE_FILE.exists():
        log("Creating silence fallback file...")
        SILENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-y", "-v", "quiet",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "30", str(SILENCE_FILE),
        ], check=True)

    # Shared state for API server
    track_info = {"track": None, "type": None, "show": None}

    # Proxy object so API health check reports "up" when ezstream is running
    class _StreamProxy:
        def poll(self):
            if ezstream_proc is None:
                return 1
            return ezstream_proc.poll()

    _proxy = _StreamProxy()

    # Start API server
    try:
        from api_server import start_api_thread
        start_api_thread(track_info, lambda: _proxy, get_listener_count)
        log(f"API server started on port {STATION.stream.api_port}")
    except Exception as e:
        log(f"API server failed: {e}")

    current_show_id: str | None = None
    current_slot: str | None = None
    playlist_entries: list[dict] = []
    last_talk_set: set[str] = set()
    last_check = 0
    last_rebuild_check = 0
    startup_sweep_done = False

    # Seed last_recorded_track so a feeder restart doesn't double-record the in-flight track
    try:
        last_recorded_track = (
            CURRENT_TRACK_FILE.read_text().strip() if CURRENT_TRACK_FILE.exists() else None
        ) or None
    except Exception:
        last_recorded_track = None

    while running:
        show = get_show()

        # Show or slot changed — archive the just-ended slot and rebuild
        if show["show_id"] != current_show_id or show["slot"] != current_slot:
            if current_show_id is not None and current_slot is not None:
                archive_slot(current_show_id, current_slot)

            log(f"Show: {show['show_name']} ({show['show_id']}) [slot {show['slot']}]")
            log(f"  Host: {show['host']} | Focus: {show['topic_focus']}")
            current_show_id = show["show_id"]
            current_slot = show["slot"]

            # First time through: archive any past/stranded slot folders
            if not startup_sweep_done:
                sweep_stale_slots(current_show_id, current_slot)
                startup_sweep_done = True

            playlist_entries = build_playlist(current_show_id, current_slot)
            last_talk_set = {p.name for p in get_talk_segments(current_show_id, current_slot)}
            write_playlist(playlist_entries)
            signal_ezstream_reload()
            log(f"  Playlist: {len(playlist_entries)} tracks")
            for e in playlist_entries:
                log(f"    [{e['type']}] {e['name']}")

        # Update now-playing info
        now = time.time()
        if now - last_check >= 5:
            last_check = now

            # Source of truth: stream_metadata.sh writes the active track's
            # absolute path here as ezstream advances. Fall back to the
            # first playlist entry only if that file is missing or stale
            # (eg. before the first track change after a restart).
            current_path = ""
            try:
                if CURRENT_TRACK_FILE.exists():
                    current_path = CURRENT_TRACK_FILE.read_text().strip()
            except Exception:
                current_path = ""

            track_name: str | None = None
            track_type: str | None = None
            current_path_obj = Path(current_path) if current_path else None
            if current_path_obj and current_path_obj.is_absolute() and current_path_obj.exists():
                try:
                    track_name, track_type = describe_track(current_path_obj)
                except Exception:
                    track_name, track_type = None, None

            if track_name is None:
                if playlist_entries:
                    track_name = playlist_entries[0]["name"]
                    track_type = playlist_entries[0]["type"]
                else:
                    track_name = show["show_name"]
                    track_type = "silence"

            np_info = {
                "station_id": STATION.id,
                "station": STATION.call_sign,
                "mount": STATION.stream.mount,
                "track": track_name,
                "type": track_type,
                "show_id": show["show_id"],
                "show": show["show_name"],
                "host": show["host"],
                "slot": show["slot"],
                "timestamp": datetime.now().isoformat(),
                "listeners": get_listener_count(),
            }
            # Update shared dict for API server
            track_info.update(np_info)
            # Write to disk for external consumers
            write_now_playing(np_info)

            # Record play when the current-track file advances to a new path
            try:
                if (
                    current_path_obj
                    and current_path_obj.is_absolute()
                    and current_path != last_recorded_track
                ):
                    record_play(current_path, show["show_id"])
                    last_recorded_track = current_path
            except Exception:
                pass

        # Check if ezstream died and restart it (with cooldown)
        if ezstream_proc and ezstream_proc.poll() is not None:
            if not hasattr(run, '_last_restart') or now - run._last_restart > 30:
                log("  ezstream died, restarting...")
                run._last_restart = now
                ezstream_proc = start_ezstream()
            elif now - run._last_restart > 30:
                pass  # still in cooldown

        # Check if we need to rebuild — a file appeared in the slot that
        # wasn't there when we last built (e.g., operator generated a new
        # segment mid-slot, or a listener response landed).
        # Files leaving the set (moved to aired/) must NOT trigger a rebuild.
        if now - last_rebuild_check >= 30:
            last_rebuild_check = now
            current_unaired = {p.name for p in get_talk_segments(current_show_id, current_slot)}
            new_files = current_unaired - last_talk_set
            if new_files:
                log(f"  New content in slot ({len(new_files)} file(s)), rebuilding playlist")
                playlist_entries = build_playlist(current_show_id, current_slot)
                last_talk_set = {p.name for p in get_talk_segments(current_show_id, current_slot)}
                write_playlist(playlist_entries)
                signal_ezstream_reload()

        time.sleep(5)

    # Clean up ezstream if we started it
    if ezstream_proc and ezstream_proc.poll() is None:
        log("Stopping ezstream...")
        ezstream_proc.terminate()
        ezstream_proc.wait(timeout=5)

    log("Feeder stopped")


# Global handle for ezstream subprocess
ezstream_proc = None

RADIO_XML = STATION.ezstream_config_file


def write_ezstream_config() -> Path:
    """Render station-specific ezstream config.

    ezstream does not know about station instances, so the generated config is
    the handoff point: mount, metadata, and playlist intake all inherit the
    station environment from the feeder process.
    """
    RADIO_XML.parent.mkdir(parents=True, exist_ok=True)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ezstream>
  <servers>
    <server>
      <hostname>{html.escape(STATION.stream.icecast_host)}</hostname>
      <port>{STATION.stream.icecast_port}</port>
      <password>{html.escape(STATION.stream.source_password)}</password>
      <tls>None</tls>
      <reconnect_attempts>0</reconnect_attempts>
    </server>
  </servers>

  <streams>
    <stream>
      <mountpoint>{html.escape(STATION.stream.mount)}</mountpoint>
      <format>{html.escape(STATION.stream.format)}</format>
      <encoder>{html.escape(STATION.stream.encoder)}</encoder>
      <stream_name>{html.escape(STATION.stream.stream_name)}</stream_name>
      <stream_genre>{html.escape(STATION.stream.stream_genre)}</stream_genre>
      <stream_description>{html.escape(STATION.stream.stream_description)}</stream_description>
    </stream>
  </streams>

  <intakes>
    <intake>
      <type>program</type>
      <filename>mac/playlist_intake.py</filename>
      <shuffle>No</shuffle>
      <stream_once>No</stream_once>
    </intake>
  </intakes>

  <metadata>
    <program>mac/stream_metadata.sh</program>
    <format_str>@a@ - @t@</format_str>
    <normalize_strings>Yes</normalize_strings>
    <no_updates>No</no_updates>
  </metadata>

  <decoders>
    <decoder>
      <name>ffmpeg-wav</name>
      <program>ffmpeg -v quiet -i @T@ -af loudnorm=I=-14:TP=-1.5:LRA=7,aresample=44100 -f s16le -acodec pcm_s16le -ar 44100 -ac 2 -</program>
      <file_ext>.wav</file_ext>
    </decoder>
    <decoder>
      <name>ffmpeg-flac</name>
      <program>ffmpeg -v quiet -i @T@ -af loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=3,aresample=44100 -f s16le -acodec pcm_s16le -ar 44100 -ac 2 -</program>
      <file_ext>.flac</file_ext>
    </decoder>
  </decoders>

  <encoders>
    <encoder>
      <name>oggenc-q4</name>
      <format>Ogg</format>
      <program>oggenc -r -B 16 -C 2 -R 44100 --raw-endianness 0 -q 4 -t @M@ -</program>
    </encoder>
  </encoders>
</ezstream>
"""
    RADIO_XML.write_text(xml)
    return RADIO_XML


def start_ezstream() -> subprocess.Popen:
    """Start ezstream as a child process. Kills any stale instances first."""
    write_ezstream_config()

    # Kill stale ezstream processes for this station only.
    subprocess.run(["pkill", "-f", f"ezstream.*{RADIO_XML}"],
                   capture_output=True, timeout=5)
    time.sleep(1)

    log("Starting ezstream...")
    # Build initial playlist before starting
    show = get_show()
    entries = build_playlist(show["show_id"], show["slot"])
    write_playlist(entries)

    proc = subprocess.Popen(
        ["ezstream", "-v", "-c", str(RADIO_XML)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
        env=apply_station_env(STATION),
        start_new_session=True,  # own process group so SIGHUP doesn't propagate
    )

    # Read stderr in a thread so it doesn't block
    def _log_ezstream(pipe):
        for line in iter(pipe.readline, b""):
            text = line.decode().strip()
            if text:
                log(f"  [ezstream] {text}")
        pipe.close()

    import threading
    threading.Thread(target=_log_ezstream, args=(proc.stderr,), daemon=True).start()

    time.sleep(2)
    if proc.poll() is not None:
        log("ERROR: ezstream failed to start")
        return proc

    log("ezstream connected to Icecast")
    return proc


if __name__ == "__main__":
    if "--start-ezstream" in sys.argv:
        ezstream_proc = start_ezstream()
    run()
