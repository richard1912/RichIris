import 'dart:async';
import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';

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

  const LivePlayer({
    super.key,
    required this.url,
    this.rotation = 0,
    this.fit = BoxFit.contain,
    this.onPlayerCreated,
    this.player,
    this.controller,
    this.onStatusChanged,
  });

  @override
  State<LivePlayer> createState() => _LivePlayerState();
}

class _LivePlayerState extends State<LivePlayer> {
  late Player _player;
  late VideoController _controller;
  bool _isExternal = false;
  Timer? _retryTimer;
  int _retryMs = 500;
  int _retryCount = 0;
  StreamSubscription? _errorSub;
  StreamSubscription? _widthSub;

  // Stall detection: if position doesn't advance for this long, reconnect
  static const _stallCheckInterval = Duration(seconds: 5);
  static const _stallThreshold = Duration(seconds: 10);
  Timer? _stallTimer;
  Duration _lastPosition = Duration.zero;
  DateTime _lastPositionChange = DateTime.now();

  @override
  void initState() {
    super.initState();
    if (widget.player != null && widget.controller != null) {
      _player = widget.player!;
      _controller = widget.controller!;
      _isExternal = true;
    } else {
      _player = Player(
        configuration: PlayerConfiguration(
          vo: 'gpu',
          logLevel: MPVLogLevel.warn,
        ),
      );
      final mpv = _player.platform as NativePlayer;
      mpv.setProperty('profile', 'low-latency');
      mpv.setProperty('cache', 'no');
      mpv.setProperty('cache-pause', 'no');
      mpv.setProperty('untimed', 'yes');
      mpv.setProperty('demuxer-max-bytes', '524288');
      mpv.setProperty('demuxer-readahead-secs', '0');
      mpv.setProperty('demuxer-lavf-o', 'timeout=15000000');
      mpv.setProperty('hwdec', 'auto');
      _controller = VideoController(_player);
    }
    _player.setVolume(0);
    _errorSub = _player.stream.error.listen((err) {
      widget.onStatusChanged?.call(LivePlayerStatus(
        LivePlayerState.error,
        errorMessage: err,
      ));
      _scheduleRetry();
    });
    _widthSub = _player.stream.width.listen((w) {
      if (w != null && w > 0) {
        widget.onStatusChanged?.call(
          const LivePlayerStatus(LivePlayerState.playing),
        );
      }
    });
    widget.onPlayerCreated?.call(_player);
    _startStallDetection();
    // External players that already have media loaded are mid-stream
    // (e.g. grid→fullscreen transition) — don't re-open.
    // Fresh external players (no media yet) still need to be opened.
    if (!_isExternal || _player.state.playlist.medias.isEmpty) {
      _open(widget.url);
    }
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
    _stallTimer?.cancel();
    _retryTimer?.cancel();
    _errorSub?.cancel();
    _widthSub?.cancel();
    if (!_isExternal) {
      _player.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final rot = widget.rotation;
    final isRotated = rot == 90 || rot == 270;

    Widget video = Video(
      controller: _controller,
      fit: widget.fit,
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
