import 'dart:typed_data';

import 'package:dio/dio.dart';

import '../models/camera.dart';
import '../models/camera_scan.dart';
import '../widgets/rtsp_wizard_dialog.dart' show RtspDiscoverResult;
import 'api_client.dart';

/// One entry in a batch RTSP discovery request. Mirrors the backend
/// `RtspDiscoverRequest` pydantic model.
class DiscoverBatchTarget {
  final String ip;
  final String username;
  final String password;
  final int port;

  DiscoverBatchTarget({
    required this.ip,
    this.username = '',
    this.password = '',
    this.port = 554,
  });

  Map<String, dynamic> toJson() => {
        'ip': ip,
        'username': username,
        'password': password,
        'port': port,
      };
}

class CameraApi {
  final ApiClient _client;
  CameraApi(this._client);

  Future<List<Camera>> fetchAll() async {
    final resp = await _client.dio.get('/api/cameras');
    return (resp.data as List)
        .map((e) => Camera.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// LAN-scan for IP cameras via the backend. Returns hosts with an open
  /// RTSP port, excluding any already attached to an existing camera.
  Future<CameraScanResponse> scan({
    List<String>? subnets,
    int port = 554,
    int timeoutMs = 300,
    int concurrency = 64,
  }) async {
    final resp = await _client.dio.post(
      '/api/cameras/scan',
      data: {
        if (subnets != null && subnets.isNotEmpty) 'subnets': subnets,
        'port': port,
        'timeout_ms': timeoutMs,
        'concurrency': concurrency,
      },
      options: Options(
        // A /24 × 3 subnets × 300 ms × 64-concurrency finishes in ~4 s but
        // allow headroom for slower LANs or higher timeouts.
        receiveTimeout: const Duration(seconds: 60),
        sendTimeout: const Duration(seconds: 10),
      ),
    );
    return CameraScanResponse.fromJson(resp.data as Map<String, dynamic>);
  }

  /// Grab a one-shot JPEG snapshot from an arbitrary RTSP URL (used by the
  /// Scan & Add wizard's name step to show a preview of each camera). Returns
  /// the raw JPEG bytes on success, or throws on failure (504/502/etc).
  Future<Uint8List> snapshot({
    required String rtspUrl,
    int width = 320,
    double timeoutS = 8.0,
  }) async {
    final resp = await _client.dio.post(
      '/api/cameras/snapshot',
      data: {
        'rtsp_url': rtspUrl,
        'width': width,
        'timeout_s': timeoutS,
      },
      options: Options(
        responseType: ResponseType.bytes,
        // The backend caps ffmpeg at timeoutS + 3 — allow double that for
        // slow network hops.
        receiveTimeout: Duration(milliseconds: ((timeoutS + 3) * 2 * 1000).toInt()),
        sendTimeout: const Duration(seconds: 10),
      ),
    );
    return Uint8List.fromList(resp.data as List<int>);
  }

  /// Run RTSP URL discovery for many hosts in parallel. Returns a map keyed
  /// by IP with the list of matching RTSP patterns per host.
  Future<Map<String, List<RtspDiscoverResult>>> discoverBatch(
    List<DiscoverBatchTarget> targets, {
    int hostConcurrency = 4,
  }) async {
    final resp = await _client.dio.post(
      '/api/cameras/discover_batch',
      data: {
        'targets': targets.map((t) => t.toJson()).toList(),
        'host_concurrency': hostConcurrency,
      },
      options: Options(
        // Each host takes up to ~15 s internally (58 patterns × ffprobe);
        // with host_concurrency=4 a batch of 8 takes ~30 s worst case.
        receiveTimeout: const Duration(minutes: 3),
        sendTimeout: const Duration(seconds: 10),
      ),
    );
    final body = resp.data as Map<String, dynamic>;
    final rawResults = body['results'] as Map<String, dynamic>;
    return rawResults.map((ip, v) => MapEntry(
          ip,
          (v as List)
              .map((e) => RtspDiscoverResult.fromJson(e as Map<String, dynamic>))
              .toList(),
        ));
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

  Future<void> delete(int id, {bool purgeData = false}) async {
    await _client.dio.delete(
      '/api/cameras/$id',
      queryParameters: {if (purgeData) 'purge_data': 'true'},
    );
  }
}
