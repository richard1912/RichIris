# RichIris Architecture

## Overview

RichIris is a custom-built NVR (Network Video Recorder). It runs natively on Windows 11, using any DirectX 12 GPU for AI object detection (RT-DETR) and facial recognition (SCRFD + ArcFace) via ONNX Runtime DirectML. An NVIDIA GPU unlocks NVENC-transcoded live/playback quality tiers but is not required for detection.

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
                     │  go2rtc (API 18700 / RTSP 18554)            │
                     │       │  RTSP relay (live view)             │
                     │       │  MJPEG sub-stream → FrameBroker     │
                     │       │  /api/frame.jpeg → main-stream snap │
                     │       │                                     │
                     │  RT-DETR + SCRFD + ArcFace (DirectML ONNX)  │
                     │       │  Motion-gated object + face ID      │
                     │       │                                     │
                     │  ┌─────────────────────┐                    │
                     │  │  IP Cameras (N×)    │                    │
                     │  │  RTSP streams       │                    │
                     │  │  LAN auto-discover  │                    │
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
- `motion_detector.py` - Snapshot-based motion detection using FrameBroker frames, gates RT-DETR, chains into face recognition.
- `object_detector.py` - RT-DETR ONNX inference for person/vehicle/animal detection on DirectML.
- `face_recognizer.py` - SCRFD (detection) + ArcFace (512-D embeddings) ONNX pipeline with in-memory cosine matcher.
- `frame_broker.py` - One persistent ffmpeg per camera pulling MJPEG sub-stream frames at 2 fps for the motion/detection pipeline.
- `thumbnail_capture.py` - Real-time thumbnail capture for trickplay preview.
- `retention.py` - Age and storage-based retention cleanup.
- `_onnx_lock.py` - Global asyncio lock serializing all ONNX inference so DirectML's GPU provider never sees concurrent sessions (prevents native crashes).

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
- RTSP output on port 18554 for direct Flutter app connections (low-latency HEVC playback)
- HTTP API on port 18700 — `/api/frame.jpeg?src=<stream>_s1_direct` returns a one-shot 4K main-stream JPEG used by the face pipeline (~100 ms latency)
- MJPEG sub-stream frames consumed by FrameBroker for motion + object detection at 2 fps
- Multiple quality tiers per camera (S1/S2 x Direct/High/Low/Ultra Low), lazy-initialized
- Httpx keepalive consumers keep camera RTSP connections alive permanently for instant live view

### AI Pipeline

```
FrameBroker (sub-stream, 2 fps)
    │
    ▼
Motion pre-filter (OpenCV running-avg baseline)
    │  (if motion_sensitivity threshold exceeded)
    ▼
RT-DETR (object_detector.py)  →  persons / vehicles / animals
    │  (if "person" + multi-frame confirmation passes)
    ▼
go2rtc /api/frame.jpeg?src=<cam>_s1_direct   →  4K main-stream JPEG
    │
    ▼
SCRFD (face_recognizer.py)  →  faces inside person bbox (scaled to main-stream coords)
    │
    ▼
ArcFace embedding  →  cosine match against in-memory enrolled embeddings
    │
    ▼
MotionEvent.face_matches / face_unknown / face_detected persisted
Per-camera motion_scripts filter on face identity (known / unknown / specific person)
```

All three ONNX inference calls (RT-DETR, SCRFD, ArcFace) share a single asyncio lock so DirectML only runs one session at a time — concurrent sessions can native-crash the DirectML provider.

### Facial Recognition Storage

- **faces** - id, name (unique), notes, created_at
- **face_embeddings** - id, face_id (FK, CASCADE), embedding (BLOB, 512 float32), source_thumbnail_path, face_crop_path, created_at
- **motion_events** extended with `face_matches` (JSON), `face_unknown`, `face_detected`

The matcher keeps all embeddings in an in-memory cache (list of 512-D numpy vectors). It rebuilds on every enroll/delete. Cosine similarity is a single numpy dot product per candidate — negligible cost even with hundreds of embeddings.

### Database (SQLite)

Key tables:
- **cameras** - id, name, rtsp_url, sub_stream_url, enabled, motion/AI settings
- **recordings** - id, camera_id, file_path, start_time, end_time, file_size, duration
- **clip_exports** - id, camera_id, start_time, end_time, file_path, status
- **motion_events** - id, camera_id, start_time, end_time, peak_intensity, detection_label, detection_confidence, face_matches, face_unknown, face_detected, thumbnail_path
- **camera_groups** - id, name, sort_order
- **faces** / **face_embeddings** - enrolled people + their 512-D ArcFace vectors (see Facial Recognition section)

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
| GPU | NVIDIA (NVENC + DirectML ONNX); AMD/Intel via DirectML; CPU fallback |

## Network

Any number of RTSP-capable IP cameras on a private LAN. Main-stream codec HEVC (H.265) or H.264 at the camera's native resolution (4K supported via `-c:v copy` recording). Sub-stream used for motion detection; sub-stream codec is probed per-camera. LAN subnet is auto-detected; users can also enter explicit subnets in the add-cameras wizard.
