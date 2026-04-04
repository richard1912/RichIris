import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'config/api_config.dart';
import 'config/constants.dart';
import 'models/camera.dart';
import 'models/playback_ref.dart';
import 'utils/time_utils.dart';
import 'models/system_status.dart';
import 'screens/home_screen.dart';
import 'screens/fullscreen_screen.dart';
import 'screens/system_screen.dart';
import 'screens/settings_screen.dart';
import 'screens/camera_form_screen.dart';
import 'services/api_client.dart';
import 'services/camera_api.dart';
import 'services/recording_api.dart';
import 'services/clip_api.dart';
import 'services/motion_api.dart';
import 'services/system_api.dart';
import 'services/stream_api.dart';
import 'theme.dart';

class RichIrisApp extends StatefulWidget {
  const RichIrisApp({super.key});

  @override
  State<RichIrisApp> createState() => RichIrisAppState();
}

class RichIrisAppState extends State<RichIrisApp> with WidgetsBindingObserver {
  ApiClient? _apiClient;
  CameraApi? _cameraApi;
  RecordingApi? _recordingApi;
  ClipApi? _clipApi;
  MotionApi? _motionApi;
  SystemApi? _systemApi;
  StreamApi? _streamApi;

  String? _serverUrl;
  bool _loading = true;
  Quality _liveQuality = Quality.high;
  Quality _playbackQuality = Quality.direct;
  Quality get _quality => _isLive ? _liveQuality : _playbackQuality;
  bool _isLive = true;
  StreamSource _streamSource = StreamSource.s2;
  int _tzOffsetMs = 0;

  List<Camera> _cameras = [];
  SystemStatus? _systemStatus;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _loadSettings();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  Future<void> _loadSettings() async {
    final url = await getSavedServerUrl();
    final prefs = await SharedPreferences.getInstance();
    final lqName = prefs.getString(kQualityKey);
    final lq = Quality.values.firstWhere(
      (v) => v.name == lqName,
      orElse: () => Quality.high,
    );
    final pqName = prefs.getString(kPlaybackQualityKey);
    final pq = Quality.values.firstWhere(
      (v) => v.name == pqName,
      orElse: () => Quality.direct,
    );
    final sName = prefs.getString(kStreamSourceKey);
    final s = StreamSource.values.firstWhere(
      (v) => v.name == sName,
      orElse: () => StreamSource.s2,
    );
    setState(() {
      _liveQuality = lq;
      _playbackQuality = pq;
      _streamSource = s;
      if (url != null && url.isNotEmpty) {
        _serverUrl = url;
        _initApi(url);
      }
      _loading = false;
    });
  }

  void _initApi(String url) {
    _apiClient = ApiClient(url);
    _cameraApi = CameraApi(_apiClient!);
    _recordingApi = RecordingApi(_apiClient!);
    _clipApi = ClipApi(_apiClient!);
    _motionApi = MotionApi(_apiClient!);
    _systemApi = SystemApi(_apiClient!);
    _streamApi = StreamApi(_apiClient!);
    _fetchInitialData();
  }

  Future<void> _fetchInitialData() async {
    try {
      final results = await Future.wait([
        _systemApi!.fetchTzOffsetMs(),
        _cameraApi!.fetchAll(),
        _systemApi!.fetchStatus(),
      ]);
      if (mounted) {
        setState(() {
          _tzOffsetMs = results[0] as int;
          _cameras = results[1] as List<Camera>;
          _systemStatus = results[2] as SystemStatus;
        });
      }
    } catch (_) {}
  }

  Future<void> _refreshCameras() async {
    try {
      final cameras = await _cameraApi!.fetchAll();
      if (mounted) setState(() => _cameras = cameras);
    } catch (_) {}
  }

  Future<void> _refreshStatus() async {
    try {
      final status = await _systemApi!.fetchStatus();
      if (mounted) setState(() => _systemStatus = status);
    } catch (_) {}
  }

  void _onServerUrlSet(String url) {
    setState(() {
      _serverUrl = url;
      _initApi(url);
    });
  }

  void _onQualityChanged(Quality q) async {
    final prefs = await SharedPreferences.getInstance();
    if (_isLive) {
      setState(() => _liveQuality = q);
      await prefs.setString(kQualityKey, q.name);
    } else {
      setState(() => _playbackQuality = q);
      await prefs.setString(kPlaybackQualityKey, q.name);
    }
  }

  void _onLiveStateChanged(bool isLive) {
    setState(() => _isLive = isLive);
  }

  void _onStreamSourceChanged(StreamSource s) async {
    setState(() => _streamSource = s);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(kStreamSourceKey, s.name);
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'RichIris',
      theme: appTheme,
      debugShowCheckedModeBanner: false,
      home: _loading
          ? const Scaffold(body: Center(child: CircularProgressIndicator()))
          : _serverUrl == null
              ? SettingsScreen(onSaved: _onServerUrlSet)
              : _MainNav(
                  cameraApi: _cameraApi!,
                  recordingApi: _recordingApi!,
                  clipApi: _clipApi!,
                  motionApi: _motionApi!,
                  systemApi: _systemApi!,
                  streamApi: _streamApi!,
                  apiClient: _apiClient!,
                  cameras: _cameras,
                  systemStatus: _systemStatus,
                  quality: _quality,
                  streamSource: _streamSource,
                  tzOffsetMs: _tzOffsetMs,
                  onQualityChanged: _onQualityChanged,
                  onLiveStateChanged: _onLiveStateChanged,
                  onStreamSourceChanged: _onStreamSourceChanged,
                  onRefreshCameras: _refreshCameras,
                  onRefreshStatus: _refreshStatus,
                  onServerUrlChanged: _onServerUrlSet,
                  serverUrl: _serverUrl,
                ),
    );
  }
}

class _MainNav extends StatefulWidget {
  final CameraApi cameraApi;
  final RecordingApi recordingApi;
  final ClipApi clipApi;
  final MotionApi motionApi;
  final SystemApi systemApi;
  final StreamApi streamApi;
  final ApiClient apiClient;
  final List<Camera> cameras;
  final SystemStatus? systemStatus;
  final Quality quality;
  final StreamSource streamSource;
  final int tzOffsetMs;
  final ValueChanged<Quality> onQualityChanged;
  final ValueChanged<bool> onLiveStateChanged;
  final ValueChanged<StreamSource> onStreamSourceChanged;
  final Future<void> Function() onRefreshCameras;
  final Future<void> Function() onRefreshStatus;
  final ValueChanged<String> onServerUrlChanged;
  final String? serverUrl;

  const _MainNav({
    required this.cameraApi,
    required this.recordingApi,
    required this.clipApi,
    required this.motionApi,
    required this.systemApi,
    required this.streamApi,
    required this.apiClient,
    required this.cameras,
    required this.systemStatus,
    required this.quality,
    required this.streamSource,
    required this.tzOffsetMs,
    required this.onQualityChanged,
    required this.onLiveStateChanged,
    required this.onStreamSourceChanged,
    required this.onRefreshCameras,
    required this.onRefreshStatus,
    required this.onServerUrlChanged,
    required this.serverUrl,
  });

  @override
  State<_MainNav> createState() => _MainNavState();
}

class _MainNavState extends State<_MainNav> {
  int? _selectedCameraId;
  int? _fullscreenCameraId;
  bool _showSystem = false;
  final Map<int, Player> _livePlayers = {};
  final Map<int, VideoController> _liveControllers = {};
  Quality? _frozenGridQuality; // quality the grid last used; frozen while fullscreen

  // Shared playback state for seamless grid <-> fullscreen transitions
  final PlaybackRef _gridRef = PlaybackRef();
  final PlaybackRef _fullscreenRef = PlaybackRef();
  String? _fullscreenInitialTime;
  Player? _handoffPlayer;
  VideoController? _handoffController;
  String? _handoffStartTime;
  String? _resumePlaybackTime;
  int _resumePlaybackGen = 0;
  int _resumeLiveGen = 0;

  @override
  void initState() {
    super.initState();
    _startPolling();
  }

  void _startPolling() {
    Future.doWhile(() async {
      await Future.delayed(const Duration(milliseconds: kCameraRefreshMs));
      if (!mounted) return false;
      await widget.onRefreshCameras();
      await widget.onRefreshStatus();
      return mounted;
    });
  }

  void _exitFullscreen() {
    // Transfer fullscreen playback state back to grid
    if (!_fullscreenRef.isLive && _fullscreenRef.getNvrTime != null) {
      // Fullscreen was in playback — restart grid at fullscreen's current time
      // But skip if grid is already playing at approximately the same time
      final fsTime = _fullscreenRef.getNvrTime!();
      final gridTime = _gridRef.getNvrTime?.call();
      final timeDrift = gridTime != null ? (fsTime - gridTime).abs() : double.infinity;
      if (_gridRef.isLive || timeDrift > 5000) {
        _resumePlaybackTime = formatLocalISOFromMs(fsTime - widget.tzOffsetMs);
        _resumePlaybackGen++;
      }
    } else if (_fullscreenRef.isLive && !_gridRef.isLive) {
      // Fullscreen went live but grid is still in playback — make grid go live
      _resumeLiveGen++;
    }
    _fullscreenRef.isLive = true;
    _fullscreenRef.getNvrTime = null;
    setState(() {
      _fullscreenCameraId = null;
      _fullscreenInitialTime = null;
      _handoffPlayer = null;
      _handoffController = null;
      _handoffStartTime = null;
    });
  }

  @override
  void dispose() {
    for (final p in _livePlayers.values) {
      p.dispose();
    }
    super.dispose();
  }

  void _ensureLivePlayer(int cameraId) {
    if (_livePlayers.containsKey(cameraId)) return;
    final player = Player(
      configuration: PlayerConfiguration(
        vo: 'gpu',
        logLevel: MPVLogLevel.warn,
      ),
    );
    final mpv = player.platform as NativePlayer;
    mpv.setProperty('profile', 'low-latency');
    mpv.setProperty('cache', 'no');
    mpv.setProperty('cache-pause', 'no');
    mpv.setProperty('untimed', 'yes');
    mpv.setProperty('demuxer-max-bytes', '524288');
    mpv.setProperty('demuxer-readahead-secs', '0');
    mpv.setProperty('hwdec', 'auto');
    player.setVolume(0);
    _livePlayers[cameraId] = player;
    _liveControllers[cameraId] = VideoController(player);
  }

  StreamStatus? _streamForCamera(int cameraId) {
    return widget.systemStatus?.streams
        .where((s) => s.cameraId == cameraId)
        .firstOrNull;
  }

  @override
  Widget build(BuildContext context) {
    if (_showSystem) {
      return SystemScreen(
        systemApi: widget.systemApi,
        cameras: widget.cameras,
        systemStatus: widget.systemStatus,
        onBack: () => setState(() => _showSystem = false),
      );
    }

    // Pre-create players for all enabled, running cameras
    for (final cam in widget.cameras) {
      final stream = _streamForCamera(cam.id);
      if (cam.enabled && (stream?.running ?? false)) {
        _ensureLivePlayer(cam.id);
      }
    }
    // Prune players for removed cameras
    final cameraIds = widget.cameras.map((c) => c.id).toSet();
    _livePlayers.keys.where((id) => !cameraIds.contains(id)).toList().forEach((id) {
      _livePlayers.remove(id)?.dispose();
      _liveControllers.remove(id);
    });

    // Freeze grid quality while fullscreen is active so grid streams
    // don't compete with the fullscreen feed on quality changes.
    if (_fullscreenCameraId == null) {
      _frozenGridQuality = widget.quality;
    }
    final gridQuality = _frozenGridQuality ?? widget.quality;

    // Resolve fullscreen camera
    final fullscreenCam = _fullscreenCameraId != null
        ? widget.cameras.where((c) => c.id == _fullscreenCameraId).firstOrNull
        : null;

    // Camera was removed while in fullscreen — clear it after this frame
    if (_fullscreenCameraId != null && fullscreenCam == null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) setState(() => _fullscreenCameraId = null);
      });
    }

    // Stack keeps HomeScreen alive (Offstage) so grid feeds don't restart
    return PopScope(
      canPop: _selectedCameraId == null && fullscreenCam == null,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) {
          if (_fullscreenCameraId != null) {
            _exitFullscreen();
          } else if (_selectedCameraId != null) {
            setState(() => _selectedCameraId = null);
          }
        }
      },
      child: Stack(
      children: [
        Offstage(
          offstage: fullscreenCam != null,
          child: HomeScreen(
            cameras: widget.cameras,
            systemStatus: widget.systemStatus,
            quality: gridQuality,
            streamSource: widget.streamSource,
            streamApi: widget.streamApi,
            recordingApi: widget.recordingApi,
            clipApi: widget.clipApi,
            motionApi: widget.motionApi,
            cameraApi: widget.cameraApi,
            systemApi: widget.systemApi,
            tzOffsetMs: widget.tzOffsetMs,
            livePlayers: _livePlayers,
            liveControllers: _liveControllers,
            fullscreenCameraId: _fullscreenCameraId,
            selectedCameraId: _selectedCameraId,
            onCameraSelected: (id) {
              if (_selectedCameraId == id) {
                // Entering fullscreen — capture grid playback state
                String? playbackTime;
                Player? pbPlayer;
                VideoController? pbController;
                String? pbStartTime;
                if (!_gridRef.isLive && _gridRef.getNvrTime != null) {
                  final nvrMs = _gridRef.getNvrTime!();
                  playbackTime = formatLocalISOFromMs(nvrMs - widget.tzOffsetMs);
                  // Hand off the grid's player for seamless transition
                  pbPlayer = _gridRef.getPlayer?.call(id);
                  pbController = _gridRef.getController?.call(id);
                  pbStartTime = _gridRef.playbackStartIso;
                }
                setState(() {
                  _fullscreenCameraId = id;
                  _fullscreenInitialTime = playbackTime;
                  _handoffPlayer = pbPlayer;
                  _handoffController = pbController;
                  _handoffStartTime = pbStartTime;
                });
              } else {
                setState(() => _selectedCameraId = id);
              }
            },
            onQualityChanged: widget.onQualityChanged,
            onLiveStateChanged: widget.onLiveStateChanged,
            onStreamSourceChanged: widget.onStreamSourceChanged,
            onOpenSystem: () => setState(() => _showSystem = true),
            onOpenSettings: () {
              Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => SettingsScreen(
                  onSaved: widget.onServerUrlChanged,
                  initialUrl: widget.serverUrl,
                ),
              ));
            },
            onAddCamera: () async {
              await Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => CameraFormScreen(cameraApi: widget.cameraApi),
              ));
              await widget.onRefreshCameras();
            },
            onEditCamera: (cam) async {
              await Navigator.of(context).push(MaterialPageRoute(
                builder: (_) =>
                    CameraFormScreen(cameraApi: widget.cameraApi, camera: cam),
              ));
              await widget.onRefreshCameras();
            },
            playbackRef: _gridRef,
            resumePlaybackTime: _resumePlaybackTime,
            resumePlaybackGen: _resumePlaybackGen,
            resumeLiveGen: _resumeLiveGen,
          ),
        ),
        if (fullscreenCam != null)
          PopScope(
            canPop: false,
            onPopInvokedWithResult: (didPop, _) {
              if (!didPop) _exitFullscreen();
            },
            child: FullscreenScreen(
            camera: fullscreenCam,
            stream: _streamForCamera(_fullscreenCameraId!),
            quality: widget.quality,
            streamSource: widget.streamSource,
            streamApi: widget.streamApi,
            recordingApi: widget.recordingApi,
            clipApi: widget.clipApi,
            motionApi: widget.motionApi,
            systemApi: widget.systemApi,
            tzOffsetMs: widget.tzOffsetMs,
            livePlayer: _livePlayers[_fullscreenCameraId],
            liveController: _liveControllers[_fullscreenCameraId],
            onQualityChanged: widget.onQualityChanged,
            onLiveStateChanged: widget.onLiveStateChanged,
            onStreamSourceChanged: widget.onStreamSourceChanged,
            onBack: _exitFullscreen,
            playbackRef: _fullscreenRef,
            initialPlaybackTime: _fullscreenInitialTime,
            initialPbPlayer: _handoffPlayer,
            initialPbController: _handoffController,
            initialPlaybackStartTime: _handoffStartTime,
          )),
      ],
    ));
  }
}
