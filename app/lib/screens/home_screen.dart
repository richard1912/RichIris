import 'dart:async';
import 'dart:io' show Platform;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:media_kit/media_kit.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../config/constants.dart';
import '../models/camera.dart';
import '../models/system_status.dart';
import '../services/stream_api.dart';
import '../services/recording_api.dart';
import '../services/clip_api.dart';
import '../services/motion_api.dart';
import '../services/system_api.dart';
import '../models/playback_session.dart';
import '../models/playback_ref.dart';
import '../services/camera_api.dart';
import '../services/update_service.dart';
import '../widgets/camera_grid.dart';
import '../widgets/version_info_dialog.dart';
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
  final SystemApi systemApi;
  final UpdateService updateService;
  final String appVersion;
  final int tzOffsetMs;
  final Map<int, Player> livePlayers;
  final Map<int, VideoController> liveControllers;
  final int? fullscreenCameraId;
  final int? selectedCameraId;
  final ValueChanged<int> onCameraSelected;
  final ValueChanged<Quality> onQualityChanged;
  final ValueChanged<bool> onLiveStateChanged;
  final ValueChanged<StreamSource> onStreamSourceChanged;
  final VoidCallback onOpenSystem;
  final VoidCallback onOpenSystemSettings;
  final VoidCallback onAddCamera;
  final ValueChanged<Camera> onEditCamera;
  final PlaybackRef playbackRef;
  final String? resumePlaybackTime;
  final int resumePlaybackGen;
  final int resumeLiveGen;

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
    required this.systemApi,
    required this.updateService,
    required this.appVersion,
    required this.tzOffsetMs,
    required this.livePlayers,
    required this.liveControllers,
    this.fullscreenCameraId,
    this.selectedCameraId,
    required this.onCameraSelected,
    required this.onQualityChanged,
    required this.onLiveStateChanged,
    required this.onStreamSourceChanged,
    required this.onOpenSystem,
    required this.onOpenSystemSettings,
    required this.onAddCamera,
    required this.onEditCamera,
    required this.playbackRef,
    this.resumePlaybackTime,
    this.resumePlaybackGen = 0,
    this.resumeLiveGen = 0,
  });

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  bool _isLive = true;
  bool _paused = false;
  final Map<int, Player> _pbPlayers = {};
  final Map<int, VideoController> _pbControllers = {};
  final Map<int, StreamSubscription> _completedSubs = {};
  final Set<int> _pbLoading = {};
  final Set<int> _pbFailed = {};
  int _generation = 0;

  // Shared playback clock — single source of truth for all cameras
  String? _playbackStartIso;
  int? _playbackWallStartMs;

  // Resume state tracking (from fullscreen transitions)
  int _lastResumePlaybackGen = 0;
  int _lastResumeLiveGen = 0;
  String? _timelineDateOverride;

  @override
  void initState() {
    super.initState();
    _setupRef();
  }

  void _setupRef() {
    final ref = widget.playbackRef;
    ref.getNvrTime = _getNvrTime;
    ref.getPlayer = (id) => _pbPlayers[id];
    ref.getController = (id) => _pbControllers[id];
    ref.detachPlayer = (id) {
      // Remove from maps without disposing — the receiver takes ownership
      _completedSubs[id]?.cancel();
      _completedSubs.remove(id);
      _pbPlayers.remove(id);
      _pbControllers.remove(id);
    };
  }

  void _syncRefState() {
    widget.playbackRef.playbackStartIso = _playbackStartIso;
  }

  @override
  void didUpdateWidget(covariant HomeScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    _setupRef();
    if (widget.resumePlaybackGen != _lastResumePlaybackGen) {
      _lastResumePlaybackGen = widget.resumePlaybackGen;
      if (widget.resumePlaybackTime != null) {
        _timelineDateOverride = widget.resumePlaybackTime!.substring(0, 10);
        _startPlayback(widget.resumePlaybackTime!);
      }
    }
    if (widget.resumeLiveGen != _lastResumeLiveGen) {
      _lastResumeLiveGen = widget.resumeLiveGen;
      if (!_isLive) _goLive();
    }
  }

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
      _paused = false;
      widget.playbackRef.isLive = false;
      widget.onLiveStateChanged(false);
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
    _syncRefState();

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

    // Keep ref session state up-to-date for the selected camera
    if (cameraId == widget.selectedCameraId) {
      widget.playbackRef.segmentEnd = session.segmentEnd;
      widget.playbackRef.hasMore = session.hasMore;
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
    if (_isLive) {
      setState(() => _paused = !_paused);
      return;
    }
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
      _paused = false;
      widget.playbackRef.isLive = true;
      widget.onLiveStateChanged(true);
      _pbLoading.clear();
      _pbFailed.clear();
    });
  }

  int _getNvrTime() {
    if (!_isLive && _playbackStartIso != null) {
      // Use a reference player's actual position so the playhead stops
      // when the stream freezes/buffers instead of advancing blindly.
      final refId = widget.selectedCameraId;
      final refPlayer = refId != null ? _pbPlayers[refId] : _pbPlayers.values.firstOrNull;
      if (refPlayer != null) {
        final startMs = DateTime.parse(_playbackStartIso!).millisecondsSinceEpoch;
        return startMs + refPlayer.state.position.inMilliseconds + widget.tzOffsetMs;
      }
    }
    return DateTime.now().millisecondsSinceEpoch + widget.tzOffsetMs;
  }

  Future<void> _showBugReportDialog(BuildContext context) async {
    String? logs;
    bool loading = true;
    bool copied = false;

    try {
      logs = await widget.systemApi.fetchRecentLogs(minutes: 10);
    } catch (e) {
      logs = 'Failed to fetch logs: $e';
    }
    loading = false;

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
                Text(
                  'Logs from the last 10 minutes:',
                  style: TextStyle(color: Colors.grey[400], fontSize: 13),
                ),
                const SizedBox(height: 8),
                Expanded(
                  child: loading
                      ? const Center(child: CircularProgressIndicator())
                      : Container(
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
                                  fontFamily: 'monospace',
                                  fontSize: 11,
                                  color: Color(0xFFCCCCCC),
                                ),
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

  @override
  Widget build(BuildContext context) {
    final activeCount = widget.systemStatus?.activeStreams ?? 0;
    final totalCount = widget.systemStatus?.totalCameras ?? widget.cameras.length;
    final isAndroid = Platform.isAndroid;

    final kofiButton = TextButton.icon(
      onPressed: () => launchUrl(
        Uri.parse('https://ko-fi.com/richard1912'),
        mode: LaunchMode.externalApplication,
      ),
      icon: const Icon(Icons.favorite, color: Colors.redAccent, size: 14),
      label: Text(
        'Support RichIris on Ko-fi',
        style: TextStyle(color: Colors.grey[400], fontSize: 12),
      ),
      style: TextButton.styleFrom(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        minimumSize: Size.zero,
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
      ),
    );

    return Scaffold(
      appBar: AppBar(
        toolbarHeight: 40,
        title: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text('RichIris', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
            if (widget.appVersion.isNotEmpty) ...[
              const SizedBox(width: 6),
              GestureDetector(
                onTap: () => showDialog(
                  context: context,
                  builder: (_) => VersionInfoDialog(
                    currentVersion: widget.appVersion,
                    updateService: widget.updateService,
                  ),
                ),
                child: Text(
                  'v${widget.appVersion}',
                  style: TextStyle(fontSize: 11, color: Colors.grey[500]),
                ),
              ),
            ],
            if (!isAndroid) ...[
              const SizedBox(width: 12),
              kofiButton,
            ],
          ],
        ),
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
          IconButton(
            icon: const Icon(Icons.bug_report, size: 20),
            tooltip: 'Report a Bug',
            onPressed: () => _showBugReportDialog(context),
          ),
          if (!isAndroid) ...[
            if (!_isLive)
              const Padding(
                padding: EdgeInsets.only(right: 8),
                child: Center(
                  child: Text('Playback',
                      style: TextStyle(fontSize: 12, color: Color(0xFF3B82F6))),
                ),
              ),
            if (_isLive) ...[
              StreamSourceSelector(value: widget.streamSource, onChanged: widget.onStreamSourceChanged),
              const SizedBox(width: 4),
            ],
            QualitySelector(value: widget.quality, onChanged: widget.onQualityChanged, isLive: _isLive),
            const SizedBox(width: 8),
          ],
          IconButton(
            icon: const Icon(Icons.storage, size: 20),
            tooltip: 'System Status',
            onPressed: widget.onOpenSystem,
          ),
          IconButton(
            icon: const Icon(Icons.settings, size: 20),
            tooltip: 'Settings',
            onPressed: widget.onOpenSystemSettings,
          ),
        ],
        bottom: isAndroid
            ? PreferredSize(
                preferredSize: const Size.fromHeight(32),
                child: Padding(
                  padding: const EdgeInsets.only(left: 12, right: 8, bottom: 4),
                  child: Row(
                    children: [
                      kofiButton,
                      const Spacer(),
                      if (!_isLive)
                        const Padding(
                          padding: EdgeInsets.only(right: 8),
                          child: Text('Playback',
                              style: TextStyle(fontSize: 12, color: Color(0xFF3B82F6))),
                        ),
                      if (_isLive) ...[
                        StreamSourceSelector(value: widget.streamSource, onChanged: widget.onStreamSourceChanged),
                        const SizedBox(width: 4),
                      ],
                      QualitySelector(value: widget.quality, onChanged: widget.onQualityChanged, isLive: _isLive),
                    ],
                  ),
                ),
              )
            : null,
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
              livePlayers: _isLive ? widget.livePlayers : const {},
              liveControllers: _isLive ? widget.liveControllers : const {},
              fullscreenCameraId: widget.fullscreenCameraId,
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
                isPaused: _paused,
                compact: true,
                onPlayback: _startPlayback,
                onLive: _goLive,
                getNvrTime: _getNvrTime,
                initialDate: _timelineDateOverride,
              ),
            ),
        ],
      ),
    );
  }
}
