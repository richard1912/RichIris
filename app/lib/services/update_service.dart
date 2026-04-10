import 'dart:io';

import 'package:dio/dio.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../config/api_config.dart';
import '../config/install_flavor.dart';
import 'api_client.dart';

const _githubReleasesUrl =
    'https://api.github.com/repos/richard1912/RichIris/releases';

class UpdateInfo {
  final String version;
  final String tagName;
  final String changelog;
  final String? windowsUrl;
  final int? windowsSize;
  final String? windowsClientUrl;
  final int? windowsClientSize;
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
    this.windowsClientUrl,
    this.windowsClientSize,
    this.androidUrl,
    this.androidSize,
    required this.publishedAt,
    this.lastChecked,
  });

  factory UpdateInfo.fromJson(Map<String, dynamic> json, String? lastChecked) {
    final latest = json['latest'] as Map<String, dynamic>;
    final assets = latest['assets'] as Map<String, dynamic>? ?? {};
    final win = assets['windows'] as Map<String, dynamic>?;
    final winClient = assets['windows_client'] as Map<String, dynamic>?;
    final android = assets['android'] as Map<String, dynamic>?;
    return UpdateInfo(
      version: latest['version'] as String,
      tagName: latest['tag_name'] as String,
      changelog: latest['changelog'] as String? ?? '',
      windowsUrl: win?['url'] as String?,
      windowsSize: win?['size'] as int?,
      windowsClientUrl: winClient?['url'] as String?,
      windowsClientSize: winClient?['size'] as int?,
      androidUrl: android?['url'] as String?,
      androidSize: android?['size'] as int?,
      publishedAt: latest['published_at'] as String? ?? '',
      lastChecked: lastChecked,
    );
  }
}

class UpdateService {
  final ApiClient _client;
  final String currentVersion;
  SharedPreferences? _prefs;

  UpdateService(this._client, {required this.currentVersion});

  Future<SharedPreferences> _getPrefs() async {
    _prefs ??= await SharedPreferences.getInstance();
    return _prefs!;
  }

  /// Fetch cached update info. For client-only installs there's no backend to
  /// broker the GitHub call, so we hit the GitHub releases API directly.
  /// Full installs keep using the backend's cached result to avoid GitHub
  /// rate limits and share the 6h periodic check across the LAN.
  Future<({UpdateInfo? update, String? lastChecked})> getUpdate() async {
    if (isClientOnlyInstall()) return _fetchFromGitHub();
    final resp = await _client.dio.get('/api/system/update');
    final data = resp.data as Map<String, dynamic>;
    final lastChecked = data['last_checked'] as String?;
    if (data['update_available'] != true || data['latest'] == null) {
      return (update: null, lastChecked: lastChecked);
    }
    return (update: UpdateInfo.fromJson(data, lastChecked), lastChecked: lastChecked);
  }

  /// Force a fresh update check. Client-only installs go straight to GitHub;
  /// full installs have the backend re-check GitHub and return the result.
  Future<({UpdateInfo? update, String? lastChecked})> checkNow() async {
    if (isClientOnlyInstall()) return _fetchFromGitHub();
    final resp = await _client.dio.post('/api/system/update/check');
    final data = resp.data as Map<String, dynamic>;
    final lastChecked = data['last_checked'] as String?;
    if (data['update_available'] != true || data['latest'] == null) {
      return (update: null, lastChecked: lastChecked);
    }
    return (update: UpdateInfo.fromJson(data, lastChecked), lastChecked: lastChecked);
  }

  /// Direct GitHub releases query used by client-only installs. Mirrors the
  /// filtering + parsing logic in `backend/app/services/update_checker.py`.
  Future<({UpdateInfo? update, String? lastChecked})> _fetchFromGitHub() async {
    final lastChecked = DateTime.now().toUtc().toIso8601String();
    final dio = Dio(BaseOptions(
      connectTimeout: const Duration(seconds: 15),
      receiveTimeout: const Duration(seconds: 15),
      headers: {'Accept': 'application/vnd.github+json'},
    ));
    try {
      final resp = await dio.get(
        _githubReleasesUrl,
        queryParameters: {'per_page': 50},
      );
      if (resp.statusCode != 200 || resp.data is! List) {
        return (update: null, lastChecked: lastChecked);
      }
      final releases = (resp.data as List).cast<Map<String, dynamic>>();
      if (releases.isEmpty) {
        return (update: null, lastChecked: lastChecked);
      }

      // Filter drafts/prereleases, keep only releases newer than current.
      final newer = <Map<String, dynamic>>[];
      for (final rel in releases) {
        if (rel['draft'] == true || rel['prerelease'] == true) continue;
        final tag = (rel['tag_name'] as String? ?? '').replaceFirst('v', '');
        if (_compareSemver(tag, currentVersion) > 0) {
          newer.add(rel);
        }
      }
      if (newer.isEmpty) {
        return (update: null, lastChecked: lastChecked);
      }

      final latest = newer.first;
      final latestTag = (latest['tag_name'] as String? ?? '');
      final latestVersion = latestTag.replaceFirst('v', '');

      // Parse assets by filename (matches update_checker.py rules).
      final assets = <String, Map<String, dynamic>>{};
      final assetList = (latest['assets'] as List? ?? []);
      for (final a in assetList) {
        if (a is! Map) continue;
        final name = a['name'] as String? ?? '';
        final info = <String, dynamic>{
          'name': name,
          'url': a['browser_download_url'] as String? ?? '',
          'size': a['size'] as int? ?? 0,
        };
        if (name.endsWith('.exe')) {
          if (name.contains('Client-Setup')) {
            assets['windows_client'] = info;
          } else {
            assets['windows'] = info;
          }
        } else if (name.endsWith('.apk')) {
          assets['android'] = info;
        }
      }

      // Combine changelogs from all newer releases (newest first).
      final changelogParts = <String>[];
      for (final rel in newer) {
        final tag = rel['tag_name'] as String? ?? '';
        final body = (rel['body'] as String? ?? '').trim();
        if (body.isNotEmpty) {
          changelogParts.add('# $tag\n$body');
        }
      }

      final synthetic = <String, dynamic>{
        'update_available': true,
        'latest': {
          'version': latestVersion,
          'tag_name': latestTag,
          'changelog': changelogParts.join('\n\n'),
          'published_at': latest['published_at'] as String? ?? '',
          'assets': assets,
        },
      };
      return (
        update: UpdateInfo.fromJson(synthetic, lastChecked),
        lastChecked: lastChecked,
      );
    } catch (_) {
      return (update: null, lastChecked: lastChecked);
    } finally {
      dio.close();
    }
  }

  /// Semver compare. Returns -1/0/1 for a <, ==, > b.
  int _compareSemver(String a, String b) {
    List<int> parts(String v) {
      final stripped = v.startsWith('v') ? v.substring(1) : v;
      return stripped.split('.').map((p) => int.tryParse(p) ?? 0).toList();
    }

    final pa = parts(a);
    final pb = parts(b);
    while (pa.length < pb.length) {
      pa.add(0);
    }
    while (pb.length < pa.length) {
      pb.add(0);
    }
    for (var i = 0; i < pa.length; i++) {
      if (pa[i] < pb[i]) return -1;
      if (pa[i] > pb[i]) return 1;
    }
    return 0;
  }

  /// Download the appropriate installer asset for the current platform and
  /// install flavor. Client-only Windows installs pull `windowsClientUrl`;
  /// full Windows installs pull `windowsUrl`; Android always pulls `androidUrl`.
  Future<String> downloadUpdate(
    UpdateInfo info,
    void Function(int received, int total)? onProgress, {
    CancelToken? cancelToken,
  }) async {
    final isClient = isClientOnlyInstall();
    String? url;
    if (Platform.isAndroid) {
      url = info.androidUrl;
    } else if (Platform.isWindows) {
      url = isClient ? info.windowsClientUrl : info.windowsUrl;
    }
    if (url == null) {
      throw Exception(
        isClient
            ? 'Client installer not available for this release'
            : 'No download URL for this platform',
      );
    }

    final ext = Platform.isWindows ? '.exe' : '.apk';
    final suffix = isClient ? '-Client' : '';
    final dir = await getTemporaryDirectory();
    final path =
        '${dir.path}${Platform.pathSeparator}RichIris$suffix-${info.version}$ext';

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
