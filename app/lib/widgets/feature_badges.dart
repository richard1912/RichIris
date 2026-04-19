import 'package:flutter/material.dart';
import '../models/camera.dart';
import 'icon_chip.dart';

/// Column of feature-indicator chips showing which detection / automation
/// features are enabled on a camera (motion / AI / face / zones / scripts).
/// Rendered identically on the grid card and in the fullscreen header so
/// users see the same at-a-glance status in both views.
class FeatureBadges extends StatelessWidget {
  final Camera camera;
  const FeatureBadges({super.key, required this.camera});

  @override
  Widget build(BuildContext context) {
    final badges = <IconChip>[];
    if (camera.motionSensitivity > 0) {
      badges.add(const IconChip(
        icon: Icons.directions_run,
        color: Color(0xFF6B7280),
        tooltip: 'Motion detection enabled',
      ));
    }
    if (camera.motionSensitivity > 0 && camera.aiDetection) {
      badges.add(const IconChip(
        icon: Icons.psychology,
        color: Color(0xFF3B82F6),
        tooltip: 'AI object detection enabled',
      ));
    }
    if (camera.faceRecognition && camera.aiDetectPersons) {
      badges.add(const IconChip(
        icon: Icons.face,
        color: Color(0xFF06B6D4),
        tooltip: 'Face recognition enabled',
      ));
    }
    if (camera.zoneCount > 0) {
      badges.add(IconChip(
        icon: Icons.hexagon_outlined,
        color: const Color(0xFF8B5CF6),
        tooltip: '${camera.zoneCount} detection zone${camera.zoneCount == 1 ? '' : 's'}',
      ));
    }
    final scriptCount = camera.motionScripts
        .where((s) =>
            (s.on != null && s.on!.trim().isNotEmpty) ||
            (s.off != null && s.off!.trim().isNotEmpty))
        .length;
    if (scriptCount > 0) {
      badges.add(IconChip(
        icon: Icons.code,
        color: const Color(0xFF22C55E),
        tooltip: '$scriptCount script${scriptCount == 1 ? '' : 's'} configured',
      ));
    }
    if (badges.isEmpty) return const SizedBox.shrink();
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        for (var i = 0; i < badges.length; i++) ...[
          if (i > 0) const SizedBox(height: 3),
          badges[i],
        ],
      ],
    );
  }
}
