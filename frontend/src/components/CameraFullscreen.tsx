import { useState, useCallback } from 'react'
import type { Camera, StreamStatus } from '../api'
import { startPlaybackSession } from '../api'
import HlsPlayer from './HlsPlayer'
import Timeline from './Timeline'

interface Props {
  camera: Camera
  stream?: StreamStatus
  onBack: () => void
}

export default function CameraFullscreen({ camera, stream, onBack }: Props) {
  const running = stream?.running ?? false
  const [mode, setMode] = useState<'live' | 'playback'>('live')
  const [paused, setPaused] = useState(false)
  const [playbackUrl, setPlaybackUrl] = useState<string | null>(null)
  const [playbackLoading, setPlaybackLoading] = useState(false)
  const [playbackError, setPlaybackError] = useState<string | null>(null)

  const handlePlayback = useCallback(
    async (start: string, end: string) => {
      setPlaybackLoading(true)
      setPlaybackError(null)
      try {
        const url = await startPlaybackSession(camera.id, start, end)
        setPlaybackUrl(url)
        setMode('playback')
      } catch (e) {
        setPlaybackError(e instanceof Error ? e.message : 'Playback failed')
      } finally {
        setPlaybackLoading(false)
      }
    },
    [camera.id],
  )

  const handleLive = useCallback(() => {
    setMode('live')
    setPaused(false)
    setPlaybackUrl(null)
    setPlaybackError(null)
  }, [])

  const handlePause = useCallback(() => {
    setPaused(p => !p)
  }, [])

  const isLive = mode === 'live'
  const rot = camera.rotation || 0

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
          <HlsPlayer
            key={playbackUrl}
            src={playbackUrl}
            muted
            rotation={rot}
            className="max-h-[calc(100vh-8rem)] w-full object-contain"
          />
        ) : null}
      </main>

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
