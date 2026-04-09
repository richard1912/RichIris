import 'dart:io' show Platform;
import 'package:flutter/foundation.dart' show kIsWeb;
import 'api_client.dart';

class StreamApi {
  final ApiClient _client;
  StreamApi(this._client);

  static bool get _isAndroid => !kIsWeb && Platform.isAndroid;

  /// Convert camera name to go2rtc stream key (matches backend get_stream_name).
  static String _toStreamName(String cameraName) {
    return cameraName
        .toLowerCase()
        .trim()
        .replaceAll(RegExp(r'[^a-z0-9]+'), '_')
        .replaceAll(RegExp(r'^_+|_+$'), '');
  }

  /// Extract host from the backend base URL for go2rtc RTSP connection.
  String get _host => Uri.parse(_client.baseUrl).host;

  /// Build the live stream URL for a camera.
  /// Uses RTSP directly from go2rtc (port 8554) for smooth HEVC playback.
  String liveUrl(int cameraId, String stream, String quality, {String cameraName = ''}) {
    final streamName = '${_toStreamName(cameraName)}_${stream}_$quality';
    return 'rtsp://$_host:8554/$streamName';
  }

  /// Build the HTTP fMP4 live stream URL (fallback).
  String liveFmp4Url(int cameraId, String stream, String quality) {
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
