# RichIris NVR

A self-hosted NVR (Network Video Recorder) built with FastAPI and Flutter. Designed for 24/7 recording of RTSP cameras with live view, timeline playback, motion detection, and AI object detection — no cloud, no subscriptions.

[![Ko-fi](https://img.shields.io/badge/Ko--fi-Support%20this%20project-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/richard1912)

## Features

- **24/7 continuous recording** — HEVC passthrough (no transcode, no GPU usage) into 15-minute `.ts` segments
- **Live view** — HTTP fMP4 via go2rtc + media_kit (libmpv) with low-latency profile
- **Multi-quality streams** — Main/Sub stream selection x Direct/High/Low quality, lazy transcoding (zero resources until a client connects)
- **Timeline playback** — zoomable 24h timeline, instant fragmented MP4 streaming (< 200ms start), speed controls (-32x to 32x), date picker
- **Trickplay thumbnails** — real-time thumbnail capture via go2rtc frame API, hover/scrub preview on timeline
- **Motion detection** — snapshot-based frame differencing with per-camera sensitivity, timeline overlay, multiple configurable script pairs with per-category triggers (e.g., run one script for persons, a different script for vehicles)
- **AI object detection** — YOLO11x on CUDA, gated by motion pre-filter. Per-camera category toggles for persons, vehicles (bicycle/car/motorcycle/bus/truck), and animals (bird/cat/dog/horse/sheep/cow/elephant/bear/zebra/giraffe). Color-coded timeline bars: amber for persons, indigo for vehicles, emerald for animals, gray for motion-only. Falls back to CPU if no GPU available
- **Clip export** — select a time range on the timeline and export an MP4 clip
- **Retention management** — configurable max age (days) and max storage (GB), oldest recordings purged first
- **Multi-camera grid** — click to select, click again for fullscreen with timeline
- **Native apps** — Flutter apps for Windows and Android with full live view, playback, and export
- **Runs as Windows services** — auto-starts on boot, no console window needed
- **Single-file config** — all settings in one `config.yaml`

## Architecture

```
Flutter App (Win/Android) → HTTP fMP4 → FastAPI:8700 → go2rtc:1984 ← RTSP sub-stream
                          → HTTP MP4 (playback) → FastAPI:8700 → FFmpeg remux
                                    |                    |
                                SQLite DB         G:\RichIris (recordings)
                                                  data\playback\ (remux cache)
```

- **Recording**: One FFmpeg process per camera, codec passthrough (`-c:v copy`), 15-minute `.ts` segments. No GPU, no transcode. Watchdog monitors file modification every 2 minutes; stale processes are killed and auto-restarted.
- **Live view**: HTTP fMP4 proxied through FastAPI from [go2rtc](https://github.com/AlexxIT/go2rtc). Flutter app uses media_kit (libmpv) with low-latency profile. Auto-reconnects on stream errors.
- **Playback**: Fragmented MP4 (`-c copy -movflags frag_keyframe+empty_moov`) streamed via StreamingResponse — playback starts in ~200ms. media_kit decodes HEVC natively. Sessions auto-cleanup after 120s idle.
- **Motion detection**: Fetches JPEG snapshots from go2rtc every ~1s. Running weighted-average baseline with adaptive alpha. Sensitivity 0-100 maps to area threshold. 10-second cooldown between events.
- **AI object detection**: YOLO11x on CUDA, triggered only when motion exceeds threshold. Per-camera toggles for person, vehicle, and animal categories. Stores specific COCO class names (e.g., "car", "dog") as detection labels. Min bounding box 0.2% of frame area. Falls back to CPU if CUDA unavailable.

## Requirements

- **Windows 10/11** (runs as Windows services via NSSM)
- **Python 3.11+**
- **Flutter 3.x** (for building the native app)
- **FFmpeg** with RTSP support
- **go2rtc** ([download from releases](https://github.com/AlexxIT/go2rtc/releases))
- **NSSM** (`winget install NSSM.NSSM`)
- RTSP-capable IP cameras
- **Optional**: NVIDIA GPU with CUDA for AI object detection

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/richard1912/RichIris.git
cd RichIris

# Backend
pip install -r backend/requirements.txt

# App (Windows)
cd app
flutter build windows --release
cd ..

# App (Android)
cd app
flutter build apk --release
cd ..
```

### 2. Configure

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` with your camera RTSP URLs, storage paths, and FFmpeg path.

### 3. Set up go2rtc

Download `go2rtc_win64.zip` from [go2rtc releases](https://github.com/AlexxIT/go2rtc/releases) and extract `go2rtc.exe` into the `go2rtc/` directory.

### 4. Install as Windows services

```bash
# go2rtc service
nssm install go2rtc "C:\path\to\RichIris\go2rtc\go2rtc.exe"
nssm set go2rtc AppDirectory "C:\path\to\RichIris\go2rtc"
nssm set go2rtc Start SERVICE_AUTO_START
nssm start go2rtc

# RichIris service
nssm install RichIris "C:\path\to\python.exe" "C:\path\to\RichIris\backend\run.py"
nssm set RichIris AppDirectory "C:\path\to\RichIris"
nssm set RichIris Start SERVICE_AUTO_START
nssm start RichIris
```

### 5. Access

- **App**: Build and run the Flutter app from `app/`
- **API docs**: `http://localhost:8700/docs`

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
│   │   │   ├── clips.py         # Clip export /api/clips
│   │   │   ├── recordings.py    # Playback /api/recordings
│   │   │   ├── streams.py       # go2rtc stream proxy /api/streams
│   │   │   ├── system.py        # System status + storage + retention
│   │   │   └── motion.py        # Motion events /api/motion
│   │   └── services/
│   │       ├── ffmpeg.py              # Command builder (recording only)
│   │       ├── stream_manager.py      # FFmpeg recording lifecycle + go2rtc registration
│   │       ├── go2rtc_client.py       # REST client for go2rtc
│   │       ├── recorder.py            # Segment scanner + DB registration
│   │       ├── clip_exporter.py       # Clip export (concat segments -> MP4)
│   │       ├── playback.py            # Fragmented MP4 streaming for playback
│   │       ├── thumbnail_capture.py   # Thumbnail capture via go2rtc frame API
│   │       ├── retention.py           # Age + storage-based retention cleanup
│   │       ├── motion_detector.py     # Snapshot-based motion detection
│   │       └── object_detector.py     # YOLO AI object detection (GPU)
│   ├── requirements.txt
│   └── run.py                   # Uvicorn entry point
├── go2rtc/
│   ├── go2rtc.exe               # go2rtc binary (not committed)
│   └── go2rtc.yaml              # go2rtc config (streams registered dynamically)
├── app/                         # Flutter app (Windows + Android)
│   ├── lib/
│   │   ├── main.dart            # Entry point, MediaKit init
│   │   ├── app.dart             # MaterialApp, navigation, state
│   │   ├── config/              # API config, quality tiers, constants
│   │   ├── models/              # Data classes
│   │   ├── services/            # API layer (Dio HTTP client)
│   │   ├── screens/             # Home, Fullscreen, System, Settings
│   │   ├── widgets/             # CameraGrid, LivePlayer, QualitySelector
│   │   │   └── timeline/        # CustomPainter timeline + minimap
│   │   └── utils/               # Time/format utilities
│   └── pubspec.yaml             # Dependencies: media_kit, dio, shared_preferences
├── config.yaml                  # Main configuration
├── data/                        # Auto-created: DB, playback cache
├── rebuild.bat                  # Full rebuild (Windows + Android)
├── service-install.bat          # Install Windows services
├── service-restart.bat          # Restart services
└── service-uninstall.bat
```

## Configuration

See [`config.yaml.example`](config.yaml.example) for all options:

| Section | Key fields |
|---------|-----------|
| `server` | `host`, `port` |
| `storage` | `recordings_dir`, `database_url` |
| `ffmpeg` | `path`, `ffprobe_path`, `segment_duration` |
| `go2rtc` | `host`, `port` |
| `retention` | `max_age_days`, `max_storage_gb` |
| `trickplay` | `enabled`, `interval`, `thumb_width`, `thumb_height` |
| `cameras` | `name`, `rtsp_url`, `sub_stream_url`, `enabled`, `motion_sensitivity`, `ai_detection`, `ai_detect_persons`, `ai_detect_vehicles`, `ai_detect_animals`, `ai_confidence_threshold`, `motion_scripts` (JSON array of on/off script pairs with per-category triggers) |

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python, FastAPI, SQLAlchemy, aiosqlite, structlog, httpx |
| App | Flutter (Windows + Android), media_kit, Dio |
| Live View | go2rtc (HTTP fMP4) |
| Recording | FFmpeg (codec passthrough) |
| Playback | FFmpeg (fragmented MP4 streaming, no transcode) |
| Motion Detection | OpenCV, NumPy (snapshot-based frame differencing) |
| AI Detection | YOLO11x, Ultralytics, CUDA |
| Database | SQLite |

## Video Quality

Stream and quality selection are independent — pick a stream source (Main or Sub) and a quality tier.

### Live View

| Quality | Main Stream | Sub Stream | Server Load |
|---------|-------------|------------|-------------|
| **Direct** | Native passthrough (HEVC) | Native passthrough | Zero (no ffmpeg) |
| **High** | H.264 re-encode, native resolution | H.264 re-encode, native resolution | Moderate |
| **Low** | H.264 re-encode, reduced bitrate | H.264 re-encode, reduced bitrate | Moderate |

### Playback (recorded .ts files)

Stream selection does not apply to playback — recordings are always from the main stream.

| Quality | Processing | Server Load |
|---------|-----------|-------------|
| **Direct** | HEVC passthrough (`-c copy`) | Near zero (container remux only) |
| **High** | H.264 NVENC re-encode, native resolution | GPU |
| **Low** | H.264 NVENC re-encode, reduced bitrate | GPU |

### Platform Notes

- **Windows**: All quality tiers available for both live view and playback
- **Android**: Direct is hidden for **live view only** (raw RTSP passthrough has compatibility issues with some camera brands). Direct is available for playback (clean fMP4 from ffmpeg). Default quality is Direct on both platforms.

## VPN Access

Live view uses push-based HTTP fMP4 streaming, which works reliably over WireGuard VPN. The stream is continuous — no polling loops to break under latency/jitter.

## Known Issues

- **Reverse playback is glitchy** — negative speed playback (e.g. -2x, -4x) can stutter or jump unexpectedly due to keyframe seeking limitations with HEVC segments

## License

MIT
