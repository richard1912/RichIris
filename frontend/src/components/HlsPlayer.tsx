import { useEffect, useRef, forwardRef, useImperativeHandle } from 'react'
import Hls from 'hls.js'
import { getStreamUrl } from '../api'

export interface HlsPlayerHandle {
  getVideoElement: () => HTMLVideoElement | null
}

interface Props {
  cameraId?: number
  src?: string
  muted?: boolean
  className?: string
  rotation?: number
  startTime?: number
  onEnded?: () => void
}

const HlsPlayer = forwardRef<HlsPlayerHandle, Props>(function HlsPlayer(
  { cameraId, src, muted = true, className, rotation = 0, startTime = 0, onEnded },
  ref,
) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)

  const url = src ?? (cameraId != null ? getStreamUrl(cameraId) : '')
  const isLive = !src

  useImperativeHandle(ref, () => ({
    getVideoElement: () => videoRef.current,
  }))

  useEffect(() => {
    const video = videoRef.current
    if (!video || !url) return

    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url
      video.play().catch(() => {})
      return
    }

    if (!Hls.isSupported()) return

    const hls = new Hls(
      isLive
        ? {
            liveSyncDurationCount: 2,
            liveMaxLatencyDurationCount: 4,
            enableWorker: true,
            lowLatencyMode: true,
          }
        : {
            enableWorker: true,
          },
    )

    hls.loadSource(url)
    hls.attachMedia(video)
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (startTime > 0) {
        video.currentTime = startTime
      }
      video.play().catch(() => {})
    })

    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) {
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR && isLive) {
          // Retry after delay - loadSource resets and re-fetches manifest
          setTimeout(() => {
            hls.loadSource(url)
            hls.startLoad()
          }, 3000)
        } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          hls.recoverMediaError()
        } else {
          hls.destroy()
        }
      }
    })

    hlsRef.current = hls

    return () => {
      hls.destroy()
      hlsRef.current = null
    }
  }, [url, isLive, startTime])

  // Attach onEnded handler
  useEffect(() => {
    const video = videoRef.current
    if (!video || !onEnded) return
    video.addEventListener('ended', onEnded)
    return () => video.removeEventListener('ended', onEnded)
  }, [onEnded])

  const isRotated = rotation === 90 || rotation === 270
  const rotationStyle = rotation ? {
    transform: `rotate(${rotation}deg)${isRotated ? ' scale(0.5625)' : ''}`,
  } : undefined

  return (
    <video
      ref={videoRef}
      muted={muted}
      autoPlay
      playsInline
      controls={!isLive}
      className={className ?? 'w-full h-full object-contain'}
      style={rotationStyle}
    />
  )
})

export default HlsPlayer
