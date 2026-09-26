# RichIris - Custom NVR
Update this file as code changes. **Keep it short**: rules and current state here; measurements,
incident write-ups and dated reasoning go in `CLAUDE-HISTORY.md` (not auto-loaded; it holds the
verbatim pre-2026-09-22 version of this file, so read it before overturning a rule).

> **Detection model, accuracy/speed table, tuning, rollback to the RTX 4080: [`INFERENCE.md`](INFERENCE.md).**

## Quick Reference
- **The product** ships as a Windows service (`RichIris` via NSSM, FastAPI on 8700) with
  installers. **Richard's deployment** is native systemd on the Debian box: `/opt/richiris`, data
  `/var/lib/richiris`, deploy with `scripts/deploy_box.sh` (`--with-models` also pushes ONNX
  models) then `ssh offload 'sudo systemctl restart richiris'`. Web UI: `scripts/deploy_web.sh`.
- **Health watchdog** (`services/self_watchdog.py`): probes `/api/health` every 30 s; 3 failures
  -> `os._exit(1)` so the service manager restarts it. Catches a dead listener with a live loop.
- **go2rtc**: child process, API 18700, RTSP 18554 (reported via `/api/system/status`).
- **Config**: `bootstrap.yaml` holds only `data_dir` + port. Everything else is the SQLite
  `settings` table (GUI or `GET/PUT /api/settings`).
- **Data dir**: `database/richiris.db`, `logs/`, `recordings/{camera}/` (`.ts`, named
  `Camera 1 2026-03-08 13.30 - 13.45.ts`), `thumbnails/{camera}/`, `playback/` (transient).
- **Builds**: `cd app && flutter build windows|apk|web --release`. Release: `build_release.bat`
  (PyInstaller + Flutter + nssm), installers `installer\richiris.iss` and `richiris_client.iss`
  (client-only; update detection keys on `"Client-Only"` in the asset name), `push_release.bat`.
  Dev: `setup_dev.bat`. API docs at `/docs`. Binary resolution: bundled `dependencies/` -> PATH.
- **Remote access**: `https://richiris.richardferretti.com`, LAN/VPN only (dnsmasq override,
  Caddy 403s public IPs, WireGuard clients need DNS 192.168.8.1). **No Authelia and no auth in
  the app**: the Flutter client cannot follow a login redirect, so the VPN boundary is the only
  control. Solve auth in the app before ever publishing this. Caddy uses `flush_interval -1` so
  streams are not buffered.
- **Windows client opens blank?** Suspect PowerToys FancyZones snapping the window during
  Flutter's first-frame handshake (tell: the window is not 1280x720; a renamed copy of the exe
  renders fine). Fix is FancyZones' *Excluded apps*. `ForceRedraw()` on `WM_SIZE` does not fix it.

## Architecture
```
Camera RTSP (main) <- go2rtc <- ffmpeg recorder (s1_direct relay, -c:v copy -> .ts)
                             <- httpx keepalive (s1_direct) -> LiveCache -> live clients + LivePoster
Camera RTSP (sub)  <- go2rtc <- FrameBroker (ffmpeg MJPEG @2fps) -> motion + thumbnails
                             <- httpx keepalive (s2_direct) -> LiveCache
Client live, `direct`     -> HTTP fMP4 /api/streams/{id}/live.mp4 (LiveCache)
Client live, transcoded   -> RTSP go2rtc :18554 (native) / go2rtc fMP4 proxy (web)
Client playback           -> HTTP fMP4 -> FastAPI -> ffmpeg
```

- **Each camera serves exactly 2 RTSP sessions, both to go2rtc** (verify:
  `ss -tnp dst 192.168.10.0/24`). The recorder reads go2rtc's relay (`_recording_source()`), not
  the camera, because all cameras share a 100 Mbps link; it falls back to the camera URL if go2rtc
  is down. Trade-off: a go2rtc crash restarts all recorders.
- **Recording**: one ffmpeg per camera, `-c:v copy` -> HEVC `.ts`. Watchdog kills a process with no
  file update in 5 min. Restart backoff 5 s -> 60 s.
- **Segment start times**: renaming a segment drops the seconds from its filename, so
  re-registering a renamed file recovers them from `mtime - probed_duration`
  (`_refine_renamed_start()`). Error is bounded to ~10 s, not eliminated.
- **Outage log throttling**: `_RepeatLogSuppressor` collapses a repeating failure to one line per
  5 min, keyed by failure site, reset on recovery. `StreamInfo.banner_logged` is per camera, so a
  down camera cannot re-log the ffmpeg banner on every relaunch.
- **RTSP credentials must be percent-encoded** (`services/rtsp_url.py`). go2rtc (Go `net/url`)
  rejects raw `@ ^ [ ] < > " \` and space in userinfo while ffmpeg accepts them, giving a camera
  that **records fine while live view, motion and thumbnails all fail** (only visible in
  `logs/go2rtc.log`). Encoded on the way into the DB and again when writing go2rtc.yaml; the
  Flutter form splits userinfo on the LAST `@`.
- **FrameBroker** (`services/frame_broker.py`): persistent ffmpeg per camera, sub stream MJPEG at
  2 fps. `get_latest()`, `get_fresh()`, `get_latest_jpeg()`. A reader with no frame for 30 s is
  killed and reconnects (a hung ffmpeg never closes stdout, which would silently stop motion).
- **GPU**: VAAPI on the box (`ffmpeg.hwaccel=vaapi`; `none` = software). The Windows product uses
  NVENC.

### Live view and the GOP cache (`services/live_cache.py`)
go2rtc does not replay the last GOP to a joining consumer, so every client used to wait out a
keyframe interval (2.0-4.0 s here; RTSP measured 2,464 ms to first frame). The keepalive
consumers (one per camera for `s1_direct` AND `s2_direct`, staggered, 5 s reconnect) now feed a
per-stream cache: init segment + fragments since the last two keyframes. A joining client is
written that buffer, then attached to the live feed. **0.2-0.4 s to first frame, and N viewers
cost one go2rtc consumer.** `GET /api/streams/cache` shows `warm`/`fragments`/`subscribers`/`age_s`.

- **`direct` quality uses it on every platform.** Transcoded tiers are NOT cached (go2rtc starts an
  ffmpeg on demand, ~11 s cold): native plays those over RTSP, web through the go2rtc proxy.
- **hvcC repair is load-bearing (`_repair_hevc_init`).** go2rtc builds the hvcC from the SDP;
  cameras 42/44/45/46/47 send no parameter sets there, so go2rtc writes a canned 2560x1440
  level-5.1 header while the streams are really 3840x2160. Software decoders adopt the in-band SPS;
  **Android MediaCodec trusts the hvcC and renders colour noise.** The cache rebuilds hvcC +
  dimensions from the first keyframe's in-band VPS/SPS/PPS. **If a tile ever shows colour noise,
  `ffprobe` the served `live.mp4` and compare its size with the in-band SPS before touching the
  client.** The legacy go2rtc proxy path still serves the unrepaired init.
- **Backlog is per client: `live.mp4?backlog=short|full`** (default `full`). The cache holds TWO
  keyframes. `short` starts at the newest (0-1 GOP behind live) and is only for a player that sets
  `vd-lavc-o=flags=+low_delay`, i.e. native (`player_tuning_io.dart`). `full` starts at the older
  one and is for browsers/ffmpeg: without that flag libavcodec wants about a full GOP in hand
  before it shows anything (2,379 ms at a 1-fragment backlog vs ~390 ms with the flag). Do not
  make `KEEP_KEYFRAMES = 1` global.
- `low_delay` is safe because live streams are IPPP. **Do not set it on playback players**
  (recordings could carry B-frames). It was once wrongly blamed for the Android corruption above.
- **Keyframe detection must be video-track aware** (`_parse_video_track_id`): Front Door carries
  audio, and audio samples are all sync samples, which would reset the GOP buffer constantly.
- `-fflags nobuffer` and a short `analyzeduration` both make startup WORSE.
  `demuxer-lavf-o=fpsprobesize=0` is the one demuxer option worth keeping.
- Keyframe interval is a camera setting and sets the latency floor: 2.14 s on most, 4.01 s on
  Front Door's sub stream (worth fixing in the camera UI).
- Native player: media_kit (libmpv), 5 s cache, hw decoding, stall detection 10 s, backoff
  500 ms -> 10 s.

### Poster frames
Grid and fullscreen paint `GET /api/cameras/{id}/latest-frame.jpg` over the video and cross-fade
it out 400 ms after the player reports playing.
- **`?stream=s1` serves a MAIN-stream poster** (`services/live_poster.py`): a 1280-wide JPEG
  pre-rendered from the cache's newest keyframe by a short-lived VAAPI ffmpeg, falling back to the
  FrameBroker (sub stream) frame. It exists because the sub stream is 4:3 on five cameras whose
  main is 16:9, so the poster visibly changed shape. `StreamApi.posterUrl(stream:)` asks for the
  stream the player is about to show.
- **`REFRESH_SECONDS = 10` is a CPU budget**: each render is ~0.22 CPU-s even on VAAPI, so 8
  cameras cost ~0.18 core (0.35 at 5 s; software doubles it, hence VAAPI is only abandoned after 5
  consecutive failures). Pre-rendered, not on demand: a cold launch asks for all eight at once.
- **"Playing" = `position` has advanced >= 500 ms past the first value seen** (`_firstPosition` in
  `live_player.dart`). Never gate on `stream.width` (published when parameter sets parse, long
  before a frame), and not on the first non-zero position either (fMP4 timestamps do not start at
  zero). Both faded tiles through black. A 5 s width-armed timer is only a safety net. A reused
  mid-stream player reports `playing` from `player.state.width`.
- The poster URL is cache-busted once per grid, not per build. Rotation matches the video.

### Playback
- All qualities go through PlaybackManager -> fMP4 (`frag_keyframe+empty_moov`). Direct =
  `-c copy` remux with `-noaccurate_seek -ss N` (lands on the nearest keyframe). Others = hardware
  HEVC transcode. **No client-side `player.seek` after open**: PTS 0 already is the chosen time;
  `seek_seconds` is metadata. HTTP Range only on completed files; growing files are a plain 200
  stream. Sessions: 30 s idle cleanup, ONE per camera (same-camera eviction).
- **Reverse playback is rendered on the SERVER** (`services/reverse_playback.py`):
  `direction=backward` returns an fMP4 that runs backwards, which the client plays forward at
  `setRate(|speed|)`. 8 s chunks, GPU decode, 720p/10 fps H.264, two chunks concurrently (~10x
  realtime), concatenated newest-first into one muxer. A chunk that kills the VAAPI decoder
  ("Could not find ref with POC") is retried in software. **Rendering is paced to consumption**
  (`MAX_LEAD_BYTES`); every streamed chunk `touch`es the session; a backward session tolerates 60 s
  of silence. On `completed` the client asks for `segment_start - 1ms` backward.
- **Grid/fullscreen session fight**: native keeps the grid mounted (Offstage) behind fullscreen,
  and one-session-per-camera meant each evicted the other every ~1.5 s. The grid ignores
  `completed` for `fullscreenCameraId`, and `PlaybackRef.ownedSession` lets `_exitFullscreen`
  restart the evicted tile.
- Forward skip near "now": `start_playback_session` falls back to the in-progress segment and
  clamps the seek 3 s inside the probed duration.
- Android memory: ~1.25 GB PSS on the grid, ~1.7 GB in fullscreen (EGL buffers for eight 4K
  decoders). A LOW_MEMORY kill is headroom, not a leak; the lever is pausing offstage grid players.

### Web client (`flutter build web`, same `app/` source)
Caddy serves `build/web` from `/opt/stacks/caddy/srv/richiris` and proxies `/api`, `/docs`,
`/openapi.json` on the same origin, so the client needs no server URL and makes no CORS request.
- **`config/platform_info.dart`: use `isWeb`/`isAndroid`/`isWindows`, never bare `Platform.*` in
  shared code.** `dart:io` COMPILES on web and throws only when a getter is read, so it is a
  runtime trap with a minified stack.
- `services/player_tuning.dart`: the mpv knobs cast to `NativePlayer` (a compile error on web);
  no-ops in the browser.
- `widgets/web_live_video.dart`: live video bypasses media_kit (its web `Video` never attaches the
  element) and plays the fMP4 endpoint in a plain `<video>`. **HEVC: Chrome/Edge/Safari work,
  Firefox does not.** The web grid defaults to the SUB stream.
- Two compositing traps: a platform view inside `InteractiveViewer` renders invisible
  (`ZoomableVideo` passes its child through on web), and the grid is UNMOUNTED behind fullscreen
  on web (Offstage would keep 8 streams open). `WebLiveVideo.elevate` z-indexes the fullscreen
  view; an `onPause` listener re-plays the element after Flutter re-parents it.
- Not usable on web (guarded): LAN scan, auto-updater, Windows-service panel.

### Motion + AI detection
Motion reads FrameBroker every 0.5 s: weighted-avg baseline, GaussianBlur(21,21), threshold 25;
sensitivity 0-100 -> area threshold `(101-s)*0.05%`. AI confirms with 2 detections in 3 frames +
bbox movement >= 1.5% of the diagonal. Categories: persons (COCO 0), vehicles (1,2,3,5,7), animals
(14-23). Per-camera toggles + confidence. `motion_scripts` JSON with per-category triggers; 10 s
cooldown. Script env: MOTION_CAMERA, MOTION_TIME, MOTION_INTENSITY, DETECTION_LABEL,
DETECTION_CONFIDENCE, FACE_NAMES. Scripts need a full interpreter path.
- **Inference is fully local** (`ai.remote_url=''`): yolo11n@320 ONNX, CPU, `LOCAL_CPU_THREADS = 1`
  on purpose, ~26 ms end to end. `ai.local_model` switches model by filename. **The parser is chosen
  from the ONNX output shape**, not the filename (RT-DETR `[1,300,84]` vs YOLO `[1,84,N]` + NMS).
  The iGPU/OpenVINO route was measured and rejected. Details and rollback: `INFERENCE.md`.
- **Region-of-interest cropping** (`ai.region_crop_enabled`, default on): detection runs on a
  square crop around the motion, or the full frame when that would cover >= 90%. **Boxes come back
  in CROP space and are offset into frame space right after the detect call; everything downstream
  assumes frame coordinates.**
- Do not use `name` as a logger `extra={}` key (collides with LogRecord). Use `camera_name` etc.

### Scripts (`Camera Light Control/`)
- `lamp_control_working.py <on|off|auto> <ip>`: camera white light via `day_night_mode`. GET the
  whole image-settings blob and POST it ALL back (the firmware replaces, not merges). **Basic auth,
  not digest** (these cameras lose the POST body on the digest challenge).
- `email.py "<camera>"` = the Intruder Alert, 23:00-06:00. It fires the floodlight burst BEFORE
  sending; a floodlight failure never blocks the email. The email takes 5-6 s (SMTP).
- **The 200W floodlight is a Shelly 1 Mini Gen3 at `192.168.8.100`, driven by `floodlight.py` from
  inside `email.py`.** There is no separate floodlight rule. It tries the Shutters backend
  (`127.0.0.1:8000/lights/100/set`) then the Shelly directly; the DEVICE counts the burst down
  (`toggle_after`, `FLOODLIGHT_BURST_SECONDS`, default 60), so the script's `off_delay` is
  irrelevant. Tuya is gone entirely.
- **`email.py` and `floodlight.py` shadow the stdlib `email` package**: both strip their own dir
  from `sys.path` before importing urllib/smtplib, and `email.py` loads `floodlight` by file path.
- Camera .45's lamp writes usually time out and do not apply (heavy packet loss, physical fault).

### Detection zones
Per-camera polygon masks, points normalized [0,1], opted into per script via `zone_ids` (empty =
whole frame). Masks cached per (zone, frame shape) in `services/zone_mask.py`; CRUD invalidates.
Motion-only scripts use `motion_in_mask`; detection scripts use `bbox_in_mask` on the bbox's
bottom-centre pixel. A script whose zone mask is unavailable **fails closed**. Deleting a zone
prunes `zone_ids`. `CameraResponse.zone_count` feeds the grid badge.

### Facial recognition: DISABLED
Kill switch `ai.face_enabled` (defaults False in code). Re-enabling needs BOTH that setting and
the per-camera `face_recognition` flags. **The per-camera flag alone does not stop face work**: its
off branch still ran SCRFD on every person event, which is why the global switch exists. Pipeline
when on: SCRFD -> ArcFace 512-D -> cosine match against `face_embeddings`; enrol via
`POST /api/faces/{id}/embeddings`.

### Video quality
Two selectors: Stream (Main/Sub, live only) and Quality (Direct/High/Low/Ultra Low), separate
prefs for live and playback. Live streams are baked into go2rtc.yaml at startup. High aliases to
Direct for HEVC sources. Low = 1/8 bitrate, Ultra Low = 1/16 + 15 fps + short GOP.

### Clip export timing
Camera `.ts` is badly VFR and ffmpeg guesses the frame rate, so `export_clip` runs **two copy
passes, no re-encode**: (1) stage with concat + OUTPUT-side `-ss`/`-t` (input-side `-ss` on concat
ignores `-t`); (2) re-time with `-bsf:v setts=ts=N*{ticks}`, `-video_track_timescale 90000`,
`+faststart`, `-tag:v hvc1`. Ticks come from the STAGED file's moov (`_probe_mp4_cadence()`),
never from sampling the source. If pass 2 fails the staged copy is promoted.
**Known, unfixable here: the window lands a few seconds early** (camera stream latency, varies
3-10 s on Front Door). **`-use_wallclock_as_timestamps 1` does NOT fix it; do not retry it.** Ask
for a wider window.

### Timezone
Recordings are stored as **local time without timezone**. The frontend must NOT use
`.toISOString()`. `created_at`/`updated_at` use `default=local_now`, not `func.now()` alone
(SQLite's `CURRENT_TIMESTAMP` is UTC). `init_db()` runs before settings load, so migrations read
the tz from the settings table, not `get_tz()`.

## Project structure
```
backend/app/  main, config, logging_config, database, models, schemas
  routers/    backup, cameras, clips, groups, recordings, settings, storage, streams, system, motion, zones
  services/   backup, ffmpeg, stream_manager, go2rtc_client, go2rtc_manager, recorder, clip_exporter,
              playback, reverse_playback, settings, thumbnail_capture, retention, storage_migration,
              motion_detector, object_detector, update_checker, frame_broker, benchmark, zone_mask,
              rtsp_url, live_cache, live_poster, self_watchdog
app/lib/      main, app, theme, config/, models/, services/, screens/, widgets/, utils/
installer/    richiris.iss, richiris_client.iss, download_deps.ps1
dependencies/ (gitignored) ffmpeg, ffprobe, nssm, go2rtc, models/*.onnx     data/ (gitignored)
```

## Code style
Small focused functions (~10-20 lines). `logger = logging.getLogger(__name__)`, structured fields
via `extra={}`. Root logger INFO, `app.*` DEBUG by default, httpx/httpcore WARNING.

## API endpoints
**Cameras**: `GET/POST/PUT/DELETE /api/cameras` (`?purge_data=true`) | `PUT reorder` | `POST discover` | `POST scan` | `POST discover_batch` | `POST snapshot` | `GET {id}/latest-frame.jpg?max_age=&stream=s1|s2` | `POST test-script`
**Groups**: `GET/POST /api/groups` | `PUT/DELETE {id}` | `POST {id}/bulk` (`enable|disable|arm_motion|disarm_motion`)
**Faces**: `GET/POST /api/faces` | `PUT/DELETE {id}` | `GET/POST {id}/embeddings` | `DELETE embeddings/{id}` | `GET thumbnails/unlabeled` | `GET thumbnails/event/{event_id}/path` | `GET embeddings/{id}/crop` | `GET {id}/latest-crop`
**Streams**: `GET /api/streams/{id}/live` | `GET .../live.mp4?stream=&quality=&backlog=short|full` | `GET /api/streams/cache` | `GET .../rtsp-info`
**Recordings**: `GET .../dates` | `GET .../segments?date=` | `POST .../playback?start=&quality=&direction=` (`X-Bench-Id` optional) | `GET .../playback/{session}/playback.mp4` | `GET .../segment/{id}` | `GET .../thumbnails?date=` | `GET .../thumb/{date}/{file}`
**System**: `GET status` | `GET storage` | `GET logs?minutes=` | `POST client-event` | `POST retention/run` | `GET/POST data-dir` | `POST data-dir/validate` | `GET version` | `GET/POST update`
**Settings**: `GET/PUT /api/settings` | **Health**: `GET /api/health` -> `{app: "richiris", version}`
**Backup**: `GET preview` | `POST create` | `GET {id}/progress` | `POST {id}/cancel` | `POST inspect` | `POST restore` | `GET restore/{id}/progress` | `POST restore/{id}/cancel`
**Clips**: `GET/POST /api/clips` | `POST /api/clips/composite` `{camera_ids, start_time, end_time, join}` (`join=true` = one synced grid composite, 960x540 cells, black cells for cameras without footage) | `GET {id}` | `GET {id}/download` | `DELETE {id}`. Rows carry `mode` + `camera_ids`.
**Motion**: `GET /api/motion/{id}/events?date=`
**Zones**: `GET/POST /api/cameras/{id}/zones` | `PUT/DELETE .../{zone_id}`, body `{name, points: [[x,y],...]}`
**Storage**: `POST validate` | `POST migrate` | `GET migrate/{id}/progress` | `POST migrate/{id}/cancel` | `POST migrate/{id}/finalize` | `POST update-path`

## App UI flow
- **Grid**: tap selects (blue ring + timeline), tap again -> fullscreen (inline, no Navigator.push).
  Long-press drag reorders. Group chip bar filters. `_FeatureBadges` under each gear icon.
- **Fullscreen**: video + timeline + speed bar (-4x to 32x), stats bar, refresh + bug report.
  Prev/next camera via edge chevrons or Up/Down/PageUp/PageDown (`FullscreenScreen` is keyed on
  camera id). Back/forward 30 s (J / L); a forward skip landing within 3 s of now goes live.
- **Timeline**: CustomPainter, zoom 1h-24h, minimap, trickplay hover, 3 s hold after taps. Events:
  person amber, vehicle indigo, animal emerald, motion grey; known face cyan, unknown rose.
- **Clip export**: timeline mode or wizard (multi-camera + "Join into one video").

## Key dependencies
Backend: fastapi, uvicorn, sqlalchemy, aiosqlite, pyyaml, structlog, httpx, opencv-python-headless,
numpy, onnxruntime(-directml on Windows). App: media_kit, dio, shared_preferences. External: go2rtc,
ffmpeg, NSSM, PyInstaller, Inno Setup.

## Android: live tiles froze after returning from background (fixed 2026-09-25)

After recents / split screen / another app, Android kills the Flutter render surface and the mpv textures come back frozen on the last frame while mpv keeps decoding (nothing reconnects because every health signal looks fine). `_MainNavState.didChangeAppLifecycleState` (app/lib/app.dart) now disposes the pooled live players on `resumed` after `paused`/`hidden` and bumps `_liveEpoch`, which keys `HomeScreen` and `FullscreenScreen`, so everything remounts and the server poster covers each tile until live video lands. Cost: a grid in playback mode returns to live. Same bug and fix as RichRD's viewer.
