import 'api_client.dart';

class StreamApi {
  final ApiClient _client;
  StreamApi(this._client);

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

  /// Build the go2rtc RTSP URL for live streaming.
  String liveUrl(int cameraId, String stream, String quality, {String cameraName = ''}) {
    final streamName = '${_toStreamName(cameraName)}_${stream}_$quality';
    return 'rtsp://$_host:8554/$streamName';
  }
}
