import { useEffect, useRef, useState } from 'react'

interface Props {
  cameraId: number
  muted?: boolean
  className?: string
  rotation?: number
}

export default function MsePlayer({ cameraId, muted = true, className, rotation = 0 }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const msRef = useRef<MediaSource | null>(null)
  const sbRef = useRef<SourceBuffer | null>(null)
  const queueRef = useRef<ArrayBuffer[]>([])
  const [reconnectKey, setReconnectKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null

    const connect = () => {
      const video = videoRef.current
      if (!video) return

      // WebSocket proxied through FastAPI on same port — no cross-port issues
      const wsProto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const wsUrl = `${wsProto}://${window.location.host}/api/streams/${cameraId}/ws`
      const ws = new WebSocket(wsUrl)
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws

      let ms: MediaSource | null = null
      let sb: SourceBuffer | null = null
      const queue: ArrayBuffer[] = []
      queueRef.current = queue

      const flushQueue = () => {
        if (sb && !sb.updating && queue.length > 0) {
          sb.appendBuffer(queue.shift()!)
        }
      }

      ws.onopen = () => {
        ws.send(JSON.stringify({ type: 'mse' }))
      }

      ws.onmessage = (ev) => {
        if (typeof ev.data === 'string') {
          // go2rtc sends JSON: {"type":"mse","value":"video/mp4; codecs=\"avc1.640029\""}
          let msg: { type: string; value?: string }
          try {
            msg = JSON.parse(ev.data)
          } catch {
            return
          }
          if (msg.type === 'error') {
            console.error('go2rtc error:', msg.value)
            return
          }
          if (msg.type !== 'mse' || !msg.value) return

          const codecs = msg.value

          ms = new MediaSource()
          msRef.current = ms
          video.src = URL.createObjectURL(ms)

          ms.addEventListener('sourceopen', () => {
            try {
              sb = ms!.addSourceBuffer(codecs)
              sbRef.current = sb

              sb.mode = 'segments'
              sb.addEventListener('updateend', () => {
                flushQueue()

                // Trim buffer to last 30s to prevent memory growth
                if (sb && !sb.updating && video.currentTime > 30) {
                  try {
                    sb.remove(0, video.currentTime - 30)
                  } catch { /* ignore */ }
                }
              })

              // Flush any binary messages that arrived before sourceopen
              flushQueue()

              video.play().catch(() => {})
            } catch (e) {
              console.error('addSourceBuffer failed:', e)
            }
          }, { once: true })
        } else {
          // Binary message: fMP4 segment
          if (sb && !sb.updating) {
            try {
              sb.appendBuffer(ev.data)
            } catch {
              queue.push(ev.data)
            }
          } else {
            queue.push(ev.data)
            // Prevent unbounded queue growth
            if (queue.length > 100) {
              queue.splice(0, queue.length - 50)
            }
          }
        }
      }

      ws.onclose = () => {
        if (!cancelled) {
          reconnectTimer = setTimeout(() => setReconnectKey(k => k + 1), 3000)
        }
      }

      ws.onerror = () => {
        ws.close()
      }
    }

    connect()

    return () => {
      cancelled = true
      if (reconnectTimer) clearTimeout(reconnectTimer)

      const ws = wsRef.current
      if (ws) {
        ws.onclose = null
        ws.onerror = null
        ws.onmessage = null
        ws.close()
        wsRef.current = null
      }

      const ms = msRef.current
      if (ms && ms.readyState === 'open') {
        try { ms.endOfStream() } catch { /* ignore */ }
      }
      msRef.current = null
      sbRef.current = null
      queueRef.current = []

      const video = videoRef.current
      if (video) {
        video.src = ''
        video.load()
      }
    }
  }, [cameraId, reconnectKey])

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
      className={className ?? 'w-full h-full object-contain'}
      style={rotationStyle}
    />
  )
}
