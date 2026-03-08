import { useEffect, useState, useCallback } from 'react'
import type { Camera, StreamStatus } from './api'
import { fetchCameras, fetchSystemStatus } from './api'
import CameraGrid from './components/CameraGrid'
import CameraFullscreen from './components/CameraFullscreen'
import CameraModal from './components/CameraModal'
import SystemPage from './components/SystemPage'
import Timeline from './components/Timeline'

export default function App() {
  const [cameras, setCameras] = useState<Camera[]>([])
  const [streams, setStreams] = useState<Map<number, StreamStatus>>(new Map())
  const [selectedCamera, setSelectedCamera] = useState<Camera | null>(null)
  const [fullscreenCamera, setFullscreenCamera] = useState<Camera | null>(null)
  const [showSystem, setShowSystem] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<'live' | 'playback'>('live')
  const [modalCamera, setModalCamera] = useState<Camera | undefined>(undefined)
  const [showModal, setShowModal] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [cams, status] = await Promise.all([fetchCameras(), fetchSystemStatus()])
      setCameras(cams)
      const map = new Map<number, StreamStatus>()
      status.streams.forEach(s => map.set(s.camera_id, s))
      setStreams(map)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Connection error')
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, 5000)
    return () => clearInterval(interval)
  }, [refresh])

  const activeCount = Array.from(streams.values()).filter(s => s.running).length

  const handleSelectCamera = useCallback((cam: Camera) => {
    if (selectedCamera?.id === cam.id) {
      setFullscreenCamera(cam)
    } else {
      setSelectedCamera(cam)
      setMode('live')
    }
  }, [selectedCamera])

  const handlePlayback = useCallback(
    (_start: string, _end: string) => {
      if (!selectedCamera) return
      setFullscreenCamera(selectedCamera)
    },
    [selectedCamera],
  )

  const handleLive = useCallback(() => {
    setMode('live')
  }, [])

  const openAddModal = useCallback(() => {
    setModalCamera(undefined)
    setShowModal(true)
  }, [])

  const openEditModal = useCallback((cam: Camera) => {
    setModalCamera(cam)
    setShowModal(true)
  }, [])

  const handleModalSaved = useCallback(() => {
    setShowModal(false)
    setModalCamera(undefined)
    refresh()
  }, [refresh])

  if (showSystem) {
    return <SystemPage onBack={() => setShowSystem(false)} />
  }

  if (fullscreenCamera) {
    return (
      <CameraFullscreen
        camera={fullscreenCamera}
        stream={streams.get(fullscreenCamera.id)}
        onBack={() => setFullscreenCamera(null)}
      />
    )
  }

  return (
    <div className="min-h-screen flex flex-col">
      <header className="flex items-center justify-between px-6 py-4 border-b border-neutral-800">
        <h1 className="text-xl font-semibold tracking-tight">RichIris</h1>
        <div className="flex items-center gap-4 text-sm text-neutral-400">
          {error && <span className="text-red-400">{error}</span>}
          <span>{activeCount}/{cameras.length} cameras active</span>
          <button
            onClick={() => setShowSystem(true)}
            className="text-neutral-400 hover:text-white transition-colors"
          >
            System
          </button>
        </div>
      </header>
      <main className="flex-1 p-4">
        <CameraGrid
          cameras={cameras}
          streams={streams}
          onSelect={handleSelectCamera}
          onEdit={openEditModal}
          onAdd={openAddModal}
          selectedId={selectedCamera?.id ?? null}
        />
      </main>

      <div className="border-t border-neutral-800">
        {selectedCamera && (
          <div className="flex items-center gap-3 px-4 pt-3">
            <span className="text-sm font-medium">{selectedCamera.name}</span>
            <button
              onClick={() => setFullscreenCamera(selectedCamera)}
              className="text-xs text-neutral-400 hover:text-white transition-colors"
            >
              Fullscreen
            </button>
            <button
              onClick={() => {
                setSelectedCamera(null)
                setMode('live')
              }}
              className="ml-auto text-xs text-neutral-500 hover:text-white transition-colors"
            >
              Close
            </button>
          </div>
        )}
        <Timeline
          cameraId={selectedCamera?.id ?? null}
          onPlayback={handlePlayback}
          onLive={handleLive}
          isLive={mode === 'live'}
        />
      </div>

      {showModal && (
        <CameraModal
          camera={modalCamera}
          onClose={() => { setShowModal(false); setModalCamera(undefined) }}
          onSaved={handleModalSaved}
        />
      )}
    </div>
  )
}
