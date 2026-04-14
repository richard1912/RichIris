import '../models/face.dart';
import 'api_client.dart';

class FaceApi {
  final ApiClient _client;
  FaceApi(this._client);

  Future<List<Face>> fetchAll() async {
    final resp = await _client.dio.get('/api/faces');
    return (resp.data as List)
        .map((e) => Face.fromJson(e as Map<String, dynamic>))
        .toList();
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
    final resp = await _client.dio.get('/api/faces/$faceId/embeddings');
    return (resp.data as List)
        .map((e) => FaceEmbeddingInfo.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<FaceEnrollResult> enroll(int faceId, String sourceThumbnailPath, {List<int>? bbox}) async {
    final resp = await _client.dio.post(
      '/api/faces/$faceId/embeddings',
      data: {
        'source_thumbnail_path': sourceThumbnailPath,
        if (bbox != null) 'bbox': bbox,
      },
    );
    return FaceEnrollResult.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<void> deleteEmbedding(int embeddingId) async {
    await _client.dio.delete('/api/faces/embeddings/$embeddingId');
  }

  Future<List<UnlabeledThumb>> unlabeledThumbnails({
    String? date,
    int? cameraId,
    int limit = 100,
    bool withFaceOnly = true,
  }) async {
    final resp = await _client.dio.get('/api/faces/thumbnails/unlabeled', queryParameters: {
      if (date != null) 'date': date,
      if (cameraId != null) 'camera_id': cameraId,
      'limit': limit,
      'with_face_only': withFaceOnly,
    });
    return (resp.data as List)
        .map((e) => UnlabeledThumb.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<String> eventThumbnailPath(int eventId) async {
    final resp = await _client.dio.get('/api/faces/thumbnails/event/$eventId/path');
    return (resp.data as Map<String, dynamic>)['source_thumbnail_path'] as String;
  }

  String embeddingCropUrl(int embeddingId) =>
      '${_client.dio.options.baseUrl}/api/faces/embeddings/$embeddingId/crop';

  String latestCropUrl(int faceId) =>
      '${_client.dio.options.baseUrl}/api/faces/$faceId/latest-crop';
}
