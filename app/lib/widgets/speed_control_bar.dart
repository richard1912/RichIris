import 'package:flutter/material.dart';
import '../config/constants.dart';

class SpeedControlBar extends StatelessWidget {
  final int currentSpeed;
  final ValueChanged<int> onSpeedChanged;

  const SpeedControlBar({
    super.key,
    required this.currentSpeed,
    required this.onSpeedChanged,
  });

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: kSpeeds.map((speed) {
        final isActive = currentSpeed == speed;
        final label = speed > 0 ? '${speed}x' : '${speed}x';
        return Padding(
          padding: const EdgeInsets.symmetric(horizontal: 2),
          child: SizedBox(
            height: 28,
            child: TextButton(
              onPressed: () => onSpeedChanged(speed),
              style: TextButton.styleFrom(
                padding: const EdgeInsets.symmetric(horizontal: 6),
                minimumSize: Size.zero,
                backgroundColor:
                    isActive ? const Color(0xFF3B82F6) : Colors.transparent,
                foregroundColor:
                    isActive ? Colors.white : const Color(0xFF737373),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(4),
                ),
              ),
              child: Text(label, style: const TextStyle(fontSize: 11)),
            ),
          ),
        );
      }).toList(),
    );
  }
}
