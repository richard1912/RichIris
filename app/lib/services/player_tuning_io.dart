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
}

/// Enable hardware decoding on a playback player.
void applyHwdec(Player player) {
  (player.platform as NativePlayer).setProperty('hwdec', 'auto');
}

/// Read an mpv property, or `''` when it is unavailable.
Future<String> playerProperty(Player player, String name) async {
  return (player.platform as NativePlayer).getProperty(name);
}
