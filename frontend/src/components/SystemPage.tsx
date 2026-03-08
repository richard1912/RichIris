import { useEffect, useState, useCallback } from 'react'
import type { Camera, StorageStats, StreamStatus, RetentionResult } from '../api'
import { fetchStorageStats, fetchSystemStatus, fetchCameras, runRetention } from '../api'

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

export default function SystemPage({ onBack }: { onBack: () => void }) {
  const [storage, setStorage] = useState<StorageStats | null>(null)
  const [streams, setStreams] = useState<StreamStatus[]>([])
  const [cameras, setCameras] = useState<Camera[]>([])
  const [retentionRunning, setRetentionRunning] = useState(false)
  const [retentionResult, setRetentionResult] = useState<RetentionResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [stor, status, cams] = await Promise.all([
        fetchStorageStats(),
        fetchSystemStatus(),
        fetchCameras(),
      ])
      setStorage(stor)
      setStreams(status.streams)
      setCameras(cams)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, 10000)
    return () => clearInterval(interval)
  }, [refresh])

  const handleRetention = async () => {
    setRetentionRunning(true)
    setRetentionResult(null)
    try {
      const result = await runRetention()
      setRetentionResult(result)
      refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Retention failed')
    } finally {
      setRetentionRunning(false)
    }
  }

  const cameraMap = new Map(cameras.map(c => [c.id, c]))

  const diskUsedPct = storage
    ? Math.round((storage.disk_used_bytes / storage.disk_total_bytes) * 100)
    : 0
  const recPct = storage && storage.max_storage_bytes > 0
    ? Math.round((storage.recordings_total_bytes / storage.max_storage_bytes) * 100)
    : 0

  return (
    <div className="min-h-screen flex flex-col">
      <header className="flex items-center justify-between px-6 py-4 border-b border-neutral-800">
        <div className="flex items-center gap-4">
          <button
            onClick={onBack}
            className="text-neutral-400 hover:text-white transition-colors"
          >
            &larr; Back
          </button>
          <h1 className="text-xl font-semibold tracking-tight">System</h1>
        </div>
        {error && <span className="text-sm text-red-400">{error}</span>}
      </header>

      <main className="flex-1 p-6 space-y-6 max-w-5xl">
        {/* Storage Overview */}
        {storage && (
          <section>
            <h2 className="text-lg font-medium mb-3">Storage</h2>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <StatCard
                label="Disk Usage"
                value={`${formatBytes(storage.disk_used_bytes)} / ${formatBytes(storage.disk_total_bytes)}`}
                sub={`${formatBytes(storage.disk_free_bytes)} free`}
                pct={diskUsedPct}
              />
              <StatCard
                label="Recordings"
                value={formatBytes(storage.recordings_total_bytes)}
                sub={`Limit: ${formatBytes(storage.max_storage_bytes)}`}
                pct={recPct}
              />
              <StatCard
                label="Retention"
                value={`${storage.max_age_days} days`}
                sub={`${storage.camera_stats.reduce((a, c) => a + c.segment_count, 0)} total segments`}
              />
            </div>
          </section>
        )}

        {/* Stream Health */}
        <section>
          <h2 className="text-lg font-medium mb-3">Streams</h2>
          <div className="border border-neutral-800 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-neutral-800 text-neutral-400 text-left">
                  <th className="px-4 py-2">Camera</th>
                  <th className="px-4 py-2">Status</th>
                  <th className="px-4 py-2">Uptime</th>
                  <th className="px-4 py-2">PID</th>
                  <th className="px-4 py-2">Error</th>
                </tr>
              </thead>
              <tbody>
                {streams.map(s => (
                  <tr key={s.camera_id} className="border-b border-neutral-800/50">
                    <td className="px-4 py-2">{s.camera_name}</td>
                    <td className="px-4 py-2">
                      <span className={`inline-block w-2 h-2 rounded-full mr-2 ${s.running ? 'bg-green-500' : 'bg-red-500'}`} />
                      {s.running ? 'Running' : 'Stopped'}
                    </td>
                    <td className="px-4 py-2 text-neutral-400">
                      {s.uptime_seconds != null ? formatUptime(s.uptime_seconds) : '-'}
                    </td>
                    <td className="px-4 py-2 text-neutral-500 font-mono text-xs">{s.pid ?? '-'}</td>
                    <td className="px-4 py-2 text-red-400 text-xs truncate max-w-48">{s.error ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* Per-Camera Storage */}
        {storage && storage.camera_stats.length > 0 && (
          <section>
            <h2 className="text-lg font-medium mb-3">Per-Camera Storage</h2>
            <div className="border border-neutral-800 rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-neutral-800 text-neutral-400 text-left">
                    <th className="px-4 py-2">Camera</th>
                    <th className="px-4 py-2">Segments</th>
                    <th className="px-4 py-2">Size</th>
                    <th className="px-4 py-2">Oldest</th>
                    <th className="px-4 py-2">Newest</th>
                  </tr>
                </thead>
                <tbody>
                  {storage.camera_stats.map(cs => {
                    const cam = cameraMap.get(cs.camera_id)
                    return (
                      <tr key={cs.camera_id} className="border-b border-neutral-800/50">
                        <td className="px-4 py-2">{cam?.name ?? `Camera ${cs.camera_id}`}</td>
                        <td className="px-4 py-2 text-neutral-400">{cs.segment_count.toLocaleString()}</td>
                        <td className="px-4 py-2">{formatBytes(cs.total_size_bytes)}</td>
                        <td className="px-4 py-2 text-neutral-400 text-xs">
                          {cs.oldest_recording ? new Date(cs.oldest_recording).toLocaleDateString() : '-'}
                        </td>
                        <td className="px-4 py-2 text-neutral-400 text-xs">
                          {cs.newest_recording ? new Date(cs.newest_recording).toLocaleDateString() : '-'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* Retention Actions */}
        <section>
          <h2 className="text-lg font-medium mb-3">Retention</h2>
          <div className="flex items-center gap-4">
            <button
              onClick={handleRetention}
              disabled={retentionRunning}
              className="px-4 py-2 bg-neutral-800 hover:bg-neutral-700 rounded text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {retentionRunning ? 'Running...' : 'Run Retention Now'}
            </button>
            {retentionResult && (
              <span className="text-sm text-neutral-400">
                Deleted {retentionResult.deleted} segments, freed {formatBytes(retentionResult.freed_bytes)}
              </span>
            )}
          </div>
        </section>
      </main>
    </div>
  )
}

function StatCard({ label, value, sub, pct }: {
  label: string
  value: string
  sub: string
  pct?: number
}) {
  return (
    <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-4">
      <div className="text-xs text-neutral-500 uppercase tracking-wide mb-1">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
      <div className="text-xs text-neutral-400 mt-1">{sub}</div>
      {pct != null && (
        <div className="mt-2 h-1.5 bg-neutral-800 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${pct > 90 ? 'bg-red-500' : pct > 70 ? 'bg-yellow-500' : 'bg-blue-500'}`}
            style={{ width: `${Math.min(pct, 100)}%` }}
          />
        </div>
      )}
    </div>
  )
}
