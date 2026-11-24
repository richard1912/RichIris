class PtzStatus {
  final double pan;
  final double tilt;
  final double zoom;
  final bool moving;

  const PtzStatus({
    required this.pan,
    required this.tilt,
    required this.zoom,
    required this.moving,
  });

  factory PtzStatus.fromJson(Map<String, dynamic> json) => PtzStatus(
        pan: (json['pan'] as num).toDouble(),
        tilt: (json['tilt'] as num).toDouble(),
        zoom: (json['zoom'] as num).toDouble(),
        moving: json['moving'] as bool? ?? false,
      );
}

class PtzPreset {
  final int id;
  final String name;

  const PtzPreset({required this.id, required this.name});

  factory PtzPreset.fromJson(Map<String, dynamic> json) => PtzPreset(
        id: json['id'] as int,
        name: json['name'] as String? ?? '',
      );

  String get label => name.trim().isEmpty ? 'Preset $id' : name;
}
