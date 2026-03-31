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
- **Frontend build**: `cd frontend && npm run build` (served from `frontend/dist/`) — legacy web UI
- **Native app**: Flutter app in `app/` (Windows + Android), replaces web UI for live/playback/export
- **App build (Windows)**: `cd app && flutter build windows --release` → `app/build/windows/x64/runner/Release/`
- **App build (Android)**: `cd app && flutter build apk --release` → `app/build/app/outputs/flutter-apk/`
- **API docs**: http://localhost:8700/docs

## Architecture
```
Flutter App (Win/Android) → HTTP fMP4 → FastAPI:8700 → go2rtc:1984 ← RTSP sub-stream
                          → HTTP MP4 (playback) → FastAPI:8700 → FFmpeg remux
Browser (legacy)          → WebSocket (MSE) → FastAPI:8700 → go2rtc:1984
                          → FastAPI:8700 → FFmpeg subprocesses (recording only)
                                    |                    |
                                SQLite DB         G:\RichIris (recordings)
                                                  data\playback\ (remux cache)
```

- One ffmpeg process per camera for recording (always on). Live view handled by go2rtc (separate service).
- Recording uses `-c:v copy` (passthrough, no transcode, no GPU) → HEVC 4K .ts files. Segments are renamed by the scanner to `{Camera Name} {YYYY-MM-DD} {HH.MM} - {HH.MM}.ts` after completion. Folders use camera name with spaces and capitals (e.g., `Camera 1/`).
- **Recording reliability**: ffmpeg uses `-timeout` (30s socket I/O timeout) so it exits if a camera stops sending data. A watchdog task per camera checks every 2 minutes that `.ts` files are being modified; if no file has been updated in 5 minutes, it kills the stale ffmpeg process. Both mechanisms trigger the existing process monitor to auto-restart recording.
- **Live view (go2rtc MSE — web UI)**: go2rtc takes RTSP input (prefers sub-stream URL when configured) and outputs MSE (fMP4 over WebSocket). Frontend opens a WebSocket to go2rtc, sends `{"type":"mse"}`, and receives a continuous push-based stream of fMP4 segments. No HLS, no polling, no file I/O. Works reliably over WireGuard VPN. Streams are registered dynamically by RichIris via go2rtc's REST API on camera startup. **MsePlayer uses a persistent video pool** — video elements and WebSocket connections are kept alive at the module level (`Map<cameraId, StreamEntry>`), surviving React unmount/remount. View transitions (grid↔fullscreen) just move the same `<video>` DOM element between containers via `appendChild`, so the feed is never interrupted.
- **Live view (HTTP fMP4 — native app)**: `GET /api/streams/{camera_id}/live.mp4?quality=` proxies go2rtc's HTTP fMP4 stream (`http://127.0.0.1:1984/api/stream.mp4?src={name}`) through the backend as a StreamingResponse. The Flutter app uses media_kit (libmpv) to play this URL natively — no MSE/WebSocket needed. Grid view uses "low" quality for bandwidth savings; fullscreen uses the selected quality. Auto-reconnects on stream errors.
- **Playback remux (no transcode)**: Recordings are HEVC .ts files. All qualities use fragmented MP4 (`-c copy -movflags frag_keyframe+empty_moov`) streamed via StreamingResponse — playback starts in ~200ms as soon as the first fragment is written, no waiting for full remux. Browser plays HEVC natively (Chrome 107+, Edge, Safari have hardware HEVC decode). Single files use `-ss` before `-i` for fast keyframe seek. Multiple files use concat demuxer with `-ss` after `-i`. Sessions auto-cleanup after 120s idle.
- NVIDIA RTX 4080 SUPER available (used for AI person detection via YOLO)
- **Motion detection**: OpenCV reads sub-stream RTSP at 2 FPS in a ThreadPoolExecutor (blocking cv2.VideoCapture off async loop). Frame differencing: grayscale → GaussianBlur(21,21) → absdiff → threshold(25) → count changed pixels as percentage. Sensitivity 0-100 maps to area threshold: `(101 - sensitivity) * 0.05%`. Events stored as MotionEvent rows (start_time, end_time, peak_intensity, detection_label, detection_confidence). 10-second cooldown between events. Per-camera fields: `motion_sensitivity` (0=off), `motion_script` (runs on motion start), `motion_script_off` (runs when motion ends after cooldown). Env vars: MOTION_CAMERA, MOTION_TIME, MOTION_INTENSITY, DETECTION_LABEL, DETECTION_CONFIDENCE. Script must use full path to python.exe (not `python3`) since NSSM service PATH differs from user PATH. Changes to sensitivity/scripts/enabled take effect immediately via `detector.update_camera()` — no service restart needed. Configurable via camera edit form.
- **AI person detection**: Optional per-camera YOLO-based person detection (Frigate-inspired two-stage pipeline). When `ai_detection` is enabled, motion detection acts as a cheap pre-filter — AI inference only runs on frames where motion was already detected. Uses YOLOv8n (nano, ~6MB) on CUDA GPU via `ultralytics` package. Single shared `ObjectDetector` instance serves all cameras. Inference takes ~2-3ms per frame on RTX 4080 SUPER. Per-camera fields: `ai_detection` (bool, default off), `ai_confidence_threshold` (0-100, default 50). When AI is on, motion events only fire when a person is detected above the confidence threshold. Falls back to CPU if CUDA unavailable.

### Video Quality Selection
- **Two independent selectors** in header (also in fullscreen view): **Stream** (S1/S2) and **Quality** (Direct/High/Low)
  - Stream persisted in SharedPreferences key `richiris-stream-source`, Quality in `richiris-quality`
- **Live streams**: go2rtc registers 6 streams per camera (S1/S2 × Direct/High/Low). Unused quality streams are lazy — zero resources until a client connects.
  - S1 Direct: raw 4K HEVC passthrough (no ffmpeg, zero CPU)
  - S1 High: 1920×1080 H.264 transcode, S1 Low: 1280×720 H.264
  - S2 Direct: raw sub-stream passthrough (H.264, ~640x480, no ffmpeg)
  - S2 High: sub-stream H.264 re-encode (native res), S2 Low: 320×180
- **Playback**: All qualities use fragmented MP4 streaming. Direct/High use `-c copy` (HEVC passthrough). Medium/Low use NVENC GPU transcode (`h264_nvenc`).
  - Direct/High: 3840x2160 (4K native), Medium: 1280x720 @ 2Mbps, Low: 640x360 @ 800kbps
- Backend `streams.py` accepts `?stream=s1/s2&quality=direct/high/low` params for both HTTP fMP4 and WebSocket
- Backend uses module-level connection-pooled httpx client for go2rtc fMP4 proxying
- Stream pre-warming at backend startup triggers go2rtc RTSP connections proactively
- **Low-latency LivePlayer**: 512KB buffer, mpv `low-latency` profile, `cache=no`, `untimed=yes`, exponential backoff retry (500ms→10s)
- Legacy web frontend still works (defaults `stream=s2`, maps old quality values)

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
│   │   │   ├── system.py        # /api/system/status + storage + retention
│   │   │   └── motion.py        # /api/motion events
│   │   └── services/
│   │       ├── ffmpeg.py              # Command builder (recording only)
│   │       ├── stream_manager.py      # FFmpeg recording process lifecycle + go2rtc registration
│   │       ├── go2rtc_client.py       # REST client for go2rtc stream management
│   │       ├── recorder.py            # Segment scanner + DB registration
│   │       ├── clip_exporter.py       # Clip export (concat segments → MP4)
│   │       ├── playback.py            # On-demand HEVC→H.264 transcode for playback
│   │       ├── thumbnail_capture.py   # Real-time RTSP thumbnail capture
│   │       ├── retention.py           # Age + storage-based retention cleanup
│   │       ├── motion_detector.py    # OpenCV frame-diff motion detection
│   │       └── object_detector.py    # YOLO AI person detection (GPU)
│   ├── requirements.txt
│   └── run.py                   # Uvicorn entry point (dev)
├── go2rtc/
│   ├── go2rtc.exe               # go2rtc binary (RTSP → MSE/WebSocket)
│   └── go2rtc.yaml              # go2rtc config (API port, streams registered dynamically)
├── frontend/                    # React 19 + Vite + Tailwind (legacy web UI)
│   └── src/
│       ├── App.tsx              # Main app - grid with selectable timeline
│       ├── api.ts               # API client functions
│       └── components/
│           ├── CameraGrid.tsx   # Camera grid with selection highlight
│           ├── CameraCard.tsx   # Individual camera thumbnail
│           ├── CameraFullscreen.tsx # Fullscreen view with timeline
│           ├── MsePlayer.tsx    # go2rtc MSE WebSocket player (persistent video pool)
│           ├── Timeline.tsx     # 24h timeline bar, date picker, clip export
│           └── SystemPage.tsx   # System status + storage
├── app/                         # Flutter native app (Windows + Android)
│   ├── lib/
│   │   ├── main.dart            # Entry point, MediaKit init
│   │   ├── app.dart             # MaterialApp, navigation, state management
│   │   ├── theme.dart           # Dark theme
│   │   ├── config/              # API config, quality tiers, constants
│   │   ├── models/              # Data classes (Camera, RecordingSegment, etc.)
│   │   ├── services/            # API layer (Dio HTTP client)
│   │   ├── screens/             # Home, Fullscreen, System, Settings, CameraForm
│   │   ├── widgets/             # CameraGrid, CameraCard, LivePlayer, QualitySelector
│   │   │   └── timeline/        # CustomPainter timeline (controller, painter, minimap)
│   │   └── utils/               # Time/format utilities
│   └── pubspec.yaml             # Dependencies: media_kit, dio, shared_preferences
├── config.yaml                  # Main configuration (cameras, paths)
├── rebuild.bat                  # Frontend build script
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
- `GET /api/streams/{id}/live.mp4?quality=` - HTTP fMP4 proxy for native app live view (proxies go2rtc stream.mp4)
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
- `GET /api/motion/{id}/events?date=YYYY-MM-DD` - Motion events for a camera on a date

## Frontend UI Flow
- **Grid page**: Click camera → selects it (blue ring), shows timeline at bottom. Click same camera again → fullscreen.
- **Fullscreen page**: Full video player + timeline + speed controls (shown in playback mode). Click timeline segment → instant MP4 remux (< 1s) → video plays natively (HEVC in browser).
- **Speed controls**: -32x to 32x. 1x/2x/4x use native `video.playbackRate`. 16x/32x use interval-based jumping. Reverse speeds request new playback sessions at earlier times.
- **Timeline**: LIVE button, date picker, zoomable timeline bar with blue segments. Mouse wheel zooms (1h-24h range), minimap shown when zoomed for pan navigation. Export clip mode, clips list with download/delete. Trickplay thumbnail preview on hover (individual JPEGs captured from RTSP).
- **Timeline (Flutter app)**: CustomPainter-based timeline with scrub indicator showing time label. Scroll wheel zoom (Windows), pinch-to-zoom (Android). Mouse hover (Windows) or touch scrub (Android) shows white scrub line with time + trickplay thumbnail via OverlayEntry (floats above timeline, `gaplessPlayback: true` prevents flicker). Tap/release navigates to time and instantly moves red playhead. Playhead timer pauses live-time updates during playback transition (`_manualPan` flag).
- **Motion events on timeline**: Orange/amber bars above blue recording segments. Visible in both main timeline and minimap. Polled alongside segments (every 15s for today's date).

- **Inline transitions**: Grid↔fullscreen transitions render inline (no Navigator.push) for faster view switching. No route transition animation delay.
- **Playback**: Uses direct `<video src="...">` for MP4 playback (no HLS.js). MsePlayer is only used for live streams.

## Key Dependencies
- **Backend**: fastapi, uvicorn, sqlalchemy, aiosqlite, pyyaml, structlog, httpx, opencv-python-headless, numpy, ultralytics (YOLOv8)
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
10. Flutter native app - Windows + Android app with media_kit, quality selection, timeline interactions (DONE)
11. Motion detection - OpenCV frame differencing on sub-streams, per-camera sensitivity, timeline overlay, script execution (DONE)
12. AI person detection - YOLOv8n GPU inference gated by motion pre-filter, per-camera toggle + confidence threshold (DONE)
