import type { Camera, StreamStatus } from '../api'
import HlsPlayer from './HlsPlayer'

interface Props {
  camera: Camera
  stream?: StreamStatus
  onClick: () => void
  selected?: boolean
}

export default function CameraCard({ camera, stream, onClick, selected }: Props) {
  const running = stream?.running ?? false

  return (
    <div
      className={`bg-neutral-900 rounded-lg overflow-hidden cursor-pointer transition-all ${
        selected
          ? 'ring-2 ring-blue-500'
          : 'hover:ring-1 hover:ring-neutral-600'
      }`}
      onClick={onClick}
    >
      <div className="aspect-video bg-black relative">
        {running ? (
          <HlsPlayer cameraId={camera.id} muted />
        ) : (
          <div className="absolute inset-0 flex items-center justify-center text-neutral-600 text-sm">
            {camera.enabled ? 'Connecting...' : 'Disabled'}
          </div>
        )}
      </div>
      <div className="flex items-center justify-between px-3 py-2">
        <span className="text-sm font-medium truncate">{camera.name}</span>
        <span
          className={`w-2 h-2 rounded-full shrink-0 ${
            running ? 'bg-green-500' : camera.enabled ? 'bg-yellow-500' : 'bg-neutral-600'
          }`}
        />
      </div>
    </div>
  )
}
