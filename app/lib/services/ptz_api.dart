import 'package:dio/dio.dart';

import '../models/ptz.dart';
import 'api_client.dart';

/// PTZ control for cameras whose `ptz` flag is set (`/api/cameras/{id}/ptz`).
class PtzApi {
  final ApiClient _client;
  PtzApi(this._client);

  String _base(int cameraId) => '/api/cameras/$cameraId/ptz';

  Future<PtzStatus> status(int cameraId) async {
    final resp = await _client.dio.get('${_base(cameraId)}/status');
    return PtzStatus.fromJson(resp.data as Map<String, dynamic>);
  }

  /// Start a continuous move, or keep the current one alive. The server stops
  /// the camera by itself [timeout] seconds after the last call, so a held
  /// button re-sends this about once a second.
  Future<void> move(int cameraId, String direction,
      {required int speed, double timeout = 2.5}) async {
    await _client.dio.post('${_base(cameraId)}/move', data: {
      'direction': direction,
      'speed': speed,
      'timeout': timeout,
    });
  }

  Future<void> stop(int cameraId) async {
    await _client.dio.post('${_base(cameraId)}/stop');
  }

  /// Refocus once on the current view.
  Future<void> focus(int cameraId) async {
    await _client.dio.post('${_base(cameraId)}/focus');
  }

  Future<List<PtzPreset>> presets(int cameraId) async {
    final resp = await _client.dio.get('${_base(cameraId)}/presets');
    return (resp.data as List)
        .map((e) => PtzPreset.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Save the camera's current position as a new preset.
  Future<PtzPreset> savePreset(int cameraId, String name) async {
    final resp = await _client.dio.post(
      '${_base(cameraId)}/presets',
      data: {if (name.trim().isNotEmpty) 'name': name.trim()},
    );
    return PtzPreset.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<void> gotoPreset(int cameraId, int presetId) async {
    await _client.dio.post('${_base(cameraId)}/presets/$presetId/goto');
  }

  Future<void> deletePreset(int cameraId, int presetId) async {
    await _client.dio.delete('${_base(cameraId)}/presets/$presetId');
  }

  /// The server's `detail` text for a failed call, or a short fallback.
  static String describeError(Object error) {
    if (error is DioException) {
      final data = error.response?.data;
      if (data is Map && data['detail'] is String) {
        return data['detail'] as String;
      }
      if (error.response == null) return 'Server not reachable';
      return 'Request failed (${error.response?.statusCode})';
    }
    return 'Request failed';
  }
}
