import { useEffect, useState, useRef, useCallback } from 'react'
import type { RecordingSegment, ThumbnailSpriteInfo } from '../api'
import { fetchSegments, fetchThumbnails, createClipExport } from '../api'

interface Props {
  cameraId: number | null
  onPlayback: (start: string) => void
  onLive: () => void
  isLive: boolean
  onPause?: () => void
  isPaused?: boolean
}

const ZOOM_LEVELS = [1, 2, 4, 8, 12, 24] as const

function todayStr(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function shiftDate(dateStr: string, days: number): string {
  const d = new Date(dateStr + 'T00:00:00')
  d.setDate(d.getDate() + days)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export default function Timeline({ cameraId, onPlayback, onLive, isLive, onPause, isPaused }: Props) {
  const [selectedDate, setSelectedDate] = useState<string>(todayStr())
  const [segments, setSegments] = useState<RecordingSegment[]>([])
  const [loading, setLoading] = useState(false)
  const barRef = useRef<HTMLDivElement>(null)

  // Zoom state
  const [zoomLevel, setZoomLevel] = useState(1) // 1=24h, 24=1h
  const [viewportStart, setViewportStart] = useState(0) // hours from 0-24

  const visibleHours = 24 / zoomLevel

  // Playhead state (stored as absolute hour 0-24)
  const [playheadHour, setPlayheadHour] = useState<number | null>(null)
  const [draggingPlayhead, setDraggingPlayhead] = useState(false)
  const draggingRef = useRef(false)

  // Hover tooltip state
  const [hoverPct, setHoverPct] = useState<number | null>(null)

  // Minimap drag state
  const minimapDragRef = useRef(false)
  const minimapBarRef = useRef<HTMLDivElement>(null)

  // Clip export state
  const [exportMode, setExportMode] = useState(false)
  const [exportStart, setExportStart] = useState<number | null>(null) // absolute hour
  const [exportEnd, setExportEnd] = useState<number | null>(null)
  const [exportBusy, setExportBusy] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)

  // Thumbnail sprites
  const [sprites, setSprites] = useState<ThumbnailSpriteInfo[]>([])

  useEffect(() => {
    if (cameraId === null) { setSegments([]); setSprites([]); return }
    setLoading(true)
    fetchSegments(cameraId, selectedDate)
      .then(setSegments)
      .catch(() => setSegments([]))
      .finally(() => setLoading(false))
    fetchThumbnails(cameraId, selectedDate)
      .then((s) => {
        setSprites(s)
        // Preload sprite images
        for (const sp of s) {
          const img = new Image()
          img.src = sp.sprite_url
        }
      })
      .catch(() => setSprites([]))
  }, [cameraId, selectedDate])

  // Reset zoom on date change
  useEffect(() => {
    setZoomLevel(1)
    setViewportStart(0)
  }, [selectedDate])

  // Convert absolute hour (0-24) to viewport percentage (0-1)
  const hourToViewportPct = useCallback(
    (hour: number): number => {
      return (hour - viewportStart) / visibleHours
    },
    [viewportStart, visibleHours],
  )

  // Convert viewport percentage (0-1) to absolute hour (0-24)
  const viewportPctToHour = useCallback(
    (pct: number): number => {
      return viewportStart + pct * visibleHours
    },
    [viewportStart, visibleHours],
  )

  const hourToTimeStr = useCallback(
    (h: number): string => {
      const hour = Math.floor(h)
      const minute = Math.floor((h - hour) * 60)
      const second = Math.floor(((h - hour) * 60 - minute) * 60)
      return `${selectedDate}T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}`
    },
    [selectedDate],
  )

  const hourToLabel = (h: number): string => {
    const clamped = Math.max(0, Math.min(24, h))
    const hour = Math.floor(clamped)
    const minute = Math.floor((clamped - hour) * 60)
    const second = Math.floor(((clamped - hour) * 60 - minute) * 60)
    return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}`
  }

  const getThumbnailForHour = useCallback(
    (hour: number): { url: string; x: number; y: number; width: number; height: number } | null => {
      if (sprites.length === 0) return null
      const timeStr = `${selectedDate}T${String(Math.floor(hour)).padStart(2, '0')}:${String(Math.floor((hour % 1) * 60)).padStart(2, '0')}:${String(Math.floor(((hour * 60) % 1) * 60)).padStart(2, '0')}`
      for (const sp of sprites) {
        if (timeStr >= sp.start_time && timeStr < sp.end_time) {
          const startDate = new Date(sp.start_time)
          const startHour = startDate.getHours() + startDate.getMinutes() / 60 + startDate.getSeconds() / 3600
          const secondsIntoSegment = (hour - startHour) * 3600
          const frameIndex = Math.max(0, Math.min(Math.floor(secondsIntoSegment / sp.interval), sp.cols * sp.rows - 1))
          const col = frameIndex % sp.cols
          const row = Math.floor(frameIndex / sp.cols)
          return {
            url: sp.sprite_url,
            x: col * sp.thumb_width,
            y: row * sp.thumb_height,
            width: sp.thumb_width,
            height: sp.thumb_height,
          }
        }
      }
      return null
    },
    [sprites, selectedDate],
  )

  const getBarPct = useCallback(
    (e: React.MouseEvent<HTMLDivElement> | MouseEvent): number => {
      if (!barRef.current) return 0
      const rect = barRef.current.getBoundingClientRect()
      return Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    },
    [],
  )

  const triggerPlaybackAtHour = useCallback(
    (hour: number) => {
      if (segments.length === 0) return
      onPlayback(hourToTimeStr(hour))
    },
    [segments, hourToTimeStr, onPlayback],
  )

  // Zoom handler (mouse wheel on bar)
  const handleWheel = useCallback(
    (e: React.WheelEvent<HTMLDivElement>) => {
      e.preventDefault()
      const cursorPct = getBarPct(e as unknown as React.MouseEvent<HTMLDivElement>)
      const cursorHour = viewportPctToHour(cursorPct)

      const currentIdx = ZOOM_LEVELS.indexOf(zoomLevel as typeof ZOOM_LEVELS[number])
      let newIdx: number
      if (e.deltaY < 0) {
        // Scroll up = zoom in
        newIdx = Math.min(currentIdx + 1, ZOOM_LEVELS.length - 1)
      } else {
        // Scroll down = zoom out
        newIdx = Math.max(currentIdx - 1, 0)
      }

      const newZoom = ZOOM_LEVELS[newIdx]
      const newVisibleHours = 24 / newZoom

      // Keep cursor hour at same screen position
      let newStart = cursorHour - cursorPct * newVisibleHours
      newStart = Math.max(0, Math.min(24 - newVisibleHours, newStart))

      setZoomLevel(newZoom)
      setViewportStart(newStart)
    },
    [getBarPct, viewportPctToHour, zoomLevel],
  )

  // Playhead drag handlers
  const handlePlayheadDown = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation()
      e.preventDefault()
      draggingRef.current = true
      setDraggingPlayhead(true)

      const onMove = (ev: MouseEvent) => {
        if (!draggingRef.current) return
        const pct = getBarPct(ev)
        setPlayheadHour(viewportPctToHour(pct))
      }
      const onUp = (ev: MouseEvent) => {
        if (!draggingRef.current) return
        draggingRef.current = false
        setDraggingPlayhead(false)
        const pct = getBarPct(ev)
        const hour = viewportPctToHour(pct)
        setPlayheadHour(hour)
        if (!exportMode && segments.length > 0) {
          triggerPlaybackAtHour(hour)
        }
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
      }
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [getBarPct, viewportPctToHour, exportMode, segments, triggerPlaybackAtHour],
  )

  const handleBarClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const pct = getBarPct(e)
      const hour = viewportPctToHour(pct)

      if (exportMode) {
        if (exportStart === null || (exportStart !== null && exportEnd !== null)) {
          setExportStart(hour)
          setExportEnd(null)
          setExportError(null)
        } else {
          if (hour > exportStart) {
            setExportEnd(hour)
          } else {
            setExportEnd(exportStart)
            setExportStart(hour)
          }
        }
        return
      }

      // Set playhead and trigger playback
      if (!barRef.current || segments.length === 0) return
      setPlayheadHour(hour)
      triggerPlaybackAtHour(hour)
    },
    [segments, triggerPlaybackAtHour, exportMode, exportStart, exportEnd, getBarPct, viewportPctToHour],
  )

  const handleExport = useCallback(async () => {
    if (exportStart === null || exportEnd === null || cameraId === null) return
    setExportBusy(true)
    setExportError(null)
    try {
      await createClipExport(cameraId, hourToTimeStr(exportStart), hourToTimeStr(exportEnd))
      setExportStart(null)
      setExportEnd(null)
      setExportMode(false)
    } catch (e) {
      setExportError(e instanceof Error ? e.message : 'Export failed')
    } finally {
      setExportBusy(false)
    }
  }, [cameraId, exportStart, exportEnd, hourToTimeStr])

  // Minimap drag handlers
  const handleMinimapDown = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      e.preventDefault()
      minimapDragRef.current = true

      const updateFromMinimap = (ev: MouseEvent) => {
        if (!minimapBarRef.current) return
        const rect = minimapBarRef.current.getBoundingClientRect()
        const pct = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width))
        const centerHour = pct * 24
        let newStart = centerHour - visibleHours / 2
        newStart = Math.max(0, Math.min(24 - visibleHours, newStart))
        setViewportStart(newStart)
      }

      updateFromMinimap(e as unknown as MouseEvent)

      const onMove = (ev: MouseEvent) => {
        if (!minimapDragRef.current) return
        updateFromMinimap(ev)
      }
      const onUp = () => {
        minimapDragRef.current = false
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
      }
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [visibleHours],
  )

  // Build segment bars clipped to visible window
  const segmentBars = segments.map((seg) => {
    const start = new Date(seg.start_time)
    const startHour = start.getHours() + start.getMinutes() / 60 + start.getSeconds() / 3600
    const duration = seg.duration || 900
    const endHour = startHour + duration / 3600

    // Clip to viewport
    const clippedStart = Math.max(startHour, viewportStart)
    const clippedEnd = Math.min(endHour, viewportStart + visibleHours)
    if (clippedStart >= clippedEnd) return null

    const leftPct = ((clippedStart - viewportStart) / visibleHours) * 100
    const widthPct = ((clippedEnd - clippedStart) / visibleHours) * 100

    return (
      <div
        key={seg.id}
        className="absolute top-0 bottom-0 bg-blue-500/60 hover:bg-blue-400/80 transition-colors"
        style={{ left: `${leftPct}%`, width: `${Math.max(widthPct, 0.2)}%` }}
        title={`${start.toLocaleTimeString()} (${Math.round(duration / 60)}m)`}
      />
    )
  }).filter(Boolean)

  // Minimap segment bars (always full 24h)
  const minimapSegmentBars = segments.map((seg) => {
    const start = new Date(seg.start_time)
    const startMinutes = start.getHours() * 60 + start.getMinutes()
    const duration = seg.duration || 900
    const leftPct = (startMinutes / 1440) * 100
    const widthPct = (duration / 60 / 1440) * 100
    return (
      <div
        key={seg.id}
        className="absolute top-0 bottom-0 bg-blue-500/60"
        style={{ left: `${leftPct}%`, width: `${Math.max(widthPct, 0.3)}%` }}
      />
    )
  })

  // Hour markers adjusted for zoom
  const hourMarkers: React.ReactNode[] = []
  // Determine marker spacing based on zoom
  let markerStep: number
  if (visibleHours <= 1) markerStep = 5 / 60 // 5 min
  else if (visibleHours <= 3) markerStep = 15 / 60 // 15 min
  else if (visibleHours <= 6) markerStep = 0.5 // 30 min
  else if (visibleHours <= 12) markerStep = 1
  else markerStep = 1

  // Label every Nth marker
  let labelStep: number
  if (visibleHours <= 1) labelStep = 15 / 60
  else if (visibleHours <= 3) labelStep = 0.5
  else if (visibleHours <= 6) labelStep = 1
  else if (visibleHours <= 12) labelStep = 2
  else labelStep = 3

  const firstMarker = Math.ceil(viewportStart / markerStep) * markerStep
  for (let h = firstMarker; h <= viewportStart + visibleHours; h += markerStep) {
    const pct = ((h - viewportStart) / visibleHours) * 100
    if (pct < 0 || pct > 100) continue

    const isLabelMarker = Math.abs(h % labelStep) < 0.001 || Math.abs(h % labelStep - labelStep) < 0.001
    const hour = Math.floor(h)
    const minute = Math.round((h - hour) * 60)

    hourMarkers.push(
      <div
        key={h}
        className={`absolute top-0 bottom-0 ${isLabelMarker ? 'border-l border-neutral-600/60' : 'border-l border-neutral-700/30'}`}
        style={{ left: `${pct}%` }}
      >
        {isLabelMarker && (
          <span className="absolute -top-4 -translate-x-1/2 text-[10px] text-neutral-500">
            {String(hour).padStart(2, '0')}:{String(minute).padStart(2, '0')}
          </span>
        )}
      </div>,
    )
  }

  // Export range overlay (using absolute hours)
  const rangeOverlay =
    exportMode && exportStart !== null ? (() => {
      const startPct = hourToViewportPct(exportStart)
      const endPct = exportEnd !== null ? hourToViewportPct(exportEnd) : startPct
      const left = Math.max(0, Math.min(1, startPct))
      const right = Math.max(0, Math.min(1, endPct))
      if (right <= 0 || left >= 1) return null
      return (
        <div
          className="absolute top-0 bottom-0 bg-green-500/30 border-l-2 border-r-2 border-green-400 pointer-events-none"
          style={{
            left: `${left * 100}%`,
            width: exportEnd !== null ? `${(right - left) * 100}%` : '2px',
          }}
        />
      )
    })() : null

  // Playhead line (using absolute hour)
  const playheadPct = playheadHour !== null ? hourToViewportPct(playheadHour) : null
  const playheadLine =
    playheadPct !== null && !exportMode && playheadPct >= 0 && playheadPct <= 1 ? (
      <div
        className="absolute top-0 bottom-0 z-10"
        style={{ left: `${playheadPct * 100}%`, transform: 'translateX(-50%)' }}
      >
        <div
          className="absolute -top-3 -bottom-1 w-5 cursor-grab active:cursor-grabbing"
          style={{ left: '50%', transform: 'translateX(-50%)' }}
          onMouseDown={handlePlayheadDown}
        />
        <div className="absolute top-0 bottom-0 w-0.5 bg-red-500 pointer-events-none" style={{ left: '50%', transform: 'translateX(-50%)' }} />
        <div className="absolute -top-2 w-3.5 h-3.5 rounded-full bg-red-500 border-2 border-red-300 shadow pointer-events-none" style={{ left: '50%', transform: 'translateX(-50%)' }} />
        {draggingPlayhead && playheadHour !== null && (
          <div className="absolute -top-9 left-1/2 -translate-x-1/2 pointer-events-none">
            <div className="bg-red-600 text-white text-sm font-medium px-2.5 py-1 rounded shadow-lg whitespace-nowrap">
              {hourToLabel(playheadHour)}
            </div>
            <div className="w-0 h-0 border-l-4 border-r-4 border-t-4 border-l-transparent border-r-transparent border-t-red-600 mx-auto" />
          </div>
        )}
      </div>
    ) : null

  // Hover hour
  const hoverHour = hoverPct !== null ? viewportPctToHour(hoverPct) : null

  return (
    <div className="bg-neutral-900/90 backdrop-blur border-t border-neutral-800 px-4 py-3">
      <div className="flex items-center gap-3 mb-2">
        <button
          onClick={onLive}
          className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
            isLive && !isPaused
              ? 'bg-red-600 text-white'
              : 'bg-neutral-800 text-neutral-400 hover:text-white'
          }`}
        >
          LIVE
        </button>

        {isLive && onPause && (
          <button
            onClick={onPause}
            className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
              isPaused
                ? 'bg-yellow-600 text-white'
                : 'bg-neutral-800 text-neutral-400 hover:text-white'
            }`}
          >
            {isPaused ? 'Resume' : 'Pause Feed'}
          </button>
        )}

        <div className="flex items-center gap-1">
          <button
            onClick={() => {
              setSelectedDate(shiftDate(selectedDate, -1))
              setExportStart(null)
              setExportEnd(null)
            }}
            className="px-2 py-1 rounded text-sm bg-neutral-800 text-neutral-400 hover:text-white border border-neutral-700 transition-colors"
          >
            &larr;
          </button>
          <span className="text-sm text-neutral-300 px-2 min-w-[7rem] text-center">
            {selectedDate}
          </span>
          <button
            onClick={() => {
              const next = shiftDate(selectedDate, 1)
              if (next <= todayStr()) {
                setSelectedDate(next)
                setExportStart(null)
                setExportEnd(null)
              }
            }}
            disabled={selectedDate >= todayStr()}
            className="px-2 py-1 rounded text-sm bg-neutral-800 text-neutral-400 hover:text-white border border-neutral-700 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
          >
            &rarr;
          </button>
        </div>

        {loading && <span className="text-xs text-neutral-500">Loading...</span>}
        {!loading && cameraId !== null && (
          <span className="text-xs text-neutral-500">
            {segments.length} segments
          </span>
        )}

        {/* Zoom indicator */}
        {zoomLevel > 1 && (
          <span className="text-xs text-neutral-500">
            {visibleHours < 1 ? `${Math.round(visibleHours * 60)}m` : `${visibleHours}h`} view
          </span>
        )}

        {/* Export controls */}
        {cameraId !== null && segments.length > 0 && (
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => {
                setExportMode(!exportMode)
                setExportStart(null)
                setExportEnd(null)
                setExportError(null)
              }}
              className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                exportMode
                  ? 'bg-green-600 text-white'
                  : 'bg-neutral-800 text-neutral-400 hover:text-white'
              }`}
            >
              {exportMode ? 'Cancel Export' : 'Select Export Range'}
            </button>
          </div>
        )}
      </div>

      {/* Export mode instructions */}
      {exportMode && (
        <div className="flex items-center gap-3 mb-2 text-xs">
          <span className="text-green-400">
            {exportStart === null
              ? 'Click timeline to set start'
              : exportEnd === null
              ? 'Click timeline to set end'
              : `${hourToLabel(exportStart)} - ${hourToLabel(exportEnd)}`}
          </span>
          {exportStart !== null && exportEnd !== null && (
            <button
              onClick={handleExport}
              disabled={exportBusy}
              className="px-3 py-1 rounded bg-green-600 text-white font-medium hover:bg-green-500 disabled:opacity-50 transition-colors"
            >
              {exportBusy ? 'Exporting...' : 'Export'}
            </button>
          )}
          {exportError && <span className="text-red-400">{exportError}</span>}
        </div>
      )}

      {/* Minimap (shown when zoomed in) */}
      {zoomLevel > 1 && (
        <div className="relative mb-1">
          <div
            ref={minimapBarRef}
            className="relative h-3 bg-neutral-800/60 rounded cursor-pointer"
            onMouseDown={handleMinimapDown}
          >
            {minimapSegmentBars}
            {/* Viewport indicator */}
            <div
              className="absolute top-0 bottom-0 border border-white/40 bg-white/10 rounded-sm"
              style={{
                left: `${(viewportStart / 24) * 100}%`,
                width: `${(visibleHours / 24) * 100}%`,
              }}
            />
            {/* Playhead on minimap */}
            {playheadHour !== null && (
              <div
                className="absolute top-0 bottom-0 w-0.5 bg-red-500 pointer-events-none"
                style={{ left: `${(playheadHour / 24) * 100}%` }}
              />
            )}
          </div>
          <div className="flex justify-between">
            <span className="text-[9px] text-neutral-600">00</span>
            <span className="text-[9px] text-neutral-600">06</span>
            <span className="text-[9px] text-neutral-600">12</span>
            <span className="text-[9px] text-neutral-600">18</span>
            <span className="text-[9px] text-neutral-600">24</span>
          </div>
        </div>
      )}

      <div className="relative pt-5 pb-1">
        <div
          ref={barRef}
          className={`relative h-10 bg-neutral-800 rounded cursor-pointer ${
            exportMode ? 'ring-1 ring-green-500/50' : ''
          }`}
          onClick={handleBarClick}
          onWheel={handleWheel}
          onMouseMove={(e) => {
            if (!draggingRef.current) setHoverPct(getBarPct(e))
          }}
          onMouseLeave={() => setHoverPct(null)}
        >
          {hourMarkers}
          {segmentBars}
          {rangeOverlay}
          {playheadLine}
          {/* Hover time tooltip with thumbnail */}
          {hoverHour !== null && !draggingPlayhead && (() => {
            const thumb = getThumbnailForHour(hoverHour)
            return (
              <div
                className="absolute z-20 pointer-events-none"
                style={{ left: `${hoverPct! * 100}%`, top: thumb ? `-${thumb.height + 36}px` : '-28px', transform: 'translateX(-50%)' }}
              >
                {thumb && (
                  <div
                    className="rounded overflow-hidden shadow-lg mb-1 border border-neutral-600"
                    style={{
                      width: thumb.width,
                      height: thumb.height,
                      backgroundImage: `url(${thumb.url})`,
                      backgroundPosition: `-${thumb.x}px -${thumb.y}px`,
                      backgroundSize: 'auto',
                    }}
                  />
                )}
                <div className="bg-neutral-700 text-white text-xs px-2 py-1 rounded shadow-lg whitespace-nowrap text-center">
                  {hourToLabel(hoverHour)}
                </div>
                <div className="w-0 h-0 border-l-4 border-r-4 border-t-4 border-l-transparent border-r-transparent border-t-neutral-700 mx-auto" />
              </div>
            )
          })()}
          {/* Hover vertical guide line */}
          {hoverPct !== null && !draggingPlayhead && (
            <div
              className="absolute top-0 bottom-0 w-px bg-white/20 pointer-events-none"
              style={{ left: `${hoverPct * 100}%` }}
            />
          )}
        </div>
        <div className="flex justify-between mt-1">
          <span className="text-[10px] text-neutral-600">{hourToLabel(viewportStart)}</span>
          <span className="text-[10px] text-neutral-600">{hourToLabel(viewportStart + visibleHours / 2)}</span>
          <span className="text-[10px] text-neutral-600">{hourToLabel(viewportStart + visibleHours)}</span>
        </div>
      </div>

    </div>
  )
}
