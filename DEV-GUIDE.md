# RichIris NVR - Developer Guide

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.12+ | [python.org](https://www.python.org/downloads/) |
| Flutter | 3.41+ (stable) | [flutter.dev](https://docs.flutter.dev/get-started/install/windows/desktop) |
| Visual Studio Build Tools | 2022+ | Required by Flutter for Windows desktop builds |
| ffmpeg + ffprobe | 7.x | [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) — add to PATH |
| go2rtc | 1.9+ | [github.com/AlexxIT/go2rtc](https://github.com/AlexxIT/go2rtc/releases) — add to PATH |
| Git | any | [git-scm.com](https://git-scm.com/) |

**Optional (for AI detection):** NVIDIA GPU with CUDA support. Falls back to CPU if unavailable.

## Getting Started

```bash
git clone https://github.com/richard-ferretti/richiris.git
cd richiris
```

### Backend setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Create `bootstrap.yaml` in the project root:

```yaml
data_dir: "C:/ProgramData/RichIris"
port: 8700
```

The `data_dir` folder will be created automatically and holds the SQLite database, recordings, and thumbnails.

### Frontend setup

```bash
cd app
flutter pub get
```

## Running in Development

Start the backend and frontend separately in two terminals:

**Terminal 1 — Backend:**
```bash
cd backend
.venv\Scripts\activate
python run.py
```

This starts the FastAPI server on port 8700. go2rtc starts automatically as a child process (or uses an existing instance if one is already running).

**Terminal 2 — Frontend:**
```bash
cd app
flutter run -d windows
```

On first launch, the app shows a Server Settings screen. Enter `http://localhost:8700` and click Save & Connect. This is persisted — you won't need to enter it again.

Use `r` for hot reload, `R` for hot restart.

### API docs

With the backend running, visit http://localhost:8700/docs for the interactive Swagger UI.

## Project Structure

```
richiris/
├── backend/           # Python FastAPI backend
│   ├── app/
│   │   ├── main.py          # FastAPI app + startup/shutdown
│   │   ├── config.py        # Bootstrap config loader
│   │   ├── database.py      # SQLAlchemy async engine
│   │   ├── models.py        # DB models (Camera, Recording, etc.)
│   │   ├── routers/         # API endpoints
│   │   └── services/        # Business logic (recording, playback, detection, etc.)
│   ├── requirements.txt
│   ├── richiris.spec        # PyInstaller build spec
│   └── run.py               # Dev entry point
├── app/               # Flutter app (Windows + Android)
│   ├── lib/
│   │   ├── main.dart        # Entry point
│   │   ├── app.dart         # App state, navigation, API wiring
│   │   ├── config/          # API config, quality tiers, constants
│   │   ├── models/          # Data classes
│   │   ├── services/        # HTTP API layer (Dio)
│   │   ├── screens/         # Full-page views
│   │   ├── widgets/         # Reusable components (grid, player, timeline)
│   │   └── utils/           # Helpers
│   └── pubspec.yaml
├── go2rtc/            # go2rtc config (binary is gitignored)
│   └── go2rtc.yaml
├── bootstrap.yaml     # Minimal runtime config (gitignored)
├── build_release.bat  # Full release build script
└── installer/
    └── richiris.iss   # Inno Setup installer script
```

## Architecture Overview

```
Flutter App ──HTTP fMP4──▸ FastAPI:8700 ──▸ go2rtc:1984 ◂── RTSP cameras
             ──HTTP MP4──▸ FastAPI:8700 ──▸ FFmpeg remux
                               │
                           SQLite DB        Recordings dir
```

- **Live view**: go2rtc receives RTSP streams and serves HTTP fMP4. The backend proxies these to the Flutter app.
- **Recording**: One ffmpeg process per camera copies the RTSP stream to `.ts` files (no transcode).
- **Playback**: Direct mode serves raw `.ts` files. Other quality tiers transcode on-the-fly via NVENC.
- **Motion/AI detection**: Snapshot-based pipeline grabs JPEG frames from go2rtc, runs OpenCV motion pre-filter, then YOLO if motion detected.

## Making Changes

### Backend

All backend code is in `backend/app/`. The server does not auto-reload by default. Restart `python run.py` after changes, or temporarily set `reload=True` in `run.py` for auto-reload during development.

Key areas:
- **API routes**: `backend/app/routers/` — each file is a FastAPI router
- **Services**: `backend/app/services/` — recording, playback, streaming, detection logic
- **Models**: `backend/app/models.py` — SQLAlchemy models (auto-migrated on startup)
- **Settings**: `backend/app/services/settings.py` — DB-backed settings with defaults

### Frontend

All Flutter code is in `app/lib/`. Hot reload works for most changes.

Key areas:
- **API layer**: `app/lib/services/` — one file per API domain (cameras, recordings, streams, etc.)
- **Screens**: `app/lib/screens/` — full-page views (home grid, fullscreen, settings)
- **Widgets**: `app/lib/widgets/` — reusable components (camera grid, live player, timeline)
- **State**: Managed in `app/lib/app.dart` — lifted state passed down via constructor params

### Important conventions

- **Timezone**: Recordings are stored as local time without timezone info. Never use `.toISOString()` in the frontend for playback times — it converts to UTC and breaks queries.
- **Logging**: Use `structlog` with `logging.getLogger(__name__)`. Pass structured fields via `extra={}`.
- **Settings**: All configuration is in the SQLite `settings` table (editable via the GUI or `GET/PUT /api/settings`). Only `data_dir` and `port` live in `bootstrap.yaml`.

## Building a Release

### Full build (backend + frontend + installer)

```bash
build_release.bat
```

This will:
1. Auto-download ffmpeg, go2rtc, and NSSM to `.build-cache/` (first run only)
2. Build the backend with PyInstaller
3. Build the Flutter Windows app
4. Assemble everything into `dist/richiris/`
5. Verify all files are present

Then build the installer:
```bash
"C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer\richiris.iss
```

Output: `dist/RichIris-Setup-1.0.0.exe`

To upgrade a dependency version, edit the version variables at the top of `build_release.bat` and delete the old binary from `.build-cache/`.

### Android APK (client only)

```bash
cd app
flutter build apk --release
```

Output: `app/build/app/outputs/flutter-apk/app-release.apk`

The APK is a standalone client — it connects to a RichIris server over the network.

## Debugging Tips

- **Backend logs**: Check the terminal output, or `data/richiris.log` when running as a service
- **Service logs**: `C:\ProgramData\RichIris\logs\service-stdout.log` / `service-stderr.log`
- **API explorer**: http://localhost:8700/docs
- **go2rtc UI**: http://localhost:1984 (when running)
- **Flutter DevTools**: Press `d` in the Flutter terminal, or use VS Code/Android Studio debugger
- **Connection issues**: The app's Server Settings screen has a "Test Connection" button
