import type { Camera, StreamStatus } from '../api'
import MsePlayer from './MsePlayer'

interface Props {
  camera: Camera
  stream?: StreamStatus
  onClick: () => void
  onEdit: () => void
  selected?: boolean
  paused?: boolean
}

export default function CameraCard({ camera, stream, onClick, onEdit, selected, paused }: Props) {
  const running = stream?.running ?? false
  const rot = camera.rotation || 0
  const isRotated = rot === 90 || rot === 270

  return (
    <div
      className={`bg-neutral-900 rounded-lg overflow-hidden cursor-pointer transition-all group ${
        selected
          ? 'ring-2 ring-blue-500'
          : 'hover:ring-1 hover:ring-neutral-600'
      }`}
      onClick={onClick}
    >
      <div className="aspect-video bg-black relative overflow-hidden">
        <div
          className="absolute inset-0 flex items-center justify-center"
          style={rot ? {
            transform: `rotate(${rot}deg)${isRotated ? ' scale(0.5625)' : ''}`,
          } : undefined}
        >
          {paused ? (
            <div className="text-yellow-500 text-sm">Paused</div>
          ) : running ? (
            <MsePlayer cameraId={camera.id} muted />
          ) : (
            <div className="text-neutral-600 text-sm">
              {camera.enabled ? 'Connecting...' : 'Disabled'}
            </div>
          )}
        </div>
        <button
          onClick={e => { e.stopPropagation(); onEdit() }}
          className="absolute top-2 right-2 z-10 p-1.5 rounded bg-black/50 text-neutral-400 hover:text-white opacity-0 group-hover:opacity-100 transition-opacity"
          title="Camera settings"
        >
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
            <path fillRule="evenodd" d="M7.84 1.804A1 1 0 018.82 1h2.36a1 1 0 01.98.804l.331 1.652a6.993 6.993 0 011.929 1.115l1.598-.54a1 1 0 011.186.447l1.18 2.044a1 1 0 01-.205 1.251l-1.267 1.113a7.047 7.047 0 010 2.228l1.267 1.113a1 1 0 01.206 1.25l-1.18 2.045a1 1 0 01-1.187.447l-1.598-.54a6.993 6.993 0 01-1.929 1.115l-.33 1.652a1 1 0 01-.98.804H8.82a1 1 0 01-.98-.804l-.331-1.652a6.993 6.993 0 01-1.929-1.115l-1.598.54a1 1 0 01-1.186-.447l-1.18-2.044a1 1 0 01.205-1.251l1.267-1.114a7.05 7.05 0 010-2.227L1.821 7.773a1 1 0 01-.206-1.25l1.18-2.045a1 1 0 011.187-.447l1.598.54A6.993 6.993 0 017.51 3.456l.33-1.652zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
          </svg>
        </button>
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
