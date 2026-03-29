import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../config/constants.dart';
import '../models/camera.dart';
import '../models/system_status.dart';
import '../services/stream_api.dart';
import '../services/recording_api.dart';
import '../services/clip_api.dart';
import '../services/system_api.dart';
import '../utils/time_utils.dart';
import '../utils/format_utils.dart';
import '../widgets/live_player.dart';
import '../widgets/quality_selector.dart';
import '../widgets/timeline/timeline_widget.dart';

class FullscreenScreen extends StatefulWidget {
  final Camera camera;
  final StreamStatus? stream;
  final Quality quality;
  final StreamSource streamSource;
  final StreamApi streamApi;
  final RecordingApi recordingApi;
  final ClipApi clipApi;
  final SystemApi systemApi;
  final int tzOffsetMs;
  final ValueChanged<Quality> onQualityChanged;
  final ValueChanged<StreamSource> onStreamSourceChanged;
  final VoidCallback? onBack;

  const FullscreenScreen({
    super.key,
    required this.camera,
    this.stream,
    required this.quality,
    required this.streamSource,
    required this.streamApi,
    required this.recordingApi,
    required this.clipApi,
    required this.systemApi,
    required this.tzOffsetMs,
    required this.onQualityChanged,
    required this.onStreamSourceChanged,
    this.onBack,
  });

  @override
  State<FullscreenScreen> createState() => _FullscreenScreenState();
}

class _FullscreenScreenState extends State<FullscreenScreen> {
  bool _isLive = true;
  bool _paused = false;
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

  int _tzOffsetMs = 0;

  @override
  void initState() {
    super.initState();
    _tzOffsetMs = widget.tzOffsetMs;
  }

  @override
  void dispose() {
    _clearSpeedTimer();
    _seekSub?.cancel();
    _pbPlayer?.dispose();
    super.dispose();
  }

  void _clearSpeedTimer() {
    _speedTimer?.cancel();
    _speedTimer = null;
    _reverseLoading = false;
    _generation++;
  }

  StreamSubscription? _seekSub;

  void _ensurePlayer() {
    if (_pbPlayer != null) return;
    _pbPlayer = Player();
    _pbController = VideoController(_pbPlayer!);
    _pbPlayer!.setVolume(0);
    _pbPlayer!.stream.completed.listen((completed) {
      if (completed && _speedTimer == null && _hasMore && _windowEnd != null) {
        _startPlayback(_windowEnd!);
      }
    });
  }

  Future<void> _startPlayback(String start) async {
    setState(() {
      _playbackLoading = true;
      _playbackError = null;
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
        _isLive = false;
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
    _pbPlayer?.stop();
    setState(() {
      _isLive = true;
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
            // Video area
            Expanded(child: _buildVideoArea(running, rot)),
            // Timeline
            TimelineWidget(
              cameraId: widget.camera.id,
              recordingApi: widget.recordingApi,
              clipApi: widget.clipApi,
              tzOffsetMs: _tzOffsetMs,
              isLive: _isLive,
              compact: false,
              onPlayback: _startPlayback,
              onLive: _goLive,
              speed: _speed,
              onSpeedChanged: _onSpeedChanged,
              getNvrTime: _getNvrTime,
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildHeader(bool running) {
    return Container(
      color: const Color(0xFF171717).withValues(alpha: 0.8),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: SafeArea(
        bottom: false,
        child: Row(
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
            StreamSourceSelector(
              value: widget.streamSource,
              onChanged: widget.onStreamSourceChanged,
            ),
            const SizedBox(width: 4),
            QualitySelector(
              value: widget.quality,
              onChanged: widget.onQualityChanged,
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
      final url = widget.streamApi.liveUrl(widget.camera.id, widget.streamSource.param, widget.quality.param);
      return LivePlayer(
        url: url,
        rotation: rot,
        fit: BoxFit.contain,
      );
    }

    // Playback mode
    if (_pbController != null) {
      Widget video = Video(
        controller: _pbController!,
        fit: BoxFit.contain,
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

    return const SizedBox.shrink();
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
