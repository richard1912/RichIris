/// The AI guard's reading of one detection: who it thinks it saw, what they
/// were doing and how suspicious that is.
class GuardAssessment {
  final String status; // "ok" | "error"
  final String? model;
  final String? subject;
  final int? peopleCount;
  final String? description;
  final String? activity;
  final int suspicion; // 0..3
  final String? suspicionLabel;
  final String? reason;
  final bool wouldAlert;
  final int? latencyMs;

  /// Relative to the API base: the frames the AI was shown, wide shots first.
  final List<String> frameUrls;
  final String? createdAt;
  final String? error;

  GuardAssessment({
    required this.status,
    this.model,
    this.subject,
    this.peopleCount,
    this.description,
    this.activity,
    this.suspicion = 0,
    this.suspicionLabel,
    this.reason,
    this.wouldAlert = false,
    this.latencyMs,
    this.frameUrls = const [],
    this.createdAt,
    this.error,
  });

  bool get isOk => status == 'ok';

  factory GuardAssessment.fromJson(Map<String, dynamic> json) =>
      GuardAssessment(
        status: json['status'] as String? ?? 'error',
        model: json['model'] as String?,
        subject: json['subject'] as String?,
        peopleCount: (json['people_count'] as num?)?.toInt(),
        description: json['description'] as String?,
        activity: json['activity'] as String?,
        suspicion: ((json['suspicion'] as num?)?.toInt() ?? 0).clamp(0, 3),
        suspicionLabel: json['suspicion_label'] as String?,
        reason: json['reason'] as String?,
        wouldAlert: json['would_alert'] as bool? ?? false,
        latencyMs: (json['latency_ms'] as num?)?.toInt(),
        frameUrls: ((json['frame_urls'] as List<dynamic>?) ?? [])
            .map((e) => e as String)
            .toList(),
        createdAt: json['created_at'] as String?,
        error: json['error'] as String?,
      );
}

/// One row of the Events feed: a motion event plus the guard's assessment.
class GuardEvent {
  final int id;
  final int cameraId;
  final String cameraName;

  /// Local time without timezone, as stored by the server.
  final String startTime;
  final String? endTime;
  final String? label;
  final double? confidence;

  /// Relative to the API base.
  final String? thumbnailUrl;
  final List<String> scriptsFired;
  final GuardAssessment? assessment;

  GuardEvent({
    required this.id,
    required this.cameraId,
    required this.cameraName,
    required this.startTime,
    this.endTime,
    this.label,
    this.confidence,
    this.thumbnailUrl,
    this.scriptsFired = const [],
    this.assessment,
  });

  /// The picture to show for this event: the first frame the AI saw, else the
  /// detection thumbnail.
  String? get previewUrl {
    final frames = assessment?.frameUrls;
    if (frames != null && frames.isNotEmpty) return frames.first;
    return thumbnailUrl;
  }

  factory GuardEvent.fromJson(Map<String, dynamic> json) => GuardEvent(
        id: (json['id'] as num).toInt(),
        cameraId: (json['camera_id'] as num).toInt(),
        cameraName: json['camera_name'] as String? ?? 'Camera',
        startTime: json['start_time'] as String,
        endTime: json['end_time'] as String?,
        label: json['label'] as String?,
        confidence: (json['confidence'] as num?)?.toDouble(),
        thumbnailUrl: json['thumbnail_url'] as String?,
        scriptsFired: ((json['scripts_fired'] as List<dynamic>?) ?? [])
            .map((e) => e as String)
            .toList(),
        assessment: json['assessment'] is Map<String, dynamic>
            ? GuardAssessment.fromJson(
                json['assessment'] as Map<String, dynamic>)
            : null,
      );
}
