import '../config/platform_info.dart';
import 'api_client.dart';

class StreamApi {
  final ApiClient _client;
  int _rtspPort = 18554; // Default, updated from backend on status fetch
  StreamApi(this._client);

  /// Update the RTSP port from the backend's system status response.
  void updateRtspPort(int port) {
    _rtspPort = port;
  }

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

  /// Build the live-streaming URL for this platform.
  ///
  /// **Native** (Windows/Android) talks RTSP straight to go2rtc. libmpv
  /// handles RTSP natively and it is the lowest-latency path, but it needs a
  /// direct route to `<host>:18554`, which is why the hostname the client is
  /// configured with has to resolve for the client itself and not merely for
  /// Caddy.
  ///
  /// **Web** cannot: no browser plays RTSP, and media_kit's web backend is an
  /// HTML `<video>` element that only knows what the browser knows. So the
  /// browser gets the backend's fMP4 proxy instead — the same
  /// `/api/streams/{id}/live.mp4` endpoint the native app keeps as a fallback,
  /// which go2rtc already serves without re-encoding. It is same-origin
  /// (so no CORS), it goes through Caddy over TLS (so no mixed content), and
  /// `stream`/`quality` map onto the endpoint's own query parameters.
  ///
  /// Note the streams are **HEVC**, which Chrome, Edge and Safari play with
  /// hardware decode but Firefox does not play at all. Serving Firefox would
  /// mean adding an `#video=h264` variant in go2rtc and paying for an extra
  /// iGPU transcode per viewer; measured against Chrome playing the existing
  /// HEVC stream at real time, that is not worth doing until someone needs it.
  String liveUrl(int cameraId, String stream, String quality, {String cameraName = ''}) {
    if (isWeb) {
      return '${_client.baseUrl}/api/streams/$cameraId/live.mp4'
          '?stream=$stream&quality=$quality';
    }
    final streamName = '${_toStreamName(cameraName)}_${stream}_$quality';
    return 'rtsp://$_host:$_rtspPort/$streamName';
  }

  /// Backend URL for a camera's newest FrameBroker JPEG — the live-view poster
  /// frame shown while the local decoder is still starting up.
  ///
  /// [cacheBust] should be a value that is stable for as long as the caller
  /// wants to keep the same image (Flutter's image cache is keyed on the URL),
  /// e.g. a timestamp captured once when the card is created.
  String posterUrl(int cameraId, {int? cacheBust}) {
    final bust = cacheBust != null ? '?t=$cacheBust' : '';
    return '${_client.baseUrl}/api/cameras/$cameraId/latest-frame.jpg$bust';
  }
}
