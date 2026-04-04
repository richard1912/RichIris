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
- **App**: Flutter app in `app/` (Windows + Android)
- **App build (Windows)**: `cd app && flutter build windows --release` → `app/build/windows/x64/runner/Release/`
- **App build (Android)**: `cd app && flutter build apk --release` → `app/build/app/outputs/flutter-apk/`
- **API docs**: http://localhost:8700/docs

## Architecture
```
Flutter App (Win/Android) → HTTP fMP4 → FastAPI:8700 → go2rtc:1984 ← RTSP sub-stream
                          → HTTP MP4 (playback) → FastAPI:8700 → FFmpeg remux
                                    |                    |
                                SQLite DB         G:\RichIris (recordings)
                                                  data\playback\ (remux cache)
```

- One ffmpeg process per camera for recording (always on). Live view handled by go2rtc (separate service).
- Recording uses `-c:v copy` (passthrough, no transcode, no GPU) → HEVC 4K .ts files. Segments are renamed by the scanner to `{Camera Name} {YYYY-MM-DD} {HH.MM} - {HH.MM}.ts` after completion. Folders use camera name with spaces and capitals (e.g., `Camera 1/`).
- **Recording reliability**: ffmpeg uses `-timeout` (30s socket I/O timeout) so it exits if a camera stops sending data. A watchdog task per camera checks every 2 minutes that `.ts` files are being modified; if no file has been updated in 5 minutes, it kills the stale ffmpeg process. Both mechanisms trigger the existing process monitor to auto-restart recording.
- **Live view (HTTP fMP4)**: `GET /api/streams/{camera_id}/live.mp4?quality=` proxies go2rtc's HTTP fMP4 stream (`http://127.0.0.1:1984/api/stream.mp4?src={name}`) through the backend as a StreamingResponse. The Flutter app uses media_kit (libmpv) to play this URL natively — no MSE/WebSocket needed. Grid view uses "low" quality for bandwidth savings; fullscreen uses the selected quality. Auto-reconnects on stream errors.
- **Playback**: Recordings are HEVC .ts files. Direct = raw `.ts` served instantly (no ffmpeg). High/Low/Ultra Low = HEVC NVENC transcode via `PlaybackManager` into fragmented MP4 (`-movflags frag_keyframe+empty_moov`) streamed via `StreamingResponse`. ffmpeg handles seek (`-ss` before `-i`). Sessions auto-cleanup after 30s idle; same-camera eviction prevents ffmpeg accumulation.
- NVIDIA RTX 4080 SUPER available (used for AI person detection via YOLO)
- **Motion detection + AI object detection**: Simplified snapshot-based pipeline. Fetches JPEG snapshots from go2rtc's HTTP frame API (`GET /api/frame.jpeg?src={stream}_s2_direct`) every ~1s using httpx (no cv2.VideoCapture — clean HTTP timeouts, no hung threads). go2rtc HTTP API on port **1984**. Motion pre-filter uses running weighted-average baseline: `avg = alpha * frame + (1-alpha) * avg` (alpha=0.2 for first 25 frames, then 0.01). Diff against baseline → GaussianBlur(21,21) → absdiff → threshold(25). `changed_pct > 40%` triggers hard baseline reset (IR switch). Sensitivity 0-100 maps to area threshold: `(101 - sensitivity) * 0.05%`. When motion exceeds threshold and AI is enabled, frame goes directly to YOLO — if matching object detected above confidence threshold, event fires immediately. No Frigate-style median/history pipeline. Uses YOLO11x on CUDA (RTX 4080 SUPER), min bounding box 0.2% of frame area. Falls back to CPU if CUDA unavailable. **Multi-category detection**: per-camera toggles for persons (COCO class 0), vehicles (bicycle/car/motorcycle/bus/truck — classes 1,2,3,5,7), and animals (bird/cat/dog/horse/sheep/cow/elephant/bear/zebra/giraffe — classes 14-23). `detection_label` stores the specific COCO class name (e.g., "car", "dog"). Events stored as MotionEvent rows (start_time, end_time, peak_intensity, detection_label, detection_confidence). 10-second cooldown between events. Per-camera fields: `motion_sensitivity` (0=off), `ai_detection` (master bool), `ai_detect_persons`/`ai_detect_vehicles`/`ai_detect_animals` (category bools), `ai_confidence_threshold` (0-100), `motion_scripts` (JSON array of script pairs with per-category triggers). **Multiple script pairs**: each entry has `on`/`off` scripts and category booleans (`persons`, `vehicles`, `animals`, `motion_only`). A script only fires if its category matches the detection — e.g., a script with only `persons: true` won't fire for vehicle detections. Legacy `motion_script`/`motion_script_off` fields auto-migrated to `motion_scripts` on startup. Env vars: MOTION_CAMERA, MOTION_TIME, MOTION_INTENSITY, DETECTION_LABEL, DETECTION_CONFIDENCE. Script must use full path to python.exe (not `python3`) since NSSM service PATH differs from user PATH. Changes take effect immediately via `detector.update_camera()`. Heartbeat log every 5 min per camera.

### Video Quality Selection
- **Two independent selectors** in header (also in fullscreen view): **Stream** (Main/Sub) and **Quality** (Direct/High/Low/Ultra Low)
  - Stream selector only shown during live view (not during playback — playback uses .ts files, not RTSP)
  - Stream persisted in SharedPreferences key `richiris-stream-source`, live quality in `richiris-quality`, playback quality in `richiris-playback-quality`
  - Separate quality preferences for live vs playback — switching modes restores the saved preference for that mode
  - Changing quality during playback triggers `didUpdateWidget` → restarts playback at current position with new quality
  - **Android**: Direct quality hidden for live view only (raw passthrough has compatibility issues with HTMS cameras). Direct available for playback. Default quality is High (live) / Direct (playback) on Android, Direct on Windows.
- **Bitrate probing**: At startup, backend probes each camera's actual bitrate (5s ffmpeg sample) and codec (ffprobe). Playback also probes .ts file bitrate+codec via ffprobe before transcoding.
- **Live streams**: go2rtc registers 8 streams per camera (Main/Sub × Direct/High/Low/Ultra Low). Unused quality streams are lazy — zero resources until a client connects.
  - Main Direct: raw 4K HEVC passthrough (no ffmpeg, zero CPU)
  - Main High: HEVC re-encode, source-matched quality (probed at startup via ffmpeg sampling)
  - Main Low: HEVC re-encode, 1/8 of source bitrate
  - Main Ultra Low: HEVC re-encode, 1/16 of source bitrate, 15fps, no B-frames, short GOP (30 frames)
  - Sub Direct: raw sub-stream passthrough (no ffmpeg)
  - Sub High: HEVC re-encode, source-matched quality
  - Sub Low: HEVC re-encode, 1/8 of source bitrate
  - Sub Ultra Low: HEVC re-encode, 1/16 of source bitrate, 15fps, no B-frames, short GOP
- **Playback**: Quality tiers work for both live and playback. Direct = raw `.ts` file served instantly (no ffmpeg). High/Low/Ultra Low = HEVC NVENC transcode via `PlaybackManager` into fragmented MP4 (`-movflags frag_keyframe+empty_moov`) streamed via `StreamingResponse`. ffmpeg applies seek (`-ss` before `-i`), so client gets `seek_seconds: 0`. Sessions auto-cleanup after 30s idle; same-camera eviction prevents ffmpeg accumulation.
  - High: source bitrate (probed from .ts file via ffprobe)
  - Low: 1/8 of source bitrate
  - Ultra Low: 1/16 of source bitrate, 15fps, no B-frames (`-bf 0`), short GOP (`-g 30`)
- Backend `streams.py` accepts `?stream=s1/s2&quality=direct/high/low/ultralow` params for HTTP fMP4
- Backend uses module-level connection-pooled httpx client for go2rtc fMP4 proxying
- Stream pre-warming at backend startup triggers go2rtc RTSP connections proactively
- **Low-latency LivePlayer**: 512KB buffer, mpv `low-latency` profile, `cache=no`, `untimed=yes`, exponential backoff retry (500ms→10s)
- **Video stats bar**: Shown by default in fullscreen view (togglable). Displays codec, resolution, FPS, bitrate. FPS reads from mpv properties `container-fps` → `estimated-vf-fps` → `video-params/fps` (first valid 0-120 value wins).

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
│   │       ├── playback.py            # On-demand HEVC NVENC transcode for playback
│   │       ├── thumbnail_capture.py   # Real-time RTSP thumbnail capture
│   │       ├── retention.py           # Age + storage-based retention cleanup
│   │       ├── motion_detector.py    # OpenCV frame-diff motion detection
│   │       └── object_detector.py    # YOLO AI person detection (GPU)
│   ├── requirements.txt
│   └── run.py                   # Uvicorn entry point (dev)
├── go2rtc/
│   ├── go2rtc.exe               # go2rtc binary (RTSP → MSE/WebSocket)
│   └── go2rtc.yaml              # go2rtc config (API port, streams registered dynamically)
├── app/                         # Flutter app (Windows + Android)
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
├── rebuild.bat                  # Full rebuild (Windows + Android)
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
- Pass structured fields via `extra={}` dicts — `ExtraAdder` promotes them to `key=value` output
- Root logger at INFO (third-party noise suppressed); `app.*` loggers at config.yaml level (DEBUG by default)
- httpx/httpcore silenced to WARNING (prevents binary fMP4 trace spam)
- ffmpeg stderr: banner logged once at INFO on startup, then only warning/error lines
- Log entry/exit, parameters, and outcomes for significant operations

## API Endpoints
- `GET/POST/PUT/DELETE /api/cameras` - Camera CRUD
- `GET /api/streams/{id}/live` - go2rtc stream info (stream_name, port) for WebSocket MSE URL construction
- `GET /api/streams/{id}/live.mp4?stream=&quality=` - HTTP fMP4 proxy for native app live view (proxies go2rtc stream.mp4)
- `GET /api/recordings/{id}/dates` - List dates with recordings
- `GET /api/recordings/{id}/segments?date=YYYY-MM-DD` - List segments for a date
- `POST /api/recordings/{id}/playback?start=ISO&quality=` - Start playback session. Direct = raw segment URL. High/Low/Ultra Low = NVENC transcode session URL.
- `GET /api/recordings/playback/{session_id}/playback.mp4` - Transcoded fragmented MP4 stream (streamed while ffmpeg writes)
- `GET /api/recordings/segment/{recording_id}` - Serve raw recording segment file (.ts)
- `GET /api/system/status` - Stream health, camera count
- `GET /api/system/storage` - Disk usage, per-camera recording stats
- `GET /api/system/logs?minutes=10` - Recent log lines (plain text, last N minutes)
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

## App UI Flow
- **Grid page**: Click camera → selects it (blue ring), shows timeline at bottom. Click same camera again → fullscreen.
- **Fullscreen page**: Full video player + timeline + speed controls (shown in playback mode). Click timeline segment → plays via media_kit (Direct = raw .ts, other qualities = NVENC transcode). Video stats bar (codec, resolution, FPS, bitrate) shown by default, togglable via icon button. Bug report button in header.
- **Speed controls**: -32x to 32x. 1x/2x/4x use native playback rate. 16x/32x use interval-based jumping. Reverse speeds request new playback sessions at earlier times.
- **Timeline**: CustomPainter-based timeline with scrub indicator showing time label. Scroll wheel zoom (Windows), pinch-to-zoom (Android). Mouse hover (Windows) or touch scrub (Android) shows white scrub line with time + trickplay thumbnail via OverlayEntry (floats above timeline, `gaplessPlayback: true` prevents flicker). Tap/release navigates to time and instantly moves red playhead — taps on future times are ignored. Playhead driven by actual player position via `getNvrTime` callback (reads `player.state.position`), so it stops when stream freezes/buffers/fails. 3-second hold after timeline taps prevents jumpback while player loads. LIVE button, date picker, zoomable timeline bar with blue segments. Mouse wheel zooms (1h-24h range), minimap shown when zoomed for pan navigation. Export clip mode, clips list with download/delete.
- **Motion events on timeline**: Color-coded bars above blue recording segments. Person=amber, Vehicle=indigo, Animal=emerald, Motion-only=gray. Visible in both main timeline and minimap. Polled alongside segments (every 15s for today's date).
- **Inline transitions**: Grid↔fullscreen transitions render inline (no Navigator.push) for faster view switching. No route transition animation delay.

## Key Dependencies
- **Backend**: fastapi, uvicorn, sqlalchemy, aiosqlite, pyyaml, structlog, httpx, opencv-python-headless, numpy, ultralytics (YOLOv8)
- **Service**: NSSM (installed via `winget install NSSM.NSSM`)
- **Live view**: go2rtc (Go binary, RTSP → HTTP fMP4, installed as Windows service)

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
12. AI object detection - YOLO11x GPU inference gated by motion pre-filter, per-camera category toggles (person/vehicle/animal) + confidence threshold, color-coded timeline (DONE)
