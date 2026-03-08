# RichIris - Custom NVR
update claude md as needed for code changes
## Quick Reference
- **Backend**: Runs as Windows service `RichIris` (FastAPI on port 8700)
- **Restart**: `powershell -Command "Restart-Service -Name 'RichIris'"`
- **Config**: `config.yaml` (cameras, storage paths, ffmpeg settings)
- **Recordings**: `G:\RichIris` (segment files per camera per day)
- **Database**: `data/richiris.db` (SQLite, auto-created on first run)
- **Frontend build**: `cd frontend && npm run build` (served from `frontend/dist/`)
- **API docs**: http://localhost:8700/docs

## Architecture
```
Browser (LAN/VPN) → FastAPI (port 8700, 0.0.0.0) → FFmpeg subprocesses (NVENC/NVDEC)
                            |                                |
                        SQLite DB                     G:\RichIris (recordings)
                                                      data\live\ (HLS segments)
                                                      data\playback\ (transcode cache)
```

- Two ffmpeg processes per camera: recording (always on) + live HLS (on-demand)
- Recording uses `-c:v copy` (passthrough, no transcode, no GPU) → HEVC 4K .ts files
- Live view uses HLS with 2s segments, transcoded to H.264 1080p (libx264), started on first viewer request, stopped after 30s idle
- **Playback transcoding**: Recordings are HEVC which browsers can't play. On-demand GPU transcode (h264_nvenc) converts to H.264 HLS in `data/playback/`. Sessions auto-cleanup after 120s idle.
- NVIDIA RTX 4080 SUPER for hardware acceleration (cuda hwaccel)

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
│   │   │   ├── streams.py       # HLS /api/streams/{id}
│   │   │   └── system.py        # /api/system/status + storage + retention
│   │   └── services/
│   │       ├── ffmpeg.py         # Command builder (composable functions)
│   │       ├── stream_manager.py # FFmpeg process lifecycle
│   │       ├── recorder.py       # Segment scanner + DB registration
│   │       ├── clip_exporter.py  # Clip export (concat segments → MP4)
│   │       ├── playback.py       # On-demand HEVC→H.264 transcode for playback
│   │       └── retention.py      # Age + storage-based retention cleanup
│   ├── service.py               # Windows Service wrapper
│   ├── requirements.txt
│   └── run.py                   # Uvicorn entry point (dev)
├── frontend/                    # React 19 + Vite + Tailwind
│   └── src/
│       ├── App.tsx              # Main app - grid with selectable timeline
│       ├── api.ts               # API client functions
│       └── components/
│           ├── CameraGrid.tsx   # Camera grid with selection highlight
│           ├── CameraCard.tsx   # Individual camera thumbnail
│           ├── CameraFullscreen.tsx # Fullscreen view with timeline
│           ├── HlsPlayer.tsx    # HLS.js video player
│           ├── Timeline.tsx     # 24h timeline bar, date picker, clip export
│           └── SystemPage.tsx   # System status + storage
├── config.yaml                  # Main configuration (cameras, paths)
├── build.bat                    # Frontend build script
├── start.bat                    # Quick launcher
├── service-install.bat          # Install Windows service
├── service-uninstall.bat
├── service-restart.bat
└── data/                        # gitignored: DB + live HLS + playback cache
```

## Code Style
- Small focused functions (~10-20 lines max)
- Verbose structured logging via `structlog` (JSON in prod, console in dev)
- Each module gets `logger = logging.getLogger(__name__)`
- Log entry/exit, parameters, and outcomes for significant operations

## API Endpoints
- `GET/POST/PUT/DELETE /api/cameras` - Camera CRUD
- `GET /api/streams/{id}/index.m3u8` - Live HLS playlist
- `GET /api/streams/{id}/{filename}` - HLS segments
- `GET /api/recordings/{id}/dates` - List dates with recordings
- `GET /api/recordings/{id}/segments?date=YYYY-MM-DD` - List segments for a date
- `POST /api/recordings/{id}/playback?start=ISO&end=ISO` - Start transcode session, returns `{session_id, playlist_url}`
- `GET /api/recordings/playback/{session_id}/playback.m3u8` - Session HLS playlist (polled by HLS.js)
- `GET /api/recordings/playback/{session_id}/{filename}` - Transcoded segment files
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

## Frontend UI Flow
- **Grid page**: Click camera → selects it (blue ring), shows timeline at bottom. Click same camera again → fullscreen.
- **Fullscreen page**: Full video player + timeline. Click timeline segment → "Preparing playback..." → GPU transcode → video plays.
- **Timeline**: LIVE button, date picker, 24h bar with blue segments showing recordings. Export clip mode, clips list with download/delete.

## Key Dependencies
- **Backend**: fastapi, uvicorn, sqlalchemy, aiosqlite, pyyaml, structlog, pywin32 (service)
- **Frontend**: react 19, vite, tailwindcss, hls.js

## Cameras
6 cameras configured in config.yaml on 192.168.8.41-46. Streams are RTSP, codec is HEVC (H.265) at 4K (3840x2160).

## Implementation Phases
1. Foundation + Recording (DONE)
2. Live View UI (React + HLS) (DONE)
3. Timeline Playback (DONE)
4. Clip Export (DONE)
5. Retention + System monitoring (DONE)
6. Production - LAN access via VPN, no reverse proxy/auth needed (DONE)
