class Face {
  final int id;
  // Nullable: null = unnamed cluster (suggested person).
  final String? name;
  final String? notes;
  final int embeddingCount;
  final String? latestCropPath;
  final String createdAt;

  Face({
    required this.id,
    this.name,
    this.notes,
    this.embeddingCount = 0,
    this.latestCropPath,
    required this.createdAt,
  });

  factory Face.fromJson(Map<String, dynamic> json) => Face(
        id: json['id'] as int,
        name: json['name'] as String?,
        notes: json['notes'] as String?,
        embeddingCount: json['embedding_count'] as int? ?? 0,
        latestCropPath: json['latest_crop_path'] as String?,
        createdAt: json['created_at'] as String,
      );

  /// Guaranteed-non-null label for UI. Clusters have no name; a caller that
  /// already filtered to named faces can still use `name!`.
  String get displayName => name ?? 'Unnamed';
}

class FaceCluster {
  final int id;
  final int embeddingCount;
  final List<int> sampleEmbeddingIds;
  final String? latestEventTime;
  final List<String> camerasSeen;
  final String createdAt;

  FaceCluster({
    required this.id,
    required this.embeddingCount,
    required this.sampleEmbeddingIds,
    this.latestEventTime,
    required this.camerasSeen,
    required this.createdAt,
  });

  factory FaceCluster.fromJson(Map<String, dynamic> json) => FaceCluster(
        id: json['id'] as int,
        embeddingCount: json['embedding_count'] as int,
        sampleEmbeddingIds: ((json['sample_embedding_ids'] as List<dynamic>?) ?? [])
            .map((e) => e as int)
            .toList(),
        latestEventTime: json['latest_event_time'] as String?,
        camerasSeen: ((json['cameras_seen'] as List<dynamic>?) ?? [])
            .map((e) => e as String)
            .toList(),
        createdAt: json['created_at'] as String,
      );
}

class FaceEmbeddingInfo {
  final int id;
  final String? sourceThumbnailPath;
  final String? faceCropPath;
  final String createdAt;

  FaceEmbeddingInfo({
    required this.id,
    this.sourceThumbnailPath,
    this.faceCropPath,
    required this.createdAt,
  });

  factory FaceEmbeddingInfo.fromJson(Map<String, dynamic> json) => FaceEmbeddingInfo(
        id: json['id'] as int,
        sourceThumbnailPath: json['source_thumbnail_path'] as String?,
        faceCropPath: json['face_crop_path'] as String?,
        createdAt: json['created_at'] as String,
      );
}

class UnlabeledThumb {
  final int eventId;
  final int cameraId;
  final String cameraName;
  final String startTime;
  final String thumbnailUrl;
  final String? detectionLabel;
  List<String> assignedFaceNames;

  UnlabeledThumb({
    required this.eventId,
    required this.cameraId,
    required this.cameraName,
    required this.startTime,
    required this.thumbnailUrl,
    this.detectionLabel,
    List<String>? assignedFaceNames,
  }) : assignedFaceNames = assignedFaceNames ?? [];

  factory UnlabeledThumb.fromJson(Map<String, dynamic> json) => UnlabeledThumb(
        eventId: json['event_id'] as int,
        cameraId: json['camera_id'] as int,
        cameraName: json['camera_name'] as String,
        startTime: json['start_time'] as String,
        thumbnailUrl: json['thumbnail_url'] as String,
        detectionLabel: json['detection_label'] as String?,
        assignedFaceNames: ((json['assigned_face_names'] as List<dynamic>?) ?? [])
            .map((e) => e as String)
            .toList(),
      );
}

class FaceEnrollCandidate {
  final List<int> bbox;
  final double score;

  FaceEnrollCandidate({required this.bbox, required this.score});

  factory FaceEnrollCandidate.fromJson(Map<String, dynamic> json) => FaceEnrollCandidate(
        bbox: (json['bbox'] as List<dynamic>).map((e) => e as int).toList(),
        score: (json['score'] as num).toDouble(),
      );
}

class FaceEnrollResult {
  final String status; // "enrolled" | "multiple_faces" | "no_face"
  final int? embeddingId;
  final String? cropPath;
  final List<FaceEnrollCandidate> candidates;

  FaceEnrollResult({
    required this.status,
    this.embeddingId,
    this.cropPath,
    this.candidates = const [],
  });

  factory FaceEnrollResult.fromJson(Map<String, dynamic> json) => FaceEnrollResult(
        status: json['status'] as String,
        embeddingId: json['embedding_id'] as int?,
        cropPath: json['crop_path'] as String?,
        candidates: ((json['candidates'] as List<dynamic>?) ?? [])
            .map((e) => FaceEnrollCandidate.fromJson(e as Map<String, dynamic>))
            .toList(),
      );
}
