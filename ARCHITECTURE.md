# RichIris Architecture

## Overview

RichIris is a custom-built NVR (Network Video Recorder). It runs natively on Windows 11, leveraging an NVIDIA RTX 4080 SUPER for AI object detection via YOLO.

## System Diagram

```
┌──────────────┐     ┌─────────────────────────────────────────────┐
│ Flutter App  │     │              Windows 11 Host                │
│ (Win/Android)│     │                                             │
│              │────▶│  FastAPI Backend (port 8700)                │
│  - Live grid │     │       │                                     │
│  - Timeline  │     │       ├── REST API (cameras, streams, etc.) │
│  - Clip      │     │       ├── SQLite DB (metadata)              │
│    export    │     │       │                                     │
│  - Settings  │     │       ▼                                     │
└──────────────┘     │  FFmpeg Subprocesses (1 per camera)         │
                     │       │  -c:v copy (passthrough)            │
                     │       │  → G:\RichIris\{cam}\YYYY-MM-DD\   │
                     │       │                                     │
                     │  go2rtc (port 1984)                         │
                     │       │  RTSP → HTTP fMP4 (live view)       │
                     │       │  RTSP → JPEG snapshots (motion)     │
                     │       │                                     │
                     │  YOLO11x (CUDA)                             │
                     │       │  Motion-gated AI object detection   │
                     │       │                                     │
                     │  ┌─────────────────────┐                    │
                     │  │  IP Cameras (6x)    │                    │
                     │  │  192.168.8.42-47    │                    │
                     │  │  RTSP streams       │                    │
                     │  └─────────────────────┘                    │
                     └─────────────────────────────────────────────┘
```

## Component Details

### FastAPI Backend (`backend/app/`)

The backend orchestrates everything: manages ffmpeg processes, serves the API, proxies live streams, and runs motion/AI detection.

**Lifespan**: On startup, loads config, initializes the database, starts streams for all enabled cameras, pre-warms go2rtc connections, and starts motion/AI detection. On shutdown, gracefully terminates all processes.

**Key services**:
- `stream_manager.py` - Manages ffmpeg subprocess lifecycle. One process per camera with automatic restart on failure.
- `ffmpeg.py` - Composable command builder for ffmpeg commands.
- `recorder.py` - Background scanner that finds new `.ts` segment files on disk and registers them in the database.
- `go2rtc_client.py` - REST client for go2rtc stream registration and management.
- `playback.py` - On-demand fragmented MP4 streaming for recording playback.
- `motion_detector.py` - Snapshot-based motion detection using go2rtc frame API.
- `object_detector.py` - YOLO11x GPU inference for person/vehicle/animal detection.
- `thumbnail_capture.py` - Real-time thumbnail capture for trickplay preview.
- `retention.py` - Age and storage-based retention cleanup.

### FFmpeg Pipeline

Each camera gets a single ffmpeg process for recording:

```bash
ffmpeg -rtsp_transport tcp -timeout 30000000 -i <stream_url> \
  -map 0:v -map 0:a? -c:v copy -c:a copy \
  -f segment -segment_time 900 -segment_atclocktime 1 \
  -reset_timestamps 1 -strftime 1 \
  "G:/RichIris/{cam}/%Y-%m-%d/rec_%H-%M-%S.ts"
```

**Why passthrough (`-c:v copy`)?** Preserves original 4K HEVC quality, eliminates GPU transcode load, maximizes the number of simultaneous cameras.

### go2rtc (Live View)

go2rtc handles all live view streaming. Streams are baked into `go2rtc.yaml` at startup (no API registration needed — survives config reloads). Provides:
- RTSP output on port 8554 for direct Flutter app connections (low-latency HEVC playback)
- JPEG frame snapshots for motion detection
- Multiple quality tiers per camera (S1/S2 x Direct/High/Low/Ultra Low), lazy-initialized
- Httpx keepalive consumers keep camera RTSP connections alive permanently for instant live view

### Database (SQLite)

Key tables:
- **cameras** - id, name, rtsp_url, sub_stream_url, enabled, motion/AI settings
- **recordings** - id, camera_id, file_path, start_time, end_time, file_size, duration
- **clip_exports** - id, camera_id, start_time, end_time, file_path, status
- **motion_events** - id, camera_id, start_time, end_time, peak_intensity, detection_label, detection_confidence

SQLite chosen for simplicity (single-file, no server process). Async access via `aiosqlite`.

### Storage Layout

```
{data_dir}/                         # Configurable via installer or Settings
├── database/richiris.db            # SQLite database
├── logs/                           # Application logs
├── recordings/{camera}/            # Recording .ts files per camera per day
│   └── Camera 1/
│       └── 2026-03-08/
│           ├── Camera 1 2026-03-08 00.00 - 00.15.ts
│           └── ...
├── thumbnails/{camera}/            # Trickplay + detection thumbnails
└── playback/                       # Transient transcoded MP4s (auto-cleaned)
```

### Flutter App (`app/`)

Flutter (Windows + Android) with media_kit (libmpv) for video playback.
- **Live View**: Responsive camera grid with direct RTSP streaming via go2rtc (port 8554). media_kit connects natively via mpv with 5s cache, 16MB demuxer buffer, TCP transport, and hardware decoding. Zoomable video (pinch/scroll to zoom in fullscreen).
- **Timeline**: CustomPainter-based zoomable timeline with recording segments, motion events, trickplay thumbnails
- **Playback**: Fragmented MP4 streaming, speed controls (-32x to 32x)
- **Clip Export**: Time range selector, MP4 export, download
- **Settings**: Camera management, system status, backup/restore

## Environment

| Component | Version |
|-----------|---------|
| OS | Windows 11 Pro |
| Python | 3.13 |
| Flutter | 3.x |
| FFmpeg | 7.1.1 |
| GPU | NVIDIA RTX 4080 SUPER |

## Network

6 IP cameras on local network (192.168.8.42-47), RTSP streams. HEVC (H.265) at 4K (3840x2160) main stream, HEVC sub-stream (HTMS cameras) / H.264 sub-stream (Reolink).
