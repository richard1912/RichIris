import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'config/api_config.dart';
import 'config/constants.dart';
import 'models/camera.dart';
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
  Quality _quality = Quality.high;
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
    final qName = prefs.getString(kQualityKey);
    final q = Quality.values.firstWhere(
      (v) => v.name == qName,
      orElse: () => Quality.direct,
    );
    final sName = prefs.getString(kStreamSourceKey);
    final s = StreamSource.values.firstWhere(
      (v) => v.name == sName,
      orElse: () => StreamSource.s2,
    );
    setState(() {
      _quality = q;
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
    setState(() => _quality = q);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(kQualityKey, q.name);
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
    return Stack(
      children: [
        Offstage(
          offstage: fullscreenCam != null,
          child: HomeScreen(
            cameras: widget.cameras,
            systemStatus: widget.systemStatus,
            quality: widget.quality,
            streamSource: widget.streamSource,
            streamApi: widget.streamApi,
            recordingApi: widget.recordingApi,
            clipApi: widget.clipApi,
            motionApi: widget.motionApi,
            cameraApi: widget.cameraApi,
            tzOffsetMs: widget.tzOffsetMs,
            selectedCameraId: _selectedCameraId,
            onCameraSelected: (id) {
              if (_selectedCameraId == id) {
                setState(() => _fullscreenCameraId = id);
              } else {
                setState(() => _selectedCameraId = id);
              }
            },
            onQualityChanged: widget.onQualityChanged,
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
          ),
        ),
        if (fullscreenCam != null)
          FullscreenScreen(
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
            onQualityChanged: widget.onQualityChanged,
            onStreamSourceChanged: widget.onStreamSourceChanged,
            onBack: () => setState(() => _fullscreenCameraId = null),
          ),
      ],
    );
  }
}
