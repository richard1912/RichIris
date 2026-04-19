import 'dart:async';
import 'dart:io' show Platform;
import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../config/constants.dart';
import '../models/camera.dart';
import '../models/grid_layout.dart';
import '../models/system_status.dart';
import '../services/stream_api.dart';
import '../services/recording_api.dart';
import '../services/clip_api.dart';
import '../services/motion_api.dart';
import '../services/system_api.dart';
import '../services/timeline_cache.dart';
import '../models/playback_session.dart';
import '../models/playback_ref.dart';
import '../services/camera_api.dart';
import '../services/update_service.dart';
import '../utils/time_utils.dart';
import '../utils/playback_benchmark.dart';
import '../widgets/bug_report_dialog.dart';
import '../models/camera_group.dart';
import '../services/group_api.dart';
import '../widgets/camera_grid.dart';
import '../widgets/group_chip_bar.dart';
import '../widgets/layout_picker_button.dart';
import '../widgets/version_info_dialog.dart';
import '../widgets/quality_selector.dart';
import '../widgets/timeline/timeline_widget.dart';

class HomeScreen extends StatefulWidget {
  final List<Camera> cameras;
  final List<Camera> allCameras;
  final List<CameraGroup> groups;
  final int? selectedGroupId;
  final ValueChanged<int?> onGroupSelected;
  final String layoutId;
  final ValueChanged<String> onLayoutChanged;
  final VoidCallback onGroupsChanged;
  final GroupApi groupApi;
  final SystemStatus? systemStatus;
  final Quality quality;
  final StreamSource streamSource;
  final StreamApi streamApi;
  final RecordingApi recordingApi;
  final ClipApi clipApi;
  final MotionApi motionApi;
  final CameraApi cameraApi;
  final SystemApi systemApi;
  final TimelineCache timelineCache;
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
  final VoidCallback? onOpenFaces;
  final VoidCallback onAddCamera;
  final ValueChanged<Camera> onEditCamera;
  final ValueChanged<Camera>? onAddToGroup;
  final Future<void> Function(List<int>) onReorder;
  final ValueChanged<bool> onDragStateChanged;
  final PlaybackRef playbackRef;
  final String? resumePlaybackTime;
  final int resumePlaybackGen;
  final int resumeLiveGen;

  const HomeScreen({
    super.key,
    required this.cameras,
    required this.allCameras,
    required this.groups,
    this.selectedGroupId,
    required this.onGroupSelected,
    required this.layoutId,
    required this.onLayoutChanged,
    required this.onGroupsChanged,
    required this.groupApi,
    this.systemStatus,
    required this.quality,
    required this.streamSource,
    required this.streamApi,
    required this.recordingApi,
    required this.clipApi,
    required this.motionApi,
    required this.cameraApi,
    required this.systemApi,
    required this.timelineCache,
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
    this.onOpenFaces,
    required this.onAddCamera,
    required this.onEditCamera,
    this.onAddToGroup,
    required this.onReorder,
    required this.onDragStateChanged,
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
  final Map<int, StreamSubscription> _seekSubs = {};
  final Map<int, StreamSubscription> _firstFrameSubs = {};
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
    for (final sub in _seekSubs.values) {
      sub.cancel();
    }
    for (final sub in _firstFrameSubs.values) {
      sub.cancel();
    }
    for (final p in _pbPlayers.values) {
      p.dispose();
    }
    super.dispose();
  }

  Player _ensurePlayer(int cameraId) {
    if (!_pbPlayers.containsKey(cameraId)) {
      final player = Player(
        configuration: PlayerConfiguration(
          vo: 'gpu',
          logLevel: MPVLogLevel.warn,
        ),
      );
      (player.platform as NativePlayer).setProperty('hwdec', 'auto');
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
    for (final sub in _seekSubs.values) {
      sub.cancel();
    }
    _seekSubs.clear();
    for (final sub in _firstFrameSubs.values) {
      sub.cancel();
    }
    _firstFrameSubs.clear();
    for (final p in _pbPlayers.values) {
      p.stop();
    }

    final enabledCameras = widget.cameras.where((c) => c.enabled).toList();
    // PlaybackBenchmark.start() was called in the timeline tap handler so that
    // gesture-to-handler latency is included. If something else triggered
    // playback (segment continuation), start a fresh trace here.
    final bench = PlaybackBenchmark.current ??
        PlaybackBenchmark.start(
            quality: widget.quality.param,
            cameraIds: enabledCameras.map((c) => c.id).toList());
    bench.mark('start_playback_enter',
        extra: {'quality': widget.quality.param, 'cameras': enabledCameras.length});

    // Set playback start immediately so _getNvrTime returns the intended time
    // even while sessions are still loading (prevents fallback to DateTime.now()
    // which causes 404 when fullscreen is entered before sessions finish).
    _playbackStartIso = start;
    _playbackWallStartMs = null;
    _syncRefState();

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

    // Concurrency tuning: 7 cameras × `-c copy` ffmpeg saturates the disk, but
    // strictly serial accumulates running ffmpegs (each one keeps writing to
    // feed its libmpv buffer) so later cameras hit growing contention. Sweet
    // spot empirically is ~3 in flight at a time for direct quality. Transcoded
    // qualities are GPU-bound (NVENC) so we cap at 2.
    final isDirect = widget.quality == Quality.direct;
    final batchSize = isDirect ? 3 : 2;

    Future<void> fetchAndOpen(Camera cam) async {
      if (_generation != gen || widget.fullscreenCameraId != null) return;
      try {
        bench.mark('api_request_start', extra: {'camera': cam.id});
        final session = await widget.recordingApi.startPlayback(
          cam.id, start, widget.quality.param,
          benchId: bench.id,
        );
        if (_generation != gen || !mounted) return;
        bench.mark('api_response_received',
            extra: {'camera': cam.id, 'seek_s': session.seekSeconds.toStringAsFixed(2)});
        // Anchor the master wall clock on the first session that resolves.
        _playbackWallStartMs ??= DateTime.now().millisecondsSinceEpoch;
        if (mounted) {
          setState(() => _pbLoading.remove(cam.id));
        }
        _openCameraSession(cam.id, session, gen);
      } catch (_) {
        if (_generation == gen && mounted) {
          setState(() {
            _pbLoading.remove(cam.id);
            _pbFailed.add(cam.id);
          });
        }
      }
    }

    for (var i = 0; i < enabledCameras.length; i += batchSize) {
      if (_generation != gen || widget.fullscreenCameraId != null) return;
      final batch = enabledCameras.skip(i).take(batchSize).toList();
      await Future.wait(batch.map(fetchAndOpen));
    }
  }

  /// Opens a player for a single camera session and wires up segment continuation.
  void _openCameraSession(int cameraId, PlaybackSession session, int gen) {
    final fullUrl = widget.recordingApi.getSegmentUrl(session.segmentUrl);
    final player = _ensurePlayer(cameraId);
    final bench = PlaybackBenchmark.current;
    bench?.mark('player_open', extra: {'camera': cameraId});
    player.open(Media(fullUrl));

    // Detect first decoded frame: position stream emits a non-zero value once
    // the player actually has a frame to render. Only the first camera's first
    // frame finalizes the bench summary.
    if (bench != null) {
      _firstFrameSubs[cameraId]?.cancel();
      _firstFrameSubs[cameraId] = player.stream.position.listen((pos) {
        if (pos > Duration.zero && identical(PlaybackBenchmark.current, bench)) {
          bench.mark('first_frame', extra: {'camera': cameraId});
          bench.summary(finalPhase: 'bench_complete');
          _firstFrameSubs[cameraId]?.cancel();
          _firstFrameSubs.remove(cameraId);
        }
      });
    }

    // No client-side seek: backend ffmpeg pre-seeks via -ss, so the served
    // fMP4's PTS=0 already corresponds to the user's chosen time. seekSeconds
    // is metadata-only (timeline display alignment).

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
      // Use master clock as the start so the backend pre-seeks this camera to
      // the correct point — keeps it aligned with cameras that didn't hit a
      // segment boundary, without needing a client-side seek inside the fMP4.
      final masterIso = _masterTimeIso();
      final start = masterIso ?? segmentEnd;
      final session = await widget.recordingApi.startPlayback(
        cameraId, start, widget.quality.param,
      );
      if (_generation != gen || !mounted) return;

      final fullUrl = widget.recordingApi.getSegmentUrl(session.segmentUrl);
      final player = _pbPlayers[cameraId];
      if (player == null) return;
      player.open(Media(fullUrl));

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
    for (final sub in _seekSubs.values) {
      sub.cancel();
    }
    _seekSubs.clear();
    for (final sub in _firstFrameSubs.values) {
      sub.cancel();
    }
    _firstFrameSubs.clear();
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
      final startMs = DateTime.parse(_playbackStartIso!).millisecondsSinceEpoch;
      // Use a reference player's actual position so the playhead stops
      // when the stream freezes/buffers instead of advancing blindly.
      final refId = widget.selectedCameraId;
      final refPlayer = refId != null ? _pbPlayers[refId] : _pbPlayers.values.firstOrNull;
      if (refPlayer != null) {
        return startMs + refPlayer.state.position.inMilliseconds + widget.tzOffsetMs;
      }
      // No player ready yet (sessions still loading) — return the intended
      // playback start time so fullscreen entry doesn't fall back to now().
      return startMs + widget.tzOffsetMs;
    }
    return DateTime.now().millisecondsSinceEpoch + widget.tzOffsetMs;
  }

  Future<void> _showBugReportDialog(BuildContext context) =>
      showBugReportDialog(context, systemApi: widget.systemApi);

  /// Refresh every camera's feed in the grid — fully tears down the current
  /// streams and re-opens them from scratch. Live: iterates every enabled
  /// camera and does stop+reopen on its live player. Playback: delegates to
  /// `_startPlayback(currentIso)` which already restarts every enabled
  /// camera's transcode session at the current scrub position.
  Future<void> _refreshFeed() async {
    // Log to backend so users can verify the refresh in the Report a Bug
    // log viewer (debugPrint only writes to client-side stdout).
    unawaited(widget.systemApi.logClientEvent(
      event: 'refresh_feed',
      details: {'screen': 'grid', 'mode': _isLive ? 'live' : 'playback'},
    ));
    if (_isLive) {
      final enabled = widget.cameras.where((c) => c.enabled).toList();
      debugPrint('[REFRESH] grid live cameras=${enabled.length}');
      for (final cam in enabled) {
        final player = widget.livePlayers[cam.id];
        if (player == null) continue;
        final url = widget.streamApi.liveUrl(
          cam.id,
          widget.streamSource.param,
          widget.quality.param,
          cameraName: cam.name,
        );
        debugPrint('[REFRESH] grid live cam=${cam.id} url=$url');
        await player.stop();
        await player.open(Media(url));
      }
    } else {
      final currentMs = _getNvrTime() - widget.tzOffsetMs;
      final iso = formatLocalISOFromMs(currentMs);
      debugPrint('[REFRESH] grid playback iso=$iso');
      await _startPlayback(iso);
    }
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
            icon: const Icon(Icons.add, size: 20),
            tooltip: 'Add Camera',
            onPressed: widget.onAddCamera,
          ),
          LayoutPickerButton(
            currentLayoutId: widget.layoutId,
            onLayoutChanged: widget.onLayoutChanged,
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
            const SizedBox(width: 4),
            IconButton(
              icon: const Icon(Icons.refresh, size: 20),
              tooltip: 'Refresh all feeds',
              onPressed: _refreshFeed,
            ),
            const SizedBox(width: 8),
          ],
          if (widget.onOpenFaces != null)
            IconButton(
              icon: const Icon(Icons.face_retouching_natural, size: 20),
              tooltip: 'Faces',
              onPressed: widget.onOpenFaces,
            ),
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
                      IconButton(
                        icon: const Icon(Icons.refresh, size: 20),
                        tooltip: 'Refresh all feeds',
                        onPressed: _refreshFeed,
                        padding: EdgeInsets.zero,
                        constraints: const BoxConstraints(minWidth: 36, minHeight: 32),
                      ),
                    ],
                  ),
                ),
              )
            : null,
      ),
      body: Column(
        children: [
          GroupChipBar(
            groups: widget.groups,
            selectedGroupId: widget.selectedGroupId,
            onGroupSelected: widget.onGroupSelected,
            onGroupsChanged: widget.onGroupsChanged,
            groupApi: widget.groupApi,
          ),
          Expanded(
            child: CameraGrid(
              cameras: widget.cameras,
              allCameras: widget.allCameras,
              systemStatus: widget.systemStatus,
              streamApi: widget.streamApi,
              streamSource: widget.streamSource.param,
              quality: widget.quality.param,
              layout: gridLayoutById(widget.layoutId),
              selectedCameraId: widget.selectedCameraId,
              onCameraSelected: widget.onCameraSelected,
              onEditCamera: widget.onEditCamera,
              onAddToGroup: widget.onAddToGroup,
              onReorder: widget.onReorder,
              onDragStateChanged: widget.onDragStateChanged,
              livePlayers: _isLive ? widget.livePlayers : const {},
              liveControllers: _isLive ? widget.liveControllers : const {},
              fullscreenCameraId: widget.fullscreenCameraId,
              playbackControllers: _isLive ? const {} : _pbControllers,
              playbackLoading: _isLive ? const {} : _pbLoading,
              playbackFailed: _isLive ? const {} : _pbFailed,
            ),
          ),
          if (widget.selectedCameraId != null)
            TimelineWidget(
              cameraId: widget.selectedCameraId!,
              recordingApi: widget.recordingApi,
              clipApi: widget.clipApi,
              motionApi: widget.motionApi,
              timelineCache: widget.timelineCache,
              tzOffsetMs: widget.tzOffsetMs,
              isLive: _isLive,
              isPaused: _paused,
              compact: true,
              onPlayback: _startPlayback,
              onLive: _goLive,
              getNvrTime: _getNvrTime,
              initialDate: _timelineDateOverride,
              cameras: widget.allCameras.where((c) => c.enabled).toList(),
            ),
        ],
      ),
    );
  }
}
