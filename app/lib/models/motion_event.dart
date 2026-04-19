class FaceMatch {
  final int faceId;
  final String name;
  final double confidence;

  FaceMatch({required this.faceId, required this.name, required this.confidence});

  factory FaceMatch.fromJson(Map<String, dynamic> json) => FaceMatch(
        faceId: json['face_id'] as int,
        name: json['name'] as String,
        confidence: (json['confidence'] as num).toDouble(),
      );
}

class MotionEvent {
  final int id;
  final int cameraId;
  final String startTime;
  final String? endTime;
  final double peakIntensity;
  final String? detectionLabel;
  final double? detectionConfidence;
  final bool hasThumbnail;
  final List<FaceMatch> faceMatches;
  final bool faceUnknown;
  final List<String> scriptsFired;
  final List<String> zonesTriggered;

  MotionEvent({
    required this.id,
    required this.cameraId,
    required this.startTime,
    this.endTime,
    required this.peakIntensity,
    this.detectionLabel,
    this.detectionConfidence,
    this.hasThumbnail = false,
    this.faceMatches = const [],
    this.faceUnknown = false,
    this.scriptsFired = const [],
    this.zonesTriggered = const [],
  });

  factory MotionEvent.fromJson(Map<String, dynamic> json) => MotionEvent(
        id: json['id'] as int,
        cameraId: json['camera_id'] as int,
        startTime: json['start_time'] as String,
        endTime: json['end_time'] as String?,
        peakIntensity: (json['peak_intensity'] as num).toDouble(),
        detectionLabel: json['detection_label'] as String?,
        detectionConfidence: (json['detection_confidence'] as num?)?.toDouble(),
        hasThumbnail: json['has_thumbnail'] as bool? ?? false,
        faceMatches: ((json['face_matches'] as List<dynamic>?) ?? [])
            .map((e) => FaceMatch.fromJson(e as Map<String, dynamic>))
            .toList(),
        faceUnknown: json['face_unknown'] as bool? ?? false,
        scriptsFired: ((json['scripts_fired'] as List<dynamic>?) ?? [])
            .map((e) => e as String)
            .toList(),
        zonesTriggered: ((json['zones_triggered'] as List<dynamic>?) ?? [])
            .map((e) => e as String)
            .toList(),
      );
}
