import 'package:flutter/material.dart';
import '../models/camera.dart';
import '../models/system_status.dart';
import 'live_player.dart';

class CameraCard extends StatelessWidget {
  final Camera camera;
  final StreamStatus? stream;
  final String streamUrl;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback onEdit;

  const CameraCard({
    super.key,
    required this.camera,
    this.stream,
    required this.streamUrl,
    this.selected = false,
    required this.onTap,
    required this.onEdit,
  });

  @override
  Widget build(BuildContext context) {
    final running = stream?.running ?? false;

    return GestureDetector(
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(8),
          border: Border.all(
            color: selected ? const Color(0xFF3B82F6) : const Color(0xFF404040),
            width: selected ? 2 : 1,
          ),
          color: const Color(0xFF1A1A1A),
        ),
        clipBehavior: Clip.antiAlias,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Expanded(
              child: Stack(
                fit: StackFit.expand,
                children: [
                  if (camera.enabled && running)
                    IgnorePointer(
                      child: LivePlayer(
                        wsUrl: streamUrl,
                        rotation: camera.rotation,
                        fit: BoxFit.cover,
                      ),
                    )
                  else
                    Container(
                      color: const Color(0xFF0A0A0A),
                      child: Center(
                        child: Icon(
                          camera.enabled ? Icons.videocam : Icons.videocam_off,
                          color: const Color(0xFF525252),
                          size: 32,
                        ),
                      ),
                    ),
                  // Edit button
                  Positioned(
                    top: 4,
                    right: 4,
                    child: Material(
                      color: Colors.transparent,
                      child: InkWell(
                        onTap: onEdit,
                        borderRadius: BorderRadius.circular(4),
                        child: Container(
                          padding: const EdgeInsets.all(4),
                          decoration: BoxDecoration(
                            color: Colors.black54,
                            borderRadius: BorderRadius.circular(4),
                          ),
                          child: const Icon(Icons.settings,
                              size: 14, color: Color(0xFFA3A3A3)),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
              color: const Color(0xFF171717),
              child: Row(
                children: [
                  Container(
                    width: 6,
                    height: 6,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: running
                          ? const Color(0xFF22C55E)
                          : camera.enabled
                              ? const Color(0xFFEAB308)
                              : const Color(0xFFEF4444),
                    ),
                  ),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      camera.name,
                      style: const TextStyle(fontSize: 12),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
