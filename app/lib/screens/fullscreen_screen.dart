import 'dart:async';
import 'dart:io' show Platform;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import 'package:url_launcher/url_launcher.dart';
import '../config/constants.dart';
import '../models/camera.dart';
import '../models/system_status.dart';
import '../services/stream_api.dart';
import '../services/recording_api.dart';
import '../services/clip_api.dart';
import '../services/motion_api.dart';
import '../services/system_api.dart';
import '../utils/time_utils.dart';
import '../utils/format_utils.dart';
import '../models/playback_ref.dart';
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
  final int tzOffsetMs;
  final ValueChanged<Quality> onQualityChanged;
  final ValueChanged<bool> onLiveStateChanged;
  final ValueChanged<StreamSource> onStreamSourceChanged;
  final VoidCallback? onBack;
  final ValueChanged<Camera>? onEditCamera;
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
    required this.tzOffsetMs,
    required this.onQualityChanged,
    required this.onLiveStateChanged,
    required this.onStreamSourceChanged,
    this.onBack,
    this.onEditCamera,
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
  int _speed = 1;
  String? _playbackUrl;
  bool _playbackLoading = false;
  String? _playbackError;
  String? _windowEnd;
  bool _hasMore = false;
  String? _playbackStartTime;
  int _virtualTimeMs = 0;
  int _generation = 0;
  bool _reverseLoading = false;
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
    if (!_adoptedPlayer) {
      _pbPlayer?.dispose();
    }
    super.dispose();
  }

  void _clearSpeedTimer() {
    _speedTimer?.cancel();
    _speedTimer = null;
    _reverseLoading = false;
    _generation++;
  }

  StreamSubscription? _seekSub;
  StreamSubscription? _completedSub;

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
    final mpv = _pbPlayer!.platform as NativePlayer;
    mpv.setProperty('hwdec', 'auto');
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
      if (completed && mounted && _speedTimer == null && _hasMore && _windowEnd != null) {
        _startPlayback(_windowEnd!);
      }
    });
  }

  Future<void> _startPlayback(String start) async {
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
    _speed = 1;
    _seekSub?.cancel();

    try {
      final session = await widget.recordingApi.startPlayback(
        widget.camera.id,
        start,
        widget.quality.param,
      );
      final fullUrl = widget.recordingApi.getSegmentUrl(session.segmentUrl);

      _ensurePlayer();
      _pbVideoReady = false;
      _pbPlayer!.open(Media(fullUrl));

      // Seek to offset within segment
      if (session.seekSeconds > 1.0) {
        _seekSub = _pbPlayer!.stream.duration.listen((dur) {
          if (dur > Duration.zero) {
            _pbPlayer!.seek(Duration(milliseconds: (session.seekSeconds * 1000).round()));
            _seekSub?.cancel();
          }
        });
      }

      setState(() {
        _playbackUrl = fullUrl;
        _windowEnd = session.segmentEnd;
        _hasMore = session.hasMore;
        if (_isLive) {
          _isLive = false;
          widget.playbackRef.isLive = false;
          widget.onLiveStateChanged(false);
        }
        _playbackStartTime = start;
        _virtualTimeMs = DateTime.parse(start).millisecondsSinceEpoch;
        _playbackLoading = false;
      });
    } catch (e) {
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
    });
  }

  void _onSpeedChanged(int newSpeed) {
    _clearSpeedTimer();
    setState(() => _speed = newSpeed);

    final player = _pbPlayer;

    // If in live mode, start playback first
    if (_isLive || player == null) {
      final startMs = DateTime.now().millisecondsSinceEpoch + _tzOffsetMs - 30 * 60 * 1000;
      final startStr = formatLocalISOFromMs(startMs);
      final gen = _generation;
      setState(() {
        _playbackLoading = true;
        _playbackError = null;
        _paused = false;
      });

      widget.recordingApi
          .startPlayback(widget.camera.id, startStr, widget.quality.param)
          .then((session) {
        if (_generation != gen || !mounted) return;
        final fullUrl = widget.recordingApi.getSegmentUrl(session.segmentUrl);

        _ensurePlayer();
        _pbVideoReady = false;
        _pbPlayer!.open(Media(fullUrl));

        if (session.seekSeconds > 1.0) {
          _seekSub?.cancel();
          _seekSub = _pbPlayer!.stream.duration.listen((dur) {
            if (dur > Duration.zero) {
              _pbPlayer!.seek(Duration(milliseconds: (session.seekSeconds * 1000).round()));
              _seekSub?.cancel();
            }
          });
        }

        setState(() {
          _playbackUrl = fullUrl;
          _windowEnd = session.segmentEnd;
          _hasMore = session.hasMore;
          _isLive = false;
          _playbackStartTime = startStr;
          _virtualTimeMs = startMs;
          _playbackLoading = false;
        });

        // Wait for player to be ready, then apply speed
        _pbPlayer!.stream.duration.listen((duration) {
          if (_generation != gen || duration == Duration.zero) return;
          _applySpeedToPlayer(newSpeed);
        });
      }).catchError((e) {
        if (_generation != gen || !mounted) return;
        setState(() {
          _playbackError = e.toString();
          _playbackLoading = false;
        });
      });
      return;
    }

    // Sync virtual time
    if (_playbackStartTime != null) {
      final startMs = DateTime.parse(_playbackStartTime!).millisecondsSinceEpoch;
      _virtualTimeMs = startMs + player.state.position.inMilliseconds;
    }

    _applySpeedToPlayer(newSpeed);
  }

  void _applySpeedToPlayer(int speed) {
    final player = _pbPlayer;
    if (player == null) return;

    // Native playback rate for 1x-4x
    if (speed >= 1 && speed <= 4) {
      player.setRate(speed.toDouble());
      player.play();
      return;
    }

    // Reverse
    if (speed < 0) {
      _startReverseInterval(speed);
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
        _startPlayback(formatLocalISOFromMs(_virtualTimeMs));
      } else {
        p.seek(newPos);
      }
    });
  }

  void _startReverseInterval(int reverseSpeed) {
    final player = _pbPlayer;
    if (player == null) return;
    player.pause();

    final gen = _generation;
    const tickMs = 500;
    final jumpMs = reverseSpeed * tickMs; // negative

    _speedTimer = Timer.periodic(const Duration(milliseconds: tickMs), (_) {
      if (_reverseLoading || _generation != gen || _pbPlayer == null) return;
      final p = _pbPlayer!;
      final proposed = p.state.position.inMilliseconds + jumpMs;

      if (proposed <= 500) {
        // Load previous window
        _reverseLoading = true;
        if (_playbackStartTime != null) {
          _virtualTimeMs = DateTime.parse(_playbackStartTime!).millisecondsSinceEpoch;
        }
        final windowMs = 30 * 60 * 1000;
        final newStartMs = _virtualTimeMs - windowMs;
        final newStart = formatLocalISOFromMs(newStartMs);

        widget.recordingApi
            .startPlayback(widget.camera.id, newStart, widget.quality.param)
            .then((session) {
          if (_generation != gen || !mounted) return;
          final fullUrl = widget.recordingApi.getSegmentUrl(session.segmentUrl);

          _ensurePlayer();
          _pbVideoReady = false;
          _pbPlayer!.open(Media(fullUrl));

          _pbPlayer!.stream.duration.listen((dur) {
            if (_generation != gen || dur == Duration.zero) return;
            final seekTarget = dur - const Duration(seconds: 1);
            _pbPlayer!.seek(seekTarget);
            _virtualTimeMs = newStartMs + seekTarget.inMilliseconds;
            setState(() {
              _playbackUrl = fullUrl;
              _windowEnd = session.segmentEnd;
              _hasMore = session.hasMore;
              _playbackStartTime = newStart;
            });
            _reverseLoading = false;
          });
        }).catchError((_) {
          if (_generation != gen) return;
          _clearSpeedTimer();
          setState(() {
            _speed = 1;
            _playbackError = 'No earlier recordings';
          });
          Future.delayed(const Duration(seconds: 3), () {
            if (mounted) setState(() => _playbackError = null);
          });
        });
      } else {
        p.seek(Duration(milliseconds: proposed));
        _virtualTimeMs += jumpMs;
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
    if (_speed >= 16 || _speed <= -1) return _virtualTimeMs + _tzOffsetMs;
    final p = _pbPlayer;
    if (p != null && _playbackStartTime != null) {
      final startMs = DateTime.parse(_playbackStartTime!).millisecondsSinceEpoch;
      return startMs + p.state.position.inMilliseconds + _tzOffsetMs;
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
        focusNode: FocusNode()..requestFocus(),
        onKeyEvent: _handleKey,
        child: Column(
          children: [
            // Header
            _buildHeader(running),
            // Video stats
            if (_showStats) _buildStatsBar(),
            // Video area
            Expanded(child: _buildVideoArea(running, rot)),
            // Timeline
            TimelineWidget(
              cameraId: widget.camera.id,
              recordingApi: widget.recordingApi,
              clipApi: widget.clipApi,
              motionApi: widget.motionApi,
              tzOffsetMs: _tzOffsetMs,
              isLive: _isLive,
              isPaused: _paused,
              compact: false,
              onPlayback: _startPlayback,
              onLive: _goLive,
              speed: _speed,
              onSpeedChanged: _onSpeedChanged,
              getNvrTime: _getNvrTime,
              initialDate: widget.initialPlaybackTime?.substring(0, 10),
              cameras: widget.cameras.where((c) => c.enabled).toList(),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildHeader(bool running) {
    final isAndroid = Platform.isAndroid;
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
                IconButton(
                  icon: const Icon(Icons.bar_chart, size: 20),
                  tooltip: 'Video Stats',
                  onPressed: _toggleStats,
                  padding: EdgeInsets.zero,
                  constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
                ),
                IconButton(
                  icon: const Icon(Icons.bug_report, size: 20),
                  tooltip: 'Report a Bug',
                  onPressed: () => _showBugReportDialog(context),
                  padding: EdgeInsets.zero,
                  constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
                ),
                if (widget.onEditCamera != null)
                  IconButton(
                    icon: const Icon(Icons.settings, size: 20),
                    tooltip: 'Camera Settings',
                    onPressed: () => widget.onEditCamera!(widget.camera),
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
                  ),
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
      final mpv = player.platform as NativePlayer;
      final codec = await mpv.getProperty('video-codec');
      final w = await mpv.getProperty('video-params/w');
      final h = await mpv.getProperty('video-params/h');
      // Try multiple mpv FPS properties in order of reliability
      var fps = '';
      for (final prop in ['container-fps', 'estimated-vf-fps', 'video-params/fps']) {
        final val = await mpv.getProperty(prop);
        final parsed = double.tryParse(val);
        if (parsed != null && parsed > 0 && parsed <= 120) {
          fps = val;
          break;
        }
      }
      final bitrate = await mpv.getProperty('video-bitrate');

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

  Future<void> _showBugReportDialog(BuildContext context) async {
    String? logs;
    bool copied = false;

    try {
      logs = await widget.systemApi.fetchRecentLogs(minutes: 10);
    } catch (e) {
      logs = 'Failed to fetch logs: $e';
    }

    if (!mounted) return;

    showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setDialogState) => AlertDialog(
          title: const Text('Report a Bug'),
          content: SizedBox(
            width: double.maxFinite,
            height: 400,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Logs from the last 10 minutes:',
                    style: TextStyle(color: Colors.grey[400], fontSize: 13)),
                const SizedBox(height: 8),
                Expanded(
                  child: Container(
                    padding: const EdgeInsets.all(8),
                    decoration: BoxDecoration(
                      color: Colors.black,
                      borderRadius: BorderRadius.circular(6),
                    ),
                    child: SelectionArea(
                      child: SingleChildScrollView(
                        child: Text(
                          logs ?? 'No logs available.',
                          style: const TextStyle(
                            fontFamily: 'monospace', fontSize: 11, color: Color(0xFFCCCCCC)),
                        ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    ElevatedButton.icon(
                      onPressed: () {
                        Clipboard.setData(ClipboardData(text: logs ?? ''));
                        setDialogState(() => copied = true);
                      },
                      icon: Icon(copied ? Icons.check : Icons.copy, size: 16),
                      label: Text(copied ? 'Copied!' : 'Copy Logs'),
                    ),
                    const SizedBox(width: 12),
                    ElevatedButton.icon(
                      onPressed: () => launchUrl(
                        Uri.parse('https://github.com/richard1912/RichIris/issues/new'),
                        mode: LaunchMode.externalApplication,
                      ),
                      icon: const Icon(Icons.open_in_new, size: 16),
                      label: const Text('Open GitHub Issues'),
                    ),
                  ],
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('Close'),
            ),
          ],
        ),
      ),
    );
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
      if (_isLive) {
        setState(() => _paused = !_paused);
      } else {
        final p = _pbPlayer;
        if (p != null) {
          p.state.playing ? p.pause() : p.play();
        }
      }
    }
  }
}
