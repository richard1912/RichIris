import { useState, useCallback, useRef, useEffect } from 'react'
import type { Camera, StreamStatus } from '../api'
import { startPlaybackSession, fetchServerTzOffsetMs } from '../api'
import MsePlayer from './MsePlayer'
import Timeline from './Timeline'

interface Props {
  camera: Camera
  stream?: StreamStatus
  onBack: () => void
  quality?: string
  onQualityChange?: (q: string) => void
}

const SPEEDS = [-32, -16, -4, -2, -1, 1, 2, 4, 16, 32] as const
type Speed = typeof SPEEDS[number]

export default function CameraFullscreen({ camera, stream, onBack, quality = 'high', onQualityChange }: Props) {
  const running = stream?.running ?? false
  const [mode, setMode] = useState<'live' | 'playback'>('live')
  const [paused, setPaused] = useState(false)
  const [playbackUrl, setPlaybackUrl] = useState<string | null>(null)
  const [playbackLoading, setPlaybackLoading] = useState(false)
  const [playbackError, setPlaybackError] = useState<string | null>(null)
  const [windowEnd, setWindowEnd] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [speed, setSpeed] = useState<Speed>(1)

  const videoRef = useRef<HTMLVideoElement>(null)
  const playbackStartTimeRef = useRef<string | null>(null)
  const virtualTimeRef = useRef<number>(0)
  const speedIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  // Guard to prevent overlapping reverse session loads
  const reverseLoadingRef = useRef(false)
  // Generation counter to cancel stale async operations (Bug 3)
  const generationRef = useRef(0)

  // Timezone difference: server_tz - client_tz in ms.
  // Adding this to Date.now() makes getHours() return server-local hours.
  const tzOffsetMsRef = useRef<number>(0)
  useEffect(() => {
    fetchServerTzOffsetMs().then(ms => { tzOffsetMsRef.current = ms })
  }, [])

  const clearSpeedInterval = useCallback(() => {
    if (speedIntervalRef.current) {
      clearInterval(speedIntervalRef.current)
      speedIntervalRef.current = null
    }
    reverseLoadingRef.current = false
    generationRef.current += 1
  }, [])

  const handlePlayback = useCallback(
    async (start: string) => {
      setPlaybackLoading(true)
      setPlaybackError(null)
      clearSpeedInterval()
      setSpeed(1)
      try {
        const { playback_url, window_end, has_more } = await startPlaybackSession(camera.id, start, quality)
        setPlaybackUrl(playback_url)
        setWindowEnd(window_end)
        setHasMore(has_more)
        setMode('playback')
        playbackStartTimeRef.current = start
        virtualTimeRef.current = new Date(start).getTime()
      } catch (e) {
        setPlaybackError(e instanceof Error ? e.message : 'Playback failed')
      } finally {
        setPlaybackLoading(false)
      }
    },
    [camera.id, clearSpeedInterval, quality],
  )

  const handleEnded = useCallback(() => {
    // Don't auto-advance during reverse or fast playback
    if (speedIntervalRef.current !== null) return
    if (hasMore && windowEnd) {
      handlePlayback(windowEnd)
    }
  }, [hasMore, windowEnd, handlePlayback])

  useEffect(() => {
    const video = videoRef.current
    if (!video || mode !== 'playback') return
    video.addEventListener('ended', handleEnded)
    return () => video.removeEventListener('ended', handleEnded)
  }, [handleEnded, mode, playbackUrl])

  const handleLive = useCallback(() => {
    if (mode === 'live') {
      setPaused(p => !p)
      return
    }
    setMode('live')
    setPaused(false)
    setPlaybackUrl(null)
    setPlaybackError(null)
    clearSpeedInterval()
    setSpeed(1)
  }, [mode, clearSpeedInterval])

  const formatLocalISO = (ms: number): string => {
    const d = new Date(ms)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}T${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
  }

  // Start a reverse interval that scrubs currentTime backwards,
  // and loads earlier sessions directly on the video element when needed.
  const startReverseInterval = useCallback(
    (vid: HTMLVideoElement, reverseSpeed: Speed) => {
      clearSpeedInterval()
      vid.pause()

      const gen = generationRef.current
      const TICK_MS = 500
      const jumpSeconds = reverseSpeed * (TICK_MS / 1000) // negative

      speedIntervalRef.current = setInterval(() => {
        if (reverseLoadingRef.current) return
        if (generationRef.current !== gen) return
        const v = videoRef.current
        if (!v) return

        // Bug 2: Check boundary BEFORE applying the jump
        const proposedTime = v.currentTime + jumpSeconds

        if (proposedTime <= 0.5) {
          // Reached start of this video — load the PREVIOUS 30-min window
          reverseLoadingRef.current = true

          // Bug 2: Snap virtualTimeRef to playbackStartTimeRef to eliminate drift
          if (playbackStartTimeRef.current) {
            virtualTimeRef.current = new Date(playbackStartTimeRef.current).getTime()
          }

          const WINDOW_MS = 30 * 60 * 1000
          const newStartMs = virtualTimeRef.current - WINDOW_MS
          const newStart = formatLocalISO(newStartMs)

          startPlaybackSession(camera.id, newStart, quality)
            .then(({ playback_url, window_end, has_more }) => {
              // Bug 3: Discard if generation changed
              if (generationRef.current !== gen) return
              const v2 = videoRef.current
              if (!v2) { reverseLoadingRef.current = false; return }

              playbackStartTimeRef.current = newStart

              // Bug 6: Set DOM source first, defer React state
              v2.src = playback_url
              v2.load()

              const onMetadata = () => {
                if (generationRef.current !== gen) return
                // Bug 4: Seek to end, then wait for seeked
                const seekTarget = Math.max(0, v2.duration - 1)
                v2.currentTime = seekTarget

                const onSeeked = () => {
                  if (generationRef.current !== gen) return
                  // Force frame rendering
                  v2.play().then(() => {
                    v2.pause()
                    // Bug 2: Resync virtualTimeRef after loading new session
                    virtualTimeRef.current = newStartMs + seekTarget * 1000
                    // Bug 6: Update React state last
                    setPlaybackUrl(playback_url)
                    setWindowEnd(window_end)
                    setHasMore(has_more)
                    reverseLoadingRef.current = false
                  }).catch(() => {
                    v2.pause()
                    virtualTimeRef.current = newStartMs + seekTarget * 1000
                    setPlaybackUrl(playback_url)
                    setWindowEnd(window_end)
                    setHasMore(has_more)
                    reverseLoadingRef.current = false
                  })
                }
                v2.addEventListener('seeked', onSeeked, { once: true })
              }

              if (v2.readyState >= 1) {
                onMetadata()
              } else {
                v2.addEventListener('loadedmetadata', onMetadata, { once: true })
              }
            })
            .catch(() => {
              // Bug 5: Stop reverse, show error, don't retry
              if (generationRef.current !== gen) return
              clearSpeedInterval()
              setSpeed(1)
              setPlaybackError('No earlier recordings available')
              setTimeout(() => setPlaybackError(null), 3000)
            })
        } else {
          // Normal tick
          v.currentTime = proposedTime
          virtualTimeRef.current += jumpSeconds * 1000
        }
      }, TICK_MS)
    },
    [camera.id, clearSpeedInterval, quality],
  )

  const handleSpeedChange = useCallback(
    (newSpeed: Speed) => {
      clearSpeedInterval()
      setSpeed(newSpeed)

      const video = videoRef.current

      // If in live mode, start a playback session first
      if (mode === 'live' || !video) {
        // Bug 1: 30-minute buffer to match backend PLAYBACK_WINDOW
        const startMs = Date.now() + tzOffsetMsRef.current - 30 * 60 * 1000
        const startStr = formatLocalISO(startMs)
        const gen = generationRef.current
        setPlaybackLoading(true)
        setPlaybackError(null)
        setPaused(false)

        startPlaybackSession(camera.id, startStr, quality)
          .then(({ playback_url, window_end, has_more }) => {
            // Bug 3: Discard if generation changed
            if (generationRef.current !== gen) return
            setPlaybackUrl(playback_url)
            setWindowEnd(window_end)
            setHasMore(has_more)
            setMode('playback')
            playbackStartTimeRef.current = startStr
            virtualTimeRef.current = startMs
            setPlaybackLoading(false)

            // Wait for video element, then apply speed
            const waitForVideo = () => {
              if (generationRef.current !== gen) return
              const v = videoRef.current
              if (!v) { requestAnimationFrame(waitForVideo); return }

              const applyOnReady = () => {
                if (generationRef.current !== gen) return
                if (newSpeed < 0) {
                  v.currentTime = Math.max(0, v.duration - 1)
                  virtualTimeRef.current = startMs + v.currentTime * 1000
                  startReverseInterval(v, newSpeed)
                } else if (newSpeed >= 1 && newSpeed <= 4) {
                  v.playbackRate = newSpeed
                  v.play().catch(() => {})
                } else {
                  // 16x, 32x forward
                  v.playbackRate = 1
                  v.play().catch(() => {})
                  const TICK_MS = 500
                  const jumpSec = newSpeed * (TICK_MS / 1000)
                  speedIntervalRef.current = setInterval(() => {
                    if (generationRef.current !== gen) return
                    virtualTimeRef.current += jumpSec * 1000
                    const vid = videoRef.current
                    if (!vid) return
                    vid.currentTime += jumpSec
                    if (vid.currentTime >= vid.duration - 1) {
                      clearInterval(speedIntervalRef.current!)
                      speedIntervalRef.current = null
                      handlePlayback(formatLocalISO(virtualTimeRef.current))
                    }
                  }, TICK_MS)
                }
              }

              if (v.readyState >= 1) applyOnReady()
              else v.addEventListener('loadedmetadata', applyOnReady, { once: true })
            }
            requestAnimationFrame(waitForVideo)
          })
          .catch((e) => {
            if (generationRef.current !== gen) return
            setPlaybackError(e instanceof Error ? e.message : 'Playback failed')
            setPlaybackLoading(false)
          })
        return
      }

      // Already in playback mode with a video element
      // Sync virtualTimeRef to actual video position
      if (playbackStartTimeRef.current) {
        const startMs = new Date(playbackStartTimeRef.current).getTime()
        virtualTimeRef.current = startMs + video.currentTime * 1000
      }

      // Native playback rate for 1x, 2x, 4x forward
      if (newSpeed >= 1 && newSpeed <= 4) {
        video.playbackRate = newSpeed
        video.play().catch(() => {})
        return
      }

      // Reverse: use dedicated reverse interval
      if (newSpeed < 0) {
        startReverseInterval(video, newSpeed)
        return
      }

      // Fast forward (16x, 32x)
      video.playbackRate = 1
      video.play().catch(() => {})

      const TICK_MS = 500
      const jumpSeconds = newSpeed * (TICK_MS / 1000)

      speedIntervalRef.current = setInterval(() => {
        virtualTimeRef.current += jumpSeconds * 1000
        const vid = videoRef.current
        if (!vid) return
        vid.currentTime += Math.abs(jumpSeconds)
        if (vid.currentTime >= vid.duration - 1) {
          clearInterval(speedIntervalRef.current!)
          speedIntervalRef.current = null
          const newStart = formatLocalISO(virtualTimeRef.current)
          handlePlayback(newStart)
        }
      }, TICK_MS)
    },
    [camera.id, mode, clearSpeedInterval, handlePlayback, startReverseInterval, quality],
  )

  useEffect(() => {
    return () => clearSpeedInterval()
  }, [clearSpeedInterval])

  const getNvrTimeRef = useRef<() => number | null>(() => null)
  getNvrTimeRef.current = () => {
    if (mode === 'live') return Date.now() + tzOffsetMsRef.current
    if (!playbackUrl) return null
    if (speed >= 16 || speed <= -1) return virtualTimeRef.current + tzOffsetMsRef.current
    const video = videoRef.current
    if (video && playbackStartTimeRef.current) {
      const startMs = new Date(playbackStartTimeRef.current).getTime()
      return startMs + video.currentTime * 1000 + tzOffsetMsRef.current
    }
    return null
  }

  const isLive = mode === 'live'
  const rot = camera.rotation || 0
  const isRotated = rot === 90 || rot === 270
  const rotationStyle = rot ? {
    transform: `rotate(${rot}deg)${isRotated ? ' scale(0.5625)' : ''}`,
  } : undefined

  return (
    <div className="min-h-screen flex flex-col bg-black">
      <header className="flex items-center gap-4 px-4 py-3 bg-neutral-900/80 backdrop-blur">
        <button
          onClick={onBack}
          className="text-sm text-neutral-400 hover:text-white transition-colors"
        >
          &larr; Back
        </button>
        <h2 className="text-sm font-medium">{camera.name}</h2>
        <span
          className={`w-2 h-2 rounded-full ${
            running ? 'bg-green-500' : 'bg-yellow-500'
          }`}
        />
        <div className="ml-auto flex items-center gap-3">
          {isLive && stream?.uptime_seconds != null && (
            <span className="text-xs text-neutral-500">
              Up {formatUptime(stream.uptime_seconds)}
            </span>
          )}
          {!isLive && (
            <span className="text-xs text-blue-400">Playback</span>
          )}
          {onQualityChange && (
            <select
              value={quality}
              onChange={e => onQualityChange(e.target.value)}
              className="bg-neutral-800 text-neutral-300 text-xs rounded px-2 py-1 border border-neutral-700 focus:outline-none focus:border-neutral-500"
            >
              <option value="direct">Direct</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
          )}
        </div>
      </header>

      <main className="flex-1 flex items-center justify-center overflow-hidden">
        {playbackLoading && (
          <div className="text-neutral-400 text-sm">Preparing playback...</div>
        )}
        {playbackError && (
          <div className="text-red-400 text-sm">{playbackError}</div>
        )}
        {!playbackLoading && !playbackError && isLive ? (
          paused ? (
            <div className="text-yellow-500 text-sm">Feed paused</div>
          ) : running ? (
            <MsePlayer
              cameraId={camera.id}
              muted
              rotation={rot}
              className="max-h-[calc(100vh-8rem)] w-full object-contain"
            />
          ) : (
            <div className="text-neutral-600">
              {camera.enabled ? 'Stream connecting...' : 'Camera disabled'}
            </div>
          )
        ) : !playbackLoading && !playbackError && playbackUrl ? (
          <video
            ref={videoRef}
            src={playbackUrl}
            muted
            autoPlay={speed >= 0}
            playsInline
            controls
            className="max-h-[calc(100vh-8rem)] w-full object-contain"
            style={rotationStyle}
          />
        ) : null}
      </main>

      <Timeline
        cameraId={camera.id}
        onPlayback={handlePlayback}
        onLive={handleLive}
        isLive={isLive}
        isPaused={paused}
        getNvrTime={getNvrTimeRef}
        speed={speed}
        onSpeedChange={handleSpeedChange as (s: number) => void}
        speeds={SPEEDS}
      />
    </div>
  )
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}
