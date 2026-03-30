import 'dart:async';
import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../config/constants.dart';
import '../models/camera.dart';
import '../models/system_status.dart';
import '../services/stream_api.dart';
import '../services/recording_api.dart';
import '../services/clip_api.dart';
import '../services/motion_api.dart';
import '../models/playback_session.dart';
import '../services/camera_api.dart';
import '../widgets/camera_grid.dart';
import '../widgets/quality_selector.dart';
import '../widgets/timeline/timeline_widget.dart';

class HomeScreen extends StatefulWidget {
  final List<Camera> cameras;
  final SystemStatus? systemStatus;
  final Quality quality;
  final StreamSource streamSource;
  final StreamApi streamApi;
  final RecordingApi recordingApi;
  final ClipApi clipApi;
  final MotionApi motionApi;
  final CameraApi cameraApi;
  final int tzOffsetMs;
  final int? selectedCameraId;
  final ValueChanged<int> onCameraSelected;
  final ValueChanged<Quality> onQualityChanged;
  final ValueChanged<StreamSource> onStreamSourceChanged;
  final VoidCallback onOpenSystem;
  final VoidCallback onOpenSettings;
  final VoidCallback onAddCamera;
  final ValueChanged<Camera> onEditCamera;

  const HomeScreen({
    super.key,
    required this.cameras,
    this.systemStatus,
    required this.quality,
    required this.streamSource,
    required this.streamApi,
    required this.recordingApi,
    required this.clipApi,
    required this.motionApi,
    required this.cameraApi,
    required this.tzOffsetMs,
    this.selectedCameraId,
    required this.onCameraSelected,
    required this.onQualityChanged,
    required this.onStreamSourceChanged,
    required this.onOpenSystem,
    required this.onOpenSettings,
    required this.onAddCamera,
    required this.onEditCamera,
  });

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  bool _isLive = true;
  final Map<int, Player> _pbPlayers = {};
  final Map<int, VideoController> _pbControllers = {};
  final Map<int, StreamSubscription> _completedSubs = {};
  final Set<int> _pbLoading = {};
  final Set<int> _pbFailed = {};
  int _generation = 0;

  // Shared playback clock — single source of truth for all cameras
  String? _playbackStartIso;
  int? _playbackWallStartMs;

  @override
  void dispose() {
    for (final sub in _completedSubs.values) {
      sub.cancel();
    }
    for (final p in _pbPlayers.values) {
      p.dispose();
    }
    super.dispose();
  }

  Player _ensurePlayer(int cameraId) {
    if (!_pbPlayers.containsKey(cameraId)) {
      final player = Player();
      player.setVolume(0);
      _pbPlayers[cameraId] = player;
      _pbControllers[cameraId] = VideoController(player);
    }
    return _pbPlayers[cameraId]!;
  }

  /// Current master playback time as ISO string, derived from shared clock.
  String? _masterTimeIso() {
    if (_playbackStartIso == null || _playbackWallStartMs == null) return null;
    final elapsed = DateTime.now().millisecondsSinceEpoch - _playbackWallStartMs!;
    final start = DateTime.parse(_playbackStartIso!);
    return start.add(Duration(milliseconds: elapsed)).toIso8601String();
  }

  Future<void> _startPlayback(String start) async {
    _generation++;
    final gen = _generation;

    // Stop old players and cancel old listeners
    for (final sub in _completedSubs.values) {
      sub.cancel();
    }
    _completedSubs.clear();
    for (final p in _pbPlayers.values) {
      p.stop();
    }

    final enabledCameras = widget.cameras.where((c) => c.enabled).toList();

    setState(() {
      _isLive = false;
      _pbLoading.clear();
      _pbFailed.clear();
      for (final cam in enabledCameras) {
        _pbLoading.add(cam.id);
      }
    });

    // Fetch all sessions in parallel, then start all players together
    final sessions = <int, PlaybackSession>{};
    final futures = enabledCameras.map((cam) async {
      try {
        final session = await widget.recordingApi.startPlayback(
          cam.id, start, widget.quality.param,
        );
        sessions[cam.id] = session;
      } catch (_) {
        if (_generation == gen && mounted) {
          _pbFailed.add(cam.id);
        }
      }
    });
    await Future.wait(futures);
    if (_generation != gen || !mounted) return;

    // Set shared clock ONCE, then open all players simultaneously
    _playbackStartIso = start;
    _playbackWallStartMs = DateTime.now().millisecondsSinceEpoch;

    for (final entry in sessions.entries) {
      _pbLoading.remove(entry.key);
      _openCameraSession(entry.key, entry.value, gen);
    }
    if (mounted) setState(() {});
  }

  /// Opens a player for a single camera session and wires up segment continuation.
  void _openCameraSession(int cameraId, PlaybackSession session, int gen) {
    final fullUrl = widget.recordingApi.getSegmentUrl(session.segmentUrl);
    final player = _ensurePlayer(cameraId);
    player.open(Media(fullUrl));

    // Seek to offset within segment
    if (session.seekSeconds > 1.0) {
      late StreamSubscription sub;
      sub = player.stream.duration.listen((dur) {
        if (dur > Duration.zero && _generation == gen) {
          player.seek(Duration(milliseconds: (session.seekSeconds * 1000).round()));
          sub.cancel();
        }
      });
    }

    _completedSubs[cameraId]?.cancel();
    _completedSubs[cameraId] = player.stream.completed.listen((completed) {
      if (completed && session.hasMore && _generation == gen) {
        _continueCameraPlayback(cameraId, session.segmentEnd, gen);
      }
    });
  }

  Future<void> _continueCameraPlayback(int cameraId, String segmentEnd, int gen) async {
    if (_generation != gen || !mounted) return;
    try {
      final session = await widget.recordingApi.startPlayback(
        cameraId, segmentEnd, widget.quality.param,
      );
      if (_generation != gen || !mounted) return;

      // Use master clock to seek into the new segment so this camera
      // stays aligned with cameras that didn't have a segment boundary.
      final masterIso = _masterTimeIso();
      double seekOverride = session.seekSeconds;
      if (masterIso != null) {
        final masterTime = DateTime.parse(masterIso);
        final segStart = DateTime.parse(session.segmentStart.isNotEmpty
            ? session.segmentStart : segmentEnd);
        final drift = masterTime.difference(segStart).inMilliseconds / 1000.0;
        if (drift > 0) seekOverride = drift;
      }

      final fullUrl = widget.recordingApi.getSegmentUrl(session.segmentUrl);
      final player = _pbPlayers[cameraId];
      if (player == null) return;
      player.open(Media(fullUrl));

      if (seekOverride > 1.0) {
        late StreamSubscription sub;
        sub = player.stream.duration.listen((dur) {
          if (dur > Duration.zero && _generation == gen) {
            player.seek(Duration(milliseconds: (seekOverride * 1000).round()));
            sub.cancel();
          }
        });
      }

      _completedSubs[cameraId]?.cancel();
      _completedSubs[cameraId] = player.stream.completed.listen((completed) {
        if (completed && session.hasMore && _generation == gen) {
          _continueCameraPlayback(cameraId, session.segmentEnd, gen);
        }
      });
    } catch (_) {}
  }

  void _goLive() {
    _generation++;
    for (final sub in _completedSubs.values) {
      sub.cancel();
    }
    _completedSubs.clear();
    for (final p in _pbPlayers.values) {
      p.stop();
    }
    _playbackStartIso = null;
    _playbackWallStartMs = null;
    setState(() {
      _isLive = true;
      _pbLoading.clear();
      _pbFailed.clear();
    });
  }

  int _getNvrTime() {
    return DateTime.now().millisecondsSinceEpoch + widget.tzOffsetMs;
  }

  @override
  Widget build(BuildContext context) {
    final activeCount = widget.systemStatus?.activeStreams ?? 0;
    final totalCount = widget.systemStatus?.totalCameras ?? widget.cameras.length;

    return Scaffold(
      appBar: AppBar(
        title: const Text('RichIris', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
        actions: [
          Center(
            child: Padding(
              padding: const EdgeInsets.only(right: 8),
              child: Text(
                '$activeCount/$totalCount active',
                style: const TextStyle(fontSize: 12, color: Color(0xFF737373)),
              ),
            ),
          ),
          if (!_isLive)
            const Padding(
              padding: EdgeInsets.only(right: 8),
              child: Center(
                child: Text('Playback',
                    style: TextStyle(fontSize: 12, color: Color(0xFF3B82F6))),
              ),
            ),
          StreamSourceSelector(value: widget.streamSource, onChanged: widget.onStreamSourceChanged),
          const SizedBox(width: 4),
          QualitySelector(value: widget.quality, onChanged: widget.onQualityChanged),
          const SizedBox(width: 8),
          IconButton(
            icon: const Icon(Icons.storage, size: 20),
            tooltip: 'System',
            onPressed: widget.onOpenSystem,
          ),
          IconButton(
            icon: const Icon(Icons.settings, size: 20),
            tooltip: 'Settings',
            onPressed: widget.onOpenSettings,
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: CameraGrid(
              cameras: widget.cameras,
              systemStatus: widget.systemStatus,
              streamApi: widget.streamApi,
              streamSource: widget.streamSource.param,
              quality: widget.quality.param,
              selectedCameraId: widget.selectedCameraId,
              onCameraSelected: widget.onCameraSelected,
              onEditCamera: widget.onEditCamera,
              onAddCamera: widget.onAddCamera,
              playbackControllers: _isLive ? const {} : _pbControllers,
              playbackLoading: _isLive ? const {} : _pbLoading,
              playbackFailed: _isLive ? const {} : _pbFailed,
            ),
          ),
          if (widget.selectedCameraId != null)
            SizedBox(
              height: 120,
              child: TimelineWidget(
                cameraId: widget.selectedCameraId!,
                recordingApi: widget.recordingApi,
                clipApi: widget.clipApi,
                motionApi: widget.motionApi,
                tzOffsetMs: widget.tzOffsetMs,
                isLive: _isLive,
                compact: true,
                onPlayback: _startPlayback,
                onLive: _goLive,
                getNvrTime: _getNvrTime,
              ),
            ),
        ],
      ),
    );
  }
}
