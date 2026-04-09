# RichIris - Custom NVR
update claude md as needed for code changes
## Quick Reference
- **Backend**: Runs as Windows service `RichIris` via NSSM (FastAPI on port 8700)
- **Restart**: `nssm restart RichIris`
- **go2rtc**: Always launched as its own child process on fixed ports (API 18700, RTSP 18554). Uses non-default ports to avoid conflicts with any user-installed go2rtc instance. Ports reported to Flutter app via `/api/system/status` → `go2rtc_rtsp_port`. Firewall rules added by installer.
- **Config**: `bootstrap.yaml` (data_dir + port only). All other settings in SQLite `settings` table, editable via GUI (Settings screen) or `GET/PUT /api/settings`.
- **Legacy**: `config.yaml` still supported — migrated to DB on first startup (one-time, idempotent).
- **Data directory**: `data_dir` (bootstrap.yaml) holds all data in structured subdirectories. Changeable from Settings screen (with optional move/copy migration). Requires service restart.
  ```
  {data_dir}/
  ├── database/richiris.db    # SQLite database
  ├── logs/                   # Application logs
  ├��─ recordings/{camera}/    # Recording .ts files per camera per day
  ├── thumbnails/{camera}/    # Trickplay + detection thumbnails per camera per day
  └── playback/               # Transient transcoded MP4s (auto-cleaned after 30s idle)
  ```
- **Recordings**: Always at `{data_dir}/recordings/`, stored per camera per day as `Camera 1 2026-03-08 13.30 - 13.45.ts`. Change data_dir to move everything.
- **Thumbnails**: Stored in `{data_dir}/thumbnails/{camera}/{date}/thumbs/` (separate from recordings). Detection thumbnails in `detection_thumbs/` subdir.
- **Database**: `{data_dir}/database/richiris.db` (SQLite, auto-created on first run). Auto-migrates from old `{data_dir}/richiris.db` location on startup.
- **Playback cache**: `{data_dir}/playback/` (transient transcoded MP4s, auto-cleaned after 30s idle).
- **App**: Flutter app in `app/` (Windows + Android)
- **App build (Windows)**: `cd app && flutter build windows --release` → `app/build/windows/x64/runner/Release/`
- **App build (Android)**: `cd app && flutter build apk --release` → `app/build/app/outputs/flutter-apk/`
- **Release build**: `build_release.bat` (PyInstaller backend + Flutter app + nssm; deps downloaded by installer at install time)
- **Dev setup**: `setup_dev.bat` (downloads all external dependencies into `dependencies/`)
- **Installer**: `ISCC.exe installer\richiris.iss` → `dist/RichIris-Setup-1.0.0.exe` (downloads deps at install time)
- **Release script**: `push_release.bat` (builds Windows installer + Android APK, creates GitHub release with Claude-generated changelog, gitignored)
- **API docs**: http://localhost:8700/docs
- **Binary resolution**: ffmpeg/ffprobe resolved in order: bundled `dependencies/` → system PATH → bare name fallback (no DB setting)

## Architecture
```
Camera RTSP (main) ← ffmpeg recording (direct, -c:v copy → .ts)
Camera RTSP (main) ← go2rtc (:18700/:18554) (keepalive) ──→ live view clients (RTSP via :18554)
                                               ──→ transcoded quality variants (lazy)

Camera RTSP (sub)  ← go2rtc (:18700/:18554) (keepalive) ──→ motion detection (snapshots)
                                               ──→ thumbnail capture (snapshots)
                                               ──→ live view clients (RTSP via :18554)
                                               ──→ transcoded quality variants (lazy)

Flutter App (Win/Android) → RTSP → go2rtc (:18554, port from /api/system/status)
                          → HTTP MP4 (playback) → FastAPI:8700 → FFmpeg remux
                                    |                    |
                          {data_dir}\database\       {data_dir}\recordings\
                          {data_dir}\thumbnails\     {data_dir}\playback\
```

- **Recording**: ffmpeg connects directly to cameras for maximum reliability (independent of go2rtc). One ffmpeg process per camera (always on). Uses `-c:v copy` (passthrough, no transcode, no GPU) → HEVC 4K .ts files. Segments renamed by scanner to `{Camera Name} {YYYY-MM-DD} {HH.MM} - {HH.MM}.ts`. Folders use camera name with spaces and capitals (e.g., `Camera 1/`).
- **go2rtc keepalives for instant live view**: StreamManager runs an in-process httpx HTTP fMP4 consumer per stream (s1_direct + s2_direct) that reads from go2rtc's HTTP API (`/api/stream.mp4`) and discards data. This keeps go2rtc's camera RTSP connections alive so live view loads instantly when a client connects. Keepalives start staggered (1s apart per camera) to avoid overwhelming go2rtc. Auto-reconnects on failure with 5s retry. Transcoded quality variants (high/low/ultralow) chain off the direct stream within go2rtc — no additional camera connections regardless of quality level. If no sub-stream URL is configured, s2_direct chains off s1_direct within go2rtc.
- **Recording reliability**: ffmpeg uses `-timeout` (30s socket I/O timeout) so it exits if a camera stops sending data. A watchdog task per camera checks every 2 minutes that `.ts` files are being modified; if no file has been updated in 5 minutes, it kills the stale ffmpeg process. Both mechanisms trigger the existing process monitor to auto-restart recording.
- **Live view (RTSP)**: Flutter app connects directly to go2rtc's RTSP output (`rtsp://{host}:8554/{stream_name}`) via media_kit (libmpv). No backend proxy needed for live view — the app constructs the RTSP URL client-side from the camera name and quality selection. This replaced the previous HTTP fMP4 proxy approach which caused choppy HEVC playback due to media_kit's texture rendering pipeline struggling with fMP4 container format. RTSP provides smooth playback for both HEVC and H.264 on Windows and Android. The HTTP fMP4 proxy endpoint (`GET /api/streams/{id}/live.mp4`) is retained as a fallback. Player uses 5s cache, 16MB demuxer buffer, TCP RTSP transport, hardware decoding. Error handler only reconnects on fatal errors (EOF, connection refused) — transient RTSP hiccups are handled by mpv internally. Stall detection reconnects if position doesn't advance for 10s. Exponential backoff retry (500ms→10s). Video controls hidden (app's own timeline handles playback controls).
- **Playback**: Recordings are HEVC .ts files. Direct = raw `.ts` served instantly (no ffmpeg). High/Low/Ultra Low = HEVC NVENC transcode via `PlaybackManager` into fragmented MP4 (`-movflags frag_keyframe+empty_moov`) streamed via `StreamingResponse`. ffmpeg handles seek (`-ss` before `-i`). Sessions auto-cleanup after 30s idle; same-camera eviction prevents ffmpeg accumulation.
- NVIDIA RTX 4080 SUPER available (used for AI object detection via RT-DETR + NVENC transcoding)
- **Motion detection + AI object detection**: Simplified snapshot-based pipeline. Fetches JPEG snapshots from go2rtc's HTTP frame API (`GET /api/frame.jpeg?src={stream}_s2_direct`) every ~1s using httpx (no cv2.VideoCapture — clean HTTP timeouts, no hung threads). go2rtc HTTP API on port 18700. Motion pre-filter uses running weighted-average baseline: `avg = alpha * frame + (1-alpha) * avg` (alpha=0.2 for first 25 frames, then 0.01). Diff against baseline → GaussianBlur(21,21) → absdiff → threshold(25). `changed_pct > 40%` triggers hard baseline reset (IR switch). Sensitivity 0-100 maps to area threshold: `(101 - sensitivity) * 0.05%`. When motion exceeds threshold and AI is enabled, frame goes to RT-DETR. Detections go through **multi-frame confirmation**: requires 2 detections in 3 consecutive frames AND positional movement (bbox center must shift by ≥1.5% of frame diagonal between frames). This filters single-frame ghost detections and static objects (statues, parked cars). Best-confidence frame from the confirmation window is used for the event thumbnail. Motion-only events (no AI) fire immediately without confirmation. Uses RT-DETR-L ONNX model (transformer-based, NMS-free) via `onnxruntime-directml` (GPU-accelerated on any GPU, no CUDA required, ~11ms inference). Falls back to CPU if no GPU available. Must be exported with opset 17 for DirectML performance. Min bounding box 0.2% of frame area. **Multi-category detection**: per-camera toggles for persons (COCO class 0), vehicles (bicycle/car/motorcycle/bus/truck — classes 1,2,3,5,7), and animals (bird/cat/dog/horse/sheep/cow/elephant/bear/zebra/giraffe — classes 14-23). `detection_label` stores the specific COCO class name (e.g., "car", "dog"). Events stored as MotionEvent rows (start_time, end_time, peak_intensity, detection_label, detection_confidence). 10-second cooldown between events. Per-camera fields: `motion_sensitivity` (0=off), `ai_detection` (master bool), `ai_detect_persons`/`ai_detect_vehicles`/`ai_detect_animals` (category bools), `ai_confidence_threshold` (0-100), `motion_scripts` (JSON array of script pairs with per-category triggers). **Multiple script pairs**: each entry has `on`/`off` scripts and category booleans (`persons`, `vehicles`, `animals`, `motion_only`). A script only fires if its category matches the detection — e.g., a script with only `persons: true` won't fire for vehicle detections. Legacy `motion_script`/`motion_script_off` fields auto-migrated to `motion_scripts` on startup. Env vars: MOTION_CAMERA, MOTION_TIME, MOTION_INTENSITY, DETECTION_LABEL, DETECTION_CONFIDENCE. Script must use full path to python.exe (not `python3`) since NSSM service PATH differs from user PATH. Changes take effect immediately via `detector.update_camera()`. Heartbeat log every 5 min per camera.

### Video Quality Selection
- **Two independent selectors** in header (also in fullscreen view): **Stream** (Main/Sub) and **Quality** (Direct/High/Low/Ultra Low)
  - Stream selector only shown during live view (not during playback — playback uses .ts files, not RTSP)
  - Stream persisted in SharedPreferences key `richiris-stream-source`, live quality in `richiris-quality`, playback quality in `richiris-playback-quality`
  - Separate quality preferences for live vs playback — switching modes restores the saved preference for that mode
  - Changing quality during playback triggers `didUpdateWidget` → restarts playback at current position with new quality
  - All quality tiers available on both Windows and Android (Direct, High, Low, Ultra Low).
- **Bitrate probing**: At startup, backend probes each camera's actual bitrate (5s ffmpeg sample) and codec (ffprobe). Playback also probes .ts file bitrate+codec via ffprobe before transcoding.
- **Live streams**: go2rtc registers streams per camera, all baked into go2rtc.yaml at startup (no API registration needed — survives config reloads). Direct streams connect to the camera RTSP URL; transcoded variants chain off the direct stream within go2rtc (no additional camera connections). Unused transcoded streams are lazy — zero resources until a client connects. **High quality aliases to direct for HEVC sources** (re-encoding HEVC→HEVC at the same bitrate wastes GPU for no benefit). For non-HEVC sources (e.g. H.264 Reolink), high re-encodes to HEVC at source bitrate.
  - Main Direct: raw 4K HEVC passthrough (always connected — httpx keepalive consumer)
  - Main High: alias to direct for HEVC sources; HEVC re-encode at source bitrate for non-HEVC sources
  - Main Low: HEVC re-encode from s1_direct, 1/8 of source bitrate
  - Main Ultra Low: HEVC re-encode from s1_direct, 1/16 of source bitrate, 15fps, short GOP (30 frames)
  - Sub Direct: raw sub-stream passthrough (always connected — httpx keepalive consumer)
  - Sub High: alias to direct for HEVC sources; HEVC re-encode at source bitrate for non-HEVC sources
  - Sub Low: HEVC re-encode from s2_direct, 1/8 of source bitrate
  - Sub Ultra Low: HEVC re-encode from s2_direct, 1/16 of source bitrate, 15fps, short GOP
- **Playback**: Quality tiers work for both live and playback. Direct = raw `.ts` file served instantly (no ffmpeg). High/Low/Ultra Low = HEVC NVENC transcode via `PlaybackManager` into fragmented MP4 (`-movflags frag_keyframe+empty_moov`) streamed via `StreamingResponse`. ffmpeg applies seek (`-ss` before `-i`), so client gets `seek_seconds: 0`. Sessions auto-cleanup after 30s idle; same-camera eviction prevents ffmpeg accumulation.
  - High: source bitrate (probed from .ts file via ffprobe)
  - Low: 1/8 of source bitrate
  - Ultra Low: 1/16 of source bitrate, 15fps, short GOP (`-g 30`)
- Backend `streams.py` retains HTTP fMP4 proxy (`?stream=s1/s2&quality=direct/high/low/ultralow`) as fallback; also provides `GET /api/streams/{id}/rtsp-info` for RTSP URL lookup
- No pre-warming needed — httpx keepalive consumers (one per stream, s1_direct + s2_direct) keep go2rtc RTSP connections alive permanently via HTTP fMP4 streaming. Snapshot-based consumers (motion/thumbnails) don't count as persistent consumers in go2rtc.
- **LivePlayer**: 5s cache, 16MB demuxer buffer, RTSP/TCP transport, hardware decoding, 30s network timeout, exponential backoff retry (500ms→10s). Video controls hidden (NoVideoControls). Only fatal errors trigger reconnect; transient hiccups handled by mpv internally.
- **Video stats bar**: Shown by default in fullscreen view (togglable). Displays codec, resolution, FPS, bitrate. FPS reads from mpv properties `container-fps` → `estimated-vf-fps` → `video-params/fps` (first valid 0-120 value wins).

### Important: Timezone handling
- Recordings are stored in the DB as **local time without timezone** (e.g. `2026-03-08T10:36:02`)
- Timezone is configurable via Settings → General (dropdown, auto-detected from system on first install via tzlocal, falls back to UTC)
- Frontend must NOT use `.toISOString()` for playback times (converts to UTC, breaks queries)
- Always format times as local ISO strings to match DB format

## Project Structure
```
RichIris/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app + lifespan
│   │   ├── config.py            # Bootstrap config + DB settings loader
│   │   ├── logging_config.py    # structlog setup
│   │   ├── database.py          # SQLAlchemy async engine
│   │   ├── models.py            # Camera, Recording, ClipExport
│   │   ├── schemas.py           # Pydantic schemas
│   │   ├── routers/
│   │   │   ├── backup.py        # Backup/restore /api/backup (create, inspect, restore, progress)
│   │   │   ├── cameras.py       # CRUD /api/cameras
│   │   │   ├── clips.py         # Clip export /api/clips (no duration limit)
│   │   │   ├── recordings.py    # Playback /api/recordings + transcode sessions
│   │   │   ├── settings.py      # GET/PUT /api/settings (system config)
│   │   │   ├── storage.py       # Storage migration /api/storage (validate, migrate, finalize)
│   │   │   ├── streams.py       # go2rtc stream info /api/streams/{id}/live
│   │   │   ├── system.py        # /api/system/status + storage + retention + update check
│   │   │   └── motion.py        # /api/motion events
│   │   └── services/
│   │       ├── backup.py              # Backup/restore archive creation + extraction
│   │       ├── ffmpeg.py              # Command builder (recording only)
│   │       ├── stream_manager.py      # FFmpeg recording process lifecycle + go2rtc registration
│   │       ├── go2rtc_client.py       # REST client for go2rtc stream management
│   │       ├── go2rtc_manager.py      # go2rtc lifecycle (start/stop as child process)
│   │       ├── recorder.py            # Segment scanner + DB registration
│   │       ├── clip_exporter.py       # Clip export (concat segments → MP4)
│   │       ├── playback.py            # On-demand HEVC NVENC transcode for playback
│   │       ├── settings.py            # DB-backed settings CRUD + defaults + seeding
│   │       ├── thumbnail_capture.py   # Real-time RTSP thumbnail capture
│   │       ├── retention.py           # Age + storage-based retention cleanup
│   │       ├── storage_migration.py  # Recordings dir migration (validate, copy/move, progress)
│   │       ├── motion_detector.py    # OpenCV frame-diff motion detection
│   │       ├── object_detector.py    # YOLO AI person detection (GPU)
│   │       └── update_checker.py    # Periodic GitHub release checker + app launcher
│   ├── requirements.txt
│   └── run.py                   # Uvicorn entry point (dev)
├── go2rtc/
│   └── go2rtc.yaml              # go2rtc config (generated at startup, all streams baked in)
├── app/                         # Flutter app (Windows + Android)
│   ├── lib/
│   │   ├── main.dart            # Entry point, MediaKit init
│   │   ├── app.dart             # MaterialApp, navigation, state management
│   │   ├── theme.dart           # Dark theme
│   │   ├── config/              # API config, quality tiers, constants
│   │   ├── models/              # Data classes (Camera, RecordingSegment, etc.)
│   │   ├── services/            # API layer (Dio HTTP client) + settings_api.dart + backup_api.dart + update_service.dart
│   │   ├── screens/             # Home, Fullscreen, System, Settings (unified), CameraForm
│   │   ├── widgets/             # CameraGrid, CameraCard, LivePlayer, QualitySelector, DateTimePickerDialog, ExportClipWizardDialog, StorageMigrationDialog, BackupRestoreDialog, UpdateDialog, VersionInfoDialog
│   │   │   └── timeline/        # CustomPainter timeline (controller, painter, minimap)
│   │   └── utils/               # Time/format utilities
│   └── pubspec.yaml             # Dependencies: media_kit, dio, shared_preferences
├── bootstrap.yaml               # Minimal config (data_dir + port)
├── config.yaml                  # Legacy config (migrated to DB on first run)
├── build_release.bat            # Full release build (PyInstaller + Flutter + deps)
├── rebuild.bat                  # Quick rebuild (Windows + Android dev)
├── start.bat                    # Quick launcher (dev)
├── service-install.bat          # Install Windows service
├── service-uninstall.bat
├── service-restart.bat
├── dependencies/                # gitignored: all external binaries
│   ├── ffmpeg.exe               # FFmpeg (recording, transcode)
│   ├── ffprobe.exe              # FFprobe (codec/bitrate probing)
│   ├── nssm.exe                 # Windows service manager
│   ├── go2rtc/go2rtc.exe        # RTSP → HTTP fMP4
│   └── models/rtdetr-l.onnx      # RT-DETR-L ONNX model (126 MB)
├── setup_dev.bat                # One-command dev setup (downloads deps + installs packages)
├── installer/
│   ├── richiris.iss             # Inno Setup installer script (full + slim modes)
│   └── download_deps.ps1       # Slim installer dependency downloader (PowerShell)
└── data/                        # gitignored: DB + playback cache + logs
```

## Code Style
- Small focused functions (~10-20 lines max)
- Verbose structured logging via `structlog` (JSON in prod, console in dev)
- Each module gets `logger = logging.getLogger(__name__)`
- Pass structured fields via `extra={}` dicts — `ExtraAdder` promotes them to `key=value` output
- Root logger at INFO (third-party noise suppressed); `app.*` loggers at configured level (DEBUG by default)
- httpx/httpcore silenced to WARNING (prevents binary fMP4 trace spam)
- ffmpeg stderr: banner logged once at INFO on startup, then only warning/error lines
- Log entry/exit, parameters, and outcomes for significant operations

## API Endpoints
- `POST /api/cameras/discover` - Probe common RTSP URL patterns on camera IP `{ip, username?, password?, port?}`, returns list of working streams with brand/codec/resolution
- `GET /api/backup/preview` - Size estimates per backup component
- `POST /api/backup/create` - Start backup `{components: [...], target_path}`, returns backup_id
- `GET /api/backup/{id}/progress` - Poll backup progress (files/bytes done, status, current_file)
- `POST /api/backup/{id}/cancel` - Cancel in-progress backup
- `POST /api/backup/inspect` - Inspect .richiris file `{file_path}`, returns manifest with available components
- `POST /api/backup/restore` - Start restore `{file_path, components: [...]}`, stops services if needed
- `GET /api/backup/restore/{id}/progress` - Poll restore progress (auto-restarts services on completion)
- `POST /api/backup/restore/{id}/cancel` - Cancel in-progress restore
- `GET/POST/PUT/DELETE /api/cameras` - Camera CRUD
- `GET /api/streams/{id}/live` - go2rtc stream info (stream_name, port) for WebSocket MSE URL construction
- `GET /api/streams/{id}/live.mp4?stream=&quality=` - HTTP fMP4 proxy fallback (proxies go2rtc stream.mp4)
- `GET /api/streams/{id}/rtsp-info?stream=&quality=` - Returns go2rtc RTSP URL for a camera stream
- `GET /api/recordings/{id}/dates` - List dates with recordings
- `GET /api/recordings/{id}/segments?date=YYYY-MM-DD` - List segments for a date
- `POST /api/recordings/{id}/playback?start=ISO&quality=&direction=forward|backward` - Start playback session. Direct = raw segment URL. High/Low/Ultra Low = NVENC transcode session URL. `direction` controls fallback when no segment contains `start`: `forward` (default) finds next segment after start, `backward` finds latest segment ending before start (used by reverse playback).
- `GET /api/recordings/playback/{session_id}/playback.mp4` - Transcoded fragmented MP4 stream (streamed while ffmpeg writes)
- `GET /api/recordings/segment/{recording_id}` - Serve raw recording segment file (.ts)
- `GET /api/system/status` - Stream health, camera count
- `GET /api/system/storage` - Disk usage, per-camera recording stats
- `GET /api/system/logs?minutes=10` - Recent log lines (plain text, last N minutes)
- `POST /api/system/retention/run` - Manually trigger retention cleanup
- `GET /api/system/data-dir` - Current data directory path, size, free space, subdirectory info
- `POST /api/system/data-dir/validate` - Validate target path for data directory migration
- `POST /api/system/data-dir` - Change data directory `{path, mode: "move"|"copy"|"path_only"}`, returns restart_required
- `GET /api/settings` - All system settings grouped by category (with requires_restart flags)
- `PUT /api/settings` - Update settings `{settings: {key: value}}`, returns restart_required flag
- `GET /api/health` - Health check
- `GET /api/system/version` - Current backend version
- `GET /api/system/update` - Cached latest release info (from periodic GitHub check)
- `POST /api/system/update/check` - Force immediate GitHub release check
- `POST /api/clips` - Create clip export (camera_id, start_time, end_time, no duration limit)
- `GET /api/clips` - List clips (optional ?camera_id=)
- `GET /api/clips/{id}` - Get clip status
- `GET /api/clips/{id}/download` - Download completed clip MP4
- `DELETE /api/clips/{id}` - Delete clip and file
- `GET /api/recordings/{id}/thumbnails?date=YYYY-MM-DD` - Thumbnail metadata for a date (individual JPEGs)
- `GET /api/recordings/{id}/thumb/{date}/{filename}` - Serve individual thumbnail JPEG (cached 24h)
- `GET /api/motion/{id}/events?date=YYYY-MM-DD` - Motion events for a camera on a date
- `POST /api/storage/validate` - Validate target path for recordings migration (writable, free space, source size)
- `POST /api/storage/migrate` - Start recordings migration (stops streams, copies/moves files). Body: `{target_path, mode: "move"|"copy"}`
- `GET /api/storage/migrate/{id}/progress` - Poll migration progress (files/bytes done, status, current_file)
- `POST /api/storage/migrate/{id}/cancel` - Cancel in-progress migration
- `POST /api/storage/migrate/{id}/finalize` - Finalize migration (update settings, restart streams)
- `POST /api/storage/update-path` - Change recordings path without migrating files

## App UI Flow
- **Grid page**: Click camera → selects it (blue ring), shows timeline at bottom. Click same camera again → fullscreen.
- **Fullscreen page**: Full video player + timeline + speed controls (shown in playback mode). Click timeline segment → plays via media_kit (Direct = raw .ts, other qualities = NVENC transcode). Video stats bar (codec, resolution, FPS, bitrate) shown by default, togglable via icon button. Bug report button in header.
- **Speed controls**: -4x to 32x. 1x/2x/4x use native playback rate. 16x/32x use interval-based jumping. Reverse speeds (-1x/-2x/-4x) pause the player and use interval-based backward seeking; when reaching segment start, loads the previous segment from its beginning (two-step API call: find previous segment, then request full segment with seek_seconds=0) to get the full fMP4 file for stepping through. Transient 1000ms fMP4 duration placeholder is ignored — waits for real duration before seeking.
- **Timeline**: CustomPainter-based timeline with scrub indicator showing time label. Scroll wheel zoom (Windows), pinch-to-zoom (Android). Mouse hover (Windows) or touch scrub (Android) shows white scrub line with time + trickplay thumbnail via OverlayEntry (floats above timeline, `gaplessPlayback: true` prevents flicker). Tap/release navigates to time and instantly moves red playhead — taps on future times are ignored. Playhead driven by actual player position via `getNvrTime` callback (reads `player.state.position`), so it stops when stream freezes/buffers/fails. 3-second hold after timeline taps prevents jumpback while player loads. LIVE button, date picker, zoomable timeline bar with blue segments. Mouse wheel zooms (1h-24h range), minimap shown when zoomed for pan navigation. Export clip panel with two modes: "Timeline" (tap start/end on timeline) or "Wizard" (dialog with camera dropdown + date/time pickers for any camera/date range). Clips list with status polling, download, and delete.
- **Motion events on timeline**: Color-coded bars above blue recording segments. Person=amber, Vehicle=indigo, Animal=emerald, Motion-only=gray. Visible in both main timeline and minimap. Polled alongside segments (every 15s for today's date).
- **Inline transitions**: Grid↔fullscreen transitions render inline (no Navigator.push) for faster view switching. No route transition animation delay.

## Key Dependencies
- **Backend**: fastapi, uvicorn, sqlalchemy, aiosqlite, pyyaml, structlog, httpx, opencv-python-headless, numpy, onnxruntime-directml (RT-DETR-L ONNX inference)
- **Service**: NSSM (installed via `winget install NSSM.NSSM`)
- **Live view**: go2rtc (Go binary, RTSP relay on :18554, API on :18700, always launched as child process of backend)
- **Packaging**: PyInstaller (backend → standalone exe), Inno Setup (installer)
- **All external binaries in `dependencies/`**: ffmpeg.exe, ffprobe.exe, nssm.exe, go2rtc/go2rtc.exe, models/yolo11x.onnx (gitignored, ~388 MB total). `build_release.bat` copies from here — no downloads during build.

## Build & Distribution

### Release build (`build_release.bat`)
Builds the backend (PyInstaller) + Flutter Windows app + assembles into `dist/richiris/`. Only nssm.exe is bundled — all other dependencies (ffmpeg, go2rtc, RT-DETR model) are downloaded by the installer at install time.

Steps: verify nssm → PyInstaller backend → Flutter Windows build → assemble `dist/richiris/` → verify.

### Installer (`installer/richiris.iss`)
Single Inno Setup script: `ISCC.exe installer\richiris.iss` → `dist/RichIris-Setup-1.0.0.exe`

Installer wizard flow:
1. Install directory picker (default: `C:\Program Files\RichIris`)
2. **Data directory picker** — custom wizard page for choosing where recordings, database, logs, thumbnails live. Default: `C:\ProgramData\RichIris`. Pre-populated from existing `bootstrap.yaml` on upgrades.
3. Post-install:
   - Creates data subdirectories (`database/`, `logs/`, `recordings/`, `thumbnails/`, `playback/`)
   - Writes `bootstrap.yaml` with chosen data_dir and port 8700
   - Sets NSSM stdout/stderr log paths to `{data_dir}/logs/`
   - Runs `download_deps.ps1` via PowerShell to download ffmpeg, go2rtc, and RT-DETR model. Skips already-present files (upgrade scenario). Shows error dialog if downloads fail but continues.
   - Installs + starts Windows service `RichIris` via NSSM
   - Optionally launches the Flutter app

### Dependency downloader (`installer/download_deps.ps1`)
PowerShell script run by the installer to download:
- ffmpeg + ffprobe from gyan.dev GitHub release
- go2rtc from AlexxIT GitHub release
- YOLO11x ONNX model from RichIris GitHub release

Files go to `{install_dir}/dependencies/`. Script is deleted after install. On upgrade, existing deps are kept (skip logic).

### Release script (gitignored)
`push_release.bat` — builds Windows installer + Android APK, generates changelog via Claude CLI, creates a single GitHub release with both assets. Auto-incrementing version tags (v0.0.1, v0.0.2, ...).

### Dev setup (`setup_dev.bat`)
One-command dev environment: downloads all external dependencies into `dependencies/`, installs Python packages. See DEV-GUIDE.md for details.

## Cameras
6 cameras configured in config.yaml on 192.168.8.42-47. Streams are RTSP, codec is HEVC (H.265) at 4K (3840x2160). Sub-streams are HEVC (H.265) for HTMS cameras (42,44,45,46,47) and H.264 for Reolink (43).

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
13. Distribution readiness - DB-backed settings (replaced config.yaml), bootstrap.yaml, GUI system settings, dependency bundling, go2rtc child process management, PyInstaller packaging, Inno Setup installer (DONE)
14. Configurable storage location - Installer data dir picker, runtime recordings dir migration (move/copy/path-only) via Settings screen, playback cache path fix (DONE)
15. Data directory restructure - Structured `{data_dir}/` with database/, logs/, recordings/, thumbnails/, playback/ subdirs. Thumbnails separated from recordings. DB in own subdir with auto-migration. Data dir changeable from System Settings with move/copy/path-only migration. Installer always writes bootstrap.yaml with user's chosen data dir. (DONE)
16. Settings simplification + installer - Removed auto-resolved settings from UI (ffmpeg paths, go2rtc host/port, recordings dir). Single data_dir controls all storage. Timezone moved to General section as dropdown (default UTC). Removed JSON log output toggle. Trickplay pane simplified to enable toggle only. Deprecated DB keys auto-cleaned on startup. Single installer that downloads deps at install time. `setup_dev.bat` for one-command dev setup. Release scripts with Claude-generated changelogs. (DONE)
17. Backup & Restore - Full data backup/restore via Settings screen (Windows only). Users select components (settings, cameras, database, recordings, thumbnails) with size previews. Creates `.richiris` ZIP archive (ZIP64, no compression for video). Restore merges recordings/thumbnails (existing files kept), upserts cameras by name, overwrites settings/DB. Progress tracking with cancel support. Services auto-stop/restart during restore. (DONE)
18. Auto-Update - Backend periodically checks GitHub releases API (every 6h), caches latest release info. Flutter app reads cached info on startup, shows changelog + update dialog with Skip/Remind/Update options. Version shown in app bar (tappable for manual check). Backend launches Flutter app with `--update-only` flag if update found and app not running (minimal mode, no streams). Windows: downloads installer, runs `/VERYSILENT`. Android: downloads APK, opens system installer. `push_release.bat` syncs version to pubspec.yaml + main.py. (DONE)
