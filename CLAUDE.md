# RichIris - Custom NVR
update claude md as needed for code changes

> **Inference: see [`INFERENCE.md`](INFERENCE.md)** — operator guide for changing the
> detection model (`ai.local_model`), the measured accuracy/speed table for each installed
> model, how to export and add a new one, the tuning knobs, and how to roll back to the
> RTX 4080. Start there if detection accuracy needs adjusting.

## Quick Reference
- **Backend**: Windows service `RichIris` via NSSM (FastAPI on port 8700). Restart: `nssm restart RichIris`
- **Health watchdog** — `backend/app/services/self_watchdog.py`: probes `127.0.0.1:{port}/api/health` every 30s (60s startup grace, 5s timeout). 3 consecutive failures → `os._exit(1)` → NSSM restarts (AppExit=Restart, throttle 1500ms). Catches the silent-listener-death failure mode where uvicorn's socket dies but the asyncio loop keeps running.
- **go2rtc**: Child process on fixed ports (API 18700, RTSP 18554). Ports reported via `/api/system/status` → `go2rtc_rtsp_port`.
- **Remote access (2026-09-01)**: point the client at `https://richiris.richardferretti.com` (box Caddy → `192.168.8.12:8700`, real LE cert). LAN/VPN only: no public DNS record — the name comes from a Flint2 dnsmasq override and Caddy 403s any non-private `remote_ip` — so reach it from outside the house over WireGuard, whose client config must use **192.168.8.1 as its DNS**. `http://192.168.8.12:8700` and LAN auto-discovery still work unchanged. There is **no Authelia** and no auth in the app: a `forward_auth` would break every client (the Flutter app cannot follow a login redirect), so the VPN boundary is the only control — solve auth in the app before ever publishing this. **Live view still bypasses Caddy**: `StreamApi._host` takes the host from the base URL and plays `rtsp://<host>:18554/...` directly, since Caddy cannot proxy RTSP — so a hostname used here must resolve for the client AND have 18554 reachable (ufw allows `10.10.10.0/24` and `192.168.9.0/24`). Everything else (REST, playback fMP4, clip downloads, poster frames) is proxied, with `flush_interval -1` so the growing-file and live streams are not buffered.
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
- **Windows client opens as a blank window? Suspect a window manager, not the app.** PowerToys FancyZones with *Move newly created windows to their last known zone* resizes the window microseconds after `CreateWindow`, landing inside Flutter's first-frame/window-show handshake. When the snap wins that race the embedder presents nothing and the window stays blank forever — a white block over black, neither being the app's `#171717` — and no resize, repaint or focus change recovers it. It is a race, so the same binary renders on some launches and not others, which makes it look like a bad build. **Tell: the window is not the size the runner asked for** (`main.cpp` requests 1280x720 logical; a snapped window comes up at the zone's size instead). Confirm by copying the exe to another filename and running that — FancyZones keys on the exe path, so the copy renders every time. Fix is FancyZones' *Excluded apps* (`richiris.exe`); a `ForceRedraw()` on `WM_SIZE` in the runner was tried and does **not** reliably fix it.

## Architecture
```
Camera RTSP (main) ← ffmpeg recording (-c:v copy → .ts, independent of go2rtc)
Camera RTSP (main) ← go2rtc ← httpx keepalive (s1_direct) → live view clients (RTSP :18554)
Camera RTSP (sub)  ← go2rtc ← FrameBroker (persistent ffmpeg, MJPEG @2fps) → motion + thumbnails
Flutter App → RTSP → go2rtc :18554 (live) | HTTP MP4 → FastAPI:8700 → FFmpeg (playback)
```

- **Segment start times**: live registration parses `rec_HH-MM-SS.ts` (accurate to the second), then `_rename_segment` rewrites the file as `Camera YYYY-MM-DD HH.MM - HH.MM.ts` — **which drops the seconds**. Re-registering an already-renamed segment (restore, storage migration, rebuilt DB) therefore used to round its start down to `:00`, up to 59s of error in the value clip export seeks with. `_refine_renamed_start()` now recovers the seconds from `mtime - probed_duration` (mtime = ffmpeg's last write = the segment's end), falling back to the truncated filename if that is unavailable or disagrees by more than a minute. This bounds the error to the PTS-vs-wall skew (~10s), it does not eliminate it.
- **Recording**: One ffmpeg per camera, `-c:v copy` passthrough → HEVC 4K .ts files. Watchdog kills stale processes (no file update in 5min). `-timeout 30s` socket timeout. Restarts back off 5s→10s→20s→40s→60s (capped).
- **Outage log throttling** (`services/stream_manager.py`): a camera that stays unreachable re-fails forever, and logging every retry buries real errors. `_RepeatLogSuppressor` collapses a repeating failure to one line per 5min (`REPEAT_LOG_INTERVAL_SECONDS`), carrying `suppressed_since_last`; it keys off the failure site (`rec_died:{cam}`, `keepalive:{stream}`, `ffmpeg_err:{cam}:{label}`) and is reset on recovery so the next outage logs loudly again. **`StreamInfo.banner_logged` is per camera, not per process** — a down camera never reaches ffmpeg's `frame=`/`size=` progress output, so the banner phase never ends and every relaunch would otherwise re-log the whole ~32-line build+stream banner. Set in a `finally`, so the banner is logged at most once per camera per backend run. Before this, three unreachable cameras produced ~30MB/day of log spam and rotated `richiris.log` every 8h.
- **go2rtc keepalives**: StreamManager runs httpx fMP4 consumer per camera (s1_direct). Sub-stream kept warm by FrameBroker. Keepalives staggered 1s apart, auto-reconnect 5s retry.
- **RTSP credentials must be percent-encoded** (`services/rtsp_url.py`). go2rtc is Go and parses stream URLs with `net/url`, which rejects anything outside RFC 3986's userinfo set — `@` above all, plus `^ [ ] < > " \` and space. ffmpeg is lenient (splits on the *last* `@`), so a raw password gives a half-broken camera that **records fine while live view, FrameBroker, motion detection and thumbnails all fail** with `net/url: invalid userinfo` (only visible in `logs/go2rtc.log`; the app just shows a dead tile and `go2rtc_connected: false`). `normalize_rtsp_url()` encodes on the way into the DB (create/update/snapshot) and again when generating go2rtc.yaml, so pre-existing rows are repaired too; it preserves valid `%XX` so re-saving is idempotent. `build_rtsp_credentials()` handles the discover/scan path, where user+password arrive as plaintext fields. Flutter's camera form decodes for display and re-encodes on save (`camera_form_screen.dart`), splitting userinfo on the **last** `@`.
- **FrameBroker** (`services/frame_broker.py`): Persistent ffmpeg per camera pulling MJPEG from go2rtc s2_direct at 2fps. Parsed via JPEG SOI/EOI → numpy frames. `get_latest()` (instant), `get_fresh(max_wait)`, or `get_latest_jpeg()` (raw bytes, no re-encode — serves the live-view poster endpoint). Auto-restarts with 3s backoff. Starts before motion + thumbnails in lifespan. **Stall watchdog**: a reader that produces no frame for 30s has its ffmpeg killed so the loop reconnects — an ffmpeg hung on a stream whose codec changed underneath it (or a half-open RTSP session) never closes stdout, which would otherwise silently stop motion detection for that camera.
- **Live view**: Flutter connects to go2rtc RTSP via media_kit (libmpv). 5s cache, 16MB demuxer, TCP transport, hw decoding. Stall detection (10s), exponential backoff (500ms→10s). HTTP fMP4 proxy retained as fallback.
- **Live-view startup latency**: go2rtc stays connected to the cameras, but each client still bootstraps its own decoder from a mid-stream join, and go2rtc does not replay the last GOP to a new RTSP consumer. Two costs: (1) libavformat's fps probe — go2rtc's SDP carries no framerate, so `avformat_find_stream_info` waits for 20 video frames (~2.9s on a 7fps camera). Killed by `demuxer-lavf-o=fpsprobesize=0` on every live player (`app.dart:_ensureLivePlayer`, `widgets/live_player.dart`); mpv still reports FPS via `estimated-vf-fps`. (2) Waiting for the next keyframe — unavoidable client-side, so the grid paints a **poster frame** instead (below). Measured first frame: 3.4-3.6s → 0.7-1.8s (Tapo main), 1.4-1.8s → 0.2-0.4s (Reolink main). Keyframe interval is a camera setting, not something the NVR can shorten — Front South 47 (Vatilon PB4) sat at 8.6s until its web UI's `I Frame Interval` was set from 4s to 1s (2026-08-21), bringing it to 2.14s like the rest.
- **Poster frame**: `CameraCard` paints `GET /api/cameras/{id}/latest-frame.jpg` (the FrameBroker's newest JPEG, ≤0.5s old, ~2ms to serve) over the video and cross-fades it out 400ms after the player reports its first frame. **The "first frame" signal is `player.stream.position` advancing, never `stream.width`/`videoParams`** — mpv publishes video dimensions as soon as it parses the stream's parameter sets (which arrive on connect), while the first decodable frame only lands at the next keyframe. Gating on width dropped the poster ~1.7s early and the tile went visibly black in between (measured on Android via `adb screenrecord` + per-tile `signalstats` YAVG). A 5s timer armed by the width event is only a safety net so a stale poster can't cover a feed whose position never advances. URL is cache-busted once per grid, not per build, so status polling doesn't re-download it. Rotation is applied to match the video. `LivePlayer` also reports `playing` from `player.state.width` for reused mid-stream players, since `stream.width` is a broadcast stream with no replay — without that the poster would never lift on a grid→fullscreen handoff.
- **Playback**: All qualities go through PlaybackManager → fMP4 (`-movflags frag_keyframe+empty_moov`). Direct = `ffmpeg -c copy` remux with `-noaccurate_seek -ss N` pre-seek (server-side seek shifts work off libmpv — first frame faster than letting it scan a raw .ts). High/Low/Ultra Low = HEVC NVENC transcode. Streaming endpoint supports HTTP Range on completed files (libmpv ranged reads); growing files served as plain `200 OK` stream because we can't promise an end byte. **No client-side `player.seek` after open** — fMP4's PTS=0 already corresponds to the user's chosen time. `seek_seconds` in the response is metadata-only (timeline display alignment). Sessions auto-cleanup 30s idle, same-camera eviction.
- **Reverse playback is rendered on the SERVER** (`services/reverse_playback.py`, 2026-09-05). `POST .../playback?direction=backward` returns an fMP4 in which the segment runs *backwards* from `seek_seconds` to its start, and the client simply plays it forward at `setRate(|speed|)` - the same call it makes for 1x-4x, so -1x/-2x/-4x work identically on mpv (Windows/Android) and a browser `<video>`. The response carries `direction: "backward"`, `seek_seconds` = the offset the reverse starts FROM (clamped to the file's ffprobed duration, so "now" on a growing segment starts at the last recorded frame), and `has_more` = an earlier segment exists. On `completed` the client requests `segment_start - 1ms` with `direction=backward`, which the backend resolves into the previous segment at its end. Render: ffmpeg's `reverse` filter buffers every frame of its input, so the renderer walks back from `seek_seconds` in 8s chunks, each decoded on the GPU (`-hwaccel vaapi -hwaccel_output_format vaapi`, `fps=10` + `scale_vaapi` to 720p *before* `hwdownload` so only 720p NV12 ever hits system memory), reversed, and encoded to raw Annex-B H.264 (x264 veryfast). Chunk bytes are concatenated newest-first into one muxing ffmpeg (`-r 10 -f h264 -i pipe:0 -c copy`) that stamps fresh CFR timestamps - so no per-chunk timestamp offsets, no non-monotonic DTS. Two chunks render concurrently; measured **~10x realtime** on the box for 4K HEVC 15fps (the -4x ceiling needs 4x), first bytes in ~2s. Quality tier is ignored for reverse (always 720p/10fps H.264). A chunk whose seek lands mid-GOP can kill the VAAPI decoder ("Could not find ref with POC"); it is retried in software automatically. **The old client-side reverse (seek a paused player backwards every 500ms) is gone** - it stalled because the fMP4 proxy has no index to seek in. Two supporting fixes in the `playback.mp4` endpoint: every streamed chunk now `touch`es the session (the 30s idle sweep used to be able to tear down a long transcode/render mid-stream), and a backward session tolerates 60s of silence between chunks instead of 5s. **Rendering is paced to consumption** (`MAX_LEAD_BYTES`, 24 MB ≈ 90 s of output): the endpoint reports bytes streamed back to the renderer, which stops launching chunks once it is that far ahead. Before this every session eagerly rendered the whole rest of the segment (up to 113 chunks, ~75 s of two busy cores + the iGPU, audible fans) even when the user tapped another speed two seconds later; an unread session now stalls at ~11 chunks and the idle sweep kills it. **Android memory (measured on the Fold 2026-09-05):** the app sits at 1.25 GB PSS on the grid and 1.70 GB in fullscreen, almost all EGL buffers for the eight offstage 4K decoders; hammering the speed bar (five bursts of 8 taps at 0.3-0.7 s, then a 45 s reverse hold) oscillated 1.70-1.98 GB with no upward trend, and reverse itself is *lighter* (720p decoder). So a LOW_MEMORY kill during a pressure test is headroom on the phone, not a leak; the lever if it recurs is pausing the grid's offstage players while fullscreen is open on Android, as the web build already does.
- **GPU**: Any NVIDIA card for NVENC transcoding + RT-DETR acceleration (DirectML also works on AMD/Intel; CPU fallback available but slow).

### Web client (browser) - `flutter build web`
The browser UI is **the same `app/` Dart source** as the Windows and Android clients, compiled for
web. There is no second frontend to keep in sync. Deploy with **`scripts/deploy_web.sh`** (builds,
wipes `/opt/stacks/caddy/srv/richiris` on the box, ships `build/web`, verifies). Caddy serves it at
`richiris.richardferretti.com` and proxies `/api`, `/docs`, `/openapi.json` on the **same origin** to
`192.168.8.12:8700` - so the client needs no configured server URL and makes no cross-origin request.

Three things had to give, each behind a conditional import so the native builds are untouched:

- **`config/platform_info.dart`** - `isWeb` / `isAndroid` / `isWindows`. **`dart:io` COMPILES on web**
  and only throws when a getter is *read*, so `Platform.isAndroid` is a runtime trap, not a build
  error: the bundle builds clean, then dies on a minified stack that never mentions `dart:io`. That
  is what `isClientOnlyInstall()` did (it runs on every prefs read) and it killed startup before the
  first frame. Each getter short-circuits on `kIsWeb`, a compile-time constant, so dart2js folds the
  `dart:io` reference away entirely. **Use these, never bare `Platform.*`, in shared code.**
- **`services/player_tuning.dart`** - the mpv knobs (`cache-secs`, `rtsp-transport`,
  `demuxer-lavf-o=fpsprobesize=0`, ...). These cast `player.platform` to `NativePlayer`, which is a
  **compile** error on web, and were the only thing blocking a web build. No-ops in the browser,
  which owns its own buffering and decode.
- **`widgets/web_live_video.dart`** - live video, bypassing media_kit completely. Its web backend
  does drive an `<video>` element correctly (streams decode and report true dimensions) but never
  attaches it to the document: `media_kit_video`'s web `Video` only mounts its `HtmlElementView`
  while `id`, `rect` and an internal `_visible` agree, and `stop()` - which `open()` calls first,
  every time - pushes a null width that clears `_visible`. The symptom is eight elements decoding 4K
  HEVC into nothing.

**Live view on web is the fMP4 proxy, not RTSP.** No browser plays RTSP, so `StreamApi.liveUrl()`
returns `/api/streams/{id}/live.mp4` instead. go2rtc serves that **without re-encoding** and a plain
`<video>` plays it - HEVC included - so there is no WebRTC signalling, no MSE buffer and no transcode.
Verified end to end: 4K main and 640x480 sub both play at real time. **Firefox will not play these
at all** (no HEVC); Chrome, Edge and Safari do, with hardware decode. Serving Firefox means adding an
`#video=h264` go2rtc variant and paying an iGPU transcode per viewer - not worth it until asked.

**The web grid defaults to the SUB stream** (native stays Main). Eight 4K `<video>` elements is both
a heavy decode and tens of Mbps, each fetched separately - painful over the VPN this is reached
through and pointless for a tile a few hundred pixels wide. The Main/Sub selector still works.

**Two Flutter-web compositing traps, both worked around, both easy to reintroduce:**
1. A platform view inside `InteractiveViewer`'s transform+clip (`ZoomableVideo`) lands on a
   compositing path where the Scaffold's opaque background is painted **over** it - the feed decodes,
   is correctly sized, and is invisible. `ZoomableVideo` passes its child straight through on web.
2. `app.dart` keeps the grid mounted under `Offstage` while fullscreen is open, so mpv players stay
   warm. On web that leaves every tile decoding **and** its HTTP stream open behind fullscreen, so
   the grid is unmounted outright there instead. Costs a second of reconnect coming back.

`WebLiveVideo.elevate` (set only by the fullscreen view, never the grid) puts a `z-index` on the
`<flt-platform-view>` host, and an `onPause` listener re-plays the element because Flutter pauses a
`<video>` whenever it re-parents the platform view on a scene rebuild.

**Not usable on web** (all `dart:io`, all guarded rather than removed): LAN backend scan, the
auto-updater, and the Windows-service panel in system settings.

### Motion + AI Detection
Snapshot pipeline reading FrameBroker every 0.5s. Motion: running weighted-avg baseline, GaussianBlur(21,21), threshold(25). Sensitivity 0-100 → area threshold `(101-s)*0.05%`. AI: RT-DETR-L ONNX via `onnxruntime-directml` (~11ms GPU inference, CPU fallback). Multi-frame confirmation: 2 detections in 3 frames + bbox movement ≥1.5% diagonal. Categories: persons (COCO 0), vehicles (1,2,3,5,7), animals (14-23). Per-camera toggles + confidence threshold. `motion_scripts` JSON array with per-category triggers. Events: MotionEvent rows, 10s cooldown. Env vars: MOTION_CAMERA, MOTION_TIME, MOTION_INTENSITY, DETECTION_LABEL, DETECTION_CONFIDENCE, FACE_NAMES. Scripts need full python.exe path (NSSM PATH differs).

**The 200W floodlight is driven by `floodlight.py` (Shelly), NOT by `tuya_floodlight.py`.** This trips people up, badly. The real device is a **Shelly 1 Mini Gen3 at `192.168.8.100`**, fired from inside `email.py` (the Intruder Alert script) as a deterrent burst — so the floodlight comes on as a side effect of the alert, and there is no separate "floodlight" entry in `motion_scripts` doing it. **Burst length is `FLOODLIGHT_BURST_SECONDS` (env, default 60s as of 2026-08-31, was 120s), and the Shelly counts it down itself via `toggle_after`** — so the light goes out even if the script, RichIris or the whole box dies mid-burst, and the motion script's `off_delay` has NOTHING to do with floodlight timing. `floodlight.py` prefers the Shutters backend (`/lights/100/set`, box-local, so the floor plan updates live) and falls back to hitting the Shelly directly. **Tuya is gone entirely (2026-08-31)** — Richard returned the Tuya floodlight (brightness not as advertised) and uses no Tuya devices. `tuya_floodlight.py`, `tuya_floodlights.json` and the `Floodlight` entry in Front North 42's `motion_scripts` are all deleted. That script had been targeting `192.168.10.50`, a subnet the box cannot route to, and **409 of its 412 invocations timed out over the preceding 7 days**, each blocking a task for the full 30s script timeout. Neither file was git-tracked, so both are archived at `C:\01-Self-Hosting\_RecycleBin\richiris-tuya-20260831\` (the JSON holds a now-dead Tuya local key). Front North 42 is down to two scripts: the camera lamp and the Intruder Alert.

**Walk-past benchmark, 2026-08-31 23:02 (night/IR, fully local yolo11n@320).** Measured from camera frame capture: inference 21-23 ms, detection confirmed 24-120 ms, **scripts fired at 30-125 ms**, camera lamp on at **356 ms**, alert email out at **5.2-6.1 s**. The 5-6 s is entirely SMTP inside `email.py` (`spawn_ms` was 17-37 ms) — RichIris itself is sub-400 ms end to end. Detection confidence was 0.82 on Front South 47 but only **0.45 on Front North 42 against its 0.40 threshold**, which is what prompted the move to `yolo11s-320`.

**Region-of-interest cropping (2026-08-31, `ai.region_crop_enabled`, default ON).** Detection no longer runs on the whole frame. `object_detector.compute_motion_region()` takes the motion threshold mask, squares up the motion bbox (`REGION_PADDING` 1.4, floor `REGION_MIN_SIZE` 224px), slides it fully inside the frame and returns `(x, y, size)`; `_preprocess` then upscales that crop to the model input, so a distant object fills far more of it. Measured 2.3-2.9x zoom in practice. Returns **None** when the square would cover ≥90% of the frame (`REGION_SKIP_RATIO`) — wide motion falls back to full-frame, which is roughly 2 in 3 events. Borrowed from Frigate, which uses the same trick to make a 320px model beat a naive 640px one on small objects. **Inference cost is unchanged** (same input tensor either way); this buys accuracy, not speed. **The returned boxes are in CROP space and are offset back into frame space immediately after the detect call** — everything downstream (zones via `bbox_in_mask`, `_fire_fast_scripts`, thumbnails, the ≥1.5%-diagonal move confirmation) assumes frame coordinates, so that offset is load-bearing. Note `MIN_BOX_AREA_FRACTION` is applied against the *crop* area, so it is proportionally more permissive on a crop; the per-camera confidence threshold is the real filter.

**INFERENCE IS FULLY LOCAL as of 2026-08-31 — the RTX 4080 offload is OFF.** `ai.remote_url` is set to `''` in settings (previously `http://192.168.8.11:8701`); pre-change DB at `richiris.db.bak-local-inference-20260831`. Richard's call, for speed: measured live, local yolo11n@320 runs at **26.5 ms** end-to-end versus **64.4 ms** via the 4080. The GPU only ever did ~11 ms of that — JPEG-encoding the frame, the HTTP hop and decode on the far side cost more than the inference they bought. Cost to the box is **+0.06 cores** (0.58 -> 0.64 of one core, 60 s cgroup sample), and it removes the dependency on a Windows PC that has crashed 12 times since 30 July. The Windows `RichIrisInference` NSSM service was **uninstalled 2026-08-31** (service, registry key and process all gone; port 8701 free; `ImmichML` deliberately untouched). Its code remains at `inference_server/`, so rollback = run `inference_server/install-service.bat` elevated, then put the URL back in `ai.remote_url` and restart — full procedure in [`INFERENCE.md`](INFERENCE.md). **Accuracy is the price** — see the eval below. `_MODEL_FILENAMES` is `["yolo11n-320.onnx", "rtdetr-l.onnx", "yolo11x.onnx"]`, small first, and `ai.local_model` overrides it by filename (a configured-but-missing file logs a warning and falls through rather than killing detection). On the box's i5-8600 rtdetr-l costs **577 ms on 2 cores** versus **22 ms on 1 core** for yolo11n@320 (53 ms end-to-end incl. pre/post). Running the big model locally is what turned the 2026-08-31 Windows crash into 6.5 h of degraded NVR at load ~14.5. `LOCAL_CPU_THREADS = 1` on purpose: 2 threads gives 14 ms and 5 threads 12 ms, but spending 4 extra cores to save 9 ms is a bad trade on a box also decoding 8 streams. **The parser is chosen from the ONNX output shape, not the filename** — RT-DETR emits `[1,300,84]` (normalized coords, NMS-free), YOLO emits `[1,84,N]` (input-space pixel coords, NMS required, `_postprocess_yolo`). That also fixes a latent bug: `yolo11x.onnx` was already in the fallback list but would have been parsed as RT-DETR and produced garbage. Input size is read from the model too, so a 320 export just works. **Model eval, 2026-08-31** — 35 real detection thumbnails across all cameras, conf 0.40, person+vehicle classes, `rtdetr-l@640` as the reference (114 objects, 1116 ms/frame on this CPU). "Recall" = agreement with RT-DETR-L, not true ground truth; the crop column assumes perfect motion localisation so it is an upper bound, and the real figure sits between the two:

| model | inference | recall, full frame | recall, region-cropped | extra boxes |
|---|---|---|---|---|
| **yolo11n-320** (current) | **20 ms** | 43.0% | **50.9%** | 0 |
| yolo11s-320 | 58 ms | 53.5% | 59.6% | 7 |
| yolo11n-640 | 85 ms | 54.4% | — | 2 |
| yolo11s-640 | 245 ms | 51.8% | — | 8 |
| yolo11m-320 | 169 ms | 60.5% | 71.1% | 6 |

`yolo11s-320` and `yolo11m-320` are installed alongside; switching is just `ai.local_model` + a restart. Note yolo11s@640 is both slower AND worse than s@320 — with region cropping, input size past 320 stops paying. yolo11n also produces **zero** extra boxes, i.e. its errors are misses, not false alarms. **Per-frame recall is not per-event recall**: at 2 FPS an object persists across many frames and confirmation needs only 2 detections in 3, so events are caught far more reliably than ~51% suggests — but brief or distant events are genuinely more likely to be missed than they were on the 4080. Confidences also run lower (0.65 mean vs RT-DETR's 0.95 on the same car); cameras sit at 40-50% thresholds, which still passes. Models are `dependencies/models/yolo11{n,s,m}-320.onnx` (`yolo export format=onnx imgsz=320 opset=17`); push with `deploy_box.sh --with-models`.

**Why NOT the iGPU / OpenVINO.** Considered for the fallback and rejected on measurement. Debian trixie has dropped `intel-opencl-icd`, so OpenVINO 2026.3.1 on the box reports `devices: ['CPU']` only — enabling GPU would mean third-party Intel compute-runtime .debs on the box whose VAAPI stack Jellyfin, Immich and RichIris transcode all depend on. Frigate quotes ~15-25 ms for MobileNetV2 on an HD 620-class iGPU; yolo11n@320 already does 21.6 ms on **one CPU core**. The iGPU buys nothing here and risks the graphics stack.

### Camera Light Control + Intruder Alert (`Camera Light Control/`)
Scripts the motion rules shell out to. `lamp_control_working.py <on|off|auto> <ip>` drives the white-light lamp inside an MWRCTV camera — there is no lamp endpoint, the lamp follows the **image** pipeline's `day_night_mode` (2=on, 1=off, 0=photoresistor), so it GETs the whole image-settings blob, flips three fields and POSTs it **all** back (the firmware replaces `param2` rather than merging, so an omitted key returns as 0). Basic auth, not digest — these cameras advertise digest but lose the POST body on the challenge's second leg, which is why curl and PowerShell both fail here.

`email.py "<camera name>"` is the **Intruder Alert**. As of 2026-08-30 it fires a **120s burst of the front floodlight before** sending the email — an alert landing while everyone is asleep does nothing to a person in the driveway. Same 23:00-06:00 window as the email; a floodlight failure never blocks the send.

`floodlight.py <on|off> [seconds]` (new 2026-08-30) drives the 200W LED floodlight on the Shelly 1 Mini Gen3 at **192.168.8.100**. It tries the Shutters backend (`127.0.0.1:8000/lights/100/set`) first so the Shutters app's floor plan updates immediately over WebSocket, and falls back to the Shelly's `Switch.Set` directly when that backend is down. Either way the **device** counts the burst down via `toggle_after`, so the light goes out even if this box dies mid-burst. **This is now the only floodlight path** — the older Tuya script and its camera-42 rule were deleted 2026-08-31 when Richard returned the Tuya unit.

**`email.py` and `floodlight.py` both shadow the stdlib `email` package** — running anything from this folder puts it on `sys.path[0]` and urllib's transitive `import email.utils` then resolves to `email.py`. Both strip their own directory from `sys.path` before importing urllib/smtplib. Consequence: `email.py` cannot `import floodlight` by name, and loads it by explicit file path via `importlib.util.spec_from_file_location`.

**Camera .45 (Backyard 45) has ~55% packet loss** (measured 2026-08-30; .42/.44/.46/.47 are all at 0%, RTT on surviving packets a normal 0.27ms). Small GETs mostly get through, but the ~1KB lamp-settings POST usually does not — the write times out *and does not apply*, while the identical write lands on .44 in 0.07s. So this camera's lamp rule has been silently failing. Physical fault (cable/port/AP), not a code one.

### Detection Zones
Per-camera polygon masks that scripts can opt into via `zone_ids: [int,...]` on a `MotionScriptConfig`. Empty `zone_ids` = whole frame (unchanged behavior) — one script can be zone-restricted while another on the same camera is not. Points stored normalized [0,1] in the `zones` table so they survive sub-stream resolution changes. Rasterized masks are cached per (zone_id, frame_shape) by `services/zone_mask.py`; union masks for multi-zone scripts cached by sorted tuple. CRUD invalidates the cache. **Filter point**: applied inside `_on_motion` after category + face filters build `firing`; motion-only scripts use `motion_in_mask` (thresh ∩ zone ≥ sensitivity_pct), detection scripts use `bbox_in_mask` on the bbox's bottom-center pixel (ground anchor — feet/wheels). If a zone-restricted script's union mask is unavailable (deleted zone), the script fails closed. Zone deletion prunes any `zone_ids` references from the owning camera's scripts. Flutter editor: tap to add vertex, drag to move, long-press to remove; snapshot via `POST /api/cameras/snapshot`. `CameraResponse` exposes `zone_count` (aggregated in one COUNT query on list) so the grid can show a badge without fetching each camera's zones.

**Do not use `name` as a logger `extra={}` key** — it collides with LogRecord's reserved attribute and raises `KeyError: "Attempt to overwrite 'name' in LogRecord"`. Use `zone_name`, `camera_name`, etc.

### Facial Recognition
**DISABLED as of 2026-08-31** — Richard's call: not reliable or useful enough to justify the cost. The kill switch is the `ai.face_enabled` setting (`AIConfig.face_enabled`), which **defaults to `False` in code** so it survives a settings-table wipe; set it to `true` in settings to turn face work back on. It gates three places: the recognizer + clusterer startup in `main.py`, the per-event face block in `motion_detector._detect_loop`, and the recognizer start in `reload_camera`. With it off the SCRFD/ArcFace ONNX models are never loaded at all. The per-camera `face_recognition` flags were also all zeroed the same day so the Flutter feature badges match reality (they read the per-camera flag, not the global switch). The six that had it on were `2 Front North 42, 4 Alfresco 44, 5 Backyard 45, 6 Backyard 2 46, 7 Front South 47, 8 Bins 43`, all at `face_match_threshold` 60; thresholds were left intact, and the pre-change DB is at `/var/lib/richiris/database/richiris.db.bak-face-off-20260831`. No motion script was face-gated (every `faces: []` / `face_unknown: false`), so nothing regressed. Re-enabling needs BOTH `ai.face_enabled=true` and the per-camera flags set again.

**Gotcha that motivated the global switch:** turning `face_recognition` off per-camera does *not* stop face work. That flag only chooses recognition-vs-detect-only; the `elif not face_recognition` branch still ran a "cheap SCRFD-only pass" on **every** person event to populate `face_detected` for the enrollment UI. So per-camera off still paid for face detection on every person.

The pipeline below describes the behaviour when `ai.face_enabled` is true. Runs only when RT-DETR confirms a `person`. Pipeline: SCRFD (`dependencies/models/det_10g.onnx`, from InsightFace buffalo_l) detects faces within the cropped person bbox → ArcFace (`w600k_r50.onnx`, 512-D) embeddings → cosine match against in-memory cache from `face_embeddings` table. Match ≥ per-camera threshold (default 0.5) → writes `face_matches` JSON on MotionEvent; else sets `face_unknown=true`. Models run on the same `onnxruntime-directml` pipeline as RT-DETR. Per-camera toggles: `face_recognition`, `face_match_threshold`. Per-script filters: `faces: [id,...]` (AND trigger) and `face_unknown: bool`. Enrollment: tag faces from past person-detection thumbnails via `POST /api/faces/{id}/embeddings` — multi-face images return `multiple_faces` with candidate bboxes so the UI can disambiguate. `reload_cache()` is called on any embedding mutation so the matcher stays fresh. Timeline tints person events cyan (known) or rose (unknown).

### Video Quality
Two selectors: **Stream** (Main/Sub, live only) and **Quality** (Direct/High/Low/Ultra Low). Separate prefs for live vs playback. Bitrate probed at startup (5s ffmpeg sample + ffprobe codec).

Live streams baked into go2rtc.yaml at startup. High aliases to direct for HEVC sources (no point re-encoding same codec/bitrate). Non-HEVC sources get HEVC re-encode. Low = 1/8 bitrate, Ultra Low = 1/16 bitrate + 15fps + short GOP.

Playback transcoding: same tiers, probed from .ts file. Direct = `-c copy` remux with `-noaccurate_seek -ss` (lands on nearest keyframe; can leave seeks ±GOP-duration off the requested time). Others = NVENC transcode with `-ss` seek.

### Clip export timing
Camera `.ts` streams are badly VFR and mpegts carries no frame rate, so ffmpeg **guesses** `r_frame_rate` — 40 fps against a true 12 on Front Door, whose frames actually arrive 12ms to 770ms apart. A straight `-c copy` remux inherits both problems: the clip visibly hitches in a browser, and any downstream CFR transcode (Immich's, for one) believes the 40 and replays a 10-minute clip in 3:20.

`export_clip` therefore runs **two copy passes, no re-encode**:
1. **Stage** — concat + output-side `-ss`/`-t` into `_staged_{id}.mp4`. Output-side seek is deliberate: input-side `-ss` on the concat demuxer does not honour `-t` (a 60s request produced 649s).
2. **Re-time** — `-c copy -bsf:v setts=ts=N*{ticks}` rewrites packet timestamps only, giving a constant cadence; plus `-video_track_timescale 90000`, `+faststart`, and `-tag:v hvc1` for HEVC (`hev1`, what mpegts copies out as, is rejected by Safari and by Chrome without a fallback).

Ticks come from `_probe_mp4_cadence()` reading the **staged file's** moov (frame count ÷ duration), never from sampling the source: frame rate drifts within a recording — Front South 47 runs 7fps in one stretch and 10fps in another — and a 20s sample of the wrong stretch stretched a 10-minute clip to 14:32. If the re-time pass fails the staged copy is promoted to the final name, so a clip is never lost over the cosmetic pass.

**Known defect (unfixable from here): the export window lands a few seconds early.** A 00:40:00–00:50:00 request on Front Door produced 00:39:50–00:49:49 — right duration, window ~10s early — and it desynchronises grid composites (a 2-camera grid had Front Door 9s behind Front North, with the lagging cell black for the first 9s).

The cause is **camera stream latency, not a labelling error**. `ss_offset` seeks from the DB segment `start_time`, which is the host clock when ffmpeg opened the file; the frame written at that instant was captured some seconds earlier. Measured by reading the burned-in OSD against the box clock: Front Door's main stream ran ~10s behind at 00:30 and ~3s behind at 01:39, Front North ~0–2s. It varies, so a fixed per-camera offset cannot correct it either.

**`-use_wallclock_as_timestamps 1` does NOT fix this — do not re-try it.** Tested 2026-08-29 on Front Door: a segment opened at host 01:44:01 still had a first frame stamped 01:43:50. The flag stamps packets with *arrival* time, which is exactly what the host clock already gives; capture time only exists in the camera's RTCP sender reports, which the ffmpeg CLI does not expose. The real remedies are camera-side (cut the doorbell's main-stream buffering) or procedural (ask for a wider window than the moment of interest).

### Timezone
Recordings stored as **local time without timezone**. Configurable via Settings → General. Frontend must NOT use `.toISOString()` (converts to UTC). Always format as local ISO strings.

`created_at` / `updated_at` follow the same rule and must use `default=local_now` (`config.local_now()`), **not** `server_default=func.now()` alone — `func.now()` compiles to SQLite's `CURRENT_TIMESTAMP`, which is UTC, so a clip exported at 01:03 local listed as 15:03 next to its own local `start_time`. The `server_default` stays as a fallback for raw-SQL inserts; the Python default wins for ORM inserts. Existing rows were shifted once by the `migration_created_at_local_v1` one-shot in `database.py`. That migration reads the tz from the **settings table**, not `get_tz()` — `init_db()` runs before `load_settings_from_db()`, so the config singleton is still on its bootstrap default at that point.

## Project Structure
```
RichIris/
├── backend/app/
│   ├── main.py, config.py, logging_config.py, database.py, models.py, schemas.py
│   ├── routers/  (backup, cameras, clips, groups, recordings, settings, storage, streams, system, motion, zones)
│   └── services/ (backup, ffmpeg, stream_manager, go2rtc_client, go2rtc_manager, recorder,
│                   clip_exporter, playback, settings, thumbnail_capture, retention,
│                   storage_migration, motion_detector, object_detector, update_checker,
│                   frame_broker, benchmark, zone_mask, rtsp_url)
├── app/lib/      # Flutter (main, app, theme, config/, models/, services/, screens/, widgets/, utils/)
├── installer/    (richiris.iss, richiris_client.iss, download_deps.ps1)
├── dependencies/ # gitignored: ffmpeg, ffprobe, nssm, go2rtc, models/rtdetr-l.onnx (~388MB)
└── data/         # gitignored: DB + playback cache + logs
```

## Code Style
- Small focused functions (~10-20 lines). Structured logging via `structlog` (`logger = logging.getLogger(__name__)`).
- Pass structured fields via `extra={}` dicts. Root logger INFO; `app.*` at configured level (DEBUG default).
- httpx/httpcore silenced to WARNING. ffmpeg banner logged once per camera per run, then warnings/errors only (throttled — see Outage log throttling).

## API Endpoints
**Cameras**: `GET/POST/PUT/DELETE /api/cameras` (CRUD, `?purge_data=true` deletes files) | `PUT reorder` (body: `{order: [id,...]}`) | `POST discover` (probe RTSP patterns) | `POST scan` (LAN scan port 554) | `POST discover_batch` (parallel probe) | `POST snapshot` (single JPEG from RTSP URL) | `GET {id}/latest-frame.jpg?max_age=` (newest FrameBroker JPEG, live-view poster frame; 404 when stale/missing) | `POST test-script` (run script command, returns exit_code/stdout/stderr)
**Groups**: `GET/POST /api/groups` | `PUT/DELETE /api/groups/{id}` | `POST /api/groups/{id}/bulk` (body: `{action: "enable"|"disable"|"arm_motion"|"disarm_motion"}`)
**Faces**: `GET /api/faces` | `POST /api/faces` `{name, notes?}` | `PUT/DELETE /api/faces/{id}` | `GET /api/faces/{id}/embeddings` | `POST /api/faces/{id}/embeddings` `{source_thumbnail_path, bbox?}` (returns `enrolled` / `multiple_faces` / `no_face`) | `DELETE /api/faces/embeddings/{id}` | `GET /api/faces/thumbnails/unlabeled?date=&camera_id=&limit=` | `GET /api/faces/thumbnails/event/{event_id}/path` | `GET /api/faces/embeddings/{id}/crop` | `GET /api/faces/{id}/latest-crop`
**Streams**: `GET /api/streams/{id}/live` (go2rtc info) | `GET .../live.mp4` (fMP4 proxy) | `GET .../rtsp-info` (RTSP URL)
**Recordings**: `GET .../dates` | `GET .../segments?date=` | `POST .../playback?start=&quality=&direction=` (optional `X-Bench-Id` header for end-to-end timing trace) | `GET .../playback/{session}/playback.mp4` (HTTP Range support on completed files; growing files served as plain stream) | `GET .../segment/{id}` (raw .ts) | `GET .../thumbnails?date=` | `GET .../thumb/{date}/{file}`
**System**: `GET status` | `GET storage` | `GET logs?minutes=` | `POST client-event` | `POST retention/run` | `GET/POST data-dir` | `POST data-dir/validate` | `GET version` | `GET/POST update`
**Settings**: `GET/PUT /api/settings`
**Backup**: `GET preview` | `POST create` | `GET {id}/progress` | `POST {id}/cancel` | `POST inspect` | `POST restore` | `GET restore/{id}/progress` | `POST restore/{id}/cancel`
**Clips**: `POST /api/clips` (single camera) | `POST /api/clips/composite` `{camera_ids:[int], start_time, end_time, join}` (multi-camera: `join=false` → one clip per camera; `join=true` → single synchronized side-by-side grid composite, `mode="grid"`, re-encoded HEVC NVENC w/ libx264 fallback, 960×540 cells, cameras w/o footage render as black cells) | `GET list` | `GET {id}` | `GET {id}/download` | `DELETE {id}`. `ClipExport` rows carry `mode` ("single"|"grid") + `camera_ids` JSON; grid clips set `camera_id` to the first camera so they still surface on its timeline. Single-camera exports are re-timed to constant cadence (see **Clip export timing**); `GET list` is not a real route — the app uses `GET /api/clips`.
**Motion**: `GET /api/motion/{id}/events?date=`
**Zones**: `GET/POST /api/cameras/{id}/zones` | `PUT/DELETE /api/cameras/{id}/zones/{zone_id}` — body: `{name, points: [[x,y],...]}` with x,y in [0,1]. Scripts reference via `zone_ids` in `MotionScriptConfig`.
**Storage**: `POST validate` | `POST migrate` | `GET migrate/{id}/progress` | `POST migrate/{id}/cancel` | `POST migrate/{id}/finalize` | `POST update-path`
**Health**: `GET /api/health` (returns `{app: "richiris", version}`)

## App UI Flow
- **Grid**: Click camera → select (blue ring + drag hint icon) + timeline. Click again → fullscreen. Long-press drag to reorder. Group chip bar above grid filters by camera group. Inline transitions (no Navigator.push). Feature badges under each card's gear icon summarize enabled detection features (motion/AI/face/zones/scripts) via `_FeatureBadges` in `widgets/camera_card.dart`.
- **Fullscreen**: Video + timeline + speed controls (-4x to 32x; negative speeds = server-rendered reverse stream, see Reverse playback). Stats bar (codec/res/FPS/bitrate). Refresh + bug report buttons.
- **Timeline**: CustomPainter, scroll/pinch zoom (1h-24h), minimap. Hover shows trickplay thumbnail via OverlayEntry. Red playhead from player position. 3s hold after taps. Motion events color-coded (person=amber, vehicle=indigo, animal=emerald, motion=gray).
- **Clip export**: Timeline mode (tap start/end) or Wizard (dialog with pickers). Wizard supports multi-camera selection (checkbox list + select-all) and a "Join into one video" toggle (shown when >1 camera) that produces a synced side-by-side grid composite; otherwise exports one clip per camera. Post-export clip list polls just the clips it created.

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
