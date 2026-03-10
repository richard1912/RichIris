export interface Camera {
  id: number
  name: string
  rtsp_url: string
  enabled: boolean
  width: number | null
  height: number | null
  codec: string | null
  fps: number | null
  rotation: number
  created_at: string
}

export interface StreamStatus {
  camera_id: number
  camera_name: string
  running: boolean
  pid: number | null
  uptime_seconds: number | null
  error: string | null
}

export interface SystemStatus {
  streams: StreamStatus[]
  total_cameras: number
  active_streams: number
}

export async function createCamera(data: { name: string; rtsp_url: string; enabled?: boolean; rotation?: number }): Promise<Camera> {
  const res = await fetch('/api/cameras', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to create camera' }))
    throw new Error(err.detail || 'Failed to create camera')
  }
  return res.json()
}

export async function updateCamera(id: number, data: { name?: string; rtsp_url?: string; enabled?: boolean; rotation?: number }): Promise<Camera> {
  const res = await fetch(`/api/cameras/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to update camera' }))
    throw new Error(err.detail || 'Failed to update camera')
  }
  return res.json()
}

export async function deleteCamera(id: number): Promise<void> {
  const res = await fetch(`/api/cameras/${id}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Failed to delete camera')
}

export async function fetchCameras(): Promise<Camera[]> {
  const res = await fetch('/api/cameras')
  if (!res.ok) throw new Error('Failed to fetch cameras')
  return res.json()
}

export async function fetchSystemStatus(): Promise<SystemStatus> {
  const res = await fetch('/api/system/status')
  if (!res.ok) throw new Error('Failed to fetch system status')
  return res.json()
}

export interface RecordingSegment {
  id: number
  camera_id: number
  file_path: string
  start_time: string
  end_time: string | null
  file_size: number | null
  duration: number | null
  in_progress: boolean
}

export function getStreamUrl(cameraId: number): string {
  return `/api/streams/${cameraId}/index.m3u8`
}

export interface PlaybackSession {
  playback_url: string
  window_end: string
  has_more: boolean
}

export async function startPlaybackSession(cameraId: number, start: string): Promise<PlaybackSession> {
  const res = await fetch(
    `/api/recordings/${cameraId}/playback?start=${encodeURIComponent(start)}`,
    { method: 'POST' },
  )
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Playback failed' }))
    throw new Error(err.detail || 'Playback failed')
  }
  const data = await res.json()
  return { playback_url: data.playback_url, window_end: data.window_end, has_more: data.has_more }
}

export async function fetchRecordingDates(cameraId: number): Promise<string[]> {
  const res = await fetch(`/api/recordings/${cameraId}/dates`)
  if (!res.ok) throw new Error('Failed to fetch recording dates')
  return res.json()
}

export async function fetchSegments(cameraId: number, date: string): Promise<RecordingSegment[]> {
  const res = await fetch(`/api/recordings/${cameraId}/segments?date=${date}`)
  if (!res.ok) throw new Error('Failed to fetch segments')
  return res.json()
}

export interface ThumbnailInfo {
  timestamp: string
  url: string
  thumb_width: number
  thumb_height: number
  interval: number
}

export async function fetchThumbnails(cameraId: number, date: string): Promise<ThumbnailInfo[]> {
  const res = await fetch(`/api/recordings/${cameraId}/thumbnails?date=${date}`)
  if (!res.ok) return []
  return res.json()
}

export interface ClipExport {
  id: number
  camera_id: number
  start_time: string
  end_time: string
  file_path: string | null
  status: string
  created_at: string
}

export async function createClipExport(cameraId: number, start: string, end: string): Promise<ClipExport> {
  const res = await fetch('/api/clips', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ camera_id: cameraId, start_time: start, end_time: end }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Export failed' }))
    throw new Error(err.detail || 'Export failed')
  }
  return res.json()
}

export async function fetchClips(cameraId?: number): Promise<ClipExport[]> {
  const url = cameraId != null ? `/api/clips?camera_id=${cameraId}` : '/api/clips'
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed to fetch clips')
  return res.json()
}

export async function fetchClip(clipId: number): Promise<ClipExport> {
  const res = await fetch(`/api/clips/${clipId}`)
  if (!res.ok) throw new Error('Failed to fetch clip')
  return res.json()
}

export function getClipDownloadUrl(clipId: number): string {
  return `/api/clips/${clipId}/download`
}

export async function deleteClip(clipId: number): Promise<void> {
  const res = await fetch(`/api/clips/${clipId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Failed to delete clip')
}

export interface CameraStorageStats {
  camera_id: number
  segment_count: number
  total_size_bytes: number
  oldest_recording: string | null
  newest_recording: string | null
}

export interface StorageStats {
  disk_total_bytes: number
  disk_used_bytes: number
  disk_free_bytes: number
  recordings_total_bytes: number
  max_storage_bytes: number
  max_age_days: number
  camera_stats: CameraStorageStats[]
}

export interface RetentionResult {
  deleted: number
  freed_bytes: number
}

export async function fetchStorageStats(): Promise<StorageStats> {
  const res = await fetch('/api/system/storage')
  if (!res.ok) throw new Error('Failed to fetch storage stats')
  return res.json()
}

export async function runRetention(): Promise<RetentionResult> {
  const res = await fetch('/api/system/retention/run', { method: 'POST' })
  if (!res.ok) throw new Error('Failed to run retention')
  return res.json()
}
