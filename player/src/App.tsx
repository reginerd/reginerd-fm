import { useEffect, useRef, useState, useCallback } from 'react'
import './App.css'

interface NowPlaying {
  artist: string | null
  title: string | null
  album: string | null
  year: number | null
  tags: string[]
  plex_rating: number | null
  show: string | null
  show_id: string | null
  art_url: string | null
  filepath: string | null
  net_votes: number
  play_count: number
}

interface Lyrics {
  synced: string | null
  plain: string | null
  instrumental: boolean
  source: string | null
}

interface SyncedLine {
  time: number
  text: string
}

const POLL_INTERVAL = 5000

function parseSynced(raw: string): SyncedLine[] {
  return raw
    .split('\n')
    .map(line => {
      const m = line.match(/^\[(\d+):(\d+\.\d+)\]\s*(.*)$/)
      if (!m) return null
      const time = parseInt(m[1]) * 60 + parseFloat(m[2])
      return { time, text: m[3] }
    })
    .filter((l): l is SyncedLine => l !== null && l.text.trim() !== '')
}

function ThumbIcon({ direction }: { direction: 'up' | 'down' }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" width="22" height="22">
      {direction === 'up' ? (
        <path d="M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"/>
      ) : (
        <path d="M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z"/>
      )}
    </svg>
  )
}

function StarRating({ rating }: { rating: number }) {
  const stars = Math.round(rating / 2)
  return (
    <div className="star-rating" title={`${rating}/10`}>
      {[1,2,3,4,5].map(i => (
        <svg key={i} viewBox="0 0 24 24" fill={i <= stars ? 'currentColor' : 'none'}
          stroke="currentColor" strokeWidth="1.5" width="12" height="12">
          <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
        </svg>
      ))}
    </div>
  )
}

export default function App() {
  const [np, setNp] = useState<NowPlaying | null>(null)
  const [fading, setFading] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [voted, setVoted] = useState<1 | -1 | null>(null)
  const [voteAnim, setVoteAnim] = useState<'up' | 'down' | null>(null)
  const [showOverlay, setShowOverlay] = useState(true)
  const [hasAirPlay, setHasAirPlay] = useState(false)
  const [showLyrics, setShowLyrics] = useState(false)
  const [lyrics, setLyrics] = useState<Lyrics | null>(null)
  const [lyricsLoading, setLyricsLoading] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [syncedLines, setSyncedLines] = useState<SyncedLine[]>([])
  const [activeLine, setActiveLine] = useState(-1)
  const audioRef = useRef<HTMLAudioElement>(null)
  const prevPathRef = useRef<string | null>(null)
  const lyricsRef = useRef<HTMLDivElement>(null)
  const activeLineRef = useRef<HTMLDivElement>(null)

  const fetchNowPlaying = useCallback(async () => {
    try {
      const res = await fetch('/now-playing')
      if (!res.ok) return
      const data: NowPlaying = await res.json()

      if (data.filepath !== prevPathRef.current) {
        if (prevPathRef.current !== null) {
          setFading(true)
          await new Promise(r => setTimeout(r, 400))
          setFading(false)
        }
        prevPathRef.current = data.filepath
        setVoted(null)
        setLyrics(null)
        setSyncedLines([])
        setActiveLine(-1)
        setShowLyrics(false)
      }

      setNp(data)
    } catch {
      // silent
    }
  }, [])

  const fetchLyrics = useCallback(async (artist: string, title: string, album: string) => {
    setLyricsLoading(true)
    try {
      const params = new URLSearchParams({ artist, title, album })
      const res = await fetch(`/lyrics?${params}`)
      if (!res.ok) return
      const data: Lyrics = await res.json()
      setLyrics(data)
      if (data.synced) {
        setSyncedLines(parseSynced(data.synced))
      }
    } catch {
      // silent
    } finally {
      setLyricsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchNowPlaying()
    const id = setInterval(fetchNowPlaying, POLL_INTERVAL)
    return () => clearInterval(id)
  }, [fetchNowPlaying])

  // Pre-fetch lyrics as soon as we have a track
  useEffect(() => {
    if (np?.artist && np?.title && !lyrics && !lyricsLoading) {
      fetchLyrics(np.artist, np.title, np.album ?? '')
    }
  }, [np, lyrics, lyricsLoading, fetchLyrics])

  // Sync lyrics to audio position
  useEffect(() => {
    if (!syncedLines.length) return
    const idx = [...syncedLines].reverse().findIndex(l => l.time <= currentTime)
    const active = idx === -1 ? -1 : syncedLines.length - 1 - idx
    setActiveLine(active)
  }, [currentTime, syncedLines])

  // Scroll active lyric line into view within the art container
  useEffect(() => {
    if (!showLyrics || activeLine < 0 || !activeLineRef.current || !lyricsRef.current) return
    const container = lyricsRef.current
    const line = activeLineRef.current
    const target = line.offsetTop - container.clientHeight / 2 + line.offsetHeight / 2
    container.scrollTo({ top: Math.max(0, target), behavior: 'smooth' })
  }, [activeLine, showLyrics])

  // Audio time tracking
  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    const onTime = () => setCurrentTime(audio.currentTime)
    audio.addEventListener('timeupdate', onTime)
    return () => audio.removeEventListener('timeupdate', onTime)
  }, [])

  // Detect AirPlay support (WebKit only)
  useEffect(() => {
    const audio = audioRef.current
    if (audio && 'remote' in audio) setHasAirPlay(true)
  }, [])

  // Lock screen / media session metadata
  useEffect(() => {
    if (!np || !('mediaSession' in navigator)) return
    navigator.mediaSession.metadata = new MediaMetadata({
      title: np.title ?? '',
      artist: np.artist ?? '',
      album: np.album ?? '',
      artwork: np.art_url
        ? [{ src: np.art_url, sizes: '512x512', type: 'image/jpeg' }]
        : [],
    })
  }, [np])

  const startPlayback = () => {
    const audio = audioRef.current
    if (!audio) return
    audio.load()
    audio.play().then(() => setPlaying(true)).catch(() => {})
    setShowOverlay(false)
  }

  const togglePlay = () => {
    const audio = audioRef.current
    if (!audio) return
    if (playing) {
      audio.pause()
      setPlaying(false)
    } else {
      audio.load()
      audio.play().then(() => setPlaying(true)).catch(() => setPlaying(false))
    }
  }

  const triggerAirPlay = () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(audioRef.current as any)?.remote?.prompt?.()
  }

  const sendVote = async (v: 1 | -1) => {
    if (voted !== null) return
    try {
      const res = await fetch('/vote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vote: v }),
      })
      if (!res.ok) return
      const data = await res.json()
      setVoted(v)
      setVoteAnim(v === 1 ? 'up' : 'down')
      setTimeout(() => setVoteAnim(null), 600)
      setNp(prev => prev ? { ...prev, net_votes: data.net_votes } : prev)
    } catch {
      // silent
    }
  }

  const artUrl = np?.art_url
  const artist = np?.artist ?? '—'
  const title = np?.title ?? '—'
  const album = np?.album ?? ''
  const show = np?.show

  const hasLyrics = lyrics && !lyrics.instrumental && (lyrics.synced || lyrics.plain)

  const lyricsContent = (() => {
    if (lyricsLoading) return <div className="lyrics-status">Loading…</div>
    if (!lyrics) return <div className="lyrics-status">Loading…</div>
    if (lyrics.instrumental) return <div className="lyrics-status">Instrumental</div>
    if (!lyrics.synced && !lyrics.plain) return <div className="lyrics-status">No lyrics found</div>
    if (lyrics.synced && syncedLines.length) {
      return syncedLines.map((line, i) => (
        <div
          key={i}
          ref={i === activeLine ? activeLineRef : undefined}
          className={`lyric-line ${i === activeLine ? 'active' : ''} ${i < activeLine ? 'past' : ''}`}
        >
          {line.text}
        </div>
      ))
    }
    return (lyrics.plain ?? '').split('\n').map((line, i) => (
      <div key={i} className={`lyric-line plain ${!line.trim() ? 'spacer' : ''}`}>{line || ' '}</div>
    ))
  })()

  return (
    <div className="player-root">
      <div
        className={`bg-art ${fading ? 'fading' : ''}`}
        style={artUrl ? { backgroundImage: `url(${artUrl})` } : undefined}
      />
      <div className="bg-overlay" />

      <div className="layout">
        <div className="card">

          {/* Art / Lyrics flip */}
          <div
            className={`art-wrap ${fading ? 'fading' : ''}`}
            onClick={() => setShowLyrics(v => !v)}
            role="button"
            aria-label={showLyrics ? 'Show album art' : 'Show lyrics'}
          >
            <div className={`art-flip ${showLyrics ? 'flipped' : ''}`}>
              <div className="art-front">
                {artUrl ? (
                  <img src={artUrl} alt={album} className="art-img" />
                ) : (
                  <div className="art-placeholder">
                    <svg viewBox="0 0 24 24" fill="currentColor" width="64" height="64" opacity={0.3}>
                      <path d="M12 3v10.55A4 4 0 1 0 14 17V7h4V3h-6z"/>
                    </svg>
                  </div>
                )}
                {hasLyrics && (
                  <div className="lyrics-hint">
                    <svg viewBox="0 0 24 24" fill="currentColor" width="13" height="13">
                      <path d="M12 3v10.55A4 4 0 1 0 14 17V7h4V3h-6z"/>
                    </svg>
                  </div>
                )}
              </div>
              <div className="art-back">
                <div className={`lyrics-in-art${syncedLines.length ? ' synced' : ''}`} ref={lyricsRef}>
                  {lyricsContent}
                </div>
              </div>
            </div>
          </div>

          <div className={`track-info ${fading ? 'fading' : ''}`}>
            <div className="track-title">{title}</div>
            <div className="track-artist">{artist}</div>
            {album && <div className="track-album">{album}{np?.year ? ` · ${np.year}` : ''}</div>}
            {show && <div className="track-show">{show}</div>}
            {np?.plex_rating ? <StarRating rating={np.plex_rating} /> : null}
          </div>

          <div className="controls">
            <button
              className={`vote-btn down ${voted === -1 ? 'active' : ''} ${voteAnim === 'down' ? 'pop' : ''}`}
              onClick={() => sendVote(-1)}
              disabled={voted !== null}
              aria-label="Thumbs down"
            >
              <ThumbIcon direction="down" />
            </button>

            <button className="play-btn" onClick={togglePlay} aria-label={playing ? 'Pause' : 'Play'}>
              {playing ? (
                <svg viewBox="0 0 24 24" fill="currentColor" width="28" height="28">
                  <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>
                </svg>
              ) : (
                <svg viewBox="0 0 24 24" fill="currentColor" width="28" height="28">
                  <path d="M8 5v14l11-7z"/>
                </svg>
              )}
            </button>

            <button
              className={`vote-btn up ${voted === 1 ? 'active' : ''} ${voteAnim === 'up' ? 'pop' : ''}`}
              onClick={() => sendVote(1)}
              disabled={voted !== null}
              aria-label="Thumbs up"
            >
              <ThumbIcon direction="up" />
            </button>
          </div>

          {hasAirPlay && (
            <button className="airplay-btn" onClick={triggerAirPlay} aria-label="AirPlay">
              <svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18">
                <path d="M6 22h12l-6-6zM21 3H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h4v-2H3V5h18v12h-4v2h4c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2z"/>
              </svg>
            </button>
          )}

          {np?.net_votes !== undefined && np.net_votes !== 0 && (
            <div className={`net-votes ${np.net_votes > 0 ? 'positive' : 'negative'}`}>
              {np.net_votes > 0 ? `+${np.net_votes}` : np.net_votes}
            </div>
          )}

          {(np?.play_count ?? 0) > 0 && (
            <div className="play-count">{np!.play_count} play{np!.play_count !== 1 ? 's' : ''} on station</div>
          )}

        </div>
      </div>

      <div className="station-badge">REGINERD·FM</div>

      {showOverlay && (
        <div className="tune-in-overlay" onClick={startPlayback}>
          <div className="tune-in-content">
            <div className="tune-in-logo">REGINERD·FM</div>
            {np && <div className="tune-in-track">{np.title} — {np.artist}</div>}
            <div className="tune-in-cta">
              <svg viewBox="0 0 24 24" fill="currentColor" width="52" height="52">
                <path d="M8 5v14l11-7z"/>
              </svg>
              <span>tap to tune in</span>
            </div>
          </div>
        </div>
      )}

      {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
      <audio ref={audioRef} preload="none" {...{ 'x-webkit-airplay': 'allow' } as any}>
        <source src="https://stream.reginerd.tv/stream.aac" type="audio/aac" />
        <source src="https://stream.reginerd.tv/stream" type="audio/ogg" />
      </audio>
    </div>
  )
}
