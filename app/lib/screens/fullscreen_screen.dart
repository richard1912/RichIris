import 'dart:async';
import 'dart:io' show Platform;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../config/constants.dart';
import '../config/platform_info.dart';
import '../models/camera.dart';
import '../models/system_status.dart';
import '../services/stream_api.dart';
import '../services/player_tuning.dart';
import '../services/recording_api.dart';
import '../services/clip_api.dart';
import '../services/motion_api.dart';
import '../services/system_api.dart';
import '../services/timeline_cache.dart';
import '../utils/time_utils.dart';
import '../utils/format_utils.dart';
import '../models/playback_ref.dart';
import '../widgets/bug_report_dialog.dart';
import '../widgets/feature_badges.dart';
import '../widgets/icon_chip.dart';
import '../widgets/live_player.dart';
import '../widgets/quality_selector.dart';
import '../widgets/zoomable_video.dart';
import '../widgets/timeline/timeline_widget.dart';

class FullscreenScreen extends StatefulWidget {
  final Camera camera;
  final List<Camera> cameras;
  final StreamStatus? stream;
  final Quality quality;
  final StreamSource streamSource;
  final StreamApi streamApi;
  final RecordingApi recordingApi;
  final ClipApi clipApi;
  final MotionApi motionApi;
  final SystemApi systemApi;
  final TimelineCache timelineCache;
  final int tzOffsetMs;
  final ValueChanged<Quality> onQualityChanged;
  final ValueChanged<bool> onLiveStateChanged;
  final ValueChanged<StreamSource> onStreamSourceChanged;
  final VoidCallback? onBack;
  final ValueChanged<Camera>? onEditCamera;
  final VoidCallback? onAddToGroup;
  final Player? livePlayer;
  final VideoController? liveController;
  final PlaybackRef playbackRef;
  final String? initialPlaybackTime;
  final Player? initialPbPlayer;
  final VideoController? initialPbController;
  final String? initialPlaybackStartTime;

  const FullscreenScreen({
    super.key,
    required this.camera,
    required this.cameras,
    this.stream,
    required this.quality,
    required this.streamSource,
    required this.streamApi,
    required this.recordingApi,
    required this.clipApi,
    required this.motionApi,
    required this.systemApi,
    required this.timelineCache,
    required this.tzOffsetMs,
    required this.onQualityChanged,
    required this.onLiveStateChanged,
    required this.onStreamSourceChanged,
    this.onBack,
    this.onEditCamera,
    this.onAddToGroup,
    this.livePlayer,
    this.liveController,
    required this.playbackRef,
    this.initialPlaybackTime,
    this.initialPbPlayer,
    this.initialPbController,
    this.initialPlaybackStartTime,
  });

  @override
  State<FullscreenScreen> createState() => _FullscreenScreenState();
}

class _FullscreenScreenState extends State<FullscreenScreen> {
  bool _isLive = true;
  bool _paused = false;
  bool _showStats = true;
  Player? _livePlayer;
  Timer? _statsTimer;
  // Persistent focus node for the keyboard-shortcut listener. Must live in
  // state (not be re-created inside build()) — otherwise every rebuild from
  // the stats timer steals focus from any active TextField in the app.
  final FocusNode _keyboardFocus =
      FocusNode(debugLabel: 'fullscreen-keyboard');
  int _speed = 1;
  String? _playbackUrl;
  bool _playbackLoading = false;
  String? _playbackError;
  String? _windowEnd;
  bool _hasMore = false;
  String? _playbackStartTime;
  int _seekOffsetMs = 0; // seek offset applied by backend (player position starts from 0)
  int _virtualTimeMs = 0;
  int _generation = 0;
  bool _reverse = false; // current session is a server-rendered reverse stream
  Timer? _speedTimer;

  // Playback player
  Player? _pbPlayer;
  VideoController? _pbController;
  bool _adoptedPlayer = false; // true = using grid's player, don't dispose

  int _tzOffsetMs = 0;

  @override
  void initState() {
    super.initState();
    _tzOffsetMs = widget.tzOffsetMs;
    widget.playbackRef.getNvrTime = _getNvrTime;
    // Give the keyboard listener initial focus exactly once, after the tree
    // is built. Never again — callers of TextField (e.g. bug report dialog)
    // need to be able to hold focus without us stealing it back.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _keyboardFocus.requestFocus();
    });
    // Stats shown by default — start refresh timer
    _statsTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });
    // Seamless playback handoff: adopt grid's player directly (no new session)
    if (widget.initialPbPlayer != null) {
      _adoptedPlayer = true;
      _pbPlayer = widget.initialPbPlayer;
      _pbController = widget.initialPbController ?? VideoController(_pbPlayer!);
      _isLive = false;
      // Grid's player is already decoding — surface frames immediately.
      // Without this the black overlay (gated on _pbVideoReady, normally
      // flipped by _ensurePlayer's position listener) sits on top of the
      // video forever since _ensurePlayer early-returns on the adopt path.
      _pbVideoReady = true;
      _playbackStartTime = widget.initialPlaybackStartTime;
      _playbackUrl = 'adopted';
      _virtualTimeMs = _playbackStartTime != null
          ? DateTime.parse(_playbackStartTime!).millisecondsSinceEpoch
          : 0;
      widget.playbackRef.isLive = false;
      widget.onLiveStateChanged(false);
      // Grid's completed listener handles segment continuation on the shared player
    } else if (widget.initialPlaybackTime != null) {
      // Fallback: no shared player available, start fresh session
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _startPlayback(widget.initialPlaybackTime!);
      });
    }
  }

  @override
  void didUpdateWidget(covariant FullscreenScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.quality != widget.quality && !_isLive && _playbackStartTime != null) {
      // Quality changed during playback — restart at current position
      final currentTimeMs = _getNvrTime() - _tzOffsetMs;
      final startStr = formatLocalISOFromMs(currentTimeMs);
      _startPlayback(startStr);
    }
  }

  @override
  void dispose() {
    _clearSpeedTimer();
    _statsTimer?.cancel();
    _seekSub?.cancel();
    _pbPositionSub?.cancel();
    _completedSub?.cancel();
    _speedDurSub?.cancel();
    if (!_adoptedPlayer) {
      _pbPlayer?.dispose();
    }
    _keyboardFocus.dispose();
    super.dispose();
  }

  void _clearSpeedTimer() {
    _speedTimer?.cancel();
    _speedTimer = null;
    _generation++;
  }

  StreamSubscription? _seekSub;
  StreamSubscription? _completedSub;
  StreamSubscription? _speedDurSub; // speed re-apply after segment transition

  bool _pbVideoReady = false;
  StreamSubscription? _pbPositionSub;

  void _ensurePlayer() {
    if (_pbPlayer != null) return;
    _pbPlayer = Player(
      configuration: PlayerConfiguration(
        vo: 'gpu',
        logLevel: MPVLogLevel.warn,
      ),
    );
    applyHwdec(_pbPlayer!);
    _pbController = VideoController(_pbPlayer!);
    _pbPlayer!.setVolume(0);
    _pbVideoReady = false;
    _pbPositionSub?.cancel();
    _pbPositionSub = _pbPlayer!.stream.position.listen((pos) {
      if (pos > Duration.zero && !_pbVideoReady && mounted) {
        setState(() => _pbVideoReady = true);
      }
    });
    _completedSub?.cancel();
    _completedSub = _pbPlayer!.stream.completed.listen((completed) {
      if (completed && mounted && _speedTimer == null && _hasMore) {
        final savedSpeed = _speed;
        if (_reverse) {
          // Ran back to the start of this segment: continue from the
          // instant before it. The backend clamps into the previous segment.
          if (_playbackStartTime == null) return;
          final prevMs =
              DateTime.parse(_playbackStartTime!).millisecondsSinceEpoch - 1;
          _startPlayback(formatLocalISOFromMs(prevMs), resumeSpeed: savedSpeed);
        } else if (_windowEnd != null) {
          _startPlayback(_windowEnd!, resumeSpeed: savedSpeed);
        }
      }
    });
  }

  Future<void> _startPlayback(String start, {int resumeSpeed = 1}) async {
    // Release adopted player if any — we're creating our own session
    if (_adoptedPlayer) {
      _adoptedPlayer = false;
      _pbPlayer = null;
      _pbController = null;
    }
    setState(() {
      _playbackLoading = true;
      _playbackError = null;
      _playbackStartTime = start;
    });
    _clearSpeedTimer();
    // _clearSpeedTimer bumped _generation; anything issued after this call
    // (a quicker second click on the speed bar, a timeline tap) bumps it
    // again, and this request must then discard its response rather than
    // open a session the user has already moved past.
    final gen = _generation;
    _speed = resumeSpeed;
    _seekSub?.cancel();

    try {
      final session = await widget.recordingApi.startPlayback(
        widget.camera.id,
        start,
        widget.quality.param,
        direction: resumeSpeed < 0 ? 'backward' : 'forward',
      );
      if (_generation != gen || !mounted) return;
      final fullUrl = widget.recordingApi.getSegmentUrl(session.segmentUrl);

      _ensurePlayer();
      _pbVideoReady = false;
      // mpv keeps `speed` across loads, so a previous 4x would otherwise
      // leak into this session before the duration listener re-applies it.
      _pbPlayer!.setRate(resumeSpeed.abs().clamp(1, 4).toDouble());
      _pbPlayer!.open(Media(fullUrl));
      // Backend already applied seek via ffmpeg — player starts at position 0
      // which maps to segmentStart + seekSeconds in NVR time

      final actualStartMs = DateTime.parse(session.segmentStart).millisecondsSinceEpoch;
      setState(() {
        _playbackUrl = fullUrl;
        _windowEnd = session.segmentEnd;
        _hasMore = session.hasMore;
        if (_isLive) {
          _isLive = false;
          widget.playbackRef.isLive = false;
          widget.onLiveStateChanged(false);
        }
        _playbackStartTime = session.segmentStart;
        _reverse = session.direction == 'backward';
        _seekOffsetMs = (session.seekSeconds * 1000).round();
        _virtualTimeMs = actualStartMs + _seekOffsetMs;
        _playbackLoading = false;
      });

      // Re-apply speed after segment transition
      if (resumeSpeed != 1) {
        _speedDurSub?.cancel();
        _speedDurSub = _pbPlayer!.stream.duration.listen((dur) {
          if (dur > Duration.zero && mounted) {
            _applySpeedToPlayer(resumeSpeed);
            _speedDurSub?.cancel();
          }
        });
      }
    } catch (e) {
      if (_generation != gen || !mounted) return;
      setState(() {
        _playbackError = e.toString();
        _playbackLoading = false;
      });
    }
  }

  void _goLive() {
    if (_isLive) {
      setState(() => _paused = !_paused);
      return;
    }
    _clearSpeedTimer();
    if (!_adoptedPlayer) {
      _pbPlayer?.stop();
    }
    // Release adopted reference without stopping grid's player
    _adoptedPlayer = false;
    setState(() {
      _isLive = true;
      widget.playbackRef.isLive = true;
      widget.onLiveStateChanged(true);
      _paused = false;
      _playbackUrl = null;
      _playbackError = null;
      _speed = 1;
      _reverse = false;
    });
  }

  bool get _isPlayingNow {
    if (_isLive) return !_paused;
    return _pbPlayer?.state.playing ?? false;
  }

  void _togglePlayPause() {
    if (_isLive) {
      setState(() => _paused = !_paused);
    } else {
      final p = _pbPlayer;
      if (p == null) return;
      p.state.playing ? p.pause() : p.play();
      setState(() {});
    }
  }

  /// Milliseconds (device-local epoch, NVR-local wall clock) of the frame
  /// currently on screen, suitable for feeding straight back into
  /// [_startPlayback].
  int _currentPlaybackMs() {
    if (_isLive || _pbPlayer == null || _playbackStartTime == null) {
      return DateTime.now().millisecondsSinceEpoch + _tzOffsetMs;
    }
    return _getNvrTime() - _tzOffsetMs;
  }

  void _onSpeedChanged(int newSpeed) {
    final wasReverse = _reverse;
    final wantReverse = newSpeed < 0;
    _clearSpeedTimer();
    setState(() => _speed = newSpeed);

    final needsSession = _isLive || _pbPlayer == null || _playbackUrl == null;
    if (needsSession || wasReverse != wantReverse) {
      // Reverse is a different render on the server (see reverse_playback.py),
      // so crossing the forward/reverse boundary always means a new session
      // from the frame currently on screen. From live, reverse starts at
      // "now" (there is nothing after it) while forward starts 30 min back.
      var fromMs = _currentPlaybackMs();
      if (needsSession && !wantReverse) {
        fromMs -= 30 * 60 * 1000;
      }
      _startPlayback(formatLocalISOFromMs(fromMs), resumeSpeed: newSpeed);
      return;
    }

    _applySpeedToPlayer(newSpeed);
  }

  void _applySpeedToPlayer(int speed) {
    final player = _pbPlayer;
    if (player == null) return;

    // Reverse: the server rendered the segment backwards, so the player just
    // runs forward at |speed|. Native rate covers 1x-4x the same way.
    if (speed < 0) {
      player.setRate(-speed.toDouble());
      player.play();
      return;
    }
    if (speed >= 1 && speed <= 4) {
      player.setRate(speed.toDouble());
      player.play();
      return;
    }

    // Fast forward (16x, 32x)
    player.setRate(1);
    player.play();
    final gen = _generation;
    const tickMs = 500;
    final jumpMs = speed * tickMs;

    _speedTimer = Timer.periodic(const Duration(milliseconds: tickMs), (_) {
      if (_generation != gen || _pbPlayer == null) return;
      _virtualTimeMs += jumpMs;
      final p = _pbPlayer!;
      final newPos = p.state.position + Duration(milliseconds: jumpMs);
      if (newPos >= p.state.duration - const Duration(seconds: 1)) {
        _clearSpeedTimer();
        _startPlayback(formatLocalISOFromMs(_virtualTimeMs), resumeSpeed: speed);
      } else {
        p.seek(newPos);
      }
    });
  }

  int _getNvrTime() {
    if (_isLive) return DateTime.now().millisecondsSinceEpoch + _tzOffsetMs;
    // During loading, return the target start time (player position not yet valid)
    if (_playbackLoading && _playbackStartTime != null) {
      return DateTime.parse(_playbackStartTime!).millisecondsSinceEpoch + _tzOffsetMs;
    }
    if (_playbackUrl == null) return DateTime.now().millisecondsSinceEpoch + _tzOffsetMs;
    if (_speed >= 16) return _virtualTimeMs + _tzOffsetMs;
    final p = _pbPlayer;
    if (p != null && _playbackStartTime != null) {
      final startMs = DateTime.parse(_playbackStartTime!).millisecondsSinceEpoch;
      final posMs = p.state.position.inMilliseconds;
      // A reverse stream's position 0 is seekOffset and it runs backwards.
      if (_reverse) return startMs + _seekOffsetMs - posMs + _tzOffsetMs;
      return startMs + _seekOffsetMs + posMs + _tzOffsetMs;
    }
    return DateTime.now().millisecondsSinceEpoch + _tzOffsetMs;
  }

  @override
  Widget build(BuildContext context) {
    final running = widget.stream?.running ?? false;
    final rot = widget.camera.rotation;

    return Scaffold(
      backgroundColor: Colors.black,
      body: KeyboardListener(
        focusNode: _keyboardFocus,
        onKeyEvent: _handleKey,
        child: Column(
          children: [
            // Header
            _buildHeader(running),
            // Video stats
            if (_showStats) _buildStatsBar(),
            // Video area — chips overlay top-right, matching grid card layout.
            Expanded(
              child: Stack(
                children: [
                  Positioned.fill(child: _buildVideoArea(running, rot)),
                  Positioned(
                    top: 4,
                    right: 4,
                    child: _buildVideoOverlayChips(),
                  ),
                ],
              ),
            ),
            // Timeline
            TimelineWidget(
              cameraId: widget.camera.id,
              recordingApi: widget.recordingApi,
              clipApi: widget.clipApi,
              motionApi: widget.motionApi,
              timelineCache: widget.timelineCache,
              tzOffsetMs: _tzOffsetMs,
              isLive: _isLive,
              isPaused: _paused,
              compact: false,
              onPlayback: _startPlayback,
              onLive: _goLive,
              speed: _speed,
              onSpeedChanged: _onSpeedChanged,
              isPlaying: _isPlayingNow,
              onPlayPauseToggle: _togglePlayPause,
              getNvrTime: _getNvrTime,
              initialDate: widget.initialPlaybackTime?.substring(0, 10),
              cameras: widget.cameras.where((c) => c.enabled).toList(),
            ),
          ],
        ),
      ),
    );
  }

  /// Top-right chip cluster — rendered as a floating vertical overlay inside
  /// the video Stack so it matches the grid camera card 1:1 (chips sit on the
  /// video, not in the surrounding chrome).
  Widget _buildVideoOverlayChips() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        IconChip(
          icon: Icons.bar_chart,
          tooltip: 'Video Stats',
          onTap: _toggleStats,
        ),
        const SizedBox(height: 3),
        IconChip(
          icon: Icons.bug_report,
          tooltip: 'Report a Bug',
          onTap: () => _showBugReportDialog(context),
        ),
        const SizedBox(height: 3),
        IconChip(
          icon: Icons.refresh,
          tooltip: 'Refresh feed',
          onTap: _refreshFeed,
        ),
        if (widget.onEditCamera != null) ...[
          const SizedBox(height: 3),
          IconChip(
            icon: Icons.settings,
            tooltip: 'Edit camera settings',
            onTap: () => widget.onEditCamera!(widget.camera),
          ),
        ],
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
    );
  }

  Widget _buildHeader(bool running) {
    return Container(
      color: const Color(0xFF171717).withValues(alpha: 0.8),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: SafeArea(
        bottom: false,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(
              children: [
                IconButton(
                  icon: const Icon(Icons.arrow_back, size: 20),
                  onPressed: () {
                    if (widget.onBack != null) {
                      widget.onBack!();
                    } else {
                      Navigator.of(context).pop();
                    }
                  },
                  padding: EdgeInsets.zero,
                  constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
                ),
                const SizedBox(width: 8),
                Text(widget.camera.name,
                    style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w500)),
                const SizedBox(width: 8),
                Container(
                  width: 8,
                  height: 8,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: running ? const Color(0xFF22C55E) : const Color(0xFFEAB308),
                  ),
                ),
                const Spacer(),
                if (!isAndroid) ...[
                  if (_isLive && widget.stream?.uptimeSeconds != null)
                    Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: Text(
                        'Up ${formatUptime(widget.stream!.uptimeSeconds!)}',
                        style: const TextStyle(fontSize: 11, color: Color(0xFF525252)),
                      ),
                    ),
                  if (!_isLive)
                    const Padding(
                      padding: EdgeInsets.only(right: 8),
                      child: Text('Playback',
                          style: TextStyle(fontSize: 11, color: Color(0xFF3B82F6))),
                    ),
                  if (_isLive) ...[
                    StreamSourceSelector(
                      value: widget.streamSource,
                      onChanged: widget.onStreamSourceChanged,
                    ),
                    const SizedBox(width: 4),
                  ],
                  QualitySelector(
                    value: widget.quality,
                    onChanged: widget.onQualityChanged,
                    isLive: _isLive,
                  ),
                ],
              ],
            ),
            if (isAndroid)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Row(
                  children: [
                    if (_isLive && widget.stream?.uptimeSeconds != null)
                      Padding(
                        padding: const EdgeInsets.only(right: 8),
                        child: Text(
                          'Up ${formatUptime(widget.stream!.uptimeSeconds!)}',
                          style: const TextStyle(fontSize: 11, color: Color(0xFF525252)),
                        ),
                      ),
                    const Spacer(),
                    if (!_isLive)
                      const Padding(
                        padding: EdgeInsets.only(right: 8),
                        child: Text('Playback',
                            style: TextStyle(fontSize: 11, color: Color(0xFF3B82F6))),
                      ),
                    if (_isLive) ...[
                      StreamSourceSelector(
                        value: widget.streamSource,
                        onChanged: widget.onStreamSourceChanged,
                      ),
                      const SizedBox(width: 4),
                    ],
                    QualitySelector(
                      value: widget.quality,
                      onChanged: widget.onQualityChanged,
                      isLive: _isLive,
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _buildVideoArea(bool running, int rot) {
    if (_playbackLoading) {
      return const Center(
        child: Text('Preparing playback...',
            style: TextStyle(color: Color(0xFF737373), fontSize: 14)),
      );
    }
    if (_playbackError != null) {
      return Center(
        child: Text(_playbackError!,
            style: const TextStyle(color: Color(0xFFEF4444), fontSize: 14)),
      );
    }

    if (_isLive) {
      if (_paused) {
        return const Center(
          child: Text('Feed paused',
              style: TextStyle(color: Color(0xFFEAB308), fontSize: 14)),
        );
      }
      if (!running) {
        return Center(
          child: Text(
            widget.camera.enabled ? 'Stream connecting...' : 'Camera disabled',
            style: const TextStyle(color: Color(0xFF525252), fontSize: 14),
          ),
        );
      }
      final url = widget.streamApi.liveUrl(widget.camera.id, widget.streamSource.param, widget.quality.param, cameraName: widget.camera.name);
      return ZoomableVideo(
        child: LivePlayer(
          url: url,
          elevateOnWeb: true,
          player: widget.livePlayer,
          controller: widget.liveController,
          onPlayerCreated: (p) => _livePlayer = p,
          rotation: rot,
          fit: BoxFit.contain,
        ),
      );
    }

    // Playback mode
    if (_pbController != null) {
      Widget video = Video(
        controller: _pbController!,
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
      // Black overlay until first real frame renders (hides green decoder artifacts)
      if (!_pbVideoReady) {
        video = Stack(
          children: [
            video,
            Positioned.fill(child: Container(color: Colors.black)),
          ],
        );
      }
      return ZoomableVideo(child: video);
    }

    return const SizedBox.shrink();
  }

  void _toggleStats() {
    if (!_showStats) {
      _statsTimer = Timer.periodic(const Duration(seconds: 1), (_) {
        if (mounted) setState(() {});
      });
    } else {
      _statsTimer?.cancel();
      _statsTimer = null;
    }
    setState(() => _showStats = !_showStats);
  }

  Future<Map<String, String>> _getPlayerStats(Player player) async {
    final stats = <String, String>{};
    try {
      final codec = await playerProperty(player, 'video-codec');
      final w = await playerProperty(player, 'video-params/w');
      final h = await playerProperty(player, 'video-params/h');
      // Try multiple mpv FPS properties in order of reliability
      var fps = '';
      for (final prop in ['container-fps', 'estimated-vf-fps', 'video-params/fps']) {
        final val = await playerProperty(player, prop);
        final parsed = double.tryParse(val);
        if (parsed != null && parsed > 0 && parsed <= 120) {
          fps = val;
          break;
        }
      }
      final bitrate = await playerProperty(player, 'video-bitrate');

      if (codec.isNotEmpty) {
        // Clean up mpv codec string: "h264 ((null))" → "H.264", "hevc ((null))" → "HEVC"
        final cleanCodec = codec.split(' ').first.toLowerCase();
        final displayCodec = switch (cleanCodec) {
          'h264' => 'H.264',
          'hevc' || 'h265' => 'HEVC',
          _ => cleanCodec.toUpperCase(),
        };
        stats['Codec'] = displayCodec;
      }
      if (w.isNotEmpty && h.isNotEmpty) stats['Resolution'] = '${w}x$h';
      if (fps.isNotEmpty) {
        final fpsVal = double.tryParse(fps);
        stats['FPS'] = fpsVal != null ? fpsVal.toStringAsFixed(1) : fps;
      }
      if (bitrate.isNotEmpty) {
        final bps = double.tryParse(bitrate);
        if (bps != null && bps > 0) {
          final kbps = bps / 1000;
          stats['Bitrate'] = kbps >= 1000
              ? '${(kbps / 1000).toStringAsFixed(1)} Mbps'
              : '${kbps.toStringAsFixed(0)} kbps';
        }
      }
    } catch (_) {}
    return stats;
  }

  Widget _buildStatsBar() {
    final player = _isLive ? (widget.livePlayer ?? _livePlayer) : _pbPlayer;
    if (player == null) {
      return const SizedBox.shrink();
    }

    return FutureBuilder<Map<String, String>>(
      future: _getPlayerStats(player),
      builder: (context, snapshot) {
        final stats = snapshot.data ?? {};
        if (stats.isEmpty) return const SizedBox.shrink();

        final parts = stats.entries.map((e) => '${e.key}: ${e.value}').join('  |  ');
        return Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
          color: const Color(0xFF1A1A1A),
          child: Text(
            parts,
            style: const TextStyle(fontSize: 11, color: Color(0xFF737373), fontFamily: 'monospace'),
          ),
        );
      },
    );
  }

  Future<void> _showBugReportDialog(BuildContext context) =>
      showBugReportDialog(context, systemApi: widget.systemApi);

  /// Refresh the current camera's feed — fully tears down the current stream
  /// and re-opens it from scratch. Live: stop + reopen the go2rtc RTSP pull.
  /// Playback: restart the transcode session at the current scrub position.
  Future<void> _refreshFeed() async {
    unawaited(widget.systemApi.logClientEvent(
      event: 'refresh_feed',
      details: {
        'screen': 'fullscreen',
        'mode': _isLive ? 'live' : 'playback',
        'camera_id': widget.camera.id,
      },
    ));
    if (_isLive) {
      final player = widget.livePlayer;
      if (player == null) return;
      final url = widget.streamApi.liveUrl(
        widget.camera.id,
        widget.streamSource.param,
        widget.quality.param,
        cameraName: widget.camera.name,
      );
      debugPrint('[REFRESH] fullscreen live cam=${widget.camera.id} url=$url');
      await player.stop();
      await player.open(Media(url));
    } else {
      final currentMs = _getNvrTime() - _tzOffsetMs;
      final iso = formatLocalISOFromMs(currentMs);
      debugPrint('[REFRESH] fullscreen playback iso=$iso');
      await _startPlayback(iso);
    }
  }

  void _handleKey(KeyEvent event) {
    if (event is! KeyDownEvent) return;
    final key = event.logicalKey;
    if (key == LogicalKeyboardKey.escape) {
      if (widget.onBack != null) {
        widget.onBack!();
      } else {
        Navigator.of(context).pop();
      }
    } else if (key == LogicalKeyboardKey.arrowRight) {
      final idx = kSpeeds.indexOf(_speed);
      if (idx < kSpeeds.length - 1) _onSpeedChanged(kSpeeds[idx + 1]);
    } else if (key == LogicalKeyboardKey.arrowLeft) {
      final idx = kSpeeds.indexOf(_speed);
      if (idx > 0) _onSpeedChanged(kSpeeds[idx - 1]);
    } else if (key == LogicalKeyboardKey.space) {
      _togglePlayPause();
    }
  }
}
