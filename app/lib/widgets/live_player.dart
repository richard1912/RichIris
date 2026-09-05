import 'dart:async';
import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../config/platform_info.dart';
import '../services/player_tuning.dart';
import 'web_live_video.dart';

enum LivePlayerState { connecting, playing, error, retrying }

class LivePlayerStatus {
  final LivePlayerState state;
  final int retryAttempt;
  final String? errorMessage;
  final int nextRetryMs;
  const LivePlayerStatus(this.state,
      {this.retryAttempt = 0, this.errorMessage, this.nextRetryMs = 0});
}

/// Live video player using media_kit (mpv) with HTTP fMP4 stream from go2rtc.
///
/// Plays the go2rtc HTTP fMP4 stream natively via libmpv, bypassing WebView
/// entirely. This ensures correct aspect ratio handling and avoids WebView2
/// DPI scaling issues on Windows.
class LivePlayer extends StatefulWidget {
  final String url;
  final int rotation;
  final BoxFit fit;
  final ValueChanged<Player>? onPlayerCreated;
  final Player? player;
  final VideoController? controller;
  final ValueChanged<LivePlayerStatus>? onStatusChanged;

  /// Web only: see [WebLiveVideo.elevate]. Set by the fullscreen view, never
  /// by the grid.
  final bool elevateOnWeb;

  const LivePlayer({
    super.key,
    required this.url,
    this.rotation = 0,
    this.fit = BoxFit.contain,
    this.onPlayerCreated,
    this.player,
    this.controller,
    this.onStatusChanged,
    this.elevateOnWeb = false,
  });

  @override
  State<LivePlayer> createState() => _LivePlayerState();
}

class _LivePlayerState extends State<LivePlayer> {
  // Both stay unset on web, where media_kit is not used at all. Every access
  // below is behind an `isWeb` guard for that reason.
  late Player _player;
  late VideoController _controller;
  bool _isExternal = false;
  Timer? _retryTimer;
  int _retryMs = 500;
  int _retryCount = 0;
  StreamSubscription? _errorSub;
  StreamSubscription? _widthSub;
  StreamSubscription? _positionSub;

  /// Playback position when the current media was opened. The first position
  /// that differs from it means mpv has actually decoded and shown a frame.
  /// Compared for inequality, not ordering: a reconnect restarts the stream
  /// near zero, so a "greater than the old position" test would never fire.
  Duration _positionBaseline = Duration.zero;
  bool _reportedPlaying = false;
  Timer? _playingFallbackTimer;

  // Stall detection: if position doesn't advance for this long, reconnect
  static const _stallCheckInterval = Duration(seconds: 5);
  static const _stallThreshold = Duration(seconds: 10);
  Timer? _stallTimer;
  Duration _lastPosition = Duration.zero;
  DateTime _lastPositionChange = DateTime.now();

  @override
  void initState() {
    super.initState();
    if (isWeb) {
      // The browser plays the fMP4 stream in a plain <video> element via
      // [WebLiveVideo]; there is no mpv to configure, no RTSP to open and no
      // media_kit Player to own. Creating one anyway would decode every feed
      // a second time, into an element that is never displayed.
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) {
          widget.onStatusChanged?.call(
            const LivePlayerStatus(LivePlayerState.connecting),
          );
        }
      });
      return;
    }
    if (widget.player != null && widget.controller != null) {
      _player = widget.player!;
      _controller = widget.controller!;
      _isExternal = true;
    } else {
      _player = Player(
        configuration: PlayerConfiguration(
          logLevel: MPVLogLevel.warn,
        ),
      );
      applyLiveTuning(_player);
      _controller = VideoController(_player);
    }
    _player.setVolume(0);
    _errorSub = _player.stream.error.listen((err) {
      debugPrint('LivePlayer error: $err');
      // Don't immediately reconnect — let the stall detector handle transient
      // RTSP hiccups. Only reconnect on fatal errors (end-of-file, connection refused).
      if (err.contains('end of file') ||
          err.contains('Connection refused') ||
          err.contains('No route to host') ||
          err.contains('Failed to resolve')) {
        widget.onStatusChanged?.call(LivePlayerStatus(
          LivePlayerState.error,
          errorMessage: err,
        ));
        _scheduleRetry();
      }
    });
    // `width` is NOT "the video is on screen". mpv publishes video dimensions
    // as soon as it parses the stream's parameter sets (VPS/SPS/PPS), which
    // arrive on connect — the first decodable frame only lands at the next
    // keyframe, up to a full GOP later (measured ~1.7s on Android). Reporting
    // `playing` here dropped the poster frame while the tile was still blank,
    // so the feed visibly went black before the video appeared. Dimensions now
    // only arm a safety net; `position` is what proves a frame was shown.
    _widthSub = _player.stream.width.listen((w) {
      if (w != null && w > 0) _armPlayingFallback();
    });
    _positionSub = _player.stream.position.listen((pos) {
      if (pos > Duration.zero && pos != _positionBaseline) _reportPlaying();
    });
    widget.onPlayerCreated?.call(_player);
    _startStallDetection();
    // External players that already have media loaded are mid-stream
    // (e.g. grid→fullscreen transition) — don't re-open.
    // Fresh external players (no media yet) still need to be opened.
    // Deferred to post-frame so onStatusChanged's setState doesn't fire
    // during the parent CameraCard's build pass.
    if (!_isExternal || _player.state.playlist.medias.isEmpty) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _open(widget.url);
      });
    } else {
      // Reused mid-stream player: the streams above are broadcast with no
      // replay, so neither listener would fire for a feed that is already up.
      // A non-zero position means frames are already on screen — report from
      // current state, otherwise the poster frame and the connecting overlay
      // would stick forever.
      if (_player.state.position > Duration.zero) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted) _reportPlaying();
        });
      }
    }
  }

  /// Report `playing` at most once per open, when a frame has actually shown.
  void _reportPlaying() {
    if (_reportedPlaying || !mounted) return;
    _reportedPlaying = true;
    _playingFallbackTimer?.cancel();
    _playingFallbackTimer = null;
    widget.onStatusChanged?.call(
      const LivePlayerStatus(LivePlayerState.playing),
    );
  }

  /// Safety net for a feed whose position never advances even though video is
  /// on screen: don't leave a stale poster covering it forever.
  void _armPlayingFallback() {
    if (_reportedPlaying || _playingFallbackTimer != null) return;
    _playingFallbackTimer = Timer(const Duration(seconds: 5), _reportPlaying);
  }

  void _startStallDetection() {
    _stallTimer?.cancel();
    _lastPosition = Duration.zero;
    _lastPositionChange = DateTime.now();
    _stallTimer = Timer.periodic(_stallCheckInterval, (_) {
      if (!mounted) return;
      final pos = _player.state.position;
      if (pos != _lastPosition) {
        _lastPosition = pos;
        _lastPositionChange = DateTime.now();
      } else if (_player.state.playing &&
          DateTime.now().difference(_lastPositionChange) > _stallThreshold) {
        // Stream has stalled — reconnect
        debugPrint('LivePlayer: stream stalled, reconnecting ${widget.url}');
        _scheduleRetry();
      }
    });
  }

  void _open(String url) {
    _retryTimer?.cancel();
    _retryMs = 500;
    _retryCount = 0;
    _lastPosition = Duration.zero;
    _lastPositionChange = DateTime.now();
    _reportedPlaying = false;
    _playingFallbackTimer?.cancel();
    _playingFallbackTimer = null;
    _positionBaseline = _player.state.position;
    widget.onStatusChanged?.call(
      const LivePlayerStatus(LivePlayerState.connecting),
    );
    _player.open(
      Media(url),
      play: true,
    );
  }

  void _scheduleRetry() {
    _retryTimer?.cancel();
    _retryCount++;
    // Reset stall detector so it doesn't re-trigger during reconnect
    _lastPosition = Duration.zero;
    _lastPositionChange = DateTime.now();
    widget.onStatusChanged?.call(LivePlayerStatus(
      LivePlayerState.retrying,
      retryAttempt: _retryCount,
      nextRetryMs: _retryMs,
    ));
    _retryTimer = Timer(Duration(milliseconds: _retryMs), () {
      if (!mounted) return;
      _lastPositionChange = DateTime.now();
      _reportedPlaying = false;
      _positionBaseline = _player.state.position;
      _player.open(Media(widget.url), play: true);
      _retryMs = (_retryMs * 2).clamp(500, 10000);
    });
  }

  @override
  void didUpdateWidget(LivePlayer oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.url != widget.url) {
      _open(widget.url);
    }
  }

  @override
  void dispose() {
    if (isWeb) {
      super.dispose();
      return;
    }
    _stallTimer?.cancel();
    _retryTimer?.cancel();
    _errorSub?.cancel();
    _widthSub?.cancel();
    _positionSub?.cancel();
    _playingFallbackTimer?.cancel();
    if (!_isExternal) {
      _player.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final rot = widget.rotation;
    final isRotated = rot == 90 || rot == 270;

    Widget video = isWeb
        ? WebLiveVideo(
            url: widget.url,
            fit: widget.fit,
            elevate: widget.elevateOnWeb,
            onPlayingChanged: (playing) {
              if (!mounted) return;
              widget.onStatusChanged?.call(LivePlayerStatus(
                playing ? LivePlayerState.playing : LivePlayerState.connecting,
              ));
            },
          )
        : Video(
            controller: _controller,
            fit: widget.fit,
            controls: NoVideoControls,
          );

    if (rot != 0) {
      video = Transform.rotate(
        angle: rot * 3.14159265 / 180,
        child: isRotated
            ? Transform.scale(scale: 0.5625, child: video)
            : video,
      );
    }

    return video;
  }
}
