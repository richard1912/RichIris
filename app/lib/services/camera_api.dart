import '../models/camera.dart';
import 'api_client.dart';

class CameraApi {
  final ApiClient _client;
  CameraApi(this._client);

  Future<List<Camera>> fetchAll() async {
    final resp = await _client.dio.get('/api/cameras');
    return (resp.data as List)
        .map((e) => Camera.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<Camera> create({
    required String name,
    required String rtspUrl,
    String? subStreamUrl,
    bool enabled = true,
    int rotation = 0,
  }) async {
    final resp = await _client.dio.post('/api/cameras', data: {
      'name': name,
      'rtsp_url': rtspUrl,
      if (subStreamUrl != null) 'sub_stream_url': subStreamUrl,
      'enabled': enabled,
      'rotation': rotation,
    });
    return Camera.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<Camera> update(int id, Map<String, dynamic> data) async {
    final resp = await _client.dio.put('/api/cameras/$id', data: data);
    return Camera.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<void> delete(int id) async {
    await _client.dio.delete('/api/cameras/$id');
  }
}
