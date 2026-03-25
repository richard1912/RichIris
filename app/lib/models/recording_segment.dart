class RecordingSegment {
  final int id;
  final int cameraId;
  final String filePath;
  final String startTime;
  final String? endTime;
  final int? fileSize;
  final double? duration;
  final bool inProgress;

  RecordingSegment({
    required this.id,
    required this.cameraId,
    required this.filePath,
    required this.startTime,
    this.endTime,
    this.fileSize,
    this.duration,
    this.inProgress = false,
  });

  factory RecordingSegment.fromJson(Map<String, dynamic> json) =>
      RecordingSegment(
        id: json['id'] as int,
        cameraId: json['camera_id'] as int,
        filePath: json['file_path'] as String,
        startTime: json['start_time'] as String,
        endTime: json['end_time'] as String?,
        fileSize: json['file_size'] as int?,
        duration: (json['duration'] as num?)?.toDouble(),
        inProgress: json['in_progress'] as bool? ?? false,
      );
}
