import { useEffect, useRef } from 'react'
import Hls from 'hls.js'
import { getStreamUrl } from '../api'

interface Props {
  cameraId?: number
  src?: string
  muted?: boolean
  className?: string
  rotation?: number
}

export default function HlsPlayer({ cameraId, src, muted = true, className, rotation = 0 }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)

  const url = src ?? (cameraId != null ? getStreamUrl(cameraId) : '')
  const isLive = !src

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
      video.play().catch(() => {})
    })

    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) {
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR && isLive) {
          setTimeout(() => hls.loadSource(url), 3000)
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
  }, [url, isLive])

  const isRotated = rotation === 90 || rotation === 270
  const rotationStyle = rotation ? {
    transform: `rotate(${rotation}deg)${isRotated ? ' scale(1.78)' : ''}`,
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
}
