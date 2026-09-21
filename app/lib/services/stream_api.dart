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

  /// Build the live-streaming URL for this platform and quality tier.
  ///
  /// **`direct` on every platform** goes to the backend's fMP4 endpoint,
  /// `/api/streams/{id}/live.mp4`. That endpoint is backed by the server's
  /// live GOP cache, which holds the init segment plus the fragments since
  /// the stream's last keyframe and writes them before anything else — so
  /// the player has a decodable keyframe in the first bytes of the response.
  /// Native used to take RTSP straight to go2rtc here, and that was the
  /// faster path right up until the cache existed: a fresh consumer on an
  /// already-connected producer measured 2,464 ms to first frame over RTSP
  /// and 4,606 ms through the uncached proxy, because both wait out a full
  /// keyframe interval (2.0-4.0 s on these cameras). Neither waits now.
  ///
  /// Three things come with the move, beyond the latency:
  /// every viewer of a camera rides the backend's single go2rtc consumer
  /// instead of opening one each; live view no longer needs a direct route to
  /// port 18554, so it works anywhere Caddy reaches; and web and native are
  /// finally on the same transport.
  ///
  /// **The transcoded tiers stay on RTSP** for native. They are not kept warm
  /// (go2rtc starts an ffmpeg on demand) so there is nothing to cache, and the
  /// proxy is the slower of the two paths when uncached.
  ///
  /// **Web is always the endpoint**: no browser plays RTSP, and media_kit's
  /// web backend is an HTML `<video>` element that only knows what the browser
  /// knows. Same-origin, so no CORS, and through Caddy over TLS, so no mixed
  /// content.
  ///
  /// Note the streams are **HEVC**, which Chrome, Edge and Safari play with
  /// hardware decode but Firefox does not play at all. Serving Firefox would
  /// mean adding an `#video=h264` variant in go2rtc and paying for an extra
  /// iGPU transcode per viewer; measured against Chrome playing the existing
  /// HEVC stream at real time, that is not worth doing until someone needs it.
  String liveUrl(int cameraId, String stream, String quality, {String cameraName = ''}) {
    // `direct` rides the server's live GOP cache on every platform. Native was
    // put here on 2026-09-20 and reverted the same day because five of eight
    // Android tiles rendered colour noise. The cause, found 2026-09-22, was
    // never the cache: go2rtc writes a canned 2560x1440 hvcC for cameras whose
    // SDP carries no parameter sets, the real streams are 3840x2160, and
    // MediaCodec is configured from the hvcC where software decoders adopt the
    // in-band SPS. The backend now rebuilds the init segment from the in-band
    // VPS/SPS/PPS (`_repair_hevc_init` in live_cache.py), so do not move this
    // back to RTSP for that symptom - check the served init first.
    if (isWeb || quality == 'direct') {
      // `backlog=short` = start at the cache's NEWEST keyframe (0-1 GOP behind
      // live) rather than the older one. Only a player with `low_delay` set
      // starts quickly on a partial GOP, which is native (player_tuning_io);
      // the browser cannot set it and takes the full backlog.
      return '${_client.baseUrl}/api/streams/$cameraId/live.mp4'
          '?stream=$stream&quality=$quality${isWeb ? '' : '&backlog=short'}';
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
  ///
  /// [stream] picks which stream the poster is drawn from, and should be the
  /// one the player is about to show: the sub stream is 4:3 on most cameras
  /// while the main is 16:9, so a sub-stream poster under a main-stream video
  /// visibly changes shape when the video takes over.
  String posterUrl(int cameraId, {int? cacheBust, String stream = 's2'}) {
    final q = [
      if (stream == 's1') 'stream=s1',
      if (cacheBust != null) 't=$cacheBust',
    ].join('&');
    return '${_client.baseUrl}/api/cameras/$cameraId/latest-frame.jpg'
        '${q.isEmpty ? '' : '?$q'}';
  }
}
