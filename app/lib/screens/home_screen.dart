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

  void _ensurePlayer(int cameraId) {
    if (_pbPlayers.containsKey(cameraId)) return;
    final player = Player();
    player.setVolume(0);
    _pbPlayers[cameraId] = player;
    _pbControllers[cameraId] = VideoController(player);
  }

  Future<void> _startPlayback(String start) async {
    _generation++;
    final gen = _generation;

    setState(() {
      _isLive = false;
      _playbackStartTime = start;
      _pbLoading.clear();
    });

    final enabledCameras = widget.cameras.where((c) => c.enabled).toList();
    for (final cam in enabledCameras) {
      _pbLoading.add(cam.id);
    }
    setState(() {});

    // Stagger requests to avoid hammering the HDD with 6 concurrent ffmpeg processes
    for (final cam in enabledCameras) {
      if (_generation != gen || !mounted) return;
      await _startCameraPlayback(cam.id, start, gen);
    }
  }

  Future<void> _startCameraPlayback(int cameraId, String start, int gen) async {
    if (_generation != gen || !mounted) return;
    try {
      final session = await widget.recordingApi.startPlayback(
        cameraId,
        start,
        widget.quality.param,
      );
      if (_generation != gen || !mounted) return;
      final fullUrl = widget.recordingApi.getPlaybackMp4Url(session.playbackUrl);

      _ensurePlayer(cameraId);
      _pbPlayers[cameraId]!.open(Media(fullUrl));

      // Cancel previous completed listener before adding new one
      _completedSubs[cameraId]?.cancel();
      _completedSubs[cameraId] = _pbPlayers[cameraId]!.stream.completed.listen((completed) {
        if (completed && session.hasMore && _generation == gen) {
          _startCameraPlayback(cameraId, session.windowEnd, gen);
        }
      });

      setState(() => _pbLoading.remove(cameraId));
    } catch (_) {
      if (_generation == gen && mounted) {
        setState(() => _pbLoading.remove(cameraId));
      }
    }
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
    setState(() {
      _isLive = true;
      _playbackStartTime = null;
      _pbLoading.clear();
    });
  }

  int _getNvrTime() {
    if (_isLive) return DateTime.now().millisecondsSinceEpoch + widget.tzOffsetMs;
    if (_playbackStartTime != null) {
      for (final entry in _pbPlayers.entries) {
        final p = entry.value;
        if (p.state.duration > Duration.zero) {
          final startMs = DateTime.parse(_playbackStartTime!).millisecondsSinceEpoch;
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
