# RichIris - Custom NVR
update claude md as needed for code changes
## Quick Reference
- **Backend**: Runs as Windows service `RichIris` via NSSM (FastAPI on port 8700)
- **Restart**: `nssm restart RichIris`
- **go2rtc**: Runs as Windows service `go2rtc` via NSSM (port 1984, MSE/WebSocket live view)
- **Restart go2rtc**: `nssm restart go2rtc`
- **Config**: `config.yaml` (cameras, storage paths, ffmpeg settings, go2rtc)
- **Recordings**: `G:\RichIris` (segment files per camera per day, named `Camera 1 2026-03-08 13.30 - 13.45.ts`)
- **Database**: `data/richiris.db` (SQLite, auto-created on first run)
- **Frontend build**: `cd frontend && npm run build` (served from `frontend/dist/`)
- **API docs**: http://localhost:8700/docs

## Architecture
```
Browser (LAN/VPN) → WebSocket (MSE) → go2rtc:1984 ← RTSP sub-stream (zero transcode)
                  → FastAPI:8700 → FFmpeg subprocesses (recording only)
                            |                    |
                        SQLite DB         G:\RichIris (recordings)
                                          data\playback\ (remux cache)
```

- One ffmpeg process per camera for recording (always on). Live view handled by go2rtc (separate service).
- Recording uses `-c:v copy` (passthrough, no transcode, no GPU) → HEVC 4K .ts files. Segments are renamed by the scanner to `{Camera Name} {YYYY-MM-DD} {HH.MM} - {HH.MM}.ts` after completion. Folders use camera name with spaces and capitals (e.g., `Camera 1/`).
- **Recording reliability**: ffmpeg uses `-timeout` (30s socket I/O timeout) so it exits if a camera stops sending data. A watchdog task per camera checks every 2 minutes that `.ts` files are being modified; if no file has been updated in 5 minutes, it kills the stale ffmpeg process. Both mechanisms trigger the existing process monitor to auto-restart recording.
- **Live view (go2rtc MSE)**: go2rtc takes RTSP input (prefers sub-stream URL when configured) and outputs MSE (fMP4 over WebSocket). Frontend opens a WebSocket to go2rtc, sends `{"type":"mse"}`, and receives a continuous push-based stream of fMP4 segments. No HLS, no polling, no file I/O. Works reliably over WireGuard VPN. Streams are registered dynamically by RichIris via go2rtc's REST API on camera startup.
- **Playback remux (no transcode)**: Recordings are HEVC .ts files. Instead of GPU transcoding, ffmpeg remuxes to MP4 with `-c copy -movflags +faststart` (instant, < 1 second). Browser plays HEVC natively (Chrome 107+, Edge, Safari have hardware HEVC decode). Single files use `-ss` before `-i` for fast keyframe seek. Multiple files use concat demuxer with `-ss` after `-i`. Sessions auto-cleanup after 120s idle.
- NVIDIA RTX 4080 SUPER available (not currently used — recording is passthrough, live view is zero-transcode via go2rtc)

### Important: Timezone handling
- Recordings are stored in the DB as **local time without timezone** (e.g. `2026-03-08T10:36:02`)
- Server is **UTC+11** (Australia)
- Frontend must NOT use `.toISOString()` for playback times (converts to UTC, breaks queries)
- Always format times as local ISO strings to match DB format

## Project Structure
```
RichIris/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app + lifespan
│   │   ├── config.py            # Settings from config.yaml
│   │   ├── logging_config.py    # structlog setup
│   │   ├── database.py          # SQLAlchemy async engine
│   │   ├── models.py            # Camera, Recording, ClipExport
│   │   ├── schemas.py           # Pydantic schemas
│   │   ├── routers/
│   │   │   ├── cameras.py       # CRUD /api/cameras
│   │   │   ├── clips.py         # Clip export /api/clips (no duration limit)
│   │   │   ├── recordings.py    # Playback /api/recordings + transcode sessions
│   │   │   ├── streams.py       # go2rtc stream info /api/streams/{id}/live
│   │   │   └── system.py        # /api/system/status + storage + retention
│   │   └── services/
│   │       ├── ffmpeg.py              # Command builder (recording only)
│   │       ├── stream_manager.py      # FFmpeg recording process lifecycle + go2rtc registration
│   │       ├── go2rtc_client.py       # REST client for go2rtc stream management
│   │       ├── recorder.py            # Segment scanner + DB registration
│   │       ├── clip_exporter.py       # Clip export (concat segments → MP4)
│   │       ├── playback.py            # On-demand HEVC→H.264 transcode for playback
│   │       ├── thumbnail_capture.py   # Real-time RTSP thumbnail capture
│   │       └── retention.py           # Age + storage-based retention cleanup
│   ├── requirements.txt
│   └── run.py                   # Uvicorn entry point (dev)
├── go2rtc/
│   ├── go2rtc.exe               # go2rtc binary (RTSP → MSE/WebSocket)
│   └── go2rtc.yaml              # go2rtc config (API port, streams registered dynamically)
├── frontend/                    # React 19 + Vite + Tailwind
│   └── src/
│       ├── App.tsx              # Main app - grid with selectable timeline
│       ├── api.ts               # API client functions
│       └── components/
│           ├── CameraGrid.tsx   # Camera grid with selection highlight
│           ├── CameraCard.tsx   # Individual camera thumbnail
│           ├── CameraFullscreen.tsx # Fullscreen view with timeline
│           ├── MsePlayer.tsx    # go2rtc MSE WebSocket player (live streams)
│           ├── Timeline.tsx     # 24h timeline bar, date picker, clip export
│           └── SystemPage.tsx   # System status + storage
├── config.yaml                  # Main configuration (cameras, paths)
├── build.bat                    # Frontend build script
├── start.bat                    # Quick launcher
├── service-install.bat          # Install Windows service
├── service-uninstall.bat
├── service-restart.bat
└── data/                        # gitignored: DB + playback cache
```

## Code Style
- Small focused functions (~10-20 lines max)
- Verbose structured logging via `structlog` (JSON in prod, console in dev)
- Each module gets `logger = logging.getLogger(__name__)`
- Log entry/exit, parameters, and outcomes for significant operations

## API Endpoints
- `GET/POST/PUT/DELETE /api/cameras` - Camera CRUD
- `GET /api/streams/{id}/live` - go2rtc stream info (stream_name, port) for WebSocket MSE URL construction
- `GET /api/recordings/{id}/dates` - List dates with recordings
- `GET /api/recordings/{id}/segments?date=YYYY-MM-DD` - List segments for a date
- `POST /api/recordings/{id}/playback?start=ISO` - Start remux session, returns `{session_id, playback_url, window_end, has_more}`
- `GET /api/recordings/playback/{session_id}/playback.mp4` - Remuxed MP4 file (HEVC, native browser playback)
- `GET /api/recordings/segment/{recording_id}` - Serve raw recording segment file
- `GET /api/system/status` - Stream health, camera count
- `GET /api/system/storage` - Disk usage, per-camera recording stats
- `POST /api/system/retention/run` - Manually trigger retention cleanup
- `GET /api/health` - Health check
- `POST /api/clips` - Create clip export (camera_id, start_time, end_time, no duration limit)
- `GET /api/clips` - List clips (optional ?camera_id=)
- `GET /api/clips/{id}` - Get clip status
- `GET /api/clips/{id}/download` - Download completed clip MP4
- `DELETE /api/clips/{id}` - Delete clip and file
- `GET /api/recordings/{id}/thumbnails?date=YYYY-MM-DD` - Thumbnail metadata for a date (individual JPEGs)
- `GET /api/recordings/{id}/thumb/{date}/{filename}` - Serve individual thumbnail JPEG (cached 24h)

## Frontend UI Flow
- **Grid page**: Click camera → selects it (blue ring), shows timeline at bottom. Click same camera again → fullscreen.
- **Fullscreen page**: Full video player + timeline + speed controls (shown in playback mode). Click timeline segment → instant MP4 remux (< 1s) → video plays natively (HEVC in browser).
- **Speed controls**: -32x to 32x. 1x/2x/4x use native `video.playbackRate`. 16x/32x use interval-based jumping. Reverse speeds request new playback sessions at earlier times.
- **Timeline**: LIVE button, date picker, zoomable timeline bar with blue segments. Mouse wheel zooms (1h-24h range), minimap shown when zoomed for pan navigation. Export clip mode, clips list with download/delete. Trickplay thumbnail preview on hover (individual JPEGs captured from RTSP).
- **Playback**: Uses direct `<video src="...">` for MP4 playback (no HLS.js). MsePlayer is only used for live streams.

## Key Dependencies
- **Backend**: fastapi, uvicorn, sqlalchemy, aiosqlite, pyyaml, structlog, httpx
- **Service**: NSSM (installed via `winget install NSSM.NSSM`)
- **Live view**: go2rtc (Go binary, RTSP → MSE/WebSocket, installed as Windows service)
- **Frontend**: react 19, vite, tailwindcss

## Cameras
6 cameras configured in config.yaml on 192.168.8.41-46. Streams are RTSP, codec is HEVC (H.265) at 4K (3840x2160). Sub-streams are H.264 (used by go2rtc for zero-transcode live view).

## Implementation Phases
1. Foundation + Recording (DONE)
2. Live View UI (React + HLS) (DONE)
3. Timeline Playback (DONE)
4. Clip Export (DONE)
5. Retention + System monitoring (DONE)
6. Production - LAN access via VPN, no reverse proxy/auth needed (DONE)
7. Trickplay thumbnails - real-time RTSP capture, individual JPEG previews on timeline hover (DONE)
8. Sub-stream live view - use camera's H.264 sub-stream for live, no transcode, no recording interruption (DONE)
9. go2rtc MSE live view - replaced HLS with go2rtc WebSocket MSE for reliable VPN streaming (DONE)
