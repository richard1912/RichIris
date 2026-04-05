import 'dart:io';

import 'package:dio/dio.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../config/api_config.dart';
import 'api_client.dart';

class UpdateInfo {
  final String version;
  final String tagName;
  final String changelog;
  final String? windowsUrl;
  final int? windowsSize;
  final String? androidUrl;
  final int? androidSize;
  final String publishedAt;
  final String? lastChecked;

  UpdateInfo({
    required this.version,
    required this.tagName,
    required this.changelog,
    this.windowsUrl,
    this.windowsSize,
    this.androidUrl,
    this.androidSize,
    required this.publishedAt,
    this.lastChecked,
  });

  factory UpdateInfo.fromJson(Map<String, dynamic> json, String? lastChecked) {
    final latest = json['latest'] as Map<String, dynamic>;
    final assets = latest['assets'] as Map<String, dynamic>? ?? {};
    final win = assets['windows'] as Map<String, dynamic>?;
    final android = assets['android'] as Map<String, dynamic>?;
    return UpdateInfo(
      version: latest['version'] as String,
      tagName: latest['tag_name'] as String,
      changelog: latest['changelog'] as String? ?? '',
      windowsUrl: win?['url'] as String?,
      windowsSize: win?['size'] as int?,
      androidUrl: android?['url'] as String?,
      androidSize: android?['size'] as int?,
      publishedAt: latest['published_at'] as String? ?? '',
      lastChecked: lastChecked,
    );
  }
}

class UpdateService {
  final ApiClient _client;
  SharedPreferences? _prefs;

  UpdateService(this._client);

  Future<SharedPreferences> _getPrefs() async {
    _prefs ??= await SharedPreferences.getInstance();
    return _prefs!;
  }

  /// Get cached update info from backend (instant, no GitHub API call).
  Future<UpdateInfo?> getUpdate() async {
    final resp = await _client.dio.get('/api/system/update');
    final data = resp.data as Map<String, dynamic>;
    if (data['update_available'] != true || data['latest'] == null) return null;
    return UpdateInfo.fromJson(data, data['last_checked'] as String?);
  }

  /// Force backend to re-check GitHub now.
  Future<UpdateInfo?> checkNow() async {
    final resp = await _client.dio.post('/api/system/update/check');
    final data = resp.data as Map<String, dynamic>;
    if (data['update_available'] != true || data['latest'] == null) return null;
    return UpdateInfo.fromJson(data, data['last_checked'] as String?);
  }

  /// Download the appropriate asset to temp dir. Returns file path.
  Future<String> downloadUpdate(
    UpdateInfo info,
    void Function(int received, int total)? onProgress, {
    CancelToken? cancelToken,
  }) async {
    final url = Platform.isWindows ? info.windowsUrl : info.androidUrl;
    if (url == null) throw Exception('No download URL for this platform');

    final ext = Platform.isWindows ? '.exe' : '.apk';
    final dir = await getTemporaryDirectory();
    final path = '${dir.path}${Platform.pathSeparator}RichIris-${info.version}$ext';

    // Use a separate Dio instance for GitHub download (not through ApiClient)
    final downloader = Dio(BaseOptions(
      connectTimeout: const Duration(seconds: 30),
      receiveTimeout: const Duration(minutes: 10),
    ));

    try {
      await downloader.download(
        url,
        path,
        onReceiveProgress: onProgress,
        cancelToken: cancelToken,
      );
    } finally {
      downloader.close();
    }

    return path;
  }

  /// Launch the downloaded installer (Windows) or open APK (Android).
  Future<void> installUpdate(String filePath) async {
    if (Platform.isWindows) {
      await Process.start(
        filePath,
        ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-'],
        mode: ProcessStartMode.detached,
      );
      // Give the installer a moment to start before we exit
      await Future.delayed(const Duration(seconds: 1));
      exit(0);
    } else if (Platform.isAndroid) {
      // Android APK install handled by the dialog via platform intent
      throw UnimplementedError('Use installApk() from the dialog');
    }
  }

  Future<bool> isVersionSkipped(String version) async {
    final prefs = await _getPrefs();
    return prefs.getString(kSkippedVersionKey) == version;
  }

  Future<void> skipVersion(String version) async {
    final prefs = await _getPrefs();
    await prefs.setString(kSkippedVersionKey, version);
  }
}
