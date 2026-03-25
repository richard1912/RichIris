class CameraStorageStats {
  final int cameraId;
  final int segmentCount;
  final int totalSizeBytes;
  final String? oldestRecording;
  final String? newestRecording;

  CameraStorageStats({
    required this.cameraId,
    required this.segmentCount,
    required this.totalSizeBytes,
    this.oldestRecording,
    this.newestRecording,
  });

  factory CameraStorageStats.fromJson(Map<String, dynamic> json) =>
      CameraStorageStats(
        cameraId: json['camera_id'] as int,
        segmentCount: json['segment_count'] as int,
        totalSizeBytes: json['total_size_bytes'] as int,
        oldestRecording: json['oldest_recording'] as String?,
        newestRecording: json['newest_recording'] as String?,
      );
}

class StorageStats {
  final int diskTotalBytes;
  final int diskUsedBytes;
  final int diskFreeBytes;
  final int recordingsTotalBytes;
  final int maxStorageBytes;
  final int maxAgeDays;
  final List<CameraStorageStats> cameraStats;

  StorageStats({
    required this.diskTotalBytes,
    required this.diskUsedBytes,
    required this.diskFreeBytes,
    required this.recordingsTotalBytes,
    required this.maxStorageBytes,
    required this.maxAgeDays,
    required this.cameraStats,
  });

  factory StorageStats.fromJson(Map<String, dynamic> json) => StorageStats(
        diskTotalBytes: json['disk_total_bytes'] as int,
        diskUsedBytes: json['disk_used_bytes'] as int,
        diskFreeBytes: json['disk_free_bytes'] as int,
        recordingsTotalBytes: json['recordings_total_bytes'] as int,
        maxStorageBytes: json['max_storage_bytes'] as int,
        maxAgeDays: json['max_age_days'] as int,
        cameraStats: (json['camera_stats'] as List)
            .map(
                (e) => CameraStorageStats.fromJson(e as Map<String, dynamic>))
            .toList(),
      );
}

class RetentionResult {
  final int deleted;
  final int freedBytes;

  RetentionResult({required this.deleted, required this.freedBytes});

  factory RetentionResult.fromJson(Map<String, dynamic> json) =>
      RetentionResult(
        deleted: json['deleted'] as int,
        freedBytes: json['freed_bytes'] as int,
      );
}
