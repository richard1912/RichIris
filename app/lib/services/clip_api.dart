import 'package:dio/dio.dart';
import '../models/clip_export.dart';
import 'api_client.dart';

class ClipApi {
  final ApiClient _client;
  ClipApi(this._client);

  Future<ClipExport> create(int cameraId, String startTime, String endTime) async {
    final resp = await _client.dio.post('/api/clips', data: {
      'camera_id': cameraId,
      'start_time': startTime,
      'end_time': endTime,
    });
    return ClipExport.fromJson(resp.data as Map<String, dynamic>);
  }

  /// Export the same time range from one or more cameras.
  /// [join] true with multiple cameras produces a single synchronized
  /// side-by-side grid composite; otherwise one clip per camera.
  Future<List<ClipExport>> createComposite(
    List<int> cameraIds,
    String startTime,
    String endTime, {
    bool join = false,
  }) async {
    final resp = await _client.dio.post('/api/clips/composite', data: {
      'camera_ids': cameraIds,
      'start_time': startTime,
      'end_time': endTime,
      'join': join,
    });
    return (resp.data as List)
        .map((e) => ClipExport.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<List<ClipExport>> fetchAll({int? cameraId}) async {
    final resp = await _client.dio.get('/api/clips',
        queryParameters: cameraId != null ? {'camera_id': cameraId} : null);
    return (resp.data as List)
        .map((e) => ClipExport.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<ClipExport> fetch(int id) async {
    final resp = await _client.dio.get('/api/clips/$id');
    return ClipExport.fromJson(resp.data as Map<String, dynamic>);
  }

  String downloadUrl(int id) => '${_client.baseUrl}/api/clips/$id/download';

  Future<void> download(int id, String savePath) async {
    await _client.dio.download(
      '/api/clips/$id/download',
      savePath,
      options: Options(responseType: ResponseType.bytes),
    );
  }

  Future<void> delete(int id) async {
    await _client.dio.delete('/api/clips/$id');
  }
}
