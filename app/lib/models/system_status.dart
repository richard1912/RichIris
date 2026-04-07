class StreamStatus {
  final int cameraId;
  final String cameraName;
  final bool running;
  final int? pid;
  final double? uptimeSeconds;
  final String? error;
  final bool? go2rtcConnected;
  final int? go2rtcConsumers;

  StreamStatus({
    required this.cameraId,
    required this.cameraName,
    required this.running,
    this.pid,
    this.uptimeSeconds,
    this.error,
    this.go2rtcConnected,
    this.go2rtcConsumers,
  });

  factory StreamStatus.fromJson(Map<String, dynamic> json) => StreamStatus(
        cameraId: json['camera_id'] as int,
        cameraName: json['camera_name'] as String,
        running: json['running'] as bool,
        pid: json['pid'] as int?,
        uptimeSeconds: (json['uptime_seconds'] as num?)?.toDouble(),
        error: json['error'] as String?,
        go2rtcConnected: json['go2rtc_connected'] as bool?,
        go2rtcConsumers: json['go2rtc_consumers'] as int?,
      );
}

class SystemStatus {
  final List<StreamStatus> streams;
  final int totalCameras;
  final int activeStreams;

  SystemStatus({
    required this.streams,
    required this.totalCameras,
    required this.activeStreams,
  });

  factory SystemStatus.fromJson(Map<String, dynamic> json) => SystemStatus(
        streams: (json['streams'] as List)
            .map((e) => StreamStatus.fromJson(e as Map<String, dynamic>))
            .toList(),
        totalCameras: json['total_cameras'] as int,
        activeStreams: json['active_streams'] as int,
      );
}
