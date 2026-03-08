import { useState } from 'react'
import type { Camera } from '../api'
import { createCamera, updateCamera, deleteCamera } from '../api'

interface Props {
  camera?: Camera  // undefined = add mode, defined = edit mode
  onClose: () => void
  onSaved: () => void
}

export default function CameraModal({ camera, onClose, onSaved }: Props) {
  const isEdit = !!camera
  const [name, setName] = useState(camera?.name ?? '')
  const [rtspUrl, setRtspUrl] = useState(camera?.rtsp_url ?? '')
  const [enabled, setEnabled] = useState(camera?.enabled ?? true)
  const [rotation, setRotation] = useState(camera?.rotation ?? 0)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const handleSave = async () => {
    if (!name.trim() || !rtspUrl.trim()) {
      setError('Name and RTSP URL are required')
      return
    }
    setSaving(true)
    setError(null)
    try {
      if (isEdit) {
        await updateCamera(camera.id, { name, rtsp_url: rtspUrl, enabled, rotation })
      } else {
        await createCamera({ name, rtsp_url: rtspUrl, enabled, rotation })
      }
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async () => {
    if (!camera) return
    setSaving(true)
    try {
      await deleteCamera(camera.id)
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed')
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="bg-neutral-900 border border-neutral-700 rounded-lg w-full max-w-md p-6"
        onClick={e => e.stopPropagation()}
      >
        <h3 className="text-lg font-medium mb-4">{isEdit ? 'Edit Camera' : 'Add Camera'}</h3>

        {error && <div className="text-red-400 text-sm mb-3">{error}</div>}

        <label className="block text-sm text-neutral-400 mb-1">Name</label>
        <input
          className="w-full bg-neutral-800 border border-neutral-600 rounded px-3 py-2 text-sm mb-3 focus:outline-none focus:border-blue-500"
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder="Front Door"
        />

        <label className="block text-sm text-neutral-400 mb-1">RTSP URL</label>
        <input
          className="w-full bg-neutral-800 border border-neutral-600 rounded px-3 py-2 text-sm mb-3 focus:outline-none focus:border-blue-500"
          value={rtspUrl}
          onChange={e => setRtspUrl(e.target.value)}
          placeholder="rtsp://user:pass@192.168.1.100/stream1"
        />

        <label className="block text-sm text-neutral-400 mb-1">Rotation</label>
        <div className="flex gap-2 mb-3">
          {[0, 90, 180, 270].map(deg => (
            <button
              key={deg}
              onClick={() => setRotation(deg)}
              className={`px-3 py-1.5 text-sm rounded border ${
                rotation === deg
                  ? 'bg-blue-600 border-blue-500 text-white'
                  : 'bg-neutral-800 border-neutral-600 text-neutral-300 hover:border-neutral-500'
              }`}
            >
              {deg}°
            </button>
          ))}
        </div>

        <label className="flex items-center gap-2 text-sm text-neutral-300 mb-4 cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={e => setEnabled(e.target.checked)}
            className="accent-blue-500"
          />
          Enabled
        </label>

        <div className="flex items-center gap-2">
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded text-sm font-medium disabled:opacity-50"
          >
            {saving ? 'Saving...' : isEdit ? 'Save' : 'Add Camera'}
          </button>
          <button
            onClick={onClose}
            className="px-4 py-2 bg-neutral-700 hover:bg-neutral-600 rounded text-sm"
          >
            Cancel
          </button>
          {isEdit && (
            <div className="ml-auto">
              {confirmDelete ? (
                <div className="flex items-center gap-2">
                  <span className="text-sm text-red-400">Delete?</span>
                  <button
                    onClick={handleDelete}
                    disabled={saving}
                    className="px-3 py-1.5 bg-red-600 hover:bg-red-500 rounded text-sm disabled:opacity-50"
                  >
                    Yes
                  </button>
                  <button
                    onClick={() => setConfirmDelete(false)}
                    className="px-3 py-1.5 bg-neutral-700 hover:bg-neutral-600 rounded text-sm"
                  >
                    No
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setConfirmDelete(true)}
                  className="px-3 py-1.5 text-red-400 hover:text-red-300 text-sm"
                >
                  Delete
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
