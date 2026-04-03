class MotionEvent {
  final int id;
  final int cameraId;
  final String startTime;
  final String? endTime;
  final double peakIntensity;
  final String? detectionLabel;
  final double? detectionConfidence;
  final bool hasThumbnail;

  MotionEvent({
    required this.id,
    required this.cameraId,
    required this.startTime,
    this.endTime,
    required this.peakIntensity,
    this.detectionLabel,
    this.detectionConfidence,
    this.hasThumbnail = false,
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
      );
}
