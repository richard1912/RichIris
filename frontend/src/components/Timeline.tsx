import { useEffect, useState, useRef, useCallback } from 'react'
import type { RecordingSegment } from '../api'
import { fetchSegments, createClipExport } from '../api'

interface Props {
  cameraId: number | null
  onPlayback: (start: string) => void
  onLive: () => void
  isLive: boolean
  onPause?: () => void
  isPaused?: boolean
}

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

  // Playhead state
  const [playheadPct, setPlayheadPct] = useState<number | null>(null)
  const [draggingPlayhead, setDraggingPlayhead] = useState(false)
  const draggingRef = useRef(false)

  // Hover tooltip state
  const [hoverPct, setHoverPct] = useState<number | null>(null)

  // Clip export state
  const [exportMode, setExportMode] = useState(false)
  const [exportStart, setExportStart] = useState<number | null>(null) // pct 0-1
  const [exportEnd, setExportEnd] = useState<number | null>(null)
  const [_dragging, _setDragging] = useState<'start' | 'end' | null>(null)
  const [exportBusy, setExportBusy] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)

  useEffect(() => {
    if (cameraId === null) { setSegments([]); return }
    setLoading(true)
    fetchSegments(cameraId, selectedDate)
      .then(setSegments)
      .catch(() => setSegments([]))
      .finally(() => setLoading(false))
  }, [cameraId, selectedDate])

  const pctToTime = useCallback(
    (pct: number): string => {
      const hour = Math.floor(pct * 24)
      const minute = Math.floor((pct * 24 - hour) * 60)
      const second = Math.floor(((pct * 24 - hour) * 60 - minute) * 60)
      return `${selectedDate}T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}`
    },
    [selectedDate],
  )

  const pctToLabel = (pct: number): string => {
    const hour = Math.floor(pct * 24)
    const minute = Math.floor((pct * 24 - hour) * 60)
    const second = Math.floor(((pct * 24 - hour) * 60 - minute) * 60)
    return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}`
  }

  const getBarPct = useCallback(
    (e: React.MouseEvent<HTMLDivElement> | MouseEvent): number => {
      if (!barRef.current) return 0
      const rect = barRef.current.getBoundingClientRect()
      return Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    },
    [],
  )

  const triggerPlaybackAtPct = useCallback(
    (pct: number) => {
      if (segments.length === 0) return
      const hour = Math.floor(pct * 24)
      const minute = Math.floor((pct * 24 - hour) * 60)
      const second = Math.floor(((pct * 24 - hour) * 60 - minute) * 60)

      const clickTime = `${selectedDate}T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}`

      onPlayback(clickTime)
    },
    [segments, selectedDate, onPlayback],
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
        setPlayheadPct(pct)
      }
      const onUp = (ev: MouseEvent) => {
        if (!draggingRef.current) return
        draggingRef.current = false
        setDraggingPlayhead(false)
        const pct = getBarPct(ev)
        setPlayheadPct(pct)
        if (!exportMode && segments.length > 0) {
          triggerPlaybackAtPct(pct)
        }
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
      }
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [getBarPct, exportMode, segments, triggerPlaybackAtPct],
  )

  const handleBarClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (exportMode) {
        const pct = getBarPct(e)
        if (exportStart === null || (exportStart !== null && exportEnd !== null)) {
          setExportStart(pct)
          setExportEnd(null)
          setExportError(null)
        } else {
          if (pct > exportStart) {
            setExportEnd(pct)
          } else {
            setExportEnd(exportStart)
            setExportStart(pct)
          }
        }
        return
      }

      // Set playhead and trigger playback
      if (!barRef.current || segments.length === 0) return
      const pct = getBarPct(e)
      setPlayheadPct(pct)
      triggerPlaybackAtPct(pct)
    },
    [segments, triggerPlaybackAtPct, exportMode, exportStart, exportEnd, getBarPct],
  )

  const handleExport = useCallback(async () => {
    if (exportStart === null || exportEnd === null || cameraId === null) return
    setExportBusy(true)
    setExportError(null)
    try {
      await createClipExport(cameraId, pctToTime(exportStart), pctToTime(exportEnd))
      setExportStart(null)
      setExportEnd(null)
      setExportMode(false)
    } catch (e) {
      setExportError(e instanceof Error ? e.message : 'Export failed')
    } finally {
      setExportBusy(false)
    }
  }, [cameraId, exportStart, exportEnd, pctToTime])

  // Build 24h bar with segments highlighted
  const segmentBars = segments.map((seg) => {
    const start = new Date(seg.start_time)
    const startMinutes = start.getHours() * 60 + start.getMinutes()
    const duration = seg.duration || 900
    const durationMinutes = duration / 60
    const leftPct = (startMinutes / 1440) * 100
    const widthPct = (durationMinutes / 1440) * 100
    return (
      <div
        key={seg.id}
        className="absolute top-0 bottom-0 bg-blue-500/60 hover:bg-blue-400/80 transition-colors"
        style={{ left: `${leftPct}%`, width: `${Math.max(widthPct, 0.3)}%` }}
        title={`${start.toLocaleTimeString()} (${Math.round(duration / 60)}m)`}
      />
    )
  })

  // Hour markers
  const hourMarkers = Array.from({ length: 24 }, (_, i) => (
    <div
      key={i}
      className="absolute top-0 bottom-0 border-l border-neutral-700/50"
      style={{ left: `${(i / 24) * 100}%` }}
    >
      {i % 3 === 0 && (
        <span className="absolute -top-4 -translate-x-1/2 text-[10px] text-neutral-500">
          {String(i).padStart(2, '0')}
        </span>
      )}
    </div>
  ))

  // Export range overlay
  const rangeOverlay =
    exportMode && exportStart !== null ? (
      <div
        className="absolute top-0 bottom-0 bg-green-500/30 border-l-2 border-r-2 border-green-400 pointer-events-none"
        style={{
          left: `${exportStart * 100}%`,
          width: exportEnd !== null ? `${(exportEnd - exportStart) * 100}%` : '2px',
        }}
      />
    ) : null

  // Playhead line
  const playheadLine =
    playheadPct !== null && !exportMode ? (
      <div
        className="absolute top-0 bottom-0 z-10"
        style={{ left: `${playheadPct * 100}%`, transform: 'translateX(-50%)' }}
      >
        {/* Wider invisible hit area for easy grabbing */}
        <div
          className="absolute -top-3 -bottom-1 w-5 cursor-grab active:cursor-grabbing"
          style={{ left: '50%', transform: 'translateX(-50%)' }}
          onMouseDown={handlePlayheadDown}
        />
        {/* Visible line */}
        <div className="absolute top-0 bottom-0 w-0.5 bg-red-500 pointer-events-none" style={{ left: '50%', transform: 'translateX(-50%)' }} />
        {/* Top handle */}
        <div className="absolute -top-2 w-3.5 h-3.5 rounded-full bg-red-500 border-2 border-red-300 shadow pointer-events-none" style={{ left: '50%', transform: 'translateX(-50%)' }} />
        {/* Time tooltip while dragging */}
        {draggingPlayhead && (
          <div className="absolute -top-9 left-1/2 -translate-x-1/2 pointer-events-none">
            <div className="bg-red-600 text-white text-sm font-medium px-2.5 py-1 rounded shadow-lg whitespace-nowrap">
              {pctToLabel(playheadPct)}
            </div>
            <div className="w-0 h-0 border-l-4 border-r-4 border-t-4 border-l-transparent border-r-transparent border-t-red-600 mx-auto" />
          </div>
        )}
      </div>
    ) : null

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

        {/* Export controls */}
        {cameraId !== null && segments.length > 0 && (
          <>
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
          </>
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
              : `${pctToLabel(exportStart)} - ${pctToLabel(exportEnd)}`}
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

      <div className="relative pt-5 pb-1">
        <div
          ref={barRef}
          className={`relative h-10 bg-neutral-800 rounded cursor-pointer ${
            exportMode ? 'ring-1 ring-green-500/50' : ''
          }`}
          onClick={handleBarClick}
          onMouseMove={(e) => {
            if (!draggingRef.current) setHoverPct(getBarPct(e))
          }}
          onMouseLeave={() => setHoverPct(null)}
        >
          {hourMarkers}
          {segmentBars}
          {rangeOverlay}
          {playheadLine}
          {/* Hover time tooltip */}
          {hoverPct !== null && !draggingPlayhead && (
            <div
              className="absolute z-20 pointer-events-none"
              style={{ left: `${hoverPct * 100}%`, top: '-28px', transform: 'translateX(-50%)' }}
            >
              <div className="bg-neutral-700 text-white text-xs px-2 py-1 rounded shadow-lg whitespace-nowrap">
                {pctToLabel(hoverPct)}
              </div>
              <div className="w-0 h-0 border-l-4 border-r-4 border-t-4 border-l-transparent border-r-transparent border-t-neutral-700 mx-auto" />
            </div>
          )}
          {/* Hover vertical guide line */}
          {hoverPct !== null && !draggingPlayhead && (
            <div
              className="absolute top-0 bottom-0 w-px bg-white/20 pointer-events-none"
              style={{ left: `${hoverPct * 100}%` }}
            />
          )}
        </div>
        <div className="flex justify-between mt-1">
          <span className="text-[10px] text-neutral-600">00:00</span>
          <span className="text-[10px] text-neutral-600">12:00</span>
          <span className="text-[10px] text-neutral-600">24:00</span>
        </div>
      </div>

    </div>
  )
}
