import 'package:media_kit/media_kit.dart';

/// Tuning applied to every live (go2rtc RTSP) player.
///
/// Kept in one place so the grid (`app.dart`) and the standalone
/// [LivePlayer] widget cannot drift apart — they had identical copies of this
/// list, and a knob added to one but not the other would show up as "the grid
/// starts faster than fullscreen" with no obvious cause.
void applyLiveTuning(Player player) {
  final mpv = player.platform as NativePlayer;
  mpv.setProperty('cache', 'yes');
  mpv.setProperty('cache-pause', 'no');
  mpv.setProperty('cache-secs', '5');
  mpv.setProperty('demuxer-max-bytes', '16777216');
  mpv.setProperty('demuxer-readahead-secs', '5');
  mpv.setProperty('rtsp-transport', 'tcp');
  mpv.setProperty('hwdec', 'auto');
  mpv.setProperty('network-timeout', '30');
  // Skip libavformat's frame-rate probe. go2rtc's SDP carries no framerate,
  // so ffmpeg would otherwise read 20 video frames before reporting the
  // stream — ~2.9s on a 7fps camera, paid on every client launch. mpv still
  // reports FPS via `estimated-vf-fps` for the stats bar.
  mpv.setProperty('demuxer-lavf-o', 'fpsprobesize=0');
  // Emit frames as soon as they decode instead of holding them for reorder.
  // Without this libavcodec wants roughly a full GOP in hand before it shows
  // anything (measured on a burst-then-realtime test server: 2,379 ms to first
  // frame at a 1-fragment backlog, 589 ms at 15), with it ~390 ms at any
  // depth. That is what lets this client ask the server's live cache for the
  // short backlog (`&backlog=short`, newest keyframe only) and so run up to a
  // whole GOP closer to live than a browser, which cannot set this. Safe
  // because these camera streams are IPPP; do not copy it to playback players,
  // where a recording could carry B-frames.
  //
  // History, because it matters: this was added 2026-09-20, blamed for colour
  // noise on the Android grid, and removed the same day. The blame was wrong.
  // The noise was go2rtc's canned 2560x1440 hvcC on cameras that really send
  // 3840x2160 (see `_repair_hevc_init` in the backend's live_cache.py). With
  // that repaired, this flag was re-tested on the Fold7 on 2026-09-22: all 8
  // tiles clean over repeated cold launches, no dark frames.
  mpv.setProperty('vd-lavc-o', 'flags=+low_delay');
}

/// Enable hardware decoding on a playback player.
void applyHwdec(Player player) {
  (player.platform as NativePlayer).setProperty('hwdec', 'auto');
}

/// Read an mpv property, or `''` when it is unavailable.
Future<String> playerProperty(Player player, String name) async {
  return (player.platform as NativePlayer).getProperty(name);
}
