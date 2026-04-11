<p align="center">
  <img src="assets/logo.png" alt="RichIris Logo" width="256">
</p>

# RichIris NVR

A self-hosted NVR (Network Video Recorder) for 24/7 recording of RTSP cameras with live view, timeline playback, trickplay thumbnails, motion detection, and AI object detection. Free and open source.

[![Ko-fi](https://img.shields.io/badge/Ko--fi-Support%20this%20project-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/richard1912)

## Features

- **24/7 continuous recording** -- HEVC passthrough (no transcode, no GPU usage) into 15-minute segments
- **Live view** -- low-latency direct RTSP via go2rtc with multi-quality streaming (Direct/High/Low/Ultra Low), zoomable video
- **Timeline playback** -- zoomable 24h timeline, instant seek, forward/reverse speed controls (-4x to 32x), date picker
- **Trickplay thumbnails** -- hover/scrub preview on timeline
- **Motion detection** -- per-camera sensitivity, timeline overlay, configurable script triggers
- **AI object detection** -- YOLO11x for persons, vehicles, and animals with color-coded timeline bars and multi-frame confirmation
- **Clip export** -- select a time range and export an MP4
- **Retention management** -- configurable max age and max storage, oldest recordings purged first
- **Native apps** -- Windows desktop and Android client
- **Runs as a Windows service** -- auto-starts on boot

## Quick Start

### 1. Download and install

Download the latest `RichIris-Setup.exe` from [Releases](https://github.com/richard1912/RichIris/releases).

The installer will:
- Install the RichIris backend and desktop app
- Ask you to choose a **data directory** for recordings, database, and logs (pick a drive with plenty of space)
- Download required dependencies (FFmpeg, go2rtc, YOLO model) automatically
- Install and start the RichIris Windows service

### 2. Add cameras

Open the RichIris app (desktop shortcut or Start Menu). On first launch, enter the server address -- `http://localhost:8700` if running on the same machine.

Go to **Settings** and add your cameras with their RTSP URLs. Cameras start recording immediately once added.

### 3. Android client (optional)

Download `RichIris-Android.apk` from [Releases](https://github.com/richard1912/RichIris/releases) and install it on your Android device. Enter your server's IP address (e.g., `http://192.168.1.100:8700`) to connect.

## Min Requirements

RichIris records via HEVC passthrough (no transcode), so per-camera CPU load is low. The heavier workloads are **AI object detection** (ONNX inference via DirectML on any DX12 GPU) and **on-the-fly transcoding** for non-Direct quality tiers (NVENC on NVIDIA GPUs). Sizing below assumes ~4K HEVC cameras at 4-8 Mbps on the main stream.

### Minimum (up to 4 cameras, AI enabled)

| Component | Minimum |
|---|---|
| **OS** | Windows 10/11 64-bit |
| **CPU** | 4 cores / 8 threads — Intel 8th gen Core i5 / AMD Ryzen 5 2600 or newer |
| **RAM** | 8 GB |
| **GPU** | Any DirectX 12 GPU with 2 GB VRAM (integrated Intel UHD 630 / AMD Vega / NVIDIA GTX 1050 all work for RT-DETR via DirectML). NVIDIA required **only** if you want NVENC transcoded quality tiers. |
| **System drive** | 256 GB SSD (OS, backend, database) |
| **Recording drive** | Dedicated HDD or SSD — plan ~60 GB/day per 4K HEVC camera @ 6 Mbps (≈1.7 TB per camera for 30-day retention) |
| **Network** | Gigabit Ethernet, cameras on the same LAN |
| **Cameras** | RTSP H.264 or HEVC with sub-stream support (motion/AI read the sub-stream at 2 fps) |

### Recommended (6-12 cameras, smooth AI + transcoded playback)

| Component | Recommended |
|---|---|
| **CPU** | 6+ cores — Intel 12th gen Core i5 / AMD Ryzen 5 5600 or newer |
| **RAM** | 16 GB |
| **GPU** | NVIDIA RTX 3050 or better (6 GB+ VRAM) — fast RT-DETR inference and NVENC for Low/Ultra Low live and playback tiers across all cameras |
| **System drive** | 500 GB NVMe SSD |
| **Recording drive** | 8+ TB HDD (CMR preferred; SMR is fine for single-writer NVR workloads). Per-camera budget: ~60 GB/day @ 4K HEVC 6 Mbps; scale linearly with resolution, bitrate, and retention. |
| **Network** | Gigabit LAN, wired cameras (PoE switch recommended) |

### Notes on sizing

- **Recording CPU cost** is near-zero per camera (copy-mode FFmpeg). 10+ cameras on a modern quad-core is realistic.
- **AI detection** runs only when motion is detected, and inference is ~11 ms per frame on a modern GPU. The bottleneck for many cameras is the persistent sub-stream decoders (one FFmpeg per camera at 2 fps), not the neural net.
- **Transcoded live/playback** (High when source is non-HEVC, Low, Ultra Low) use NVENC on NVIDIA GPUs. Without an NVIDIA GPU, stick to **Direct** quality — Direct is raw passthrough and costs nothing.
- **Storage** is the usual dominant cost. Use the retention controls in **System Settings** (max age + max storage) to bound disk usage.
- **Memory** is mostly FFmpeg + go2rtc + ONNX runtime. Budget ~300-500 MB per camera as a rough ceiling.

## Configuration

All settings are managed through the app's **System Settings** screen:

| Section | What it controls |
|---------|-----------------|
| **General** | Timezone |
| **Storage** | Data directory (recordings, database, logs, thumbnails) |
| **Retention** | Max recording age (days), max storage (GB) |
| **Trickplay** | Enable/disable timeline thumbnail previews |
| **Logging** | Log level |

The data directory and port are also stored in `bootstrap.yaml` next to the application (editable if needed before first launch).

## Architecture

```
Flutter App (Windows/Android)
    |
    v
FastAPI backend (:8700) --> go2rtc (:18700/:18554) <-- RTSP cameras
    |
    v
SQLite DB + Recordings + Thumbnails
```

- **Recording**: One FFmpeg process per camera, codec passthrough (no transcode), 15-minute `.ts` segments
- **Live view**: go2rtc receives camera RTSP streams and re-serves via RTSP (:8554). Flutter app connects directly to go2rtc for smooth HEVC playback. Zoomable video in fullscreen.
- **Playback**: Direct mode serves raw segments instantly. Other quality tiers transcode on-the-fly via NVENC
- **AI detection**: Snapshot-based pipeline -- motion pre-filter, then YOLO inference with multi-frame confirmation (2 detections in 3 frames + positional movement)

## Video Quality

| Quality | Live View | Playback | Server Load |
|---------|-----------|----------|-------------|
| **Direct** | Raw passthrough | Raw `.ts` file | Zero |
| **High** | HEVC re-encode, source quality | HEVC NVENC, source quality | GPU |
| **Low** | HEVC re-encode, 1/8 bitrate | HEVC NVENC, 1/8 bitrate | GPU |
| **Ultra Low** | 1/16 bitrate, 15fps | 1/16 bitrate, 15fps | GPU (light) |

## Developing

See [DEV-GUIDE.md](DEV-GUIDE.md) for development setup, project structure, and build instructions.

## License

MIT
