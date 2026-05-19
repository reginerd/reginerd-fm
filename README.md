# reginerd-fm

24/7 internet radio powered by reginerd's actual music library. One DJ. Five blocks. All day.

**Live at:** [radio.reginerd.tv](http://radio.reginerd.tv)

Forked from [WRIT-FM](https://github.com/keltokhy/writ-fm). Key difference: music comes from a real Plex library on a NAS (not AI-generated). One DJ persona (reginerd) instead of multiple hosts.

## Stack

- **Music source:** Plex on DS220+ NAS → `plex_music_feeder.py` (replaces ACE-Step)
- **Streaming:** Icecast + ezstream on Mac Studio → Cloudflare Tunnel
- **DJ voice:** ElevenLabs Professional Voice Clone (Reggie's actual voice) + Kokoro TTS fallback
- **Talk generation:** Claude API — reginerd persona with per-block context injection

## Schedule (Weekday)

| Block | Time | Genres |
|---|---|---|
| Morning | 6am–10am | R&B, Soul, Jazz |
| Midday | 10am–3pm | Pop/Rock, Reggae, Folk, Electronic |
| Prime Time | 3pm–8pm | Rap & R&B |
| Wind Down | 8pm–10pm | Instrumentals, chill |
| Late Night | 10pm–6am | Stage & Screen (VGM + film scores) |

Friday + weekend schedules TBD.

## Build Phases

- [x] Phase 0 — Foundation (repo, design, stack decisions)
- [ ] Phase 1 — Plex Music Feeder (`plex_music_feeder.py`)
- [ ] Phase 2 — Stream Core (Icecast + Cloudflare Tunnel)
- [ ] Phase 3 — DJ Personas (reginerd voice clone + prompts)
- [ ] Phase 4 — Talk Generation (Plex metadata, anniversaries, game context)
- [ ] Phase 5 — Content Pipeline (operator daemon, no-repeat, day-of-week)
- [ ] Phase 6 — Launch (`radio.reginerd.tv`)

[Track progress on Linear](https://linear.app/reginerd/project/reginerdfm-8cccf7fe5782)

## Listen (local dev)

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  writ CLI                     (tmux-based process manager)   │
├──────────────────────────────────────────────────────────────┤
│  station_config.py                                           │
│    ├── writ-fm  → /stream   API :8001  agent: Claude         │
│    ├── klod-fm  → /klod-fm API :8011  agent: Claude CLI      │
│    └── cdex-fm  → /cdex-fm API :8012  agent: Codex           │
├──────────────────────────────────────────────────────────────┤
│  ezstream + feeder.py per station                            │
│    ├── ezstream: Icecast source client (Ogg Vorbis)          │
│    ├── feeder.py: builds playlists per show schedule          │
│    ├── station runtime paths isolate queues/state/logs        │
│    ├── Detects new content and reloads playlist (SIGHUP)      │
│    └── Runs API server as daemon thread on station API port   │
├──────────────────────────────────────────────────────────────┤
│  Icecast :8000 ──► /stream /klod-fm /cdex-fm                 │
│  APIs ───► /now-playing /schedule /health /messages /diary   │
│  relays ─► YouTube RTMP                                      │
├──────────────────────────────────────────────────────────────┤
│  content_generator/                                          │
│    ├── talk_generator.py        (Claude CLI + Kokoro TTS)    │
│    ├── music_bumper_generator.py (ACE-Step via music-gen)    │
│    ├── listener_response_generator.py                        │
│    ├── topic_bank.py             (station-local topic pools) │
│    ├── ledger.py                 (editorial memory)          │
│    └── persona.py                (5 hosts, station identity) │
├──────────────────────────────────────────────────────────────┤
│  operator_daemon.sh             (station agent maintenance)  │
│  listener_daemon.sh             (message → on-air response)  │
└──────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Install dependencies

```bash
# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install system dependencies (macOS)
brew install icecast ffmpeg ezstream vorbis-tools

# Set up Python environment
uv sync
```

### 2. Set up TTS

```bash
cd mac/kokoro
uv venv
uv pip install kokoro soundfile
# Downloads ~200MB model on first run
```

### 3. Configure

```bash
cp config/icecast.xml.example config/icecast.xml
cp mac/config.yaml.example mac/config.yaml

# Edit mac/config.yaml and config/icecast.xml.
# The Icecast source password must match in both files.
```

Station instances live in `config/stations.yaml`. The default station is
`writ-fm`; KLOD-FM and CDEX-FM use isolated output directories, home
directories, API ports, ledgers, topic banks, and playlist state.

### 4. Start the station

The `writ` CLI manages all components via tmux:

```bash
./writ start          # Start core streaming stack (icecast, stream, tunnel)
./writ start all      # Start core + content daemons
./writ status         # Health check all components
./writ stop           # Stop everything
```

Start individual components:
```bash
./writ start icecast  # Icecast server
./writ start stream   # Streamer + API
./writ start tunnel   # Cloudflared tunnel
./writ start content  # music-gen, operator, listener
./writ start operator # Claude Code maintenance loop
./writ start youtube  # Relay selected station to YouTube RTMP
```

Run a specific station instance:
```bash
./writ --station writ-fm start stream   # WRIT-FM on /stream, API :8001
./writ --station klod-fm start stream   # KLOD-FM on /klod-fm, API :8011
./writ --station cdex-fm start stream   # CDEX-FM on /cdex-fm, API :8012
./writ start stations                   # Start KLOD-FM and CDEX-FM streams
./writ status all                       # Show all configured stations
```

Relay a station to YouTube without committing the stream key:
```bash
YOUTUBE_STREAM_KEY=... ./writ --station klod-fm start youtube
# or provide the full endpoint:
YOUTUBE_RTMP_URL=rtmp://x.rtmp.youtube.com/live2/... ./writ --station klod-fm start youtube
# optionally use a still image as the video background:
YOUTUBE_BACKGROUND_IMAGE=/path/to/background.png YOUTUBE_STREAM_KEY=... ./writ --station klod-fm start youtube
```

Other commands:
```bash
./writ logs stream -f     # Tail streamer logs
./writ attach operator    # Attach to operator tmux window
./writ restart stream     # Restart a component
```

### 5. Generate content

```bash
./writ generate talk                          # 2 talk breaks per upcoming slot
./writ generate talk --show midnight_signal   # Specific show
./writ generate music                         # AI music tracks
./writ generate status                        # Show segment counts
```

Or run generators directly:
```bash
uv run python mac/content_generator/talk_generator.py --all --count 2 --min 3
uv run python mac/content_generator/music_bumper_generator.py --all --min 20
```

## Hosts

| Host | Voice | Focus |
|------|-------|-------|
| **The Liminal Operator** | `am_michael` | Philosophy, radio lore, morning reflections |
| **Dr. Resonance** | `bm_daniel` | Music history, genre archaeology |
| **Nyx** | `af_heart` | Dreams, night philosophy |
| **Signal** | `am_onyx` | News analysis, current events |
| **Ember** | `af_bella` | Soul, funk, music as feeling |

## Weekly Schedule

8 talk shows rotate across the day. See `config/schedule.yaml` for the full definition.

**Daily base schedule:**
- 00:00-04:00 — Midnight Signal (Liminal Operator — philosophy)
- 04:00-06:00 — The Night Garden (Nyx — dreams, night)
- 06:00-09:00 — Dawn Chorus (Liminal Operator — morning reflections)
- 09:00-12:00 — Sonic Archaeology (Dr. Resonance — music history)
- 12:00-14:00 — Signal Report (Signal — news analysis)
- 14:00-16:00 — The Groove Lab (Ember — soul, funk)
- 16:00-18:00 — Crosswire (Dr. Resonance + Ember — panel debate)
- 18:00-20:00 — Sonic Archaeology
- 20:00-22:00 — The Groove Lab
- 22:00-00:00 — The Night Garden

**Weekly override:**
- Sunday 18:00-20:00 — Listener Hours (mailbag)

## Segment Types

**Hosted talk breaks** (450-1000 words):
- `deep_dive` — Compact single-topic exploration
- `news_analysis` — Current events through a late-night lens (uses RSS headlines)
- `interview` — Short simulated interview with a historical or fictional figure
- `panel` — Two hosts discuss a topic from different angles
- `story` — Narrative storytelling from music and culture
- `listener_mailbag` — Listener letters and responses
- `music_essay` — Focused essay on an artist, album, or genre

**Short-form** (transitions):
- `station_id` — Station identification
- `show_intro` — Show opening
- `show_outro` — Show closing

## Automated Operation

The operator daemon runs Claude Code on a 15-minute loop to:
1. Health-check the stream, Icecast, and encoder
2. Stock AI music tracks when music-gen.server is available (minimum 20 per show)
3. Stock short talk breaks for current and upcoming shows (minimum 3 per slot)
4. Process listener messages into on-air responses
5. Grow the station-local operator topic bank when scheduled focus areas are thin
6. Carry editorial continuity across runs via the station ledger and intent cards

```bash
./writ start operator   # Start via writ CLI (tmux-managed)
./run_operator.sh       # Run once manually
bash mac/operator_daemon.sh  # Run as a persistent loop
```

Each run reads an operator brief (`mac/content_generator/context.py
--operator-brief`) summarizing recent topics, active threads, unread
listener messages, station-local topic-bank counts, and the operator's own
recent diary entries. The
operator picks a run mode — `maintenance`, `responsive`, `continuity`,
`special`, or `quiet` — and may write intent cards in
`output/operator_intents/` to guide specific segments. Editorial decisions
and free-form diary notes are appended to the station ledger
(`~/.writ/station_ledger.jsonl`) so future runs can carry threads forward
and pick up the operator's voice across passes instead of starting cold
each time.

Each station also has an operator-managed topic bank at
`$WRIT_TOPIC_BANK_FILE`. `talk_generator.py` automatically merges those
operator-added topics with the built-in seed pools, so KLOD-FM and CDEX-FM can
keep expanding their own editorial surfaces without sharing content or editing
source code during normal operation.

The listener daemon polls for new messages every 30 seconds and generates spoken responses:
```bash
./writ start listener
```

## Customizing

**Change hosts and personalities** — Edit `mac/content_generator/persona.py`. Each host has an identity, voice style, philosophy, and anti-patterns.

**Modify the schedule** — Edit `config/schedule.yaml` to add/remove shows, change time slots, or assign different hosts and voices.

**Use different TTS voices** — Kokoro includes 28 voices (see `mac/kokoro/tts.py`). Assign voices per-show in `config/schedule.yaml`.

**Add music styles** — Edit `mac/content_generator/music_pools_expanded.py` to change the AI music generation prompts per show.

## Development

Use `uv` for Python entry points and checks:

```bash
uv sync
uv run python -m unittest discover -s tests
uv run ruff check .
```

Useful health checks while the station is running:

```bash
./writ status all
curl -sf http://localhost:8001/health
curl -sf http://localhost:8001/now-playing
```

## Files

```
├── writ                        # Station CLI (start/stop/status/logs/generate)
├── run_operator.sh             # Single operator run (station agent, with lock + timeout)
├── config/stations.yaml        # Station instances, mounts, API ports, agents, paths
├── mac/
│   ├── station_config.py        # Station config resolver and env exporter
│   ├── feeder.py               # Playlist feeder (manages ezstream + API)
│   ├── radio.xml               # Legacy ezstream config; station configs generate runtime configs
│   ├── api_server.py           # Now-playing API (daemon thread in feeder)
│   ├── schedule.py             # Schedule parser and resolver
│   ├── play_history.py         # Track history and dedup
│   ├── music_gen_client.py     # REST client for music-gen.server
│   ├── operator_prompt.md      # Music-forward operator maintenance prompt
│   ├── operator_daemon.sh      # Operator loop (runs run_operator.sh)
│   ├── listener_daemon.sh      # Listener message polling daemon
│   ├── start_music_gen.sh      # Start music-gen + daemons in tmux
│   ├── kokoro/                 # Kokoro TTS wrapper
│   ├── content_generator/
│   │   ├── talk_generator.py              # Talk segment generator (with --intent support)
│   │   ├── topic_bank.py                  # Station-local operator topic bank
│   │   ├── music_bumper_generator.py      # AI music bumper generator
│   │   ├── listener_response_generator.py # Listener message → audio
│   │   ├── context.py                     # Operator brief and intent card templates
│   │   ├── ledger.py                      # Append-only editorial memory
│   │   ├── music_pools_expanded.py        # Music generation prompts
│   │   ├── persona.py                     # Host definitions and station identity
│   │   └── helpers.py                     # Shared utilities
│   └── config.yaml             # Local config
├── config/
│   ├── schedule.yaml           # Weekly show schedule
│   └── icecast.xml.example     # Icecast template
├── output/
│   ├── talk_segments/{show}/   # Generated hosted breaks
│   ├── music_bumpers/{show}/   # AI-generated music tracks
│   └── scripts/                # Script metadata
└── docs/                       # Web-facing pages
```

## Requirements

- Python 3.11+
- uv
- ffmpeg, ezstream, vorbis-tools
- Icecast2
- Claude CLI and/or Codex CLI (for station agents)
- Kokoro TTS (~200MB model)
- music-gen.server + ACE-Step (optional, for AI music tracks)
- cloudflared (optional, for public tunnel)
- Apple Silicon recommended

## Star History

<a href="https://www.star-history.com/?repos=keltokhy%2Fwrit-fm&type=timeline&logscale=&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=keltokhy/writ-fm&type=timeline&theme=dark&logscale&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=keltokhy/writ-fm&type=timeline&logscale&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=keltokhy/writ-fm&type=timeline&logscale&legend=top-left" />
 </picture>
</a>

## License

MIT
