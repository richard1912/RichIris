class ClipExport {
  final int id;
  final int cameraId;
  final List<int>? cameraIds;
  final String mode; // "single" | "grid"
  final String startTime;
  final String endTime;
  final String? filePath;
  final String status;
  final String createdAt;

  ClipExport({
    required this.id,
    required this.cameraId,
    this.cameraIds,
    this.mode = 'single',
    required this.startTime,
    required this.endTime,
    this.filePath,
    required this.status,
    required this.createdAt,
  });

  bool get isGrid => mode == 'grid';

  factory ClipExport.fromJson(Map<String, dynamic> json) => ClipExport(
        id: json['id'] as int,
        cameraId: json['camera_id'] as int,
        cameraIds: (json['camera_ids'] as List?)?.map((e) => e as int).toList(),
        mode: (json['mode'] as String?) ?? 'single',
        startTime: json['start_time'] as String,
        endTime: json['end_time'] as String,
        filePath: json['file_path'] as String?,
        status: json['status'] as String,
        createdAt: json['created_at'] as String,
      );
}
