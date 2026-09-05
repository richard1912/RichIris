/// mpv-specific player tuning, behind a conditional import.
///
/// The live/playback players are tuned through libmpv properties
/// (`cache-secs`, `rtsp-transport`, `demuxer-lavf-o=fpsprobesize=0`, ...),
/// reached by casting `player.platform` to `NativePlayer`. That cast only
/// exists in media_kit's native backend: on web `NativePlayer` resolves to a
/// stub with no `setProperty`/`getProperty`, which is a COMPILE error, not a
/// runtime one — it was the only thing standing between this app and a web
/// build.
///
/// media_kit's web backend is an HTML `<video>` element, so none of these
/// knobs have an equivalent there and the web implementation is a no-op. That
/// is not a loss: the browser owns its own buffering and hardware decoding,
/// and the fps-probe cost these avoid is a libavformat behaviour that a
/// `<video>` element never pays.
export 'player_tuning_io.dart' if (dart.library.html) 'player_tuning_web.dart';
