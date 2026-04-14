import 'package:dio/dio.dart';
import '../models/playback_session.dart';
import '../models/recording_segment.dart';
import '../models/thumbnail_info.dart';
import 'api_client.dart';

class RecordingApi {
  final ApiClient _client;
  RecordingApi(this._client);

  Future<List<String>> fetchDates(int cameraId) async {
    final resp = await _client.dio.get('/api/recordings/$cameraId/dates');
    return (resp.data as List).cast<String>();
  }

  Future<List<RecordingSegment>> fetchSegments(
      int cameraId, String date) async {
    final resp = await _client.dio
        .get('/api/recordings/$cameraId/segments', queryParameters: {'date': date});
    return (resp.data as List)
        .map((e) => RecordingSegment.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<PlaybackSession> startPlayback(
      int cameraId, String start, String quality,
      {String direction = 'forward', String? benchId}) async {
    final resp = await _client.dio.post(
      '/api/recordings/$cameraId/playback',
      queryParameters: {
        'start': start,
        'quality': quality,
        'direction': direction,
      },
      options: benchId != null
          ? Options(headers: {'X-Bench-Id': benchId})
          : null,
    );
    return PlaybackSession.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<List<ThumbnailInfo>> fetchThumbnails(
      int cameraId, String date) async {
    try {
      final resp = await _client.dio.get(
        '/api/recordings/$cameraId/thumbnails',
        queryParameters: {'date': date},
      );
      return (resp.data as List)
          .map((e) => ThumbnailInfo.fromJson(e as Map<String, dynamic>))
          .toList();
    } catch (_) {
      return [];
    }
  }

  String getSegmentUrl(String segmentPath) {
    return '${_client.baseUrl}$segmentPath';
  }

  String getThumbnailUrl(String relativeUrl) {
    return '${_client.baseUrl}$relativeUrl';
  }
}
