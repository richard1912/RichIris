class MotionScriptConfig {
  String? on;
  String? off;
  bool persons;
  bool vehicles;
  bool animals;
  bool motionOnly;

  MotionScriptConfig({
    this.on,
    this.off,
    this.persons = true,
    this.vehicles = true,
    this.animals = true,
    this.motionOnly = true,
  });

  factory MotionScriptConfig.fromJson(Map<String, dynamic> json) =>
      MotionScriptConfig(
        on: json['on'] as String?,
        off: json['off'] as String?,
        persons: json['persons'] as bool? ?? true,
        vehicles: json['vehicles'] as bool? ?? true,
        animals: json['animals'] as bool? ?? true,
        motionOnly: json['motion_only'] as bool? ?? true,
      );

  Map<String, dynamic> toJson() => {
        'on': on,
        'off': off,
        'persons': persons,
        'vehicles': vehicles,
        'animals': animals,
        'motion_only': motionOnly,
      };

  MotionScriptConfig copy() => MotionScriptConfig(
        on: on,
        off: off,
        persons: persons,
        vehicles: vehicles,
        animals: animals,
        motionOnly: motionOnly,
      );
}

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
  final int motionSensitivity;
  final String? motionScript;
  final String? motionScriptOff;
  final List<MotionScriptConfig> motionScripts;
  final bool aiDetection;
  final bool aiDetectPersons;
  final bool aiDetectVehicles;
  final bool aiDetectAnimals;
  final int aiConfidenceThreshold;
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
    this.motionSensitivity = 0,
    this.motionScript,
    this.motionScriptOff,
    this.motionScripts = const [],
    this.aiDetection = false,
    this.aiDetectPersons = true,
    this.aiDetectVehicles = false,
    this.aiDetectAnimals = false,
    this.aiConfidenceThreshold = 50,
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
        motionSensitivity: json['motion_sensitivity'] as int? ?? 0,
        motionScript: json['motion_script'] as String?,
        motionScriptOff: json['motion_script_off'] as String?,
        motionScripts: (json['motion_scripts'] as List<dynamic>?)
                ?.map((e) => MotionScriptConfig.fromJson(e as Map<String, dynamic>))
                .toList() ??
            [],
        aiDetection: json['ai_detection'] as bool? ?? false,
        aiDetectPersons: json['ai_detect_persons'] as bool? ?? true,
        aiDetectVehicles: json['ai_detect_vehicles'] as bool? ?? false,
        aiDetectAnimals: json['ai_detect_animals'] as bool? ?? false,
        aiConfidenceThreshold: json['ai_confidence_threshold'] as int? ?? 50,
        createdAt: json['created_at'] as String,
      );

  Map<String, dynamic> toJson() => {
        'name': name,
        'rtsp_url': rtspUrl,
        if (subStreamUrl != null) 'sub_stream_url': subStreamUrl,
        'enabled': enabled,
        'rotation': rotation,
        'motion_sensitivity': motionSensitivity,
        if (motionScript != null) 'motion_script': motionScript,
        'motion_scripts': motionScripts.map((s) => s.toJson()).toList(),
        'ai_detection': aiDetection,
        'ai_detect_persons': aiDetectPersons,
        'ai_detect_vehicles': aiDetectVehicles,
        'ai_detect_animals': aiDetectAnimals,
        'ai_confidence_threshold': aiConfidenceThreshold,
      };
}
