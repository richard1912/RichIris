import 'package:dio/dio.dart';

import '../models/guard_event.dart';
import 'api_client.dart';

class EventsApi {
  final ApiClient _client;
  EventsApi(this._client);

  /// Newest first. [before] is a local ISO datetime (the oldest `start_time`
  /// already loaded) for paging further back.
  ///
  /// A backend without the endpoint (404) reads as "no events"; any other
  /// failure is thrown so the screen can say what went wrong.
  Future<List<GuardEvent>> fetchEvents({
    int limit = 50,
    String? before,
    int? cameraId,
    int? minSuspicion,
    bool assessed = true,
  }) async {
    try {
      final resp = await _client.dio.get(
        '/api/events',
        queryParameters: {
          'limit': limit,
          'before': ?before,
          'camera_id': ?cameraId,
          'min_suspicion': ?minSuspicion,
          'assessed': assessed,
        },
      );
      final data = resp.data;
      if (data is! List) return [];
      return data
          .whereType<Map<String, dynamic>>()
          .map(GuardEvent.fromJson)
          .toList();
    } on DioException catch (e) {
      if (e.response?.statusCode == 404) return [];
      rethrow;
    }
  }

  /// Event image URLs come back relative to the API base.
  String absoluteUrl(String path) {
    if (path.startsWith('http://') || path.startsWith('https://')) return path;
    return '${_client.baseUrl}${path.startsWith('/') ? '' : '/'}$path';
  }
}
