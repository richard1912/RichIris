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
- **Icons missing on Windows only?** The Windows build can reuse a stale tree-shaken `MaterialIcons-Regular.otf`
  (2026-11-17: the font in `app/build/flutter_assets/fonts/` was two months old, so the new PTZ arrows had no glyph
  while Android was fine). Delete `app/build/flutter_assets`, the Release `data/flutter_assets` and
  `.dart_tool/flutter_build/*/release_bundle_windows-x64_assets.stamp`, rebuild, and check the font's date.
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
- **Every ffmpeg/ffprobe that reads a recorded `.ts` must pass `TS_INPUT_ARGS`** (`services/ffmpeg.py`, `-analyzeduration 20M`). The cameras expose u-law audio, the recorder copies `0:a?`, and mpegts cannot carry pcm_mulaw as audio, so it lands in the file as a private DATA stream (PID 0x101, 25 packets/s, unplayable). At night the video drops to 7 fps and can sit more than 5 s of that stream's timestamps behind it, so ffmpeg's default analysis window closes with no SPS seen and the remux dies with "dimensions not set": every playback of that segment 500s. Hit 2026-10-11 from 18:00 on Front North 42 and Alfresco 44 (19 segments each). `-probesize` does not help; the longer window costs nothing on normal files. Open follow-up: record that audio as AAC or drop it, so the data stream stops being written at all.
- **Recording**: one ffmpeg per camera, `-c:v copy` -> HEVC `.ts`. Watchdog kills a process with no
  file update in 5 min. Restart backoff 5 s -> 60 s.
- **The watchdog also kills a recorder over `RECORDER_MAX_MEMORY_MB` (1024, RAM + swap from `/proc`)** (2026-11-19).
  A recorder is ~60 MB; twice one ran away to ~12 GB while still writing its file (Garage 2026-09-23, Front Door
  2026-11-19 07:05 to 10:29), filled swap and stalled the whole box, so every feed sat on "Reconnecting". The
  runaway also stops rolling segments (one 3.9 GB `rec_08-16-22.ts`). Cause in ffmpeg not found. The watchdog used
  to exit for good the first time it saw its recorder dead; it now keeps running for the life of the stream.
- **And a recorder that stops rolling segments** (`SEGMENT_OVERRUN_SECONDS`, 180 s past the due clock cut, 2026-11-19).
  Front Door reconnected at 00:57 and from then the recorder's relay session carried AUDIO ONLY (the file: 12 min of
  video, 6.4 h of AAC). With no video keyframe the segment muxer never cuts, the file keeps growing so it is not
  "stale", and seven hours of video were missing with nothing logged. At 08:16 the relay hit EOF, ffmpeg looped on
  "Failed reading RTSP data: End of file" and that is the state that balloons memory (same signature as Garage in
  September). Why the relay drops video after a camera reconnect is not found (only Front Door and Garage, the two
  cameras with native AAC, have shown it). A segment named with an end hours after its start is the tell.
- **Segment start times**: renaming a segment drops the seconds from its filename, so
  re-registering a renamed file recovers them from `mtime - probed_duration`
  (`_refine_renamed_start()`). Error is bounded to ~10 s, not eliminated.
- **Outage log throttling**: `_RepeatLogSuppressor` collapses a repeating failure to one line per
  5 min, keyed by failure site, reset on recovery. `StreamInfo.banner_logged` is per camera, so a
  down camera cannot re-log the ffmpeg banner on every relaunch.
- **Direct camera URLs get `#backchannel=0`** (`_direct_source()` in `services/go2rtc_client.py`,
  since 2026-11-17). go2rtc otherwise claims every camera's ONVIF two-way-talk channel, which on the
  Dahua SDT3E410 (PTZ 49) occupies the speaker so the camera refuses its own siren and alarm tones
  for as long as go2rtc is connected. RichIris has no talk feature; if one is ever added, enable the
  backchannel per camera, not globally.
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

### RIV recordings (`services/riv/`, since 2026-11-24)
RichIris's own container. The live cache's fMP4 (one `moof+mdat` per frame from go2rtc) is stored as it arrives,
with a keyframe index, so playback is served like `live.mp4`: no ffmpeg, no sessions, exact seeks, continuous across
files and into live. Plan + reasoning: `~/.claude/plans/wild-gliding-starfish.md` on RICHARD-PC.
- **Per camera `cameras.rec_format`**: `ts` (ffmpeg MPEG-TS only), `shadow` (both), `riv` (RIV only, no ffmpeg
  recorder; `_riv_watchdog` restarts the main keepalive after 60 s without a VIDEO frame written). Set it with
  `PUT /api/cameras/{id} {"rec_format": ...}` (restarts the stream and re-registers go2rtc). **State 2026-11-24:
  PTZ 49 + PTZ Panorama 49 `riv`; Front Door (H.264+AAC) + Alfresco 44 (G.711) `shadow` to soak; the rest `ts`.**
  Next: after a clean week of `riv_verify` on the shadow pair, move them and then the others to `riv` (one change
  each); do NOT shadow everything at once: retention is capped at 5,000 GB, and doubling the data halves history.
- **Format** (`format.py`): append-only records `RIVR|type|flags|track|len|arrival_ms|media_ts|crc32|payload`
  (HEAD json, INIT = ftyp+moov after `_repair_hevc_init`, FRAG = one moof+mdat verbatim, INDEX json trailer + 16-byte
  footer). Every go2rtc connection is an "epoch" (timestamps restart at 0; a reconnect = new INIT). Wall clock per
  frame = media time + sliding-window min of (arrival - media) (`WallClock`, deterministic, so crash recovery
  rebuilds the identical index). No trailer = crash: `scan` stops at the first torn/bad-CRC record; `recover_file`
  truncates and appends the trailer at startup. `INIT + FRAGs` of one epoch is a plain fMP4 (escape hatch).
- **Files**: one per camera per stream (s1 AND s2) per 15-min window, cut on a keyframe, named from the first byte
  (`PTZ 49 2026-11-24 20.11.31 s1.riv`), written straight into the ARCHIVE tree (no hot->archive copy). Table
  `riv_files` (separate from `recordings` on purpose: legacy readers run ffmpeg on .ts). Retention covers both.
  ~1.3 GB/h for PTZ 49 s1, ~2.2 GB/h panorama, ~0.2 GB/h per s2.
- **Writer** (`writer.py`): `LiveCache.set_sink` -> `RivManager.sink` -> per-stream bounded queue -> its own thread
  (a slow disk never stalls live view; overflow drops and counts). `GET /api/riv/stats`.
- **Serving** (`serve.py`): `find_run` = continuous footage from the keyframe at/before `start` (a gap > 30 s ends
  the run); `stream_fmp4` rewrites only mfhd + tfdt, re-anchors on wall time across epochs/files, follows the live
  file. `POST /recordings/{id}/playback` returns it for RIV footage with the SAME JSON contract (`segment_start` =
  exact keyframe time, `seek_seconds` 0, `has_more` false while it reaches live, `format: riv`). Reverse = the ffmpeg
  renderer fed keyframe-aligned chunks (`reverse_chunks`, no `-ss`, so no mid-GOP decoder failures). Transcoded
  qualities = the same presets reading the RIV URL. 16x/32x = keyframe-only stream (`speed=`), position p = start +
  p*speed. Timeline hover = `GET /api/riv/{id}/frame.jpg?at=` (the exact keyframe, sub stream).
- **Other readers**: clip export (single copy pass, `hvc1` tag only on HEVC), grid clips (trim filter), guard backfill
  (`services/footage.py` picks RIV or .ts), `people_snapshots.py` (`riv_covering`, reads the RIV URL over HTTP).
- **G.711 cameras get AAC**: for `rec_format != ts`, go2rtc gets a second source
  `ffmpeg:<stream>_s1_direct#audio=aac`; it only starts when the camera's own audio cannot go into MP4 (verified:
  Alfresco 44 starts it, Front Door does not).
- **Tests**: `cd backend && python -m pytest tests -q` (real go2rtc captures in `tests/fixtures`). Shadow check:
  `scripts/riv_verify.py` nightly 04:10 (`/etc/cron.d/richiris-riv-verify`, log `logs/riv_verify.log`).
- Known gaps: camera rename does not move RIV files (nor archived .ts, pre-existing); `/clips` grid trims on the
  decoded stream; Android APK not rebuilt with the client changes yet.

### Live view and the GOP cache (`services/live_cache.py`)
go2rtc does not replay the last GOP to a joining consumer, so every client used to wait out a
keyframe interval (2.0-4.0 s here; RTSP measured 2,464 ms to first frame). The keepalive
consumers (one per camera for `s1_direct` AND `s2_direct`, staggered, 5 s reconnect) now feed a
per-stream cache: init segment + fragments since the last two keyframes. A joining client is
written that buffer, then attached to the live feed. **0.2-0.4 s to first frame, and N viewers
cost one go2rtc consumer.** `GET /api/streams/cache` shows `warm`/`fragments`/`subscribers`/`age_s`.

- **Never pass `chunk_size` to httpx `aiter_bytes` on a live feed** (2026-11-19): httpx then holds bytes back until a
  full chunk exists, so the keepalive released each stream to the cache in 64 KB lumps, 0.3 to 1 s apart on a
  ~2.8 Mbit/s camera (go2rtc itself: 165 ms max). Live looked laggy/frozen, worst on quiet scenes. Now `aiter_bytes()`
  in the keepalive and in the go2rtc proxy; measured max gap ~150 ms on every camera.
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
- **PTZ cameras chase the live edge in fullscreen** (`_chaseLiveEdge`, 2026-11-16): a player that joins
  at the newest keyframe stays up to one GOP behind for good, which on a PTZ is the delay between
  pressing a button and seeing the move. Every 300 ms the screen reads mpv's `demuxer-cache-duration`
  and plays at 1.25x (2x above 1 s) until under 0.2 s is buffered. Native only, `hasPtz` only, and the
  rate is reset on dispose because the player may be the grid's. PTZ 49's GOP was also cut 50 -> 25.
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
- **The newest playback request per camera wins** (`_latest_playback_req` in `routers/recordings.py`, 2026-11-14).
  Requests are handled concurrently, so a slow older request used to start late and evict a NEWER one's session,
  which then 500'd on screen. A superseded request now gets **409**, never starts ffmpeg, and fullscreen retries a
  409 while it is still the newest thing asked for. The trigger was the timeline: `ScaleGestureRecognizer` ends and
  restarts the gesture whenever the pointer count changes, so one pinch fired several seeks; `_multiTouch` in
  `timeline_widget.dart` (set by the outer `Listener`, which sees pointers first) makes any two-finger touch zoom-only.
- **A forward request in the last 1.5 s of a finished segment starts at the NEXT segment** (`SEGMENT_TAIL_SECONDS`,
  2026-11-18). The grid continues on its master clock, which can land a few ms before a camera's segment end. Two
  freezes came from serving that tail: a closed segment gave an empty fMP4, so the player never reported `completed`
  (PTZ 49 stopped at 06:06 after playing all night); and a just-rolled segment, whose `in_progress` flag the scanner
  only clears a minute or two later, was clamped 3 s back, so the last seconds replayed in a loop. **"Still being
  written" in the resolver means `in_progress` AND no later segment exists** (`growing`), never the flag alone.
  `has_more` is "any segment starting after this one", because a probed end can overlap the next start by up to 1 s.
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
- **Wide frames are also detected as tiles** (`compute_tiles`, 2026-11-18). The panorama sub stream is 1344x368; the
  motion crop is capped at the frame height, so any wide motion fell back to the whole frame letterboxed into 320,
  where a person across the street is ~6 px wide. Three people walked the street between 21:32 and 22:07 on
  2026-11-18, the PTZ tracked each, and the panorama raised no person. In that fallback, frames at least 1.6x wider
  than tall now also run overlapping square tiles over the columns that changed, merged by IoU (the 22:07 walker:
  nothing full-frame, 0.72 to 0.79 tiled). Up to 5 extra inferences (~300 ms) per such frame.
- **A frame where >= 30% of pixels change at once is a lighting change, not motion** (`LIGHT_CHANGE_PCT`): the
  baseline snaps to it and re-settles at the startup rate, and no detection runs on that frame. Without it the slow
  baseline (alpha 0.01) read a light switching as motion everywhere for minutes, every parked car overlapped the
  mask and counted as moving, and the Floodlight Zone vehicle rule re-fired the front lights every ~137 s all
  night (120 s max event + cooldown). It also quiets the PTZ lens, whose own movement did the same.
- **An event starts when its thumbnail frame was captured** (2026-11-18). The thumbnail is the best-scoring frame
  seen while waiting for confirmation; the event used to be stamped at confirmation instead, and that frame had no
  age limit, so thumbnails were 5 to 74 s older than the moment a click played (measured on four events against the
  burnt-in clocks). The pending frame now carries its capture time, which becomes `start_time`, and is replaced once
  older than `PENDING_MAX_AGE` (4 s). Separately the detection (sub) stream arrives 2 to 3.5 s after the same moment
  is in the recording, and direct playback snaps to a keyframe that can be a second or two late, so the timeline's click-on-event lead is 5 s (`_onTimelineTap`), not 0.5 (verified 2026-11-19 on a car: thumbnail clock 01:34:38, a 3 s lead started at :39, a 5 s lead starts before it). Events recorded
  before this change keep their old mismatch.
- Do not use `name` as a logger `extra={}` key (collides with LogRecord). Use `camera_name` etc.

### Scripts (`Camera Light Control/`)
- `lamp_control_working.py <on|off|auto> <ip>`: camera white light via `day_night_mode`. GET the
  whole image-settings blob and POST it ALL back (the firmware replaces, not merges). **Basic auth,
  not digest** (these cameras lose the POST body on the digest challenge).
- `email.py "<camera>"` = the Intruder Alert, 22:00-06:00 (`NOTIFY_START_HOUR`; start was 23:00, then 19:30 from 2026-11-17, then 22:00 from 2026-11-20). The Loitering Alert notifies from 22:00 too (its lights still run from sunset); before 22:00 nothing rings the phone. A persons "on" rule on eight cameras (PTZ Panorama 49 added 2026-11-18 and added to `INTRUDER_FRONT_CAMERAS`; not on the PTZ zoom lens, Garage or Prius Cam), whole frame on all of them (Front Door's zone 3 was removed from this rule on 2026-11-17, Richard wants no zone restriction). On 2026-11-17 20:04 an unidentified batch API update cleared `persons` on every one of these rules, silently disabling the alert until it was re-enabled at 22:29: if it stops firing, check that flag first. It fires the floodlight burst BEFORE
  sending; a floodlight failure never blocks the email. The email takes 5-6 s (SMTP). Since
  2026-11-08 it attaches the triggering camera's main-stream poster (`latest-frame.jpg?stream=s1`,
  pre-rendered so it is free) plus the other front cameras' when the trigger is a front camera
  (`INTRUDER_FRONT_CAMERAS`); a poster or API failure just means fewer attachments.
- **The subject prefixes "Intruder Alert:" and "Loitering Alert:" are load-bearing**: RichMacro on
  the Pixel 10 (Richard's own app, `Misc Projects\RichMacro`) matches Gmail notifications containing
  them and rings on the alarm stream, through silent/vibrate, until stopped (verified 2026-11-08,
  ~4 s from send to alarm). Gmail does notify for self-sent mail. ntfy was rejected for this.
- **The phone alarm no longer depends on Gmail** (2026-11-19). Gmail push stopped reaching the Pixel that day
  (alerts were sent and received by Gmail, the phone showed nothing; reboot and clearing Gmail's data did not fix
  it) and nothing rang all evening. `email.py` and `loitering.py` now first call `phone_alert.py` `push(subject)`,
  which POSTs to `/api/alerts` (`routers/alerts.py`: in memory, POST from the box only, `GET /api/alerts/wait`
  long-poll, `GET /api/alerts` shows `waiters`). RichMacro 1.2 holds that long-poll and sounds the alarm in ~0.2 s;
  the subject prefixes still pick the tone. The email remains the backup and the record. If the phone is silent,
  check `waiters` is 1 first. Ring by hand: `phone_alert.py "Intruder Alert: TEST"`.
- **The 200W floodlight is a Shelly 1 Mini Gen3 at `192.168.10.103` (Shutters light code 100), driven by `floodlight.py` from
  inside `email.py`.** There is no separate floodlight rule. It tries the Shutters backend
  (`127.0.0.1:8000/lights/100/set`) then the Shelly directly; the DEVICE counts the burst down
  (`toggle_after`, `FLOODLIGHT_BURST_SECONDS`, default 60), so the script's `off_delay` is
  irrelevant. Tuya is gone entirely.
- **Automatic floodlight bursts are DISABLED since 2026-11-19 20:10** (Richard is testing whether the picture is
  clearer without it: glare). Switch = the flag file `/var/lib/richiris/floodlight.disabled`, checked in
  `floodlight.burst()`, which every rule goes through; `rm` it to re-enable. Manual on/off still works. The camera
  white lamps and the PTZ police lights still fire.
- **Shutter tests 2026-11-19 (faces smear at night, both cameras left on auto)**. Front South 47: the image blob's
  `exposure` is a FIXED shutter (0 auto, 6 = 1/25, 8 = 1/50, 10 = 1/100; table in the camera's
  `view/image_setting.html`), not a cap: unlit, 1/25 is already near black, so it only works while the floodlight is
  on (1/25 and 1/50 are clean then, 1/100 dim). The lamp scripts post the whole blob from a cache, so a changed
  `exposure` must also go into `lamp_settings_cache.json`. PTZ 49: `VideoInExposure` is Mode 0 (auto, 0 to 40 ms, so
  never slower than 1/25); Mode 4 with `Value2` 20 or 10 ms holds 1/50 or 1/100 but the picture goes dark with the
  illuminators off. Original: `VideoInExposure.before-shutter-20261119.json` in the backup dir.
- **Zones are attached to the Intruder Alert and Loitering Alert rules on the front cameras again** (2026-11-20,
  Richard: only notify for people around the house): Front South 47 zone 1, Front North 42 zone 2, Front Door zone 3.
  PTZ Panorama 49 has no zone yet (Richard will draw one; its rules stay whole-frame until then), the backyard
  cameras and Bins 43 are whole-frame on purpose. The Floodlight Zone rules are unchanged. Earlier notes in this file
  saying these rules carry no zone describe 17 to 19 Nov. Previous config:
  `/opt/richiris/secrets/motion_scripts.before-zones-20261120.json`. The loitering watcher still counts any person
  event on the camera once an in-zone person has started it.
- **`email.py` and `floodlight.py` shadow the stdlib `email` package**: both strip their own dir
  from `sys.path` before importing urllib/smtplib, and `email.py` loads `floodlight` by file path.
- Camera .45's lamp writes usually time out and do not apply (heavy packet loss, physical fault).
- **`floodlight_zone.py "<camera>"` = the zone front lights** (added 2026-11-14; since 2026-11-17 the rule also has `vehicles` on, so a MOVING car lights the front too: RichIris only confirms a detection whose box moves, so parked cars do not fire it; Front Door has vehicle detection off, so in practice Front South 47, Front North 42 and PTZ Panorama 49 do this; the same rule, persons + vehicles, no zone, was added to PTZ Panorama 49 on 2026-11-17, and deliberately NOT to the PTZ lens, whose own movement would make parked cars look like moving ones): any person inside the camera's
  detection zone, sunset to sunrise, switches on ALL the front lights (Shelly floodlight + the .42/.47 camera white
  lamps) for `FLOODZONE_BURST_SECONDS` (60), no email. Configured as a persons "on" script. NOTE (2026-11-17): only Front Door's Floodlight Zone rule actually
  carries a zone (3); on Front South 47 and Front North 42 `zone_ids` is empty for this rule and for the Intruder Alert,
  so both fire on a person anywhere in frame (zones 1 and 2 exist but are not attached). Where a zone is set RichIris does the zone test. It is just
  `loitering.is_night()` + `loitering.lights_on(seconds)` (loaded by path), so both rules share one lamp
  baseline/restore state in `/var/lib/richiris/loitering/` and log to `loitering.log` (lines tagged "zone").
  `lights_on` never shortens a longer burst already running: it reads the Shelly first (a new `toggle_after`
  REPLACES the timer) and only moves `lamp_restore_until` later. Test knobs `FLOODZONE_FORCE=1`,
  `FLOODZONE_DRY_RUN=1`, `--sun [ISO]`; a dry run does not consume the 20 s lights cooldown.
- **`backyard_lights.py` = backyard lamps on all night** (added 2026-11-16, `richiris-backyard-lights.timer` every
  5 min, units in `scripts/systemd/`): from sunset to sunrise (`loitering.is_night()`) it puts the Backyard 2 46
  white lamp back to "on" whenever it is not (Backyard 45 was in it for one day and taken out on 2026-11-17 at
  Richard's request; it has its per-person "Light Controls" rule back), so a manual off in the Shutters app
  lasts at most 5 min; after sunrise it hands the lamps it holds back to auto once. The per-person
  "Light Controls" motion rule was REMOVED from Backyard 2 46 (its "off" 10 s after each person would fight
  this); do not re-add it. Alfresco 44 is not included. `--status` prints the gate and lamp modes.
- **`loitering.py "<camera>"` = the Loitering Alert** (added 2026-11-08 after the brick attack at
  20:59 on 2026-11-07, two hours before the Intruder Alert window). Configured as a persons "on"
  script with NO zone on Front South 47, Front North 42 and Front Door. Active sunset to sunrise
  (NOAA sun calc for Austral, `--sun [ISO time]` prints the gate). The "on" call only records the
  onset and spawns a detached `--watch` process that polls `/api/motion/{id}/events`; it fires on
  a person chain >= 30 s on one camera (gaps < 15 s merged) OR >= 3 separate passes across the
  front cameras in 180 s (onsets within 40 s of each other count as one pass; one walk along the
  frontage trips all three cameras in ~5 s and falsely fired on 2026-11-08 19:33). Fire = Shelly floodlight burst 120 s + white lamps on cameras .42/.47
  (restored afterwards by a detached `--restore-lamps` to the baseline persisted in `lamp_baseline.json`
  by the FIRST fire of a chain; overlapping fires once read our own "on" as the baseline and left Front North 42
  lit for 27 h on 2026-11-12. Anything but "off" restores to auto) + email with the
  three main-stream posters attached; email at most once per 10 min, lights at most once per 20 s.
  State and log: `/var/lib/richiris/loitering/`. Test knobs: `LOITER_DRY_RUN=1`, `LOITER_FORCE=1`,
  `LOITER_FAKE_PRESENCE=<s>`, `LOITER_BURST_SECONDS`. If the API fails 3 polls in a row it fires
  anyway (Richard prefers false positives). Each test fire really lights the street.
- **`scripts/people_snapshots.py` = a crop of every detected person** (`richiris-people.service`, unit in
  `scripts/systemd/`, added 2026-11-08 so the brick-throwers can be matched by clothing/shoes on earlier
  visits). Reads the NVR DB read-only, takes each closed person event newest-first, decodes keyframes
  (`-skip_frame nokey`) from the covering recording over [start-8 s, end+12 s], runs `yolo11s-1280.onnx`
  (exported at 1280 so a 100 px person in 4K is still found; ~0.7 s/frame, 3 threads) and saves one crop
  per person per sampled frame (static people deduped by IoU, tallest 10 per event) plus a 1280-wide
  context frame with boxes. Events older than the archive fall back to the 640x480 onset thumbnail.
  Output `/var/lib/richiris/people/<date>/<camera>/`, state `people.db`, log `people.log`,
  `--stats` for progress. Crops carry EXIF time/description and are uploaded into Immich under the
  **separate user `richiris-people@richardferretti.com` ("RichIris People")**, archived, into ONE "People"
  album (all cameras, consolidated 2026-11-10) shared with Richard as viewer (Albums > Shared; his account has `emailNotifications.albumUpdate` OFF so the uploads do not email him). Key + album id (`IMMICH_ALBUM`) in
  `/opt/richiris/secrets/people_immich.env`, the user's password and id in `people_immich_user.env`
  (password login is globally OFF; to mint a new key, enable `passwordLogin` in the system config, log in,
  create the key, disable it again). **Never upload crops into Richard's own account again**: 31k archived
  crops lived there until 2026-11-10 and the Android app (3.3.4) showed them in his timeline regardless of the
  archive flag, and their faces polluted his People page. CLIP search over the crops therefore means logging
  in as that user on the web (search only spans partners with "show in timeline" on, which would put them
  back in the timeline). Niced + `CPUQuota=280%`; the first backfill of ~16.8k events ran for about half a day.
- **The same daemon also fills a "Vehicles" album and adds clips to both** (2026-11-19, `Kind` in
  `people_snapshots.py`). Vehicles = car/truck/bus/motorcycle/bicycle events, output `/var/lib/richiris/vehicles/`
  (`vehicles.db`), album id `IMMICH_ALBUM_VEHICLES` in the same env file, same Immich user, shared with Richard as
  viewer. A vehicle in the same place in the first and last frame, or in at least half the frames, is parked and
  skipped; an event whose first and last frames differ everywhere (PTZ lens moved) is skipped (`viewmoved`). No
  thumbnail fallback, so events older than the recordings are `nosource`. Some parked cars still get through
  (a passing car changes their box). **Clips**: each event that yields a crop also gets a 720p H.264 clip with
  audio (6 s before to 4 s after, max 40 s, 1 to 4 MB), uploaded archived into the album and deleted locally;
  only events from `CLIPS_FROM` (2026-11-19) on, no backfill. The loop does 8 people events then 8 vehicle
  events; the log is `people.log` for both. `--kind people|vehicles` restricts any command.
  Previous script: `/opt/richiris/people_snapshots.py.bak-20261119`.
- **`scripts/suspect_watch.py` = the Suspect Alert** (`richiris-suspects.service`, added 2026-11-09). Visual
  re-identification of the brick throwers is not reliable (CLIP over the 30k crops ranked lighting, not people),
  so it fires on the one robust signature: >= 3 people in ONE frame on the front cameras between
  max(20:30, sunset+45 min) and sunrise, from people.db crops (polled every 15 s, clusters with gaps <= 120 s,
  one email per cluster). Calibrated at about two alerts a week on the 18 days before it existed. Email only
  ("Suspect Alert:" does NOT ring RichMacro; set `SUSPECT_SUBJECT_PREFIX` to "Loitering Alert:" to make it
  ring), with a contact sheet (12 offender reference crops on top, the new crops under them) plus one context
  frame per camera. State `/var/lib/richiris/suspects/` (`state.json`, `sheets/`, `reference/`). Test:
  `--replay "2026-11-07 20:58" "2026-11-07 21:02" [--send]`, gate: `--sun [ISO]`.
- **Front Door (.48) and Bins 43 (.43) are Reolink.** Front Door has HTTP/HTTPS/ONVIF disabled
  (only RTSP 554 + Baichuan 9000); to reach its API, enable HTTP over Baichuan with `reolink-aio`
  (`Host(...).baichuan.set_port_enabled(PortType.http, True)`, venv in `/tmp/reolink-venv` on the
  box, re-create if gone) and disable it again after. Its burnt-in clock was an hour behind until
  2026-11-08 because DST was off with northern-hemisphere defaults; now `Dst.enable=1`, Oct wk1 ->
  Apr wk1, matching Bins 43. NTP pool.ntp.org.

### PTZ (`services/ptz.py`, `services/ptz_dahua.py`, `routers/ptz.py`, added 2026-11-16)
- **Capability is the `cameras.ptz_driver` column**: NULL = no driver recognises the RTSP URL (never contacted),
  `none` = probed, no PTZ, else a driver name. Probed on create, on an `rtsp_url` change, at startup for NULL rows
  (background) and by `POST .../ptz/detect`; `PUT /api/cameras/{id}` takes `ptz_driver` = `auto|none|<driver>`.
  An unreachable camera keeps its value. `CameraResponse.ptz` is what the client reads (`Camera.hasPtz`).
- **Host and credentials come from `rtsp_url`** (percent-decoded), HTTP port 80 assumed. Nothing PTZ-specific is stored.
- **New vendor = subclass `PtzDriver` + `@register_driver`**, imported in `get_ptz_manager()`. The router only
  talks to `PtzManager`. Directions are vectors (pan, tilt, zoom in -1/0/1), speed 1 to 8.
- **A continuous move is always time-limited, twice.** `move` re-arms a server deadman (`timeout`, default 2.5 s,
  max 10) and the Dahua driver sends `code=Continuously` with the camera's own auto-stop (`arg4`, timeout + 1 s),
  so the camera stops even if RichIris dies. The client re-sends `move` every second while a button is held
  and sends `stop` on release. A failed stop is retried in the background; shutdown stops active moves.
  `position` and preset go-to stop a running move first so the deadman cannot cut them short.
- **Dahua facts (DH-SDT3E410, verified)**: channel = `channel=` of the RTSP URL; the fixed lens answers
  `getStatus` with zeros, so the probe reads `caps.Pan/Tile/Zoom`. `Left` raises the pan angle, UP is NEGATIVE
  tilt (-10 to 90). `Continuously`: arg1 pan -8..8 (negative = left, about 7 deg/s per step), arg2 tilt (positive =
  up), arg3 zoom -100..100. `PositionABS` arg3 is NOT a zoom factor: an integer 1..128 mapped linearly onto 1x..10x
  (`status.AbsPosition[2]`/100); `status.ZoomValue` is another scale (569 at full zoom). Speed 9+ on the classic
  codes is accepted and very fast, hence the clamp. Preset names are cut at `&` even when encoded (sanitised).
  A missing preset answers HTTP 400 (mapped to 404). Errors: 400 no PTZ, 502 refused/unreachable, 504 timeout.
- **REPLACEMENT UNIT since 2026-11-24** (warranty swap from CCTV Masters): S/N CE088AAPAJCA00D, MAC 8c:e9:b4:4a:9b:73
  (router reservation and `Flint2/rebuild-flint2.sh` updated), firmware V3.211.0000019.1.R.261016 since 2026-11-24 16:05 (same zip, sha256 4a4d0572...39ad2, multipart
  `-F file=@...bin` to `upgrader.cgi?action=uploadFirmware`, 2m17s upload + ~5 min reboot; every setting survived). Set up by
  script, no web wizard: `DevInit.account` on `/OutsideCmd` (RSA-PKCS1 wraps a 16-digit AES-128-ECB key, zero padding,
  over `{name,pwd,CellPhone,Mail}`), same admin password as before, so RichIris needed no change. Restored from the old
  unit's dump: AU/Canberra TZ + DST, NTP 192.168.10.1, cloud off, AAC 16 kHz audio, PTZ GOP 25, reboot Mon 04:55.
  Re-applied: smart plan + PersonTrack rule (**write `VideoAnalyseGlobal` first: setting the scene wipes the rules**),
  illuminators off, tracker return + power-up to preset 1. **`TrackZoomScale=60`** ("Target size ratio";
  Dahua support (Wayne, 23 Nov) recommends 15-25%, Richard rejected that: he paid for 10x and wants the close-up) and
  **Home at 1x** (pan 125.6, tilt 4.2, Dahua's "zoom the preset out" advice; the old unit's Home was 2x).
- **PTZ 49 person tracking is the camera's own panorama-to-PTZ linkage, not RichIris** (enabled 2026-11-17, confirmed
  by Richard walking the frontage). It needed three things on the camera: `MasterSlaveGlobal` Enable/AutoTrack (was
  already on, mode RelayTracking), the smart plan on the panorama (`VideoAnalyseGlobal[0].Scene.Type=Normal`, was
  empty) and an IVS rule there (`VideoAnalyseRule[0][0]` "PersonTrack": CrossRegionDetection, Appear+Cross, Human,
  whole frame, `TrackEnable=true`, no alarm/snapshot actions). Rules must be written over RPC2
  (`configManager.setConfig` with the table); the CGI `setConfig` answers "Error" for them. The tracker follows
  humans only (`masterSlaveGroup.getCaps`), one target at a time. RPC logins must `global.logout` or the camera
  hits "too many connections". Originals + helper: `/root/dahua49-backup-20261117/` on the box (`dh.py`).
  Since 2026-11-17 `TrackingDurationLimit` is on at 12 s and `PriorityNewTarget` at 10 s, so a group gets one
  close-up each instead of the first person holding the lens (untested with a real group).
  **The factory/auto lens calibration was about 13.5 deg out in pan and 3 deg in tilt** (found 2026-11-17 after the
  tracker zoomed onto empty concrete beside a slow walker). Measured with `masterSlaveGroup.open {group:0,master:0,
  slave:1}` then `locate {token, point:[x,y] (0..8191 panorama), masterInfo:null}` on 8 landmarks: every one landed
  ~22% of a 1x frame left of target (1x FOV is ~60 x 31 deg). `startAutoCalibrate` refuses over RPC, so the fix is in
  `CalibrateMatrix`: each `autoPointPairEx[].slaveCenter` got pan -135 and tilt +30 (tenths of a degree); after it all
  8 landmarks land within ~1-2 deg. Tool `patch.py <dpan> <dtilt>` (applies to the saved ORIGINAL, so `patch.py 0 0`
  restores it), `aim.py`, before/after sheets and `CalibrateMatrix.orig.json` are in the backup dir. A camera reset or
  re-calibration from its web UI will undo this. Settings now: RelayTracking, `TrackZoomScale=60` (Richard wants the
  tight close-up), 30 s cap (12 s dropped a lone walker mid-pass at 15:36: when the cap expires the camera does NOT re-pick the same person, it just stops), new-target priority, `TrackIdleTime=5` (was 15: it stared at a ute for 13 s after losing a group). Rest position = preset 1 "Home" (since 2026-11-18 00:05 pan 125.6, tilt 4.2, 2x: Richard aimed it at the letterbox and the street in front of the house; before that it was the far street view at pan 246.9, tilt -4.9, 10x. See OPEN FAULT below): `MasterSlaveGroup[0].Slaver[0].Preset=1` (tracker return, was -1 = pan 167.2 / tilt 13.4) and `PowerUp[1]` Function=Preset. To change it, re-save preset 1. Return-after-tracking not yet observed. `IdleMotion` (return after manual moves) is left OFF: its timer unit is unverified.
  **Cap is driven by head count** (`scripts/ptz_track_cap.py`, `richiris-ptz-trackcap.service`, 2026-11-17): Richard
  wants one person followed all the way and several people cycled, which no single camera setting does, so the
  daemon listens to the rule's events and sets `TrackingDurationLimit` ON (12 s) while >= 2 distinct `ObjectID`s
  appeared in the last 45 s, OFF otherwise. Do not set the cap by hand, the daemon will overwrite it. Unverified on
  a real group, including whether a change applies to a track already in progress.
  **Police lights**: the rule's `EventHandler.LightingLink` (red/blue flicker) defaulted ON and flashed at every
  passer-by; it is OFF. The red/blue light is now fired only by the Intruder Alert (`email.py`) and
  `loitering.fire()` through `Camera Light Control/ptz_police.py on <s>` (camera's manual warning light,
  `coaxialControlIO` Type 1 on CGI channel 2 = RPC channel 1; channel 1/RPC 0 accepts the call but does nothing).
  The camera has no timer for it, so a detached child switches it off (latest burst wins, `police_until`).
  The zone floodlight rule does NOT fire it. Colour never seen by Claude (daylight): confirm it is red/blue.
  **OPEN FAULT: the lens loses its pan/tilt reference** (found 2026-11-17). The motor is open loop (caps:
  `PTZAbnormalDetect` unsupported), so after lost steps it reports the Home angle while pointing 2 to 25 deg away:
  Home shows the wrong view and the tracker aims beside people. Fast moves cause it: a 40 s burst of speed-8
  reversals and absolute swings shifted Home ~2 deg, and an hour of evening tracking left it on the grass.
  `ptz.restartPtz {channel:1}` over RPC2 re-homes it (~50 s) and is the only fix found. **Not the cable**: Richard
  replaced the 30 m flat "Cat 6e" lead with a 5 m Panduit copper Cat6 at 19:20 and it still slipped (switch is a
  TL-SG1005LP, 40 W PoE budget; a 12 V 3 A adapter test is still untried).
  **The 13.5 deg calibration patch above was WRONG and is removed** (`patch.py 0 0`, 2026-11-17 19:30): it had
  compensated for a reference that was already lost. With a freshly homed motor the FACTORY `CalibrateMatrix` lands
  all 8 landmarks within ~1-2 deg. Never re-patch the matrix; if aim is off, re-home first. Until 2026-11-18 Home was preset 1 at
  pan 246.9 / tilt -4.9 / 10x (camera was repositioned ~1 deg at 19:20; panorama landmark coordinates changed too).
  Never measure aim while someone is being tracked, and compare Home with a known-good frame first.
  **Firmware updated 2026-11-17 22:48** from 3.211.0000010.1.R (2026-04-11) to V3.211.0000019.1.R.261016 (PN build from
  Dahua's product page, hashes verified, uploaded with `upgrader.cgi?action=uploadFirmware`; config, presets,
  `CalibrateMatrix` and the tracking tables all survived, pre-flash export `config-all-before-fw-20261117.backup` in
  the backup dir). The same 40 s fast-move burst that shifted Home ~2 deg on the old firmware now leaves it within
  0.18 deg over three rounds (a gentle move gives the same 0.18, so that is backlash, not slip). NOT yet proven on a
  real evening of tracking: if Home is wrong again on this firmware, it is a warranty return (Gmail draft to
  support.oc@dahuatech.com with the evidence sheet exists, unsent). S/N CE088AAPAJF6A1E. Power is cleared: the camera
  is alone on the TL-SG1005LP and the 12 V pigtail has been cut off.
  **Still drifting 2026-11-19**: re-homed 18 Nov 12:03 (letterbox at 62% of frame width), 63% at 17:00, 70% at
  07:00 on the 19th, 79% at 12:00, and off the right edge of the frame by 15:14, with the camera reporting pan
  125.55 throughout. Sheet `home-drift-fw19-20261119.jpg` in the backup dir. Re-homed once at 15:15 (back to 62%).
  **Firmware did NOT fix it (2026-11-18).** On 019 firmware, with preset 1 untouched and the camera reporting pan
  125.55 throughout, the Home view was ~4.5 deg left by 10:30 and ~6 deg left by 12:02, and at 12:00:41 the tracker
  zoomed onto the parked Prius while a woman and child walked past to its right. A re-home at 12:03 put Home back
  exactly (letterbox at 62% of frame width, as at 07:00). So it is hardware: warranty return. The Gmail draft to
  support.oc@dahuatech.com was rewritten for this (latest firmware, 18 Nov figures, three-frame sheet attached; the
  sheet is also `home-drift-fw19-20261118.jpg` in the backup dir). Richard has not sent it yet. Richard rejects a
  scheduled re-home as a band-aid; do not install one unasked.
  **Illuminators are OFF on both lenses** (2026-11-18, Richard: IR glare blew out number plates). Two settings, both
  needed: `LightingScheme` every profile `AIMode` -> `InfraredMode`, and `Lighting_V2` InfraredLight `Mode: Off`
  (WhiteLight is Off too). In `AIMode` the `AIMixLight` entry drives the LEDs and refuses `Off` (the write is
  accepted and reverts to Auto/ZoomPrio), so setting IR off alone changes nothing. Night picture now relies on the
  street lights and the front floodlight; plates on parked cars became readable. Originals:
  `Lighting_V2.before-ir-off-20261118.json` and `LightingScheme.before-ir-off-20261118.json` in the backup dir.
  In portrait the app docks the PTZ panel under the video in the timeline's place (it covered the picture).
- **App**: `widgets/ptz_controls.dart`, toggled by the PTZ chip on fullscreen live (only when `hasPtz`): 8-way
  pad + zoom (hold to move), speed, presets (go to, save, delete). Requests are queued so `stop` never
  overtakes a `move`. Amber PTZ badge in `FeatureBadges`.

### Audio in recordings (`cameras.record_audio`, added 2026-11-16)
- **ffmpeg 7.1 drops RTSP AAC on stream copy**: AAC is no longer "all keyframes" and the RTP packets carry no key
  flag, so `-c:a copy` writes an audio stream with ZERO packets (what Bins 43, Garage and Front Door have always
  recorded). `record_audio=true` adds `-copyinkf:a` to that camera's recorder. Per camera (`PUT /api/cameras/{id}`),
  on for every camera since 2026-11-16 (Richard's call).
- **G.711 cameras are encoded to AAC** (`-c:a aac -b:a 32k`, 8 kHz mono): mpegts cannot carry pcm_mulaw, a copy
  lands it as the unplayable private data stream described under `TS_INPUT_ARGS`. With `record_audio` on, the
  recorder probes the source's audio codec once per stream (`probe_audio_codec`, cached on `StreamInfo.audio_codec`)
  and copies AAC, encodes anything else. An unreachable camera probes as None and is re-probed on each restart.
- **A `.ts` with real AAC needs `-bsf:a aac_adtstoasc` to copy into MP4** (otherwise "Malformed AAC bitstream" kills
  the mux, video included). `ts_audio_bsf_args()` (`services/ffmpeg.py`) adds it for playback and clip export
  only when the segment's PMT declares ADTS AAC (it reads the first 64 KB, no ffprobe); the filter is invalid
  on other audio codecs.
- go2rtc keeps the SDP from when it connected: after changing a camera's audio settings, restart the service
  (it relayed a stale 8 kHz config for 16 kHz audio until then). Live `direct` (`live.mp4`) carries the AAC track.
- **Every player in the app is muted (`setVolume(0)`)**, so audio is recorded and served but not heard yet.

### Playback transcode fallback (2026-11-16)
- The box's iGPU (UHD 630) cannot transcode big frames at native size: PTZ Panorama 49 (5376x1520) fails in the
  HEVC decoder, Bins 43 (4512x2512) at encoder open. ffmpeg exits 0 with an empty fMP4, which used to be served
  as "ready". GPU decode + `scale_vaapi` is NOT a fix (it drops frames on the panorama).
- `PlaybackManager.start_session` now builds a chain of commands (`PlaybackSession.fallback_cmds`): requested
  quality at native size, then the same bitrate through CPU decode + downscale to 1920 wide + `hevc_vaapi`
  (`sw_downscale` preset, 2x to 3x realtime on the box), then a stream copy. Sources over
  `VAAPI_MAX_WIDTH`/`VAAPI_MAX_HEIGHT` skip the first step. `_wait_ready` moves to the next command when one
  ends without a first fragment; the session id and URL do not change, so the client needs nothing.

### AI guard (`services/guard.py`, `routers/events.py`) - person verification, gates notifications (2026-11-20)
A Claude vision model answers ONE question for every person event: is a real human visible, or was the detector
fooled (insect or web on the lens, rain, glare, shadow, post)? `_on_motion` calls `get_guard().submit()` when a person
event starts (queues a task). The model gets the sub-stream frame the detector fired on with its box drawn, the newest
main-stream keyframe (1568 wide) and a padded crop of the box, and returns `human_present`, `people_count`,
`description`, `reason`. Stored in `event_assessments` (`would_alert` = human confirmed, `activity` =
"human confirmed" / "false detection"; the suspicion columns are unused); frames in `<data>/guard/<date>/<event_id>/`,
pruned after 30 days. About 5 to 8 s per verdict.
- **It gates the NOTIFICATIONS, not the lights.** `email.py` fires the floodlight and police lights, then calls
  `phone_alert.human_confirmed(camera)` -> `GET /api/events/human?camera=&window=&wait=` and skips the phone alarm and
  the email only on `human: false`. `loitering.py` `send_email` does the same over 180 s, before stamping its cooldown.
  **Fail open**: `null` (check off, slow, error, no assessment) notifies. A script has 30 s in total, so the wait is
  12 s. The prompt tells the model to say "human" whenever it cannot rule one out.
- **History**: 19 to 20 Nov it was a suspicion judge in shadow mode. Backfilled over the 7 Nov brick attack it rated
  the running group "low", and Richard cut it back to this single job. Do not grow it back unasked.
- **Model call**: the Claude Code CLI on the subscription token, one turn, no tools: images as content blocks on stdin
  (`-p --input-format stream-json --output-format stream-json --verbose --tools "" --system-prompt ... --json-schema`),
  answer read from the result line's `structured_output`. No native `claude` on the box, so it runs
  `docker run --rm -i ... --entrypoint claude lemmy-ai-lemmy-ai:latest` (richard is in the docker group); a native
  `claude` on PATH is preferred if one ever exists. Token: `/opt/richiris/secrets/guard.env`
  (`CLAUDE_CODE_OAUTH_TOKEN`, copied from Do-My-CTL's `.env`; when that token is renewed, copy it again). The CLI
  there (2.1.283) prints `unrecognized_model` for the 5.5 ids and still runs them.
- **Settings** (`guard.*`): `enabled`, `model` (`claude-sonnet-5-5`), `cameras` (comma-separated names, empty = all;
  set to everything except the PTZ zoom lens and Prius Cam), `cooldown_seconds` (0 = every event: a reused verdict
  could hide a real person). Read on every event, no restart needed.
- **Limits**: 2 at once, a 9th queued event is skipped (so unknown, so it notifies). ~400 person events a day, each a
  Sonnet call with 3 images on the shared subscription. `POST /api/events/try?camera_id=N` runs it on the live
  picture and stores nothing. `POST /api/events/backfill?date=&from_time=&hours=&cameras=` re-checks a past period
  from the recordings (no detection frame or crop there).
- **Events pane** (app): `GET /api/events` feed of detection events with their assessment.

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
  routers/    alerts, backup, cameras, clips, events, groups, recordings, riv, settings, storage, streams, system, motion, zones, ptz
  services/   backup, ffmpeg, stream_manager, go2rtc_client, go2rtc_manager, recorder, clip_exporter,
              playback, reverse_playback, settings, thumbnail_capture, retention, storage_migration,
              motion_detector, object_detector, update_checker, frame_broker, benchmark, zone_mask,
              rtsp_url, live_cache, live_poster, self_watchdog, ptz, ptz_dahua, guard, footage,
              riv/ (format, fmp4, writer, manager, serve)
backend/tests/  pytest suite for RIV (fixtures = real go2rtc captures)
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
**Alerts** (phone alarm feed): `POST /api/alerts` `{subject, body}` (box only) | `GET /api/alerts` | `GET /api/alerts/wait?after=&timeout=`
**Events** (AI check feed): `GET /api/events?limit=&before=&camera_id=&min_suspicion=&assessed=` | `GET /api/events/human?camera=&window=&wait=` | `GET /api/events/{id}/frames/{n}.jpg` | `POST /api/events/try?camera_id=` | `POST/GET /api/events/backfill`
**RIV**: `GET /api/riv/stats` | `GET /api/riv/{id}/files?date=&stream=` | `GET .../run?start=` | `GET .../stream.mp4?start=&end=&stream=s1|s2&speed=` | `GET .../frame.jpg?at=&stream=&width=` (playback itself goes through `POST /recordings/{id}/playback`, which also takes `stream=` and `speed=`)
**Motion**: `GET /api/motion/{id}/events?date=`
**Zones**: `GET/POST /api/cameras/{id}/zones` | `PUT/DELETE .../{zone_id}`, body `{name, points: [[x,y],...]}`
**PTZ** (`/api/cameras/{id}/ptz`): `GET` (driver + limits) | `GET status` | `POST move` `{direction, speed 1-8, timeout}` | `POST stop` | `POST position` `{pan?, tilt?, zoom?}` | `GET/POST presets` `{name?, id?}` | `POST presets/{n}/goto` | `DELETE presets/{n}` | `POST detect`
**Storage**: `POST validate` | `POST migrate` | `GET migrate/{id}/progress` | `POST migrate/{id}/cancel` | `POST migrate/{id}/finalize` | `POST update-path`

## App UI flow
- **The fullscreen live poster lives INSIDE `ZoomableVideo`** (2026-11-20). As a sibling above it, a pinch while the
  poster was still up (slow start, or a stalled stream reconnecting, which re-shows it) left an unzoomed stale frame
  as a band across the zoomed video (seen on PTZ Panorama 49 on the Pixel while the phone was busy after an OS update).
- **The selected group view is restored at launch and only its cameras are loaded** (2026-11-20, `_viewGroupId` /
  `_viewCameras` in `app.dart`). The saved id used to be dropped when the group list had not arrived yet, and the
  first frames showed "all", opening every stream. Pooled live players now exist only for the cameras in view plus
  the fullscreen one; players of other cameras are disposed (a pooled player keeps streaming after its tile is gone,
  so every camera ever shown used to stay live). Prev/next in fullscreen cycles within the group; the startup
  timeline preload is limited to the saved group. Check with `GET /api/streams/cache` (`subscribers`).
- **Grid playback bar** (2026-11-24, `_buildPlaybackBar` in `home_screen.dart`): shown under the grid only in
  and live: back/forward 30 s (a forward skip within 3 s of now goes live), pause/play, -4/-2/-1/1/2/4/8x, Live. The
  grid's master clock (`_clockMs`) is virtual (base + wall elapsed x rate, frozen while paused, negative in reverse)
  because segment continuation seeks new sessions to it. Crossing forward/reverse restarts every camera with
  `direction=backward` (server render per camera). Timeline zoom is multiplicative, 1x to 1440x (one minute).
- **Playback players have no audio track (`aid=no` in `applyHwdec`), and that is what stops the green tiles**
  (2026-11-24). With the AAC track selected, mpv's position follows the audio clock and ticks while the 4K hardware
  decoder is still starting, so any "position is moving" rule uncovered the tile onto an empty (green) texture.
  Measured after the fix: video params at 50 to 600 ms, first video position at 550 to 2,500 ms. The grid opens
  through `_openCovered` (listener armed after `open()` completes, zero ignored, 500 ms of movement, 10 s
  fallback, each start logged as `pb_uncover` in the server log); fullscreen uses the same rule for `_pbVideoReady`.
  The tile keeps its `Video` mounted under the cover (a remounted texture is green too). If audio playback is ever
  added, gate the cover on something other than position. Verified by a 30 fps capture of the Windows app: 4 skips
  and 2 grid/fullscreen round trips, no green frame.
- **RIV is documented under "RIV recordings" below.**
- **Grid tile boundaries are draggable** (2026-11-18, `camera_grid.dart`): drag the gap between tiles, double-click
  it to reset; saved per group + layout in SharedPreferences (`richiris-grid-cuts-<group>-<layout>`). A drag moves
  one "cut" (a distinct interior edge line of the layout), so every tile edge on that line follows.
- **The fullscreen PTZ button shows in playback too** and pressing it there goes live first (it used to vanish in
  playback, which read as "the controls are gone").
- **Grid**: tap selects (blue ring + timeline), tap again -> fullscreen (inline, no Navigator.push).
  Reorder = select a tile, then drag the small six-dot handle that appears bottom-left of the SELECTED tile onto
  another tile (`Draggable` in `camera_card.dart`; there is NO long-press drag, this file wrongly said so until 2026-11-17). Group chip bar filters. `_FeatureBadges` under each gear icon.
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
