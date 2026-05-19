# reginerd-fm

24/7 internet radio powered by reginerd's actual Plex music library. One DJ. Five blocks. All day.

**Live at:** [radio.reginerd.tv](https://radio.reginerd.tv/stream)

Forked from [WRIT-FM](https://github.com/keltokhy/writ-fm). Key differences: music comes from a real Plex library on a NAS (not AI-generated), and there's one DJ persona (reginerd) instead of multiple hosts.

## Stack

| Component | Tech |
|-----------|------|
| Music source | Plex on DS220+ NAS → `mac/plex_music_feeder.py` |
| Streaming | Icecast + ezstream on Mac Studio |
| Public URL | Cloudflare Tunnel → radio.reginerd.tv |
| DJ voice | ElevenLabs Professional Voice Clone (Reggie's voice) |
| Talk generation | Qwen14B via Hermes (local, Mac Studio) |
| Play tracking | SQLite at `~/.writ/history.db` |

## Schedule (Weekday)

| Block | Time | Genres |
|-------|------|--------|
| Morning | 6am–10am | R&B, Easy Listening, Jazz, Vocal |
| Midday | 10am–3pm | Pop/Rock, Reggae, Folk, Electronic |
| Prime Time | 3pm–8pm | Rap, R&B |
| Wind Down | 8pm–10pm | Jazz, Easy Listening, Electronic |
| Late Night | 10pm–6am | Stage & Screen (VGM + film scores) |

Friday extends Prime Time to 10pm. Saturday adds Sabbath Morning (Gospel, 6–10am). See `config/schedule.yaml` for the full definition.

## Build Status

- [x] Phase 0 — Foundation
- [x] Phase 1 — Plex Music Feeder
- [x] Phase 2 — Stream Core (live at radio.reginerd.tv)
- [x] Phase 3 — DJ Personas (code done; voice clone pending)
- [x] Phase 4 — Talk Generation (code done)
- [x] Phase 5 — Content Pipeline (no-repeat, day-of-week, orchestrator)
- [ ] Phase 6 — Launch (QA → 24hr test → soft launch)

**Remaining manual steps:** record voice samples (REG-124) → clone voice on ElevenLabs (REG-128) → QA talk breaks (REG-115).

## Setup

### 1. Install dependencies

```bash
brew install icecast ffmpeg ezstream vorbis-tools
uv sync
```

### 2. Configure

```bash
cp mac/config.yaml.example mac/config.yaml
# Fill in: plex host/token, nas_mount, ElevenLabs API key + voice_id
```

Icecast config lives in `config/icecast.xml` (already configured on Mac Studio).

### 3. Stock music from Plex

```bash
# Sync entire library (no limits — run whenever you add music to Plex)
set -a && source .env && set +a
uv run python mac/plex_music_feeder.py --all --full

# Check queue status
uv run python mac/plex_music_feeder.py --status
```

### 4. Start the stream

```bash
# Start Icecast (if not already running)
icecast -b -c config/icecast.xml

# Start feeder + ezstream
set -a && source .env && set +a
uv run python mac/feeder.py --station rgnrd-fm --start-ezstream
```

## Operations

### Force playlist refresh

When you add tracks or want fresh songs immediately — no restart needed:

```bash
kill -USR1 $(pgrep -f "feeder.py")
```

### Check play history

```bash
uv run python mac/play_history.py stats        # totals + breakdown
uv run python mac/play_history.py recent 20    # last 20 tracks
uv run python mac/play_history.py most_played  # repeat offenders
```

### No-repeat logic

- Tracks played in the last 4 hours are filtered out of the next playlist build
- If the entire pool has been played recently, the least-recently-played tracks come first (LRU order)
- Adjust the window: `WRIT_BUMPER_REPEAT_HOURS=6 uv run python mac/feeder.py ...`

## Content Pipeline (Talk Generation)

The nightly orchestrator agent (`mac/agents/orchestrator.py`) runs the full pipeline:

```
Curator → Scriptwriter → Narrator → output/talk_segments/
```

Dry run to validate:
```bash
uv run python mac/agents/orchestrator.py --dry-run
```

Talk breaks require a working ElevenLabs voice_id in `mac/config.yaml`. Until the voice clone is ready, use any ElevenLabs voice ID for testing.

## Architecture

```
Plex NAS (DS220+)
  └── plex_music_feeder.py ──symlinks──► output/music_bumpers/{show_id}/

feeder.py (daemon)
  ├── builds playlists per schedule block
  ├── filters by play_history.py (4hr no-repeat window, LRU fallback)
  ├── writes output/runtime/.playlist.m3u
  ├── signals ezstream (SIGHUP) on rebuild
  └── SIGUSR1 → force immediate rebuild

ezstream ──► Icecast :8000/stream ──► Cloudflare Tunnel ──► radio.reginerd.tv

orchestrator.py (nightly, 2am)
  └── curator → scriptwriter → narrator → output/talk_segments/
```

## Key Files

| File | Purpose |
|------|---------|
| `mac/feeder.py` | Playlist daemon — core of the stream |
| `mac/plex_music_feeder.py` | Populate bumper dirs from Plex by genre |
| `mac/play_history.py` | SQLite play tracker + no-repeat logic |
| `mac/agents/orchestrator.py` | Nightly talk generation pipeline |
| `mac/agents/curator.py` | Track selection per block |
| `mac/agents/scriptwriter.py` | Qwen14B DJ script generation |
| `mac/agents/narrator.py` | ElevenLabs TTS → WAV |
| `config/persona.yaml` | reginerd base DJ persona prompt |
| `config/blocks.yaml` | Per-block context overlays |
| `config/schedule.yaml` | Full 7-day schedule |
| `config/genres.yaml` | Show → Plex genre mapping |
