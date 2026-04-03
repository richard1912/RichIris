import 'dart:io' show Platform;
import 'package:flutter/foundation.dart' show kIsWeb;
import 'api_client.dart';

class StreamApi {
  final ApiClient _client;
  StreamApi(this._client);

  static bool get _isAndroid => !kIsWeb && Platform.isAndroid;

  /// Build the HTTP fMP4 live stream URL for a camera.
  /// On Android, Direct is swapped to High (raw passthrough compatibility issues).
  String liveUrl(int cameraId, String stream, String quality) {
    final effectiveQuality = (_isAndroid && quality == 'direct') ? 'high' : quality;
    return '${_client.baseUrl}/api/streams/$cameraId/live.mp4'
        '?stream=$stream&quality=$effectiveQuality';
  }

  /// Build the WebSocket URL for go2rtc MSE streaming.
  String wsUrl(int cameraId, String stream, String quality) {
    final base = _client.baseUrl
        .replaceFirst('http://', 'ws://')
        .replaceFirst('https://', 'wss://');
    return '$base/api/streams/$cameraId/ws?stream=$stream&quality=$quality';
  }
}
