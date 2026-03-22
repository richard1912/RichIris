import { useEffect, useRef } from 'react'

interface Props {
  cameraId: number
  muted?: boolean
  className?: string
  rotation?: number
}

// ── Persistent video stream pool ──────────────────────────────────────
// Video elements and WebSocket connections live here, outside React.
// MsePlayer mounts just move the existing <video> into their container;
// unmounts detach it from the DOM but keep the stream alive.

interface StreamEntry {
  video: HTMLVideoElement
  ws: WebSocket | null
  ms: MediaSource | null
  sb: SourceBuffer | null
  queue: ArrayBuffer[]
  reconnectTimer: ReturnType<typeof setTimeout> | null
}

const streams = new Map<number, StreamEntry>()

function connectStream(cameraId: number, entry: StreamEntry) {
  const { video } = entry

  // Tear down any previous connection
  if (entry.ws) {
    entry.ws.onclose = null
    entry.ws.onerror = null
    entry.ws.onmessage = null
    entry.ws.close()
  }
  if (entry.ms && entry.ms.readyState === 'open') {
    try { entry.ms.endOfStream() } catch { /* ignore */ }
  }
  entry.ms = null
  entry.sb = null
  entry.queue = []

  const wsProto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const wsUrl = `${wsProto}://${window.location.host}/api/streams/${cameraId}/ws`
  const ws = new WebSocket(wsUrl)
  ws.binaryType = 'arraybuffer'
  entry.ws = ws

  const queue = entry.queue

  const flushQueue = () => {
    if (entry.sb && !entry.sb.updating && queue.length > 0) {
      entry.sb.appendBuffer(queue.shift()!)
    }
  }

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'mse' }))
  }

  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      let msg: { type: string; value?: string }
      try { msg = JSON.parse(ev.data) } catch { return }
      if (msg.type === 'error') { console.error('go2rtc error:', msg.value); return }
      if (msg.type !== 'mse' || !msg.value) return

      const codecs = msg.value
      const ms = new MediaSource()
      entry.ms = ms
      video.src = URL.createObjectURL(ms)

      ms.addEventListener('sourceopen', () => {
        try {
          const sb = ms.addSourceBuffer(codecs)
          entry.sb = sb
          sb.mode = 'segments'
          sb.addEventListener('updateend', () => {
            flushQueue()
            // Trim buffer to last 30s — uses buffered range, not currentTime,
            // so it works even when the video is detached from the DOM.
            if (sb && !sb.updating && sb.buffered.length > 0) {
              const end = sb.buffered.end(sb.buffered.length - 1)
              const trimTo = end - 30
              if (trimTo > sb.buffered.start(0)) {
                try { sb.remove(sb.buffered.start(0), trimTo) } catch { /* ignore */ }
              }
            }
          })
          flushQueue()
          video.play().catch(() => {})
        } catch (e) {
          console.error('addSourceBuffer failed:', e)
        }
      }, { once: true })
    } else {
      // Binary fMP4 segment
      if (entry.sb && !entry.sb.updating) {
        try { entry.sb.appendBuffer(ev.data) } catch { queue.push(ev.data) }
      } else {
        queue.push(ev.data)
        if (queue.length > 100) queue.splice(0, queue.length - 50)
      }
    }
  }

  ws.onclose = () => {
    if (streams.has(cameraId)) {
      entry.reconnectTimer = setTimeout(() => {
        if (streams.has(cameraId)) connectStream(cameraId, entry)
      }, 3000)
    }
  }

  ws.onerror = () => { ws.close() }
}

function ensureStream(cameraId: number): StreamEntry {
  const existing = streams.get(cameraId)
  if (existing) return existing

  const video = document.createElement('video')
  video.muted = true
  video.autoplay = true
  video.playsInline = true

  const entry: StreamEntry = {
    video, ws: null, ms: null, sb: null, queue: [], reconnectTimer: null,
  }
  streams.set(cameraId, entry)
  connectStream(cameraId, entry)
  return entry
}

export function destroyStream(cameraId: number) {
  const entry = streams.get(cameraId)
  if (!entry) return
  streams.delete(cameraId)
  if (entry.reconnectTimer) clearTimeout(entry.reconnectTimer)
  if (entry.ws) {
    entry.ws.onclose = null
    entry.ws.onerror = null
    entry.ws.onmessage = null
    entry.ws.close()
  }
  if (entry.ms && entry.ms.readyState === 'open') {
    try { entry.ms.endOfStream() } catch { /* ignore */ }
  }
  entry.video.src = ''
  entry.video.load()
  entry.video.remove()
}

// ── React component ───────────────────────────────────────────────────

export default function MsePlayer({ cameraId, muted = true, className, rotation = 0 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)

  // Attach the persistent video element into this component's container
  useEffect(() => {
    const entry = ensureStream(cameraId)
    const { video } = entry
    const container = containerRef.current
    if (!container) return

    video.className = 'w-full h-full object-contain'

    // Move video into this container (removes from any previous parent)
    container.appendChild(video)

    // Seek to live edge if we've been detached and buffer moved on
    if (entry.sb && entry.sb.buffered.length > 0) {
      const end = entry.sb.buffered.end(entry.sb.buffered.length - 1)
      video.currentTime = Math.max(0, end - 0.1)
    }

    video.play().catch(() => {})

    return () => {
      // Detach from DOM but keep the stream alive in the pool
      if (video.parentNode === container) {
        video.remove()
      }
    }
  }, [cameraId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Keep video sizing in sync
  useEffect(() => {
    const entry = streams.get(cameraId)
    if (entry) {
      entry.video.className = 'w-full h-full object-contain'
    }
  }, [cameraId])

  // Keep muted in sync
  useEffect(() => {
    const entry = streams.get(cameraId)
    if (entry) entry.video.muted = muted
  }, [cameraId, muted])

  const isRotated = rotation === 90 || rotation === 270
  const rotationStyle = rotation ? {
    transform: `rotate(${rotation}deg)${isRotated ? ' scale(0.5625)' : ''}`,
  } : undefined

  return <div ref={containerRef} className={className ?? 'w-full h-full'} style={rotationStyle} />
}
