import 'dart:async';

import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../models/camera.dart';
import '../models/system_status.dart';
import 'feature_badges.dart';
import 'icon_chip.dart';
import 'live_player.dart';

class CameraCard extends StatefulWidget {
  final Camera camera;
  final StreamStatus? stream;
  final String streamUrl;
  /// Backend JPEG of the camera's most recent frame, painted over the video
  /// until the local decoder produces its first frame. Null disables it.
  final String? posterUrl;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback onEdit;
  final Player? livePlayer;
  final VideoController? liveController;
  final bool isFullscreen;
  final VideoController? playbackController;
  final bool playbackLoading;
  final bool playbackFailed;
  final bool showDragHint;
  final Size? dragFeedbackSize;
  final VoidCallback? onDragStarted;
  final VoidCallback? onDragEnd;
  final VoidCallback? onAddToGroup;

  const CameraCard({
    super.key,
    required this.camera,
    this.stream,
    required this.streamUrl,
    this.posterUrl,
    this.selected = false,
    required this.onTap,
    required this.onEdit,
    this.livePlayer,
    this.liveController,
    this.isFullscreen = false,
    this.playbackController,
    this.playbackLoading = false,
    this.playbackFailed = false,
    this.showDragHint = false,
    this.dragFeedbackSize,
    this.onDragStarted,
    this.onDragEnd,
    this.onAddToGroup,
  });

  @override
  State<CameraCard> createState() => _CameraCardState();
}

class _CameraCardState extends State<CameraCard> {
  LivePlayerStatus? _liveStatus;

  /// The poster frame is dropped shortly after the live feed reports its first
  /// decoded frame — kept briefly so it can cross-fade instead of popping.
  bool _posterGone = false;
  Timer? _posterTimer;

  @override
  void initState() {
    super.initState();
    // A player that is already mid-stream (card rebuilt while its feed keeps
    // running) has nothing to cover — showing the poster there would flash a
    // still frame over live video. Position, not width: width is set from the
    // stream's parameter sets before any frame is decoded (see live_player).
    final pos = widget.livePlayer?.state.position;
    if (pos != null && pos > Duration.zero) _posterGone = true;
  }

  void _onLiveStatus(LivePlayerStatus status) {
    if (!mounted) return;
    setState(() => _liveStatus = status);
    if (status.state == LivePlayerState.playing &&
        !_posterGone &&
        _posterTimer == null) {
      _posterTimer = Timer(const Duration(milliseconds: 400), () {
        if (mounted) setState(() => _posterGone = true);
      });
    }
  }

  /// Poster image, rotated to match the camera the same way the video is.
  Widget _buildPoster() {
    Widget img = Image.network(
      widget.posterUrl!,
      fit: BoxFit.contain,
      gaplessPlayback: true,
      // No frame yet on the server (camera reconnecting, just added) — show
      // nothing rather than a broken-image box.
      errorBuilder: (_, __, ___) => const SizedBox.shrink(),
    );
    final rot = widget.camera.rotation;
    if (rot != 0) {
      final isRotated = rot == 90 || rot == 270;
      img = Transform.rotate(
        angle: rot * 3.14159265 / 180,
        child: isRotated ? Transform.scale(scale: 0.5625, child: img) : img,
      );
    }
    return img;
  }

  @override
  void dispose() {
    _posterTimer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final running = widget.stream?.running ?? false;
    final showLive = widget.camera.enabled && running && !widget.isFullscreen;

    return GestureDetector(
      onTap: widget.onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(8),
          border: Border.all(
            color: widget.selected ? const Color(0xFF3B82F6) : const Color(0xFF404040),
            width: widget.selected ? 2 : 1,
          ),
          color: const Color(0xFF1A1A1A),
        ),
        clipBehavior: Clip.antiAlias,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Expanded(
              child: Stack(
                fit: StackFit.expand,
                children: [
                  if (widget.isFullscreen)
                    Container(
                      color: const Color(0xFF0A0A0A),
                      child: const Center(
                        child: Icon(Icons.fullscreen,
                            color: Color(0xFF525252), size: 32),
                      ),
                    )
                  else if (widget.playbackLoading)
                    const Center(
                      child: SizedBox(
                        width: 24,
                        height: 24,
                        child: CircularProgressIndicator(
                          strokeWidth: 2,
                          color: Color(0xFF3B82F6),
                        ),
                      ),
                    )
                  else if (widget.playbackFailed)
                    const Center(
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(Icons.videocam_off,
                              color: Color(0xFF737373), size: 28),
                          SizedBox(height: 4),
                          Text('No recording',
                              style: TextStyle(
                                  color: Color(0xFF737373), fontSize: 11)),
                        ],
                      ),
                    )
                  else if (widget.playbackController != null)
                    IgnorePointer(
                      child: _buildPlaybackVideo(),
                    )
                  else if (showLive)
                    IgnorePointer(
                      child: LivePlayer(
                        url: widget.streamUrl,
                        rotation: widget.camera.rotation,
                        fit: BoxFit.contain,
                        player: widget.livePlayer,
                        controller: widget.liveController,
                        onStatusChanged: _onLiveStatus,
                      ),
                    )
                  else
                    Container(
                      color: const Color(0xFF0A0A0A),
                      child: Center(
                        child: Icon(
                          widget.camera.enabled ? Icons.videocam : Icons.videocam_off,
                          color: const Color(0xFF525252),
                          size: 32,
                        ),
                      ),
                    ),
                  // Poster frame — a server-side JPEG of the camera's current
                  // view, painted while this client's decoder is still probing
                  // the stream and waiting for a keyframe. Fades out once the
                  // real feed has a frame.
                  if (showLive && widget.posterUrl != null && !_posterGone)
                    IgnorePointer(
                      child: AnimatedOpacity(
                        opacity:
                            _liveStatus?.state == LivePlayerState.playing ? 0 : 1,
                        duration: const Duration(milliseconds: 300),
                        child: _buildPoster(),
                      ),
                    ),
                  // Stream status overlay
                  if (showLive && _liveStatus != null && _liveStatus!.state != LivePlayerState.playing)
                    Positioned(
                      left: 0,
                      right: 0,
                      bottom: 0,
                      child: Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                        color: Colors.black54,
                        child: Text(
                          _statusText(_liveStatus!),
                          style: const TextStyle(fontSize: 10, color: Color(0xFFD4D4D4)),
                          textAlign: TextAlign.center,
                        ),
                      ),
                    ),
                  // Edit button + add-to-group + feature indicator badges
                  Positioned(
                    top: 4,
                    right: 4,
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.end,
                      children: [
                        IconChip(
                          icon: Icons.settings,
                          tooltip: 'Edit camera settings',
                          onTap: widget.onEdit,
                        ),
                        if (widget.onAddToGroup != null) ...[
                          const SizedBox(height: 3),
                          IconChip(
                            icon: Icons.add,
                            tooltip: 'Add to group',
                            onTap: widget.onAddToGroup,
                          ),
                        ],
                        const SizedBox(height: 3),
                        FeatureBadges(camera: widget.camera),
                      ],
                    ),
                  ),
                  // Drag handle — shown when card is selected
                  if (widget.showDragHint)
                    Positioned(
                      bottom: 4,
                      left: 4,
                      child: Draggable<int>(
                        data: widget.camera.id,
                        onDragStarted: widget.onDragStarted,
                        onDragEnd: (_) => widget.onDragEnd?.call(),
                        onDraggableCanceled: (_, __) => widget.onDragEnd?.call(),
                        feedback: Material(
                          elevation: 8,
                          borderRadius: BorderRadius.circular(8),
                          color: Colors.transparent,
                          child: SizedBox(
                            width: widget.dragFeedbackSize?.width ?? 200,
                            height: widget.dragFeedbackSize?.height ?? 112,
                            child: Opacity(
                              opacity: 0.85,
                              child: Container(
                                decoration: BoxDecoration(
                                  borderRadius: BorderRadius.circular(8),
                                  border: Border.all(color: const Color(0xFF3B82F6), width: 2),
                                  color: const Color(0xFF1A1A1A),
                                ),
                                child: Center(
                                  child: Text(
                                    widget.camera.name,
                                    style: const TextStyle(color: Colors.white, fontSize: 14, decoration: TextDecoration.none),
                                  ),
                                ),
                              ),
                            ),
                          ),
                        ),
                        childWhenDragging: const SizedBox.shrink(),
                        child: MouseRegion(
                          cursor: SystemMouseCursors.grab,
                          child: AnimatedOpacity(
                            opacity: widget.selected ? 0.7 : 0.0,
                            duration: const Duration(milliseconds: 200),
                            child: Container(
                              padding: const EdgeInsets.all(4),
                              decoration: BoxDecoration(
                                color: Colors.black54,
                                borderRadius: BorderRadius.circular(4),
                              ),
                              child: const Icon(Icons.drag_indicator,
                                  size: 18, color: Color(0xFFD4D4D4)),
                            ),
                          ),
                        ),
                      ),
                    ),
                ],
              ),
            ),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
              color: const Color(0xFF171717),
              child: Row(
                children: [
                  Container(
                    width: 6,
                    height: 6,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: widget.playbackController != null
                          ? const Color(0xFF3B82F6)
                          : running && (widget.stream?.go2rtcConnected ?? true)
                              ? const Color(0xFF22C55E)
                              : running
                                  ? const Color(0xFFEAB308)
                                  : widget.camera.enabled
                                      ? const Color(0xFFEAB308)
                                      : const Color(0xFFEF4444),
                    ),
                  ),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      widget.camera.name,
                      style: const TextStyle(fontSize: 12),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  if (widget.playbackController != null)
                    const Text('Playback',
                        style: TextStyle(fontSize: 10, color: Color(0xFF3B82F6))),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  String _statusText(LivePlayerStatus status) {
    switch (status.state) {
      case LivePlayerState.connecting:
        return _reasonFromBackend() ?? 'Connecting...';
      case LivePlayerState.retrying:
        final reason = _reasonFromBackend() ?? 'Reconnecting';
        final retrySec = (status.nextRetryMs / 1000).ceil();
        return '$reason (attempt ${status.retryAttempt}) - retry in ${retrySec}s';
      case LivePlayerState.error:
        final reason = _reasonFromBackend();
        final msg = status.errorMessage ?? 'unknown';
        return reason != null ? '$reason: $msg' : 'Error: $msg';
      case LivePlayerState.playing:
        return '';
    }
  }

  String? _reasonFromBackend() {
    final s = widget.stream;
    if (s == null) return 'Waiting for backend...';
    if (!s.running) return 'Stream not started';
    if (s.error != null) return s.error;
    if (s.go2rtcConnected == false) return 'Establishing live view...';
    return null;
  }

  Widget _buildPlaybackVideo() {
    final rot = widget.camera.rotation;
    Widget video = Video(
      controller: widget.playbackController!,
      fit: BoxFit.contain,
      controls: NoVideoControls,
    );
    if (rot != 0) {
      final isRotated = rot == 90 || rot == 270;
      video = Transform.rotate(
        angle: rot * 3.14159265 / 180,
        child: isRotated ? Transform.scale(scale: 0.5625, child: video) : video,
      );
    }
    return video;
  }
}

