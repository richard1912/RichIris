import 'dart:async';
import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../config/constants.dart';
import '../models/camera.dart';
import '../models/playback_session.dart';
import '../models/system_status.dart';
import '../services/stream_api.dart';
import '../services/recording_api.dart';
import '../services/clip_api.dart';
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
  String? _playbackStartTime;
  final Map<int, String> _pbStartTimes = {};
  int _generation = 0;

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

    // Kill old backend ffmpeg processes before starting new ones
    await widget.recordingApi.stopAllPlayback();
    if (_generation != gen || !mounted) return;

    final enabledCameras = widget.cameras.where((c) => c.enabled).toList();

    setState(() {
      _isLive = false;
      _playbackStartTime = start;
      _pbStartTimes.clear();
      _pbLoading.clear();
      for (final cam in enabledCameras) {
        _pbLoading.add(cam.id);
        _pbStartTimes[cam.id] = start;
      }
    });

    // Fire all API requests in parallel, collect results
    final futures = <int, Future<PlaybackSession?>>{};
    for (final cam in enabledCameras) {
      futures[cam.id] = widget.recordingApi
          .startPlayback(cam.id, start, widget.quality.param)
          .then<PlaybackSession?>((s) => s)
          .catchError((_) => null as PlaybackSession?);
    }

    // Wait for all to complete
    final results = <int, PlaybackSession?>{};
    for (final entry in futures.entries) {
      results[entry.key] = await entry.value;
    }

    if (_generation != gen || !mounted) return;

    // Open all players together for sync
    for (final entry in results.entries) {
      final cameraId = entry.key;
      final session = entry.value;
      _pbLoading.remove(cameraId);

      if (session == null) continue;

      final fullUrl = widget.recordingApi.getPlaybackMp4Url(session.playbackUrl);
      final player = _ensurePlayer(cameraId);
      player.open(Media(fullUrl));

      _completedSubs[cameraId]?.cancel();
      _completedSubs[cameraId] = player.stream.completed.listen((completed) {
        if (completed && session.hasMore && _generation == gen) {
          _continueCameraPlayback(cameraId, session.windowEnd, gen);
        }
      });
    }

    if (mounted) setState(() {});
  }

  Future<void> _continueCameraPlayback(int cameraId, String start, int gen) async {
    if (_generation != gen || !mounted) return;
    try {
      final session = await widget.recordingApi.startPlayback(
        cameraId, start, widget.quality.param,
      );
      if (_generation != gen || !mounted) return;
      _pbStartTimes[cameraId] = start;
      final fullUrl = widget.recordingApi.getPlaybackMp4Url(session.playbackUrl);
      final player = _pbPlayers[cameraId];
      if (player == null) return;
      player.open(Media(fullUrl));

      _completedSubs[cameraId]?.cancel();
      _completedSubs[cameraId] = player.stream.completed.listen((completed) {
        if (completed && session.hasMore && _generation == gen) {
          _continueCameraPlayback(cameraId, session.windowEnd, gen);
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
    widget.recordingApi.stopAllPlayback();
    setState(() {
      _isLive = true;
      _playbackStartTime = null;
      _pbStartTimes.clear();
      _pbLoading.clear();
    });
  }

  int _getNvrTime() {
    if (_isLive) return DateTime.now().millisecondsSinceEpoch + widget.tzOffsetMs;
    if (_playbackStartTime != null) {
      for (final entry in _pbPlayers.entries) {
        final p = entry.value;
        if (p.state.duration > Duration.zero) {
          final camStart = _pbStartTimes[entry.key] ?? _playbackStartTime!;
          final startMs = DateTime.parse(camStart).millisecondsSinceEpoch;
          return startMs + p.state.position.inMilliseconds + widget.tzOffsetMs;
        }
      }
      return DateTime.parse(_playbackStartTime!).millisecondsSinceEpoch + widget.tzOffsetMs;
    }
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
            ),
          ),
          if (widget.selectedCameraId != null)
            SizedBox(
              height: 120,
              child: TimelineWidget(
                cameraId: widget.selectedCameraId!,
                recordingApi: widget.recordingApi,
                clipApi: widget.clipApi,
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
