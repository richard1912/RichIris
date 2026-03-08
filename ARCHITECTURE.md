# RichIris Architecture

## Overview

RichIris is a custom-built NVR (Network Video Recorder) replacing Blue Iris. It runs natively on Windows 11, leveraging an NVIDIA RTX 4080 SUPER for hardware-accelerated video processing via NVENC/NVDEC.

## System Diagram

```
┌──────────────┐     ┌─────────────────────────────────────────────┐
│   Browser    │     │              Windows 11 Host                │
│   (Web UI)   │     │                                             │
│              │────▶│  Caddy (HTTPS reverse proxy)                │
│  - Live grid │     │       │                                     │
│  - Timeline  │     │       ▼                                     │
│  - Clip      │     │  FastAPI Backend (port 8700)                │
│    export    │     │       │                                     │
│  - Settings  │     │       ├── REST API (cameras, streams, etc.) │
└──────────────┘     │       ├── Serves React SPA (static files)   │
                     │       ├── SQLite DB (metadata)              │
                     │       │                                     │
                     │       ▼                                     │
                     │  FFmpeg Subprocesses (1 per camera)         │
                     │       │                                     │
                     │       ├── Output 1: Recording segments      │
                     │       │   -c:v copy (passthrough)           │
                     │       │   → G:\RichIris\{cam}\YYYY-MM-DD\  │
                     │       │                                     │
                     │       └── Output 2: Live HLS                │
                     │           -c:v copy, 2s segments            │
                     │           → data\live\{cam}\stream.m3u8     │
                     │                                             │
                     │  ┌─────────────────────┐                    │
                     │  │  IP Cameras (6x)    │                    │
                     │  │  192.168.8.41-46    │                    │
                     │  │  HTTP streams       │                    │
                     │  └─────────────────────┘                    │
                     └─────────────────────────────────────────────┘
```

## Component Details

### FastAPI Backend (`backend/app/`)

The backend orchestrates everything: manages ffmpeg processes, serves the API, and hosts the frontend.

**Lifespan**: On startup, loads config, initializes the database, and auto-starts streams for all enabled cameras. On shutdown, gracefully terminates all ffmpeg processes.

**Key services**:
- `stream_manager.py` - Manages ffmpeg subprocess lifecycle. One process per camera with automatic restart on failure (exponential backoff, max 30s).
- `ffmpeg.py` - Composable command builder. Small functions (`build_input_args`, `build_recording_output`, `build_live_output`) that assemble the full ffmpeg command.
- `recorder.py` - Background scanner that finds new `.ts` segment files on disk and registers them in the database with metadata (start time, duration, file size).

### FFmpeg Pipeline

Each camera gets a single ffmpeg process with dual output:

```bash
ffmpeg -hwaccel cuda -rtsp_transport tcp -i <stream_url> \
  # Output 1: Recording (15-min segments, codec passthrough)
  -map 0:v -map 0:a? -c:v copy -c:a copy \
    -f segment -segment_time 900 -segment_atclocktime 1 \
    -reset_timestamps 1 -strftime 1 \
    "G:/RichIris/{cam}/%Y-%m-%d/rec_%H-%M-%S.ts" \
  # Output 2: Live HLS (2s segments, rolling 5-segment playlist)
  -map 0:v -map 0:a? -c:v copy -c:a copy \
    -f hls -hls_time 2 -hls_list_size 5 \
    -hls_flags delete_segments+temp_file \
    "data/live/{cam}/stream.m3u8"
```

**Why passthrough (`-c:v copy`)?** Preserves original quality, eliminates GPU transcode load, maximizes the number of simultaneous cameras. GPU is reserved for clip export (NVENC re-encode to MP4).

### Database (SQLite)

Three tables:
- **cameras** - id, name, rtsp_url, enabled, width, height, codec, fps, created_at
- **recordings** - id, camera_id, file_path, start_time, end_time, file_size, duration
- **clip_exports** - id, camera_id, start_time, end_time, file_path, status, created_at

SQLite chosen for simplicity (single-file, no server process). Async access via `aiosqlite`.

### Storage Layout

```
G:\RichIris\                    # Recording storage (configurable)
├── camera_1\
│   ├── 2026-03-08\
│   │   ├── rec_00-00-00.ts
│   │   ├── rec_00-15-00.ts
│   │   └── ...
│   └── 2026-03-09\
│       └── ...
├── camera_2\
│   └── ...

C:\01-Self-Hosting\RichIris\
├── data\
│   ├── live\                   # Ephemeral HLS segments (auto-cleaned)
│   │   ├── camera_1\
│   │   │   ├── stream.m3u8
│   │   │   └── segment_001.ts
│   │   └── ...
│   └── richiris.db             # SQLite database
```

### Frontend (Phase 2)

React 19 + TypeScript + Vite + Tailwind CSS. Served as static files by FastAPI.
- **Live View**: Responsive camera grid with HLS players (hls.js)
- **Recordings**: Interactive timeline with drag-to-seek, date picker
- **Clip Export**: Time range selector, GPU-accelerated MP4 export, download
- **Settings**: Camera management, system status (GPU, disk, stream health)

## Environment

| Component | Version |
|-----------|---------|
| OS | Windows 11 Pro |
| Python | 3.13 |
| Node.js | 22 |
| FFmpeg | 7.1.1 |
| GPU | NVIDIA RTX 4080 SUPER |
| GPU Codecs | h264_nvenc, hevc_nvenc, h264_cuvid, hevc_cuvid |

## Network

6 IP cameras on local network (192.168.8.41-46), HTTP streams. No RTSP - these cameras serve streams over HTTP directly.
