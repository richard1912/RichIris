class MotionEvent {
  final int id;
  final int cameraId;
  final String startTime;
  final String? endTime;
  final double peakIntensity;

  MotionEvent({
    required this.id,
    required this.cameraId,
    required this.startTime,
    this.endTime,
    required this.peakIntensity,
  });

  factory MotionEvent.fromJson(Map<String, dynamic> json) => MotionEvent(
        id: json['id'] as int,
        cameraId: json['camera_id'] as int,
        startTime: json['start_time'] as String,
        endTime: json['end_time'] as String?,
        peakIntensity: (json['peak_intensity'] as num).toDouble(),
      );
}
