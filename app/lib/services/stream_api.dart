import 'api_client.dart';

class StreamApi {
  final ApiClient _client;
  StreamApi(this._client);

  /// Build the HTTP fMP4 live stream URL for a camera.
  String liveUrl(int cameraId, String stream, String quality) {
    return '${_client.baseUrl}/api/streams/$cameraId/live.mp4'
        '?stream=$stream&quality=$quality';
  }

  /// Build the WebSocket URL for go2rtc MSE streaming.
  String wsUrl(int cameraId, String stream, String quality) {
    final base = _client.baseUrl
        .replaceFirst('http://', 'ws://')
        .replaceFirst('https://', 'wss://');
    return '$base/api/streams/$cameraId/ws?stream=$stream&quality=$quality';
  }
}
