import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart'; // TEMP FACE-DIAG (for debugPrint in non-widget file)

import '../models/face.dart';
import 'api_client.dart';

// TEMP FACE-DIAG: tag every request/response on the Faces workflow so the dev
// can grep "[FACE-DIAG]" in flutter logs and correlate with backend
// "[BENCH:face-" traces. Remove when the audit is done.
void _diag(String msg) => debugPrint('[FACE-DIAG] $msg');

class FaceApi {
  final ApiClient _client;
  FaceApi(this._client);

  Future<List<Face>> fetchAll() async {
    final sw = Stopwatch()..start(); // TEMP FACE-DIAG
    final resp = await _client.dio.get('/api/faces');
    final list = (resp.data as List)
        .map((e) => Face.fromJson(e as Map<String, dynamic>))
        .toList();
    _diag('fetchAll count=${list.length} ${sw.elapsedMilliseconds}ms'); // TEMP FACE-DIAG
    return list;
  }

  Future<Face> create(String name, {String? notes}) async {
    final resp = await _client.dio.post('/api/faces', data: {
      'name': name,
      if (notes != null) 'notes': notes,
    });
    return Face.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<Face> update(int id, {String? name, String? notes}) async {
    final resp = await _client.dio.put('/api/faces/$id', data: {
      if (name != null) 'name': name,
      if (notes != null) 'notes': notes,
    });
    return Face.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<void> delete(int id) async {
    await _client.dio.delete('/api/faces/$id');
  }

  Future<List<FaceEmbeddingInfo>> listEmbeddings(int faceId) async {
    final sw = Stopwatch()..start(); // TEMP FACE-DIAG
    final resp = await _client.dio.get('/api/faces/$faceId/embeddings');
    final list = (resp.data as List)
        .map((e) => FaceEmbeddingInfo.fromJson(e as Map<String, dynamic>))
        .toList();
    _diag('listEmbeddings face=$faceId count=${list.length} ${sw.elapsedMilliseconds}ms'); // TEMP FACE-DIAG
    return list;
  }

  Future<FaceEnrollResult> enroll(int faceId, String sourceThumbnailPath, {List<int>? bbox}) async {
    final sw = Stopwatch()..start(); // TEMP FACE-DIAG
    _diag('enroll -> face=$faceId path=$sourceThumbnailPath bbox=$bbox'); // TEMP FACE-DIAG
    final resp = await _client.dio.post(
      '/api/faces/$faceId/embeddings',
      data: {
        'source_thumbnail_path': sourceThumbnailPath,
        if (bbox != null) 'bbox': bbox,
      },
    );
    final result = FaceEnrollResult.fromJson(resp.data as Map<String, dynamic>);
    _diag('enroll <- face=$faceId status=${result.status} embedding_id=${result.embeddingId} candidates=${result.candidates.length} ${sw.elapsedMilliseconds}ms'); // TEMP FACE-DIAG
    return result;
  }

  Future<void> deleteEmbedding(int embeddingId) async {
    await _client.dio.delete('/api/faces/embeddings/$embeddingId');
  }

  Future<List<UnlabeledThumb>> unlabeledThumbnails({
    String? date,
    int? cameraId,
    int limit = 60,
    bool withFaceOnly = true,
  }) async {
    final sw = Stopwatch()..start(); // TEMP FACE-DIAG
    _diag('unlabeledThumbnails -> date=$date camera=$cameraId limit=$limit withFace=$withFaceOnly'); // TEMP FACE-DIAG
    final resp = await _client.dio.get(
      '/api/faces/thumbnails/unlabeled',
      queryParameters: {
        if (date != null) 'date': date,
        if (cameraId != null) 'camera_id': cameraId,
        'limit': limit,
        'with_face_only': withFaceOnly,
      },
      // The backend query is cheap but the response plus the flood of parallel
      // 4K thumbnail downloads the Flutter grid then fires can take a while
      // over WiFi — give it a generous ceiling.
      options: Options(receiveTimeout: const Duration(seconds: 90)),
    );
    final list = (resp.data as List)
        .map((e) => UnlabeledThumb.fromJson(e as Map<String, dynamic>))
        .toList();
    _diag('unlabeledThumbnails <- count=${list.length} ${sw.elapsedMilliseconds}ms'); // TEMP FACE-DIAG
    return list;
  }

  Future<String> eventThumbnailPath(int eventId) async {
    final sw = Stopwatch()..start(); // TEMP FACE-DIAG
    final resp = await _client.dio.get('/api/faces/thumbnails/event/$eventId/path');
    final path = (resp.data as Map<String, dynamic>)['source_thumbnail_path'] as String;
    _diag('eventThumbnailPath event=$eventId -> $path ${sw.elapsedMilliseconds}ms'); // TEMP FACE-DIAG
    return path;
  }

  String embeddingCropUrl(int embeddingId) =>
      '${_client.dio.options.baseUrl}/api/faces/embeddings/$embeddingId/crop';

  String latestCropUrl(int faceId) =>
      '${_client.dio.options.baseUrl}/api/faces/$faceId/latest-crop';

  // --- Clustering ("suggested people") --------------------------------------

  Future<List<FaceCluster>> listClusters({int minSize = 1, int limit = 100}) async {
    final sw = Stopwatch()..start(); // TEMP FACE-DIAG
    final resp = await _client.dio.get(
      '/api/faces/clusters',
      queryParameters: {'min_size': minSize, 'limit': limit},
    );
    final list = (resp.data as List)
        .map((e) => FaceCluster.fromJson(e as Map<String, dynamic>))
        .toList();
    _diag('listClusters <- count=${list.length} min_size=$minSize ${sw.elapsedMilliseconds}ms');
    return list;
  }

  Future<Face> nameCluster(int clusterId, String name) async {
    final resp = await _client.dio.post(
      '/api/faces/clusters/$clusterId/name',
      data: {'name': name},
    );
    return Face.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<Face> mergeCluster(int clusterId, int targetFaceId) async {
    final resp = await _client.dio.post(
      '/api/faces/clusters/$clusterId/merge',
      data: {'target_face_id': targetFaceId},
    );
    return Face.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<void> discardCluster(int clusterId) async {
    await _client.dio.delete('/api/faces/clusters/$clusterId');
  }

  Future<void> recluster() async {
    await _client.dio.post('/api/faces/clusters/recluster');
  }
}
