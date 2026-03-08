# RichIris NVR

A self-hosted Network Video Recorder built with FastAPI and React. Designed for 24/7 recording of RTSP cameras with on-demand live view and playback — no cloud, no subscriptions.

Built to run on a Windows machine with an NVIDIA GPU for hardware-accelerated transcoding.

## Features

- **24/7 continuous recording** — HEVC passthrough (no transcode, no GPU usage) into 15-minute `.ts` segments
- **Live view** — On-demand HLS streaming with H.264 transcode, auto-starts when you open a camera and stops after 30s idle
- **Timeline playback** — Click any point on the 24-hour timeline to play back recordings. GPU-accelerated HEVC→H.264 transcode (NVENC) since browsers can't decode HEVC
- **Clip export** — Select a time range on the timeline and export an MP4 clip, no duration limit
- **Retention management** — Configurable max age (days) and max storage (GB), oldest recordings purged first
- **Multi-camera grid** — Click to select, click again for fullscreen with timeline
- **Runs as a Windows service** — Auto-starts on boot, no console window needed
- **Single-file config** — All settings in one `config.yaml`

## Architecture

```
Browser (LAN/VPN) → FastAPI (port 8700) → FFmpeg subprocesses
                          |                       |
                      SQLite DB            Recording storage
                                           HLS live segments
                                           Playback transcode cache
```

- **Backend**: Python/FastAPI with async SQLite (SQLAlchemy + aiosqlite)
- **Frontend**: React 19 + Vite + Tailwind CSS 4 + HLS.js
- **Recording**: Two FFmpeg processes per camera — one for recording (always on, codec copy), one for live HLS (on-demand)
- **Playback**: On-demand GPU transcode sessions with automatic cleanup

## Requirements

- Windows 10/11
- Python 3.11+
- Node.js 18+ (for building frontend)
- FFmpeg with NVENC support (for GPU transcode)
- NVIDIA GPU (tested with RTX 4080 SUPER)
- RTSP-compatible IP cameras

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/richard1912/RichIris.git
cd RichIris

# Backend
cd backend
pip install -r requirements.txt

# Frontend
cd ../frontend
npm install
npm run build
```

### 2. Configure

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` with your camera RTSP URLs, storage paths, and FFmpeg path.

### 3. Run

**Development:**
```bash
cd backend
python run.py
```

**As a Windows service:**
```bash
# Run as Administrator
service-install.bat
```

The service auto-starts on boot. Use `service-restart.bat` to restart or `service-uninstall.bat` to remove.

### 4. Access

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
│   │   └── services/            # FFmpeg, recording, playback, clips, retention
│   ├── service.py               # Windows Service wrapper
│   └── run.py                   # Dev entry point
├── frontend/
│   └── src/
│       ├── App.tsx              # Main app with grid + timeline
│       ├── api.ts               # API client
│       └── components/          # CameraGrid, Timeline, HlsPlayer, etc.
├── config.yaml.example          # Template config (copy to config.yaml)
└── data/                        # Auto-created: DB, live HLS, playback cache
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | FastAPI, SQLAlchemy, aiosqlite, structlog |
| Frontend | React 19, Vite, Tailwind CSS 4, HLS.js |
| Recording | FFmpeg (codec copy, no GPU) |
| Live view | FFmpeg HLS (libx264 software encode) |
| Playback | FFmpeg HLS (h264_nvenc GPU encode) |
| Service | pywin32 Windows Service |

## License

MIT
