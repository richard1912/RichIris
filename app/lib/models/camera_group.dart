class CameraGroup {
  final int id;
  final String name;
  final int sortOrder;
  final int cameraCount;

  CameraGroup({
    required this.id,
    required this.name,
    this.sortOrder = 0,
    this.cameraCount = 0,
  });

  factory CameraGroup.fromJson(Map<String, dynamic> json) => CameraGroup(
        id: json['id'] as int,
        name: json['name'] as String,
        sortOrder: json['sort_order'] as int? ?? 0,
        cameraCount: json['camera_count'] as int? ?? 0,
      );
}
