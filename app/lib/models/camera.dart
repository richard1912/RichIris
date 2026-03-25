class Camera {
  final int id;
  final String name;
  final String rtspUrl;
  final String? subStreamUrl;
  final bool enabled;
  final int? width;
  final int? height;
  final String? codec;
  final double? fps;
  final int rotation;
  final String createdAt;

  Camera({
    required this.id,
    required this.name,
    required this.rtspUrl,
    this.subStreamUrl,
    this.enabled = true,
    this.width,
    this.height,
    this.codec,
    this.fps,
    this.rotation = 0,
    required this.createdAt,
  });

  factory Camera.fromJson(Map<String, dynamic> json) => Camera(
        id: json['id'] as int,
        name: json['name'] as String,
        rtspUrl: json['rtsp_url'] as String,
        subStreamUrl: json['sub_stream_url'] as String?,
        enabled: json['enabled'] as bool? ?? true,
        width: json['width'] as int?,
        height: json['height'] as int?,
        codec: json['codec'] as String?,
        fps: (json['fps'] as num?)?.toDouble(),
        rotation: json['rotation'] as int? ?? 0,
        createdAt: json['created_at'] as String,
      );

  Map<String, dynamic> toJson() => {
        'name': name,
        'rtsp_url': rtspUrl,
        if (subStreamUrl != null) 'sub_stream_url': subStreamUrl,
        'enabled': enabled,
        'rotation': rotation,
      };
}
