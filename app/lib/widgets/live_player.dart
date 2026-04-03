import 'dart:async';
import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';

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

  const LivePlayer({
    super.key,
    required this.url,
    this.rotation = 0,
    this.fit = BoxFit.contain,
    this.onPlayerCreated,
  });

  @override
  State<LivePlayer> createState() => _LivePlayerState();
}

class _LivePlayerState extends State<LivePlayer> {
  late final Player _player;
  late final VideoController _controller;
  Timer? _retryTimer;
  int _retryMs = 500;
  StreamSubscription? _errorSub;

  @override
  void initState() {
    super.initState();
    _player = Player(
      configuration: PlayerConfiguration(
        vo: 'gpu',
        logLevel: MPVLogLevel.warn,
      ),
    );
    // Low-latency live stream settings for mpv
    final mpv = _player.platform as NativePlayer;
    mpv.setProperty('profile', 'low-latency');
    mpv.setProperty('cache', 'no');
    mpv.setProperty('cache-pause', 'no');
    mpv.setProperty('untimed', 'yes');
    mpv.setProperty('demuxer-max-bytes', '524288'); // 512KB buffer
    mpv.setProperty('demuxer-readahead-secs', '0');
    mpv.setProperty('hwdec', 'auto');               // HEVC hardware decode
    _controller = VideoController(_player);
    _player.setVolume(0);
    _errorSub = _player.stream.error.listen((_) => _scheduleRetry());
    widget.onPlayerCreated?.call(_player);
    _open(widget.url);
  }

  void _open(String url) {
    _retryTimer?.cancel();
    _retryMs = 500;
    _player.open(
      Media(url),
      play: true,
    );
  }

  void _scheduleRetry() {
    _retryTimer?.cancel();
    _retryTimer = Timer(Duration(milliseconds: _retryMs), () {
      if (!mounted) return;
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
    _retryTimer?.cancel();
    _errorSub?.cancel();
    _player.dispose();
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
