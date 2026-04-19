# RichIris - Custom NVR
update claude md as needed for code changes
## Quick Reference
- **Backend**: Windows service `RichIris` via NSSM (FastAPI on port 8700). Restart: `nssm restart RichIris`
- **Health watchdog (2 layers)**:
  1. **In-process self-watchdog** (`backend/app/services/self_watchdog.py`): probes `127.0.0.1:{port}/api/health` every 30s (60s startup grace, 5s timeout). 3 consecutive failures → `os._exit(1)` → NSSM restarts (AppExit=Restart, throttle 1500ms). Catches the silent-listener-death failure mode where uvicorn's socket dies but the asyncio loop keeps running.
  2. **External scheduled-task watchdog** (`scripts/watchdog.ps1`, Scheduled Task runs as SYSTEM every 60s via `pwsh.exe`): probes health; on failure calls `Restart-Service RichIris` and optionally pushes an ntfy notification. Fallback for full event-loop deadlocks the in-process watchdog can't catch. Logs to `{data_dir}/logs/watchdog.log`. **Requires PowerShell 7 (pwsh) — PS 5.1 parser chokes on the script.** User-specific ntfy URL + credentials live in `scripts/watchdog.config.psd1` (gitignored; template: `scripts/watchdog.config.psd1.template`).
- **go2rtc**: Child process on fixed ports (API 18700, RTSP 18554). Ports reported via `/api/system/status` → `go2rtc_rtsp_port`.
- **Config**: `bootstrap.yaml` (data_dir + port only). All other settings in SQLite `settings` table via GUI or `GET/PUT /api/settings`. Legacy `config.yaml` migrated to DB on first startup.
- **Data directory** (`data_dir` from bootstrap.yaml):
  ```
  {data_dir}/
  ├── database/richiris.db    # SQLite (auto-migrates from old location)
  ├── logs/                   # Application logs
  ├── recordings/{camera}/    # .ts files per camera per day: "Camera 1 2026-03-08 13.30 - 13.45.ts"
  ├── thumbnails/{camera}/    # Trickplay in thumbs/, detection in detection_thumbs/
  └── playback/               # Transient transcoded MP4s (auto-cleaned 30s idle)
  ```
- **Builds**: `cd app && flutter build windows --release` | `flutter build apk --release`
- **Release build**: `build_release.bat` (PyInstaller + Flutter + nssm)
- **Installers**: Full: `ISCC.exe installer\richiris.iss` | Client-only: `installer\richiris_client.iss` (Flutter app + VC redist only, ships `client_only.txt` marker). Update detection keys on `"Client-Only"` substring in asset filename.
- **Release script**: `push_release.bat` (gitignored) — builds both installers + APK, GitHub release with changelog
- **Dev setup**: `setup_dev.bat` | **API docs**: http://localhost:8700/docs
- **Binary resolution**: bundled `dependencies/` → system PATH → bare name fallback

## Architecture
```
Camera RTSP (main) ← ffmpeg recording (-c:v copy → .ts, independent of go2rtc)
Camera RTSP (main) ← go2rtc ← httpx keepalive (s1_direct) → live view clients (RTSP :18554)
Camera RTSP (sub)  ← go2rtc ← FrameBroker (persistent ffmpeg, MJPEG @2fps) → motion + thumbnails
Flutter App → RTSP → go2rtc :18554 (live) | HTTP MP4 → FastAPI:8700 → FFmpeg (playback)
```

- **Recording**: One ffmpeg per camera, `-c:v copy` passthrough → HEVC 4K .ts files. Watchdog kills stale processes (no file update in 5min). `-timeout 30s` socket timeout.
- **go2rtc keepalives**: StreamManager runs httpx fMP4 consumer per camera (s1_direct). Sub-stream kept warm by FrameBroker. Keepalives staggered 1s apart, auto-reconnect 5s retry.
- **FrameBroker** (`services/frame_broker.py`): Persistent ffmpeg per camera pulling MJPEG from go2rtc s2_direct at 2fps. Parsed via JPEG SOI/EOI → numpy frames. `get_latest()` (instant) or `get_fresh(max_wait)`. Auto-restarts with 3s backoff. Starts before motion + thumbnails in lifespan.
- **Live view**: Flutter connects to go2rtc RTSP via media_kit (libmpv). 5s cache, 16MB demuxer, TCP transport, hw decoding. Stall detection (10s), exponential backoff (500ms→10s). HTTP fMP4 proxy retained as fallback.
- **Playback**: All qualities go through PlaybackManager → fMP4 (`-movflags frag_keyframe+empty_moov`). Direct = `ffmpeg -c copy` remux with `-noaccurate_seek -ss N` pre-seek (server-side seek shifts work off libmpv — first frame faster than letting it scan a raw .ts). High/Low/Ultra Low = HEVC NVENC transcode. Streaming endpoint supports HTTP Range on completed files (libmpv ranged reads); growing files served as plain `200 OK` stream because we can't promise an end byte. **No client-side `player.seek` after open** — fMP4's PTS=0 already corresponds to the user's chosen time. `seek_seconds` in the response is metadata-only (timeline display alignment). Sessions auto-cleanup 30s idle, same-camera eviction.
- **GPU**: Any NVIDIA card for NVENC transcoding + RT-DETR acceleration (DirectML also works on AMD/Intel; CPU fallback available but slow).

### Motion + AI Detection
Snapshot pipeline reading FrameBroker every 0.5s. Motion: running weighted-avg baseline, GaussianBlur(21,21), threshold(25). Sensitivity 0-100 → area threshold `(101-s)*0.05%`. AI: RT-DETR-L ONNX via `onnxruntime-directml` (~11ms GPU inference, CPU fallback). Multi-frame confirmation: 2 detections in 3 frames + bbox movement ≥1.5% diagonal. Categories: persons (COCO 0), vehicles (1,2,3,5,7), animals (14-23). Per-camera toggles + confidence threshold. `motion_scripts` JSON array with per-category triggers. Events: MotionEvent rows, 10s cooldown. Env vars: MOTION_CAMERA, MOTION_TIME, MOTION_INTENSITY, DETECTION_LABEL, DETECTION_CONFIDENCE, FACE_NAMES. Scripts need full python.exe path (NSSM PATH differs).

### Detection Zones
Per-camera polygon masks that scripts can opt into via `zone_ids: [int,...]` on a `MotionScriptConfig`. Empty `zone_ids` = whole frame (unchanged behavior) — one script can be zone-restricted while another on the same camera is not. Points stored normalized [0,1] in the `zones` table so they survive sub-stream resolution changes. Rasterized masks are cached per (zone_id, frame_shape) by `services/zone_mask.py`; union masks for multi-zone scripts cached by sorted tuple. CRUD invalidates the cache. **Filter point**: applied inside `_on_motion` after category + face filters build `firing`; motion-only scripts use `motion_in_mask` (thresh ∩ zone ≥ sensitivity_pct), detection scripts use `bbox_in_mask` on the bbox's bottom-center pixel (ground anchor — feet/wheels). If a zone-restricted script's union mask is unavailable (deleted zone), the script fails closed. Zone deletion prunes any `zone_ids` references from the owning camera's scripts. Flutter editor: tap to add vertex, drag to move, long-press to remove; snapshot via `POST /api/cameras/snapshot`. `CameraResponse` exposes `zone_count` (aggregated in one COUNT query on list) so the grid can show a badge without fetching each camera's zones.

**Do not use `name` as a logger `extra={}` key** — it collides with LogRecord's reserved attribute and raises `KeyError: "Attempt to overwrite 'name' in LogRecord"`. Use `zone_name`, `camera_name`, etc.

### Facial Recognition
Runs only when RT-DETR confirms a `person`. Pipeline: SCRFD (`dependencies/models/det_10g.onnx`, from InsightFace buffalo_l) detects faces within the cropped person bbox → ArcFace (`w600k_r50.onnx`, 512-D) embeddings → cosine match against in-memory cache from `face_embeddings` table. Match ≥ per-camera threshold (default 0.5) → writes `face_matches` JSON on MotionEvent; else sets `face_unknown=true`. Models run on the same `onnxruntime-directml` pipeline as RT-DETR. Per-camera toggles: `face_recognition`, `face_match_threshold`. Per-script filters: `faces: [id,...]` (AND trigger) and `face_unknown: bool`. Enrollment: tag faces from past person-detection thumbnails via `POST /api/faces/{id}/embeddings` — multi-face images return `multiple_faces` with candidate bboxes so the UI can disambiguate. `reload_cache()` is called on any embedding mutation so the matcher stays fresh. Timeline tints person events cyan (known) or rose (unknown).

### Video Quality
Two selectors: **Stream** (Main/Sub, live only) and **Quality** (Direct/High/Low/Ultra Low). Separate prefs for live vs playback. Bitrate probed at startup (5s ffmpeg sample + ffprobe codec).

Live streams baked into go2rtc.yaml at startup. High aliases to direct for HEVC sources (no point re-encoding same codec/bitrate). Non-HEVC sources get HEVC re-encode. Low = 1/8 bitrate, Ultra Low = 1/16 bitrate + 15fps + short GOP.

Playback transcoding: same tiers, probed from .ts file. Direct = `-c copy` remux with `-noaccurate_seek -ss` (lands on nearest keyframe; can leave seeks ±GOP-duration off the requested time). Others = NVENC transcode with `-ss` seek.

### Timezone
Recordings stored as **local time without timezone**. Configurable via Settings → General. Frontend must NOT use `.toISOString()` (converts to UTC). Always format as local ISO strings.

## Project Structure
```
RichIris/
├── backend/app/
│   ├── main.py, config.py, logging_config.py, database.py, models.py, schemas.py
│   ├── routers/  (backup, cameras, clips, groups, recordings, settings, storage, streams, system, motion, zones)
│   └── services/ (backup, ffmpeg, stream_manager, go2rtc_client, go2rtc_manager, recorder,
│                   clip_exporter, playback, settings, thumbnail_capture, retention,
│                   storage_migration, motion_detector, object_detector, update_checker,
│                   frame_broker, benchmark, zone_mask)
├── app/lib/      # Flutter (main, app, theme, config/, models/, services/, screens/, widgets/, utils/)
├── installer/    (richiris.iss, richiris_client.iss, download_deps.ps1)
├── dependencies/ # gitignored: ffmpeg, ffprobe, nssm, go2rtc, models/rtdetr-l.onnx (~388MB)
└── data/         # gitignored: DB + playback cache + logs
```

## Code Style
- Small focused functions (~10-20 lines). Structured logging via `structlog` (`logger = logging.getLogger(__name__)`).
- Pass structured fields via `extra={}` dicts. Root logger INFO; `app.*` at configured level (DEBUG default).
- httpx/httpcore silenced to WARNING. ffmpeg banner logged once, then warnings/errors only.

## API Endpoints
**Cameras**: `GET/POST/PUT/DELETE /api/cameras` (CRUD, `?purge_data=true` deletes files) | `PUT reorder` (body: `{order: [id,...]}`) | `POST discover` (probe RTSP patterns) | `POST scan` (LAN scan port 554) | `POST discover_batch` (parallel probe) | `POST snapshot` (single JPEG from RTSP URL) | `POST test-script` (run script command, returns exit_code/stdout/stderr)
**Groups**: `GET/POST /api/groups` | `PUT/DELETE /api/groups/{id}` | `POST /api/groups/{id}/bulk` (body: `{action: "enable"|"disable"|"arm_motion"|"disarm_motion"}`)
**Faces**: `GET /api/faces` | `POST /api/faces` `{name, notes?}` | `PUT/DELETE /api/faces/{id}` | `GET /api/faces/{id}/embeddings` | `POST /api/faces/{id}/embeddings` `{source_thumbnail_path, bbox?}` (returns `enrolled` / `multiple_faces` / `no_face`) | `DELETE /api/faces/embeddings/{id}` | `GET /api/faces/thumbnails/unlabeled?date=&camera_id=&limit=` | `GET /api/faces/thumbnails/event/{event_id}/path` | `GET /api/faces/embeddings/{id}/crop` | `GET /api/faces/{id}/latest-crop`
**Streams**: `GET /api/streams/{id}/live` (go2rtc info) | `GET .../live.mp4` (fMP4 proxy) | `GET .../rtsp-info` (RTSP URL)
**Recordings**: `GET .../dates` | `GET .../segments?date=` | `POST .../playback?start=&quality=&direction=` (optional `X-Bench-Id` header for end-to-end timing trace) | `GET .../playback/{session}/playback.mp4` (HTTP Range support on completed files; growing files served as plain stream) | `GET .../segment/{id}` (raw .ts) | `GET .../thumbnails?date=` | `GET .../thumb/{date}/{file}`
**System**: `GET status` | `GET storage` | `GET logs?minutes=` | `POST client-event` | `POST retention/run` | `GET/POST data-dir` | `POST data-dir/validate` | `GET version` | `GET/POST update`
**Settings**: `GET/PUT /api/settings`
**Backup**: `GET preview` | `POST create` | `GET {id}/progress` | `POST {id}/cancel` | `POST inspect` | `POST restore` | `GET restore/{id}/progress` | `POST restore/{id}/cancel`
**Clips**: `POST /api/clips` | `GET list` | `GET {id}` | `GET {id}/download` | `DELETE {id}`
**Motion**: `GET /api/motion/{id}/events?date=`
**Zones**: `GET/POST /api/cameras/{id}/zones` | `PUT/DELETE /api/cameras/{id}/zones/{zone_id}` — body: `{name, points: [[x,y],...]}` with x,y in [0,1]. Scripts reference via `zone_ids` in `MotionScriptConfig`.
**Storage**: `POST validate` | `POST migrate` | `GET migrate/{id}/progress` | `POST migrate/{id}/cancel` | `POST migrate/{id}/finalize` | `POST update-path`
**Health**: `GET /api/health` (returns `{app: "richiris", version}`)

## App UI Flow
- **Grid**: Click camera → select (blue ring + drag hint icon) + timeline. Click again → fullscreen. Long-press drag to reorder. Group chip bar above grid filters by camera group. Inline transitions (no Navigator.push). Feature badges under each card's gear icon summarize enabled detection features (motion/AI/face/zones/scripts) via `_FeatureBadges` in `widgets/camera_card.dart`.
- **Fullscreen**: Video + timeline + speed controls (-4x to 32x). Stats bar (codec/res/FPS/bitrate). Refresh + bug report buttons.
- **Timeline**: CustomPainter, scroll/pinch zoom (1h-24h), minimap. Hover shows trickplay thumbnail via OverlayEntry. Red playhead from player position. 3s hold after taps. Motion events color-coded (person=amber, vehicle=indigo, animal=emerald, motion=gray).
- **Clip export**: Timeline mode (tap start/end) or Wizard (dialog with pickers).

## Key Dependencies
- **Backend**: fastapi, uvicorn, sqlalchemy, aiosqlite, pyyaml, structlog, httpx, opencv-python-headless, numpy, onnxruntime-directml
- **External**: NSSM, go2rtc, PyInstaller, Inno Setup
- **App**: media_kit, dio, shared_preferences

## Build & Distribution
- **Release build** (`build_release.bat`): verify nssm → PyInstaller → Flutter Windows → assemble `dist/richiris/`. Only nssm bundled; other deps downloaded by installer.
- **Installer**: Data dir picker (default `C:\ProgramData\RichIris`), creates subdirs, writes bootstrap.yaml, runs `download_deps.ps1`, installs service.
- **Client-only installer**: Flutter app + VC redist only. LAN auto-discovery via `/api/health` probing. Flavor-aware auto-updater (direct GitHub API check).
- **Dev setup**: `setup_dev.bat` downloads all deps + installs packages.

## Implementation Phases (all DONE)
1-6: Foundation, Recording, Live View, Timeline, Clips, Retention, Production
7-9: Trickplay thumbnails, Sub-stream live view, go2rtc MSE
10-12: Flutter native app, Motion detection, AI object detection (RT-DETR)
13-16: Distribution (DB settings, PyInstaller, Inno Setup), Storage config, Data dir restructure, Settings simplification
17-19: Backup/Restore, Auto-Update, Client-only installer + LAN discovery
20-23: Bulk Camera Wizard, Timeline cache + bug report, Refresh feed + client events, Settings cache + camera purge
24: Camera grouping (CameraGroup table, group CRUD + bulk actions, grid chip bar filter, drag-to-reorder, wizard/form group selector)
