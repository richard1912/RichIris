import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';

/// Mutable reference for sharing playback state between grid and fullscreen views.
/// Written to by the owning view, read by _MainNav during view transitions.
class PlaybackRef {
  bool isLive = true;
  int Function()? getNvrTime;

  /// Callbacks for handing off playback players during view transitions.
  /// Avoids creating new players/sessions — the video continues uninterrupted.
  Player? Function(int cameraId)? getPlayer;
  VideoController? Function(int cameraId)? getController;

  /// Detach a player so the source view doesn't dispose it during cleanup.
  void Function(int cameraId)? detachPlayer;

  /// Session state needed by the receiving view for segment continuation.
  String? playbackStartIso;
  String? segmentEnd;
  bool hasMore = false;
}
