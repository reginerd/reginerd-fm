#!/usr/bin/env python3
"""
Shared helpers for RGNRD-FM content generators.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))
from station_config import load_station_config  # noqa: E402

STATION = load_station_config()

DEFAULT_NEWS_FEEDS = (
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.npr.org/1001/rss.xml",
)
NEWS_CACHE_TTL_SECONDS = int(os.environ.get("RGNRD_NEWS_CACHE_TTL", "600"))
NEWS_TIMEOUT_SECONDS = int(os.environ.get("RGNRD_NEWS_TIMEOUT", "6"))

_NEWS_CACHE: dict[str, object] = {"timestamp": 0.0, "items": []}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def get_time_of_day(hour: int | None = None, profile: str = "default") -> str:
    if hour is None:
        hour = datetime.now().hour

    if profile == "extended":
        if 6 <= hour < 10:
            return "morning"
        if 10 <= hour < 14:
            return "daytime"
        if 14 <= hour < 15:
            return "early_afternoon"
        if 15 <= hour < 18:
            return "afternoon"
        if 18 <= hour < 24:
            return "evening"
        return "late_night"

    if 6 <= hour < 10:
        return "morning"
    if 10 <= hour < 18:
        return "daytime"
    if 18 <= hour < 24:
        return "evening"
    return "late_night"


def preprocess_for_tts(text: str, *, include_cough: bool = True) -> str:
    text = text.replace("[pause]", "...")
    text = text.replace("[chuckle]", "heh...")
    if include_cough:
        text = text.replace("[cough]", "ahem...")
    text = text.replace('"', "")
    return text.strip()


def clean_claude_output(text: str, *, strip_quotes: bool = True) -> str:
    cleaned = text.replace("*", "").replace("_", "").strip()
    if strip_quotes and cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1].strip()
    return cleaned


def run_claude(
    prompt: str,
    *,
    timeout: int = 60,
    model: str | None = None,
    min_length: int = 0,
    strip_quotes: bool = True,
) -> str | None:
    if STATION.agent.kind == "codex":
        args = [
            STATION.agent.command,
            "exec",
            "-C",
            str(PROJECT_ROOT),
            "-s",
            "danger-full-access",
            "--color",
            "never",
            "--ephemeral",
        ]
        if model:
            args.extend(["--model", model])
        args.append(prompt)
    else:
        args = [STATION.agent.command, *STATION.agent.args, "-p", prompt]
        if model:
            args.extend(["--model", model])

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log(f"{STATION.agent.kind} timed out")
        return None
    except Exception as exc:
        log(f"{STATION.agent.kind} error: {exc}")
        return None

    if result.returncode != 0:
        stderr = result.stderr.strip().splitlines()
        if stderr:
            log(f"{STATION.agent.kind} failed: {stderr[-1]}")
        return None

    if not result.stdout.strip():
        return None

    script = clean_claude_output(result.stdout, strip_quotes=strip_quotes)
    if len(script) <= min_length:
        return None
    return script


def _strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_child_text(elem: ET.Element, name: str) -> str:
    for child in elem:
        if _strip_namespace(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _extract_source_title(root: ET.Element, fallback: str) -> str:
    tag = _strip_namespace(root.tag)
    if tag == "rss":
        for child in root:
            if _strip_namespace(child.tag) == "channel":
                title = _find_child_text(child, "title")
                return title or fallback
    if tag == "feed":
        title = _find_child_text(root, "title")
        return title or fallback
    return fallback


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def fetch_headlines(max_items: int | None = None) -> list[dict]:
    now = time.time()
    cached_items = _NEWS_CACHE.get("items", [])
    if cached_items and now - float(_NEWS_CACHE.get("timestamp", 0.0)) < NEWS_CACHE_TTL_SECONDS:
        return list(cached_items)

    max_items = max_items or int(os.environ.get("RGNRD_NEWS_MAX_ITEMS", "8"))
    feed_env = os.environ.get("RGNRD_NEWS_FEEDS")
    feeds = [f.strip() for f in feed_env.split(",")] if feed_env else list(DEFAULT_NEWS_FEEDS)
    feeds = [f for f in feeds if f]

    headlines: list[dict] = []
    seen: set[str] = set()

    for feed_url in feeds:
        try:
            with urllib.request.urlopen(feed_url, timeout=NEWS_TIMEOUT_SECONDS) as response:
                content = response.read()
            root = ET.fromstring(content)
        except Exception:
            continue

        fallback = urllib.parse.urlparse(feed_url).netloc or "Unknown Source"
        source = _extract_source_title(root, fallback)

        for elem in root.iter():
            tag = _strip_namespace(elem.tag)
            if tag not in ("item", "entry"):
                continue
            title = _find_child_text(elem, "title")
            if not title:
                continue
            norm = _normalize_title(title)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            headlines.append({"title": title, "source": source})
            if len(headlines) >= max_items:
                break
        if len(headlines) >= max_items:
            break

    _NEWS_CACHE["timestamp"] = now
    _NEWS_CACHE["items"] = list(headlines)
    return headlines


def format_headlines(headlines: list[dict], max_items: int | None = None) -> str:
    if not headlines:
        return ""
    max_items = max_items or len(headlines)
    lines = []
    for item in headlines[:max_items]:
        title = item.get("title", "").strip()
        source = item.get("source", "").strip() or "Source"
        if title:
            lines.append(f"- [{source}] {title}")
    return "\n".join(lines)


# =============================================================================
# SHARED TTS RENDERING
# =============================================================================

_ELEVENLABS_MODEL = "eleven_turbo_v2_5"
_ELEVENLABS_CHUNK_CHARS = 2000


def _resolve_elevenlabs_voice_id(voice_name: str) -> str | None:
    """Map a voice alias to an ElevenLabs voice ID via env vars."""
    if voice_name == "reginerd_clone":
        return (
            os.environ.get("ELEVENLABS_VOICE_ID", "").strip() or
            os.environ.get("ELEVENLABS_PLACEHOLDER_VOICE_ID", "").strip() or
            None
        )
    # Raw voice IDs (long strings) pass through directly
    if len(voice_name) > 10:
        return voice_name
    return None


def render_elevenlabs(text: str, output_path: Path, voice_id: str, api_key: str) -> bool:
    """Render text to MP3 using ElevenLabs TTS, chunking if needed."""
    import shutil
    import tempfile
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        log("ElevenLabs SDK not installed")
        return False

    client = ElevenLabs(api_key=api_key)

    if len(text) <= _ELEVENLABS_CHUNK_CHARS:
        chunks = [text]
    else:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        chunks = []
        current = ""
        for sentence in sentences:
            if len(current) + len(sentence) + 1 > _ELEVENLABS_CHUNK_CHARS and current:
                chunks.append(current.strip())
                current = sentence
            else:
                current = (current + " " + sentence).strip() if current else sentence
        if current:
            chunks.append(current)

    tmp_dir = Path(tempfile.mkdtemp(prefix="rgnrd_el_"))
    chunk_files: list[Path] = []

    try:
        for i, chunk in enumerate(chunks):
            mp3_path = tmp_dir / f"chunk{i:03d}.mp3"
            try:
                audio_gen = client.text_to_speech.convert(
                    voice_id=voice_id,
                    text=chunk,
                    model_id=_ELEVENLABS_MODEL,
                    output_format="mp3_44100_128",
                )
                mp3_path.write_bytes(b"".join(audio_gen))
                chunk_files.append(mp3_path)
            except Exception as e:
                log(f"ElevenLabs API error on chunk {i}: {e}")
                continue

        if not chunk_files:
            log("ElevenLabs: no chunks rendered")
            return False

        if len(chunk_files) == 1:
            shutil.move(str(chunk_files[0]), str(output_path))
            return output_path.exists()

        return concatenate_audio(chunk_files, output_path)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def get_audio_duration(filepath: Path) -> float | None:
    """Get audio duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(filepath)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def concatenate_audio(chunk_files: list[Path], output_path: Path, gap_seconds: float = 0) -> bool:
    """Concatenate audio files. Uses stream copy for MP3; re-encodes for WAV."""
    if len(chunk_files) == 1:
        shutil.move(str(chunk_files[0]), str(output_path))
        return True

    list_file = output_path.with_suffix('.concat.txt')
    is_mp3 = output_path.suffix.lower() == ".mp3"

    try:
        with open(list_file, 'w') as f:
            for cf in chunk_files:
                f.write(f"file '{cf}'\n")

        if is_mp3:
            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                str(output_path)
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-ar", "24000", "-ac", "1",
                str(output_path)
            ]

        result = subprocess.run(cmd, capture_output=True, timeout=120)

        list_file.unlink(missing_ok=True)
        for cf in chunk_files:
            cf.unlink(missing_ok=True)

        if result.returncode != 0:
            stderr = result.stderr.decode()
            stdout = result.stdout.decode()
            log(f"  Concat failed (rc={result.returncode}):")
            log(f"  STDERR (last 1500): {stderr[-1500:]}")
            if stdout.strip():
                log(f"  STDOUT: {stdout[-500:]}")
            return False

        return output_path.exists()

    except Exception as e:
        log(f"  Concat error: {e}")
        list_file.unlink(missing_ok=True)
        return False


def render_single_voice(text: str, output_path: Path, voice: str) -> bool:
    """Render a single-voice script to audio.

    Backend is controlled by RGNRD_TTS_BACKEND (default: kokoro).
    Set to 'elevenlabs' to use ElevenLabs instead.
    Kokoro voice is controlled by RGNRD_KOKORO_VOICE (default: am_michael).
    """
    backend = os.environ.get("RGNRD_TTS_BACKEND", "kokoro").lower()

    if backend == "elevenlabs":
        api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            log("  No ELEVENLABS_API_KEY set — cannot render TTS")
            return False
        voice_id = _resolve_elevenlabs_voice_id(voice)
        if not voice_id:
            log(f"  No ElevenLabs voice ID for '{voice}'")
            return False
        log(f"  ElevenLabs: voice={voice_id[:8]}...")
        return render_elevenlabs(text, output_path, voice_id, api_key)

    # Kokoro (default)
    import importlib.util
    kokoro_path = PROJECT_ROOT / "mac" / "kokoro" / "tts.py"
    spec = importlib.util.spec_from_file_location("kokoro_local_tts", kokoro_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    kokoro_voice = os.environ.get("RGNRD_KOKORO_VOICE", mod.DEFAULT_VOICE)
    log(f"  Kokoro TTS: voice={kokoro_voice}")
    return mod.render_speech(text, output_path, voice=kokoro_voice)
