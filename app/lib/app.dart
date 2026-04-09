import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import 'package:package_info_plus/package_info_plus.dart';
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
import 'screens/system_settings_screen.dart';
import 'services/api_client.dart';
import 'services/camera_api.dart';
import 'services/recording_api.dart';
import 'services/clip_api.dart';
import 'services/motion_api.dart';
import 'services/backup_api.dart';
import 'services/settings_api.dart';
import 'services/system_api.dart';
import 'services/stream_api.dart';
import 'services/update_service.dart';
import 'widgets/update_dialog.dart';
import 'theme.dart';

class RichIrisApp extends StatefulWidget {
  final bool updateOnly;
  const RichIrisApp({super.key, this.updateOnly = false});

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
  SettingsApi? _settingsApi;
  BackupApi? _backupApi;
  UpdateService? _updateService;

  String? _serverUrl;
  String _appVersion = '';
  bool _loading = true;
  Quality _liveQuality = Quality.direct;
  Quality _playbackQuality = Quality.direct;
  Quality get _quality => _isLive ? _liveQuality : _playbackQuality;
  bool _isLive = true;
  StreamSource _streamSource = StreamSource.s1;
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
    try {
      final info = await PackageInfo.fromPlatform();
      _appVersion = info.version;
    } catch (_) {}
    final url = await getSavedServerUrl();
    final prefs = await SharedPreferences.getInstance();
    final lqName = prefs.getString(kQualityKey);
    final lq = Quality.values.firstWhere(
      (v) => v.name == lqName,
      orElse: () => Quality.direct,
    );
    final pqName = prefs.getString(kPlaybackQualityKey);
    final pq = Quality.values.firstWhere(
      (v) => v.name == pqName,
      orElse: () => Quality.direct,
    );
    final sName = prefs.getString(kStreamSourceKey);
    final s = StreamSource.values.firstWhere(
      (v) => v.name == sName,
      orElse: () => StreamSource.s1,
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
    _updateService = UpdateService(_apiClient!);

    if (widget.updateOnly) {
      // Minimal mode: only need the update service, skip everything else
      _checkUpdateOnly();
      return;
    }

    _cameraApi = CameraApi(_apiClient!);
    _recordingApi = RecordingApi(_apiClient!);
    _clipApi = ClipApi(_apiClient!);
    _motionApi = MotionApi(_apiClient!);
    _systemApi = SystemApi(_apiClient!);
    _streamApi = StreamApi(_apiClient!);
    _settingsApi = SettingsApi(_apiClient!);
    _backupApi = BackupApi(_apiClient!);
    _fetchInitialData();
  }

  Future<void> _checkUpdateOnly() async {
    // In update-only mode, wait for backend to be reachable, then show dialog
    for (var attempt = 0; attempt < 10; attempt++) {
      try {
        final result = await _updateService!.getUpdate();
        if (result.update != null && mounted) {
          final update = result.update!;
          // ignore: use_build_context_synchronously
          showDialog(
            context: context,
            barrierDismissible: false,
            builder: (_) => UpdateDialog(
              update: update,
              updateService: _updateService!,
              updateOnly: true,
            ),
          );
          return;
        }
        // No update available (edge case: update was installed between check and launch)
        if (mounted) exit(0);
        return;
      } catch (_) {
        await Future.delayed(const Duration(seconds: 2));
        if (!mounted) return;
      }
    }
    // Couldn't reach backend after retries — exit
    exit(0);
  }

  Future<void> _fetchInitialData() async {
    // Retry on connection errors (service may still be starting after install)
    for (var attempt = 0; attempt < 15; attempt++) {
      try {
        final results = await Future.wait([
          _systemApi!.fetchTzOffsetMs(),
          _cameraApi!.fetchAll(),
          _systemApi!.fetchStatus(),
        ]);
        if (mounted) {
          final status = results[2] as SystemStatus;
          _streamApi?.updateRtspPort(status.go2rtcRtspPort);
          setState(() {
            _tzOffsetMs = results[0] as int;
            _cameras = results[1] as List<Camera>;
            _systemStatus = status;
          });
        }
        return;
      } catch (_) {
        if (attempt < 14) {
          await Future.delayed(const Duration(seconds: 2));
          if (!mounted) return;
        }
      }
    }
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
      _streamApi?.updateRtspPort(status.go2rtcRtspPort);
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
          : widget.updateOnly
              ? const Scaffold(body: SizedBox.shrink())
              : _serverUrl == null
              ? SettingsScreen(onSaved: _onServerUrlSet)
              : _MainNav(
                  cameraApi: _cameraApi!,
                  recordingApi: _recordingApi!,
                  clipApi: _clipApi!,
                  motionApi: _motionApi!,
                  systemApi: _systemApi!,
                  streamApi: _streamApi!,
                  settingsApi: _settingsApi!,
                  backupApi: _backupApi!,
                  apiClient: _apiClient!,
                  updateService: _updateService!,
                  appVersion: _appVersion,
                  cameras: _cameras,
                  systemStatus: _systemStatus,
                  quality: _quality,
                  isLive: _isLive,
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
  final SettingsApi settingsApi;
  final BackupApi backupApi;
  final ApiClient apiClient;
  final UpdateService updateService;
  final String appVersion;
  final List<Camera> cameras;
  final SystemStatus? systemStatus;
  final Quality quality;
  final bool isLive;
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
    required this.settingsApi,
    required this.backupApi,
    required this.apiClient,
    required this.updateService,
    required this.appVersion,
    required this.cameras,
    required this.systemStatus,
    required this.quality,
    required this.isLive,
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
  DateTime? _lastBackPress;
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
    _scheduleUpdateCheck();
  }

  @override
  void didUpdateWidget(covariant _MainNav oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Stop live players when entering playback to free GPU decoder memory.
    // On Android especially, 6 live + 6 playback = 12 decoders → OOM.
    if (oldWidget.isLive && !widget.isLive) {
      for (final p in _livePlayers.values) {
        p.stop();
      }
    }
    // Live players will be re-opened by LivePlayer widgets when they rebuild
    // with the shared player (checks playlist.medias.isEmpty → calls _open).
  }

  void _scheduleUpdateCheck() {
    Future.delayed(const Duration(seconds: 10), () async {
      if (!mounted) return;
      try {
        final result = await widget.updateService.getUpdate();
        if (result.update == null || !mounted) return;
        final update = result.update!;
        if (await widget.updateService.isVersionSkipped(update.version)) return;
        showDialog(
          // ignore: use_build_context_synchronously
          context: context,
          barrierDismissible: false,
          builder: (_) => UpdateDialog(
            update: update,
            updateService: widget.updateService,
          ),
        );
      } catch (_) {}
    });
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
      debugPrint('EXIT_FS: fsTime=$fsTime gridTime=$gridTime drift=$timeDrift gridIsLive=${_gridRef.isLive}');
      if (_gridRef.isLive || timeDrift > 5000) {
        debugPrint('EXIT_FS: RESTARTING playback (gridIsLive=${_gridRef.isLive} drift=$timeDrift)');
        _resumePlaybackTime = formatLocalISOFromMs(fsTime - widget.tzOffsetMs);
        _resumePlaybackGen++;
      } else {
        debugPrint('EXIT_FS: SEAMLESS return (no restart needed)');
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
        logLevel: MPVLogLevel.warn,
      ),
    );
    final mpv = player.platform as NativePlayer;
    mpv.setProperty('cache', 'yes');
    mpv.setProperty('cache-pause', 'no');
    mpv.setProperty('cache-secs', '5');
    mpv.setProperty('demuxer-max-bytes', '16777216');
    mpv.setProperty('demuxer-readahead-secs', '5');
    mpv.setProperty('rtsp-transport', 'tcp');
    mpv.setProperty('hwdec', 'auto');
    mpv.setProperty('network-timeout', '30');
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
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        if (_fullscreenCameraId != null) {
          _exitFullscreen();
        } else if (_selectedCameraId != null) {
          setState(() => _selectedCameraId = null);
        } else if (Platform.isAndroid) {
          final now = DateTime.now();
          if (_lastBackPress != null &&
              now.difference(_lastBackPress!) < const Duration(seconds: 2)) {
            SystemNavigator.pop();
          } else {
            _lastBackPress = now;
            ScaffoldMessenger.of(context).showSnackBar(
              const SnackBar(
                content: Text('Press back again to exit'),
                duration: Duration(seconds: 2),
              ),
            );
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
            updateService: widget.updateService,
            appVersion: widget.appVersion,
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
            onOpenSystemSettings: () {
              Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => SystemSettingsScreen(
                  settingsApi: widget.settingsApi,
                  backupApi: widget.backupApi,
                ),
              ));
            },
            onAddCamera: () async {
              await Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => CameraFormScreen(cameraApi: widget.cameraApi, apiClient: widget.apiClient),
              ));
              await widget.onRefreshCameras();
            },
            onEditCamera: (cam) async {
              await Navigator.of(context).push(MaterialPageRoute(
                builder: (_) =>
                    CameraFormScreen(cameraApi: widget.cameraApi, apiClient: widget.apiClient, camera: cam),
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
            cameras: widget.cameras,
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
            onEditCamera: (cam) async {
              await Navigator.of(context).push(MaterialPageRoute(
                builder: (_) =>
                    CameraFormScreen(cameraApi: widget.cameraApi, apiClient: widget.apiClient, camera: cam),
              ));
              await widget.onRefreshCameras();
            },
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
