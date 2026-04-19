import 'package:flutter/material.dart';

/// Small black54 chip wrapping a 14-px icon, with a tooltip and optional tap
/// handler. Used for the gear / + / status / feature-badge icon clusters on
/// both the grid camera card and the fullscreen single-camera header so they
/// stay visually identical.
class IconChip extends StatelessWidget {
  final IconData icon;
  final String tooltip;
  final VoidCallback? onTap;
  final Color color;

  const IconChip({
    super.key,
    required this.icon,
    required this.tooltip,
    this.onTap,
    this.color = const Color(0xFFA3A3A3),
  });

  @override
  Widget build(BuildContext context) {
    final chip = Container(
      padding: const EdgeInsets.all(4),
      decoration: BoxDecoration(
        color: Colors.black54,
        borderRadius: BorderRadius.circular(4),
      ),
      child: Icon(icon, size: 14, color: color),
    );
    final tappable = onTap == null
        ? chip
        : Material(
            color: Colors.transparent,
            child: InkWell(
              onTap: onTap,
              borderRadius: BorderRadius.circular(4),
              child: chip,
            ),
          );
    return Tooltip(message: tooltip, child: tappable);
  }
}
