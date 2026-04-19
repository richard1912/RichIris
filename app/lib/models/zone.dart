import 'dart:ui' show Offset;

/// User-drawn polygon that per-script configs can reference to limit
/// where motion detection / object detection triggers. Points are
/// normalized [0,1] against the capture frame so they survive stream
/// resolution changes.
class Zone {
  final int id;
  final int cameraId;
  final String name;
  final List<Offset> points;
  final String? createdAt;
  final String? updatedAt;

  Zone({
    required this.id,
    required this.cameraId,
    required this.name,
    required this.points,
    this.createdAt,
    this.updatedAt,
  });

  factory Zone.fromJson(Map<String, dynamic> json) {
    final raw = (json['points'] as List<dynamic>?) ?? [];
    final pts = raw.whereType<List<dynamic>>().where((p) => p.length >= 2).map(
          (p) => Offset((p[0] as num).toDouble(), (p[1] as num).toDouble()),
        );
    return Zone(
      id: json['id'] as int,
      cameraId: json['camera_id'] as int,
      name: json['name'] as String,
      points: pts.toList(),
      createdAt: json['created_at'] as String?,
      updatedAt: json['updated_at'] as String?,
    );
  }

  Map<String, dynamic> toPayload() => {
        'name': name,
        'points': points.map((p) => [p.dx, p.dy]).toList(),
      };
}
