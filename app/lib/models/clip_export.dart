class ClipExport {
  final int id;
  final int cameraId;
  final String startTime;
  final String endTime;
  final String? filePath;
  final String status;
  final String createdAt;

  ClipExport({
    required this.id,
    required this.cameraId,
    required this.startTime,
    required this.endTime,
    this.filePath,
    required this.status,
    required this.createdAt,
  });

  factory ClipExport.fromJson(Map<String, dynamic> json) => ClipExport(
        id: json['id'] as int,
        cameraId: json['camera_id'] as int,
        startTime: json['start_time'] as String,
        endTime: json['end_time'] as String,
        filePath: json['file_path'] as String?,
        status: json['status'] as String,
        createdAt: json['created_at'] as String,
      );
}
