import 'dart:ui' show Offset;

import '../models/zone.dart';
import 'api_client.dart';

class ZoneApi {
  final ApiClient _client;
  ZoneApi(this._client);

  Future<List<Zone>> listForCamera(int cameraId) async {
    final resp = await _client.dio.get('/api/cameras/$cameraId/zones');
    return (resp.data as List)
        .map((e) => Zone.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<Zone> create({
    required int cameraId,
    required String name,
    required List<Offset> points,
  }) async {
    final resp = await _client.dio.post(
      '/api/cameras/$cameraId/zones',
      data: {
        'name': name,
        'points': points.map((p) => [p.dx, p.dy]).toList(),
      },
    );
    return Zone.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<Zone> update({
    required int cameraId,
    required int zoneId,
    String? name,
    List<Offset>? points,
  }) async {
    final resp = await _client.dio.put(
      '/api/cameras/$cameraId/zones/$zoneId',
      data: {
        if (name != null) 'name': name,
        if (points != null)
          'points': points.map((p) => [p.dx, p.dy]).toList(),
      },
    );
    return Zone.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<void> delete({required int cameraId, required int zoneId}) async {
    await _client.dio.delete('/api/cameras/$cameraId/zones/$zoneId');
  }
}
