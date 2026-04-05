# RichIris NVR - Developer Guide

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.12+ | [python.org](https://www.python.org/downloads/) |
| Flutter | 3.41+ (stable) | [flutter.dev](https://docs.flutter.dev/get-started/install/windows/desktop) |
| Visual Studio Build Tools | 2022+ | Required by Flutter for Windows desktop builds |
| Git | any | [git-scm.com](https://git-scm.com/) |

**Optional (for AI detection):** GPU with DirectML support (NVIDIA, AMD, or Intel). Falls back to CPU if unavailable.

## Getting Started

```bash
git clone https://github.com/richard1912/RichIris.git
cd RichIris
```

### One-command setup

```bash
setup_dev.bat
```

This automatically downloads all external dependencies into `dependencies/` and installs Python packages:

```
dependencies/
├── ffmpeg.exe          FFmpeg 7.1.1
├── ffprobe.exe         FFprobe 7.1.1
├── nssm.exe            NSSM service manager
├── go2rtc/
│   └── go2rtc.exe      go2rtc 1.9.14
└── models/
    └── yolo11x.onnx    YOLO11x ONNX model (218 MB)
```

The YOLO model step requires `pip install ultralytics` (for the one-time `.pt` to `.onnx` export). It is not needed at runtime.

To update a dependency version, edit the version variables at the top of `setup_dev.bat`, delete the old binary from `dependencies/`, and re-run.

### Bootstrap config

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
├── dependencies/      # External binaries (gitignored, populated by setup_dev.bat)
│   ├── ffmpeg.exe
│   ├── ffprobe.exe
│   ├── nssm.exe
│   ├── go2rtc/go2rtc.exe
│   └── models/yolo11x.onnx
├── go2rtc/            # go2rtc config (generated at runtime)
│   └── go2rtc.yaml
├── bootstrap.yaml     # Minimal runtime config (gitignored)
├── setup_dev.bat      # One-command dev setup (downloads dependencies)
├── build_release.bat  # Release build script (PyInstaller + Flutter + assembly)
└── installer/
    ├── richiris.iss       # Inno Setup installer script
    └── download_deps.ps1  # Dependency downloader (run by installer at install time)
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

Requires nssm.exe in `dependencies/` (run `setup_dev.bat` first).

```bash
build_release.bat
```

This will:
1. Verify nssm.exe is present in `dependencies/`
2. Build the backend with PyInstaller
3. Build the Flutter Windows app
4. Assemble everything into `dist/richiris/`
5. Verify all files are present

### Creating the installer

```bash
ISCC.exe /DMyAppVersion=0.0.1 installer\richiris.iss
```

Output: `dist/RichIris-Setup-0.0.1.exe`

The version is passed via `/D` flag — omit it to use the default (`0.0.1`).

The installer is lightweight (~150 MB) and downloads dependencies (ffmpeg, go2rtc, YOLO model) at install time via `installer/download_deps.ps1`. This keeps the installer small and ensures users always get the latest dependency versions.

The installer wizard includes a **Data Directory** page where the user picks where recordings, database, logs, and thumbnails are stored (default: `C:\ProgramData\RichIris`). This writes `bootstrap.yaml` with the chosen path. On upgrades, it pre-populates from the existing `bootstrap.yaml`.

Post-install, the installer:
1. Creates data subdirectories (`database/`, `logs/`, `recordings/`, `thumbnails/`, `playback/`)
2. Writes `bootstrap.yaml` with chosen data_dir
3. Downloads dependencies (ffmpeg, go2rtc, YOLO model) — skips already-present files on upgrade
4. Installs + starts the `RichIris` Windows service via NSSM

If a download fails, the installer warns but continues — the NVR works without the YOLO model (no AI detection), but requires ffmpeg and go2rtc. The download script is deleted after install.

### Publishing a release

`push_release.bat` (gitignored, local-only) automates the full release cycle:

1. Auto-increments the version tag (v0.0.1 → v0.0.2 → ...)
2. Builds the Windows installer and Android APK
3. Generates a changelog from git commits via Claude CLI
4. Creates a GitHub release with both assets

### Android APK (client only)

```bash
cd app
flutter build apk --release
```

Output: `app/build/app/outputs/flutter-apk/app-release.apk`

The APK is a standalone client — it connects to a RichIris server over the network.

## Debugging Tips

- **Backend logs**: Check the terminal output, or `{data_dir}/logs/` when running as a service
- **Service logs**: `C:\ProgramData\RichIris\logs\service-stdout.log` / `service-stderr.log`
- **API explorer**: http://localhost:8700/docs
- **go2rtc UI**: http://localhost:1984 (when running)
- **Flutter DevTools**: Press `d` in the Flutter terminal, or use VS Code/Android Studio debugger
- **Connection issues**: The app's Server Settings screen has a "Test Connection" button
