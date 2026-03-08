import type { Camera, StreamStatus } from '../api'
import CameraCard from './CameraCard'

interface Props {
  cameras: Camera[]
  streams: Map<number, StreamStatus>
  onSelect: (camera: Camera) => void
  onEdit: (camera: Camera) => void
  onAdd: () => void
  selectedId: number | null
  paused?: boolean
}

export default function CameraGrid({ cameras, streams, onSelect, onEdit, onAdd, selectedId, paused }: Props) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
      {cameras.map(cam => (
        <CameraCard
          key={cam.id}
          camera={cam}
          stream={streams.get(cam.id)}
          onClick={() => onSelect(cam)}
          onEdit={() => onEdit(cam)}
          selected={cam.id === selectedId}
          paused={paused}
        />
      ))}
      <button
        onClick={onAdd}
        className="bg-neutral-900 border-2 border-dashed border-neutral-700 rounded-lg aspect-video flex flex-col items-center justify-center gap-2 text-neutral-500 hover:text-neutral-300 hover:border-neutral-500 transition-colors cursor-pointer"
      >
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-8 h-8">
          <path d="M10.75 4.75a.75.75 0 00-1.5 0v4.5h-4.5a.75.75 0 000 1.5h4.5v4.5a.75.75 0 001.5 0v-4.5h4.5a.75.75 0 000-1.5h-4.5v-4.5z" />
        </svg>
        <span className="text-sm">Add Camera</span>
      </button>
    </div>
  )
}
