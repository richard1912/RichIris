import { useState, useCallback, useRef, useEffect } from 'react'
import type { Camera, StreamStatus } from '../api'
import { startPlaybackSession } from '../api'
import HlsPlayer from './HlsPlayer'
import Timeline from './Timeline'

interface Props {
  camera: Camera
  stream?: StreamStatus
  onBack: () => void
}

const SPEEDS = [-32, -16, -4, -2, -1, 1, 2, 4, 16, 32] as const
type Speed = typeof SPEEDS[number]

export default function CameraFullscreen({ camera, stream, onBack }: Props) {
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
  // Track the NVR time that playback started at
  const playbackStartTimeRef = useRef<string | null>(null)
  // Virtual NVR time for fast/reverse playback
  const virtualTimeRef = useRef<number>(0) // ms since epoch (local)
  const speedIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const clearSpeedInterval = useCallback(() => {
    if (speedIntervalRef.current) {
      clearInterval(speedIntervalRef.current)
      speedIntervalRef.current = null
    }
  }, [])

  const handlePlayback = useCallback(
    async (start: string) => {
      setPlaybackLoading(true)
      setPlaybackError(null)
      clearSpeedInterval()
      setSpeed(1)
      try {
        const { playback_url, window_end, has_more } = await startPlaybackSession(camera.id, start)
        setPlaybackUrl(playback_url)
        setWindowEnd(window_end)
        setHasMore(has_more)
        setMode('playback')
        playbackStartTimeRef.current = start
        // Parse start as local time
        virtualTimeRef.current = new Date(start).getTime()
      } catch (e) {
        setPlaybackError(e instanceof Error ? e.message : 'Playback failed')
      } finally {
        setPlaybackLoading(false)
      }
    },
    [camera.id, clearSpeedInterval],
  )

  const handleEnded = useCallback(() => {
    if (hasMore && windowEnd) {
      handlePlayback(windowEnd)
    }
  }, [hasMore, windowEnd, handlePlayback])

  // Attach onEnded handler to playback video
  useEffect(() => {
    const video = videoRef.current
    if (!video || mode !== 'playback') return
    video.addEventListener('ended', handleEnded)
    return () => video.removeEventListener('ended', handleEnded)
  }, [handleEnded, mode, playbackUrl])

  const handleLive = useCallback(() => {
    setMode('live')
    setPaused(false)
    setPlaybackUrl(null)
    setPlaybackError(null)
    clearSpeedInterval()
    setSpeed(1)
  }, [clearSpeedInterval])

  const handlePause = useCallback(() => {
    setPaused(p => !p)
  }, [])

  // Format local ISO string without timezone conversion
  const formatLocalISO = (ms: number): string => {
    const d = new Date(ms)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}T${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
  }

  // Handle speed changes
  const handleSpeedChange = useCallback(
    (newSpeed: Speed) => {
      clearSpeedInterval()
      setSpeed(newSpeed)

      const video = videoRef.current

      // Native playback rate for 1x, 2x, 4x forward
      if (newSpeed >= 1 && newSpeed <= 4) {
        if (video) video.playbackRate = newSpeed
        return
      }

      // For high-speed forward or any reverse, use interval-based jumping
      if (video) video.playbackRate = 1 // Reset native rate

      const TICK_MS = 500
      const jumpSeconds = newSpeed * (TICK_MS / 1000) // How many NVR seconds per tick

      speedIntervalRef.current = setInterval(() => {
        virtualTimeRef.current += jumpSeconds * 1000

        if (newSpeed > 0) {
          // Fast forward: jump video currentTime or request new session
          const vid = videoRef.current
          if (vid) {
            vid.currentTime += Math.abs(jumpSeconds)
            // If we've gone past the video duration, request next window
            if (vid.currentTime >= vid.duration - 1) {
              clearInterval(speedIntervalRef.current!)
              speedIntervalRef.current = null
              const newStart = formatLocalISO(virtualTimeRef.current)
              handlePlayback(newStart)
            }
          }
        } else {
          // Reverse: request new playback session at earlier time
          clearInterval(speedIntervalRef.current!)
          speedIntervalRef.current = null
          const newStart = formatLocalISO(virtualTimeRef.current)
          // Don't use handlePlayback since it resets speed
          setPlaybackLoading(true)
          setPlaybackError(null)
          startPlaybackSession(camera.id, newStart)
            .then(({ playback_url, window_end, has_more }) => {
              setPlaybackUrl(playback_url)
              setWindowEnd(window_end)
              setHasMore(has_more)
              setMode('playback')
              playbackStartTimeRef.current = newStart
              setPlaybackLoading(false)
              // Resume reverse interval
              if (newSpeed < 0) {
                speedIntervalRef.current = setInterval(() => {
                  virtualTimeRef.current += jumpSeconds * 1000
                  clearInterval(speedIntervalRef.current!)
                  speedIntervalRef.current = null
                  handleSpeedChange(newSpeed)
                }, TICK_MS)
              }
            })
            .catch((e) => {
              setPlaybackError(e instanceof Error ? e.message : 'Playback failed')
              setPlaybackLoading(false)
            })
        }
      }, TICK_MS)
    },
    [camera.id, clearSpeedInterval, handlePlayback],
  )

  // Cleanup interval on unmount
  useEffect(() => {
    return () => clearSpeedInterval()
  }, [clearSpeedInterval])

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
        {isLive && !paused && stream?.uptime_seconds != null && (
          <span className="text-xs text-neutral-500 ml-auto">
            Up {formatUptime(stream.uptime_seconds)}
          </span>
        )}
        {isLive && paused && (
          <span className="text-xs text-yellow-400 ml-auto">Paused</span>
        )}
        {!isLive && (
          <span className="text-xs text-blue-400 ml-auto">Playback</span>
        )}
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
            <HlsPlayer
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
            key={playbackUrl}
            src={playbackUrl}
            muted
            autoPlay
            playsInline
            controls
            className="max-h-[calc(100vh-8rem)] w-full object-contain"
            style={rotationStyle}
          />
        ) : null}
      </main>

      {/* Speed controls - only in playback mode */}
      {!isLive && playbackUrl && !playbackLoading && (
        <div className="flex items-center justify-center gap-1 px-4 py-2 bg-neutral-900/80">
          {SPEEDS.map((s) => (
            <button
              key={s}
              onClick={() => handleSpeedChange(s)}
              className={`px-2 py-1 rounded text-xs font-medium transition-colors ${
                speed === s
                  ? 'bg-blue-600 text-white'
                  : 'bg-neutral-800 text-neutral-400 hover:text-white'
              }`}
            >
              {s > 0 ? `${s}x` : `${s}x`}
            </button>
          ))}
        </div>
      )}

      <Timeline
        cameraId={camera.id}
        onPlayback={handlePlayback}
        onLive={handleLive}
        isLive={isLive}
        onPause={handlePause}
        isPaused={paused}
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
