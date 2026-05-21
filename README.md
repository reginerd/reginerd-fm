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
| Script generation | Qwen14B via Hermes (local, Mac Studio) |
| Play tracking | SQLite play history + no-repeat window |
| Show cards | Obsidian vault at `Projects/reginerd.fm/Show Cards/` |

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
- [x] Phase 5.5 — Show Cards + Intelligent Sequencing
- [ ] Phase 6 — Launch (QA → 24hr test → soft launch)

**Remaining manual steps:** record voice samples (REG-124) → clone voice on ElevenLabs (REG-128) → QA talk breaks (REG-115) → 24hr stability run (REG-166).

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

The feeder runs as a launchd daemon and starts automatically on login. To manage it manually:

```bash
# Load/start
launchctl load ~/Library/LaunchAgents/local.rgnrd.feeder.plist

# Restart
launchctl kickstart -k gui/$(id -u)/local.rgnrd.feeder

# Stop
launchctl unload ~/Library/LaunchAgents/local.rgnrd.feeder.plist
```

Or start directly:
```bash
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
- Adjust the window: `RGNRD_BUMPER_REPEAT_HOURS=6 uv run python mac/feeder.py ...`

## Content Pipeline (Talk Generation)

The nightly orchestrator agent (`mac/agents/orchestrator.py`) runs the full pipeline at 2am:

```
Curator → Researcher → Scriptwriter → Narrator → Show Card Gen
```

- **Curator** — selects featured artist per block, writes `output/manifests/{block}_{date}.json`
- **Researcher** — pulls Last.fm bio, discography, and tag context for the featured artist
- **Scriptwriter** — writes DJ break scripts via Qwen14B; outputs MP3 filenames + `setlist.json`
- **Narrator** — renders scripts to MP3 via ElevenLabs; caches reusable track intros in `output/tts_cache/`
- **Show Card Gen** — writes per-block Obsidian show cards + `output/playlists/{block}_{date}.json`

Dry run to validate:
```bash
uv run python mac/agents/orchestrator.py --dry-run
```

Generate show cards for a specific date:
```bash
uv run python mac/agents/show_card_gen.py --date 2026-05-21
```

Talk breaks require a working ElevenLabs voice_id in `mac/config.yaml`. Until the voice clone is ready, use any ElevenLabs voice ID for testing.

## Show Cards

Each night the pipeline drops Obsidian show cards into `~/life-os/life-os/Projects/reginerd.fm/Show Cards/YYYY-MM-DD/`:

- `YYYY-MM-DD.md` — daily index with wikilinked table of all blocks
- `YYYY-MM-DD-morning.md` etc. — one file per block with:
  - **Run of Show** — exact numbered sequence of music + DJ breaks
  - **Breaks** — rendered/pending status per segment
  - **Full Library** — all tracks in pool, grouped by artist

The feeder reads `output/playlists/{block}_{date}.json` (written by show card gen) to follow the exact show card order, keeping stream and card in sync.

## Sequencing

Tracks are ordered using an energy arc (low → medium → high → medium → low) derived from Last.fm genre tags, with priority artists (Ye, Kendrick Lamar, Young Dolph, Key Glock, Tyler the Creator) boosted throughout. Artist spacing prevents the same artist from playing in consecutive slots.

Optional BPM/energy analysis via librosa (install with `uv add librosa`):
```bash
uv run python mac/content_generator/audio_features.py --all --librosa
```

## Architecture

```
Plex NAS (DS220+)
  └── plex_music_feeder.py ──symlinks──► output/music_bumpers/{block}/

feeder.py (launchd daemon)
  ├── reads output/playlists/{block}_{date}.json (show card order)
  ├── filters play history (4hr no-repeat window, LRU fallback)
  ├── queues setlist-matched track after each track_intro break
  ├── writes output/runtime/.playlist.m3u
  └── signals ezstream (SIGHUP) on rebuild

ezstream ──► Icecast :8000/stream ──► Cloudflare Tunnel ──► radio.reginerd.tv

orchestrator.py (launchd, nightly 2am)
  └── curator → researcher → scriptwriter → narrator → show_card_gen
```

## Key Files

| File | Purpose |
|------|---------|
| `mac/feeder.py` | Playlist daemon — core of the stream |
| `mac/plex_music_feeder.py` | Populate bumper dirs from Plex by genre |
| `mac/play_history.py` | SQLite play tracker + no-repeat logic |
| `mac/agents/orchestrator.py` | Nightly talk generation pipeline |
| `mac/agents/curator.py` | Featured artist selection per block |
| `mac/agents/researcher.py` | Last.fm bio + discography context |
| `mac/agents/scriptwriter.py` | Qwen14B DJ script generation + setlist.json |
| `mac/agents/narrator.py` | ElevenLabs TTS → MP3 + TTS cache |
| `mac/agents/show_card_gen.py` | Obsidian show cards + playlist JSON |
| `mac/content_generator/audio_features.py` | Duration + BPM/energy cache (mutagen + librosa) |
| `mac/launchd/local.rgnrd.feeder.plist` | launchd daemon for feeder |
| `mac/launchd/local.rgnrd.nightly.plist` | launchd daemon for nightly batch |
| `config/persona.yaml` | reginerd base DJ persona prompt |
| `config/blocks.yaml` | Per-block context overlays |
| `config/schedule.yaml` | Full 7-day schedule |
| `config/genres.yaml` | Show → Plex genre mapping |
