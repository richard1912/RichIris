# RichIris NVR

A self-hosted NVR (Network Video Recorder) built with FastAPI and React. Designed for 24/7 recording of RTSP cameras with live view and timeline playback — no cloud, no subscriptions.

## Features

- **24/7 continuous recording** — HEVC passthrough (no transcode, no GPU usage) into 15-minute `.ts` segments
- **Live view** — go2rtc MSE streaming over WebSocket (push-based, works reliably over VPN)
- **Timeline playback** — zoomable 24h timeline, instant MP4 remux (< 1 second), speed controls (-32x to 32x), date picker
- **Trickplay thumbnails** — real-time RTSP thumbnail capture, hover preview on timeline
- **Clip export** — select a time range on the timeline and export an MP4 clip, no duration limit
- **Retention management** — configurable max age (days) and max storage (GB), oldest recordings purged first
- **Multi-camera grid** — click to select, click again for fullscreen with timeline
- **Runs as Windows services** — auto-starts on boot, no console window needed
- **Single-file config** — all settings in one `config.yaml`

## Architecture

```
Browser → WebSocket (MSE) → go2rtc ← RTSP sub-stream
       → FastAPI (port 8700) → FFmpeg (recording, passthrough)
                             → SQLite (metadata)
                             → Disk (HEVC .ts segments)
```

- **Recording**: One FFmpeg process per camera, codec passthrough (`-c:v copy`), 15-minute `.ts` segments. No GPU, no transcode.
- **Live view**: [go2rtc](https://github.com/AlexxIT/go2rtc) takes RTSP input and delivers fMP4 over WebSocket (MSE). Prefers the camera's H.264 sub-stream for zero-transcode live view; uses FFmpeg transcode for HEVC sources. The WebSocket is proxied through FastAPI so everything runs on a single port.
- **Playback**: FFmpeg remuxes HEVC `.ts` to MP4 with `-c copy -movflags +faststart` (< 1 second). Browsers decode HEVC natively (Chrome 107+, Edge, Safari).

## Requirements

- **Windows 10/11** (runs as Windows services via NSSM)
- **Python 3.11+**
- **Node.js 18+** (for building the frontend)
- **FFmpeg** with RTSP support
- **go2rtc** ([download from releases](https://github.com/AlexxIT/go2rtc/releases))
- **NSSM** (`winget install NSSM.NSSM`)
- RTSP-capable IP cameras

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/richard1912/RichIris.git
cd RichIris

# Backend
pip install -r backend/requirements.txt

# Frontend
cd frontend
npm install
npm run build
cd ..
```

### 2. Configure

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` with your camera RTSP URLs, storage paths, and FFmpeg path.

### 3. Set up go2rtc

Download `go2rtc_win64.zip` from [go2rtc releases](https://github.com/AlexxIT/go2rtc/releases) and extract `go2rtc.exe` into the `go2rtc/` directory.

```bash
cp go2rtc/go2rtc.yaml.example go2rtc/go2rtc.yaml
```

Edit `go2rtc/go2rtc.yaml` and set the `ffmpeg.bin` path.

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

Open `http://localhost:8700` in your browser. API docs at `http://localhost:8700/docs`.

## Project Structure

```
RichIris/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app + lifespan
│   │   ├── config.py            # Settings from config.yaml
│   │   ├── database.py          # SQLAlchemy async engine
│   │   ├── models.py            # Camera, Recording, ClipExport
│   │   ├── schemas.py           # Pydantic schemas
│   │   ├── routers/             # API route handlers
│   │   └── services/            # FFmpeg, recording, playback, go2rtc, clips, retention
│   └── run.py                   # Uvicorn entry point
├── go2rtc/
│   ├── go2rtc.exe               # go2rtc binary (not committed — download separately)
│   └── go2rtc.yaml.example      # go2rtc config template
├── frontend/
│   └── src/
│       ├── App.tsx              # Main app with grid + timeline
│       ├── api.ts               # API client
│       └── components/          # CameraGrid, Timeline, MsePlayer, etc.
├── config.yaml.example          # Main config template
└── data/                        # Auto-created: DB, playback cache
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
| `cameras` | `name`, `rtsp_url`, `sub_stream_url`, `enabled` |

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python, FastAPI, SQLAlchemy, aiosqlite, structlog |
| Frontend | React 19, Vite, Tailwind CSS |
| Live view | go2rtc (MSE/WebSocket) |
| Recording | FFmpeg (codec passthrough) |
| Playback | FFmpeg (remux to MP4, no transcode) |
| Database | SQLite |

## VPN Access

Live view uses WebSocket MSE (push-based), which works reliably over WireGuard VPN. Unlike HLS (which relies on polling and can freeze under latency/jitter), MSE receives a continuous stream of fMP4 segments — no polling loops to break.

## License

MIT
