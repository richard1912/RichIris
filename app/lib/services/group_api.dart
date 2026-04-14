import '../models/camera_group.dart';
import 'api_client.dart';

class GroupApi {
  final ApiClient _client;
  GroupApi(this._client);

  Future<List<CameraGroup>> fetchAll() async {
    final resp = await _client.dio.get('/api/groups');
    return (resp.data as List)
        .map((e) => CameraGroup.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<CameraGroup> create(String name) async {
    final resp = await _client.dio.post('/api/groups', data: {'name': name});
    return CameraGroup.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<CameraGroup> update(int id, {String? name, int? sortOrder}) async {
    final resp = await _client.dio.put('/api/groups/$id', data: {
      if (name != null) 'name': name,
      if (sortOrder != null) 'sort_order': sortOrder,
    });
    return CameraGroup.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<void> delete(int id) async {
    await _client.dio.delete('/api/groups/$id');
  }

  Future<void> bulkAction(int groupId, String action) async {
    await _client.dio.post('/api/groups/$groupId/bulk', data: {
      'action': action,
    });
  }
}
