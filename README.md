# reginerd-fm

24/7 internet radio powered by reginerd's actual Plex music library. One DJ. Five blocks. All day.

**Listen at:** [radio.reginerd.tv](https://radio.reginerd.tv)

Forked from [WRIT-FM](https://github.com/keltokhy/writ-fm). Key differences: music comes from a real Plex library on a NAS (not AI-generated), track selection is vibe-scored (not genre-gated), and there's one DJ persona instead of multiple hosts.

## Stack

| Component | Tech |
|-----------|------|
| Music source | Plex on DS220+ NAS → `mac/music_indexer.py` |
| Streaming | Icecast + ezstream on Mac Studio |
| Relay / CDN | DigitalOcean VPS → Icecast2 → `stream.reginerd.tv` (Ogg + AAC) |
| Public URL | Cloudflare Tunnel → `radio.reginerd.tv` (player + API) |
| Web player | React/Vite SPA — album art, lyrics flip, AirPlay, lock screen |
| Player API | FastAPI on port 8090 (`mac/web_player_server.py`) |
| DJ voice | ElevenLabs Professional Voice Clone (Reggie's voice) |
| Script generation | Qwen14B via Hermes (local, Mac Studio) |
| Play tracking | SQLite play history + no-repeat window |
| Show cards | Obsidian vault at `Projects/reginerd.fm/Show Cards/` |

## Schedule (Weekday)

| Block | Time | Vibe |
|-------|------|------|
| Morning | 6am–10am | R&B, Easy Listening, Jazz, Vocal |
| Midday | 10am–3pm | Pop/Rock, Reggae, Folk, Electronic |
| Prime Time | 3pm–8pm | Rap, R&B |
| Wind Down | 8pm–10pm | Mellow, atmospheric, easy BPM |
| Late Night | 10pm–6am | Ambient, instrumental, VGM, film scores |

Friday extends Prime Time to 10pm. Saturday adds Sabbath Morning (Gospel, 6–10am). See `config/schedule.yaml` for the full definition.

Block pools are built from vibe profiles (`config/blocks.yaml`) — BPM range, energy ceiling, brightness range, and boost tags — scored against the full library index. No genre gating.

## Build Status

- [x] Phase 0 — Foundation
- [x] Phase 1 — Plex Music Feeder
- [x] Phase 2 — Stream Core (live at radio.reginerd.tv)
- [x] Phase 3 — DJ Personas (code done; voice clone pending)
- [x] Phase 4 — Talk Generation (code done)
- [x] Phase 5 — Content Pipeline (no-repeat, day-of-week, orchestrator)
- [x] Phase 5.5 — Show Cards + Intelligent Sequencing
- [x] Phase 5.6 — Web Player (lyrics, AirPlay, lock screen, tap-to-tune)
- [x] Phase 5.7 — VPS Relay + Safari AAC stream
- [x] Phase 5.8 — Vibe-based library index (replaces genre gating)
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

### 3. Index the music library

```bash
set -a && source .env && set +a

# Incremental — only analyzes new/changed tracks
uv run python mac/music_indexer.py

# Force full reanalysis
uv run python mac/music_indexer.py --force

# Dry run — report counts without writing
uv run python mac/music_indexer.py --dry-run
```

Output: `output/runtime/music_library.json` — all Plex tracks with BPM, energy, brightness, Last.fm tags.

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

### 5. Start the web player server

```bash
set -a && source .env && set +a
uv run python mac/web_player_server.py
# Serves at http://localhost:8090 — Cloudflare Tunnel exposes it at radio.reginerd.tv
```

The player build is pre-committed at `output/player/`. To rebuild after editing `player/src/`:

```bash
cd player && npm install && npm run build
# Output goes to ../output/player/
```

## Operations

### Force playlist refresh

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

Tracks played in the last 4 hours are filtered from the next playlist build. If the entire pool has been played recently, the least-recently-played tracks come first (LRU order).

```bash
RGNRD_BUMPER_REPEAT_HOURS=6 uv run python mac/feeder.py ...
```

## Content Pipeline (Talk Generation)

The nightly orchestrator (`mac/agents/orchestrator.py`) runs at 2am:

```
music_indexer → curator → researcher → scriptwriter → narrator → show_card_gen
```

- **music_indexer** — indexes all Plex tracks; saves BPM/energy/brightness + Last.fm tags to `music_library.json`
- **curator** — scores library against block vibe profiles; selects featured artist; writes manifest + pool
- **researcher** — pulls Last.fm bio, discography, and tag context for the featured artist
- **scriptwriter** — writes DJ break scripts via Qwen14B; outputs MP3 filenames + `setlist.json`
- **narrator** — renders scripts to MP3 via ElevenLabs; caches reusable track intros
- **show_card_gen** — writes Obsidian show cards + `output/playlists/{block}_{date}.json`

Dry run:
```bash
uv run python mac/agents/orchestrator.py --dry-run
```

Generate show cards for a specific date:
```bash
uv run python mac/agents/show_card_gen.py --date 2026-05-21
```

## Web Player

The player at `radio.reginerd.tv` is a React SPA served by the FastAPI backend.

**Features:**
- Tap-to-tune-in overlay (browser autoplay policy workaround)
- Album art with blurred background
- Tap art to flip to synced/plain lyrics (3D CSS card flip)
- Auto-scrolling synced lyrics keyed to audio position
- Lock screen / notification shade metadata (Media Session API)
- AirPlay button on Safari/WebKit (throws stream to HomePod/Apple TV)
- Thumbs up/down voting (stored in `~/.rgnrd/votes.db`)
- Star rating from Plex, play count from station history
- Net votes badge

**Stream sources** (in priority order for the `<audio>` element):
1. `https://stream.reginerd.tv/stream.aac` — AAC 128k (Safari compatible)
2. `https://stream.reginerd.tv/stream` — Ogg/Vorbis

## VPS Relay (`stream.reginerd.tv`)

A DigitalOcean droplet relays the stream so the home Mac only serves one upstream connection. Listeners hit the VPS directly.

```
Mac Studio (Icecast :8000)
  └── Cloudflare Tunnel → radio.reginerd.tv/stream (one connection)
        └── ffmpeg relay (rgnrd-relay.service)
              └── Icecast on VPS :8000/stream  (Ogg)
                    └── ffmpeg transcode (rgnrd-aac.service)
                          └── Icecast on VPS :8000/stream.aac  (AAC 128k)
                                └── Nginx + Let's Encrypt → stream.reginerd.tv
```

Setup script: `vps/setup.sh`

## Architecture

```
music_indexer.py ──► output/runtime/music_library.json

curator.py
  ├── scores library against block vibe profiles
  ├── writes output/manifests/{block}_{date}.json  (featured + pool)
  └── refreshes output/music_bumpers/{block}/  (symlinks)

feeder.py (launchd daemon)
  ├── reads output/playlists/{block}_{date}.json
  ├── filters play history (4hr no-repeat, LRU fallback)
  ├── writes output/runtime/.playlist.m3u
  └── signals ezstream (SIGHUP) on rebuild

ezstream ──► Icecast :8000/stream ──► Cloudflare Tunnel ──► radio.reginerd.tv
                                  └── VPS relay ──► stream.reginerd.tv (Ogg + AAC)

web_player_server.py (FastAPI :8090, Cloudflare Tunnel)
  ├── GET /now-playing — current track metadata + votes + play count
  ├── GET /art        — Plex album art proxy (hides token)
  ├── GET /lyrics     — LRClib lyrics (synced + plain)
  ├── POST /vote      — thumbs up/down → votes.db
  └── /*              — serves output/player/ (React SPA)

orchestrator.py (launchd, nightly 2am)
  └── music_indexer → curator → researcher → scriptwriter → narrator → show_card_gen
```

## Key Files

| File | Purpose |
|------|---------|
| `mac/feeder.py` | Playlist daemon — core of the stream |
| `mac/music_indexer.py` | Index full Plex library with Librosa + Last.fm tags |
| `mac/audio_analyzer.py` | BPM / energy / brightness analysis (Librosa) |
| `mac/web_player_server.py` | FastAPI: now-playing, art proxy, lyrics, votes, SPA |
| `mac/play_history.py` | SQLite play tracker + no-repeat logic |
| `mac/agents/orchestrator.py` | Nightly pipeline runner |
| `mac/agents/curator.py` | Vibe scoring + featured artist selection |
| `mac/agents/researcher.py` | Last.fm bio + discography context |
| `mac/agents/scriptwriter.py` | Qwen14B DJ script generation + setlist.json |
| `mac/agents/narrator.py` | ElevenLabs TTS → MP3 + TTS cache |
| `mac/agents/show_card_gen.py` | Obsidian show cards + playlist JSON |
| `mac/health_monitor.sh` | Process health checks + alerting |
| `player/src/App.tsx` | React web player source |
| `output/player/` | Pre-built player (served by web_player_server) |
| `vps/setup.sh` | DigitalOcean relay VPS setup script |
| `config/persona.yaml` | reginerd DJ persona prompt |
| `config/blocks.yaml` | Per-block vibe profiles (BPM, energy, tags) |
| `config/schedule.yaml` | Full 7-day schedule |
