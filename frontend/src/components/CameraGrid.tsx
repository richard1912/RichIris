import type { Camera, StreamStatus } from '../api'
import CameraCard from './CameraCard'

interface Props {
  cameras: Camera[]
  streams: Map<number, StreamStatus>
  onSelect: (camera: Camera) => void
  selectedId: number | null
}

export default function CameraGrid({ cameras, streams, onSelect, selectedId }: Props) {
  if (cameras.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 text-neutral-500">
        No cameras configured
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
      {cameras.map(cam => (
        <CameraCard
          key={cam.id}
          camera={cam}
          stream={streams.get(cam.id)}
          onClick={() => onSelect(cam)}
          selected={cam.id === selectedId}
        />
      ))}
    </div>
  )
}
