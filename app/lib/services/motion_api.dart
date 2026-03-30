import '../models/motion_event.dart';
import 'api_client.dart';

class MotionApi {
  final ApiClient _client;
  MotionApi(this._client);

  Future<List<MotionEvent>> fetchEvents(int cameraId, String date) async {
    try {
      final resp = await _client.dio.get(
        '/api/motion/$cameraId/events',
        queryParameters: {'date': date},
      );
      return (resp.data as List)
          .map((e) => MotionEvent.fromJson(e as Map<String, dynamic>))
          .toList();
    } catch (_) {
      return [];
    }
  }
}
