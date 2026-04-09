import 'package:flutter/material.dart';
import '../config/constants.dart';

/// Selector chip for stream source (Main / Sub).
class StreamSourceSelector extends StatelessWidget {
  final StreamSource value;
  final ValueChanged<StreamSource> onChanged;

  const StreamSourceSelector({
    super.key,
    required this.value,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    return PopupMenuButton<StreamSource>(
      initialValue: value,
      onSelected: onChanged,
      tooltip: 'Stream source',
      child: _SelectorChip(label: value.label),
      itemBuilder: (_) => StreamSource.values
          .map((s) => PopupMenuItem(
                value: s,
                child: Text(s.description, style: const TextStyle(fontSize: 13)),
              ))
          .toList(),
    );
  }
}

/// Selector chip for quality tier (Direct / High / Low).
/// On Android live view, Direct is hidden (compatibility issues with raw RTSP passthrough).
/// Playback Direct is fine on Android (clean fMP4 from ffmpeg).
class QualitySelector extends StatelessWidget {
  final Quality value;
  final ValueChanged<Quality> onChanged;
  final bool isLive;

  const QualitySelector({
    super.key,
    required this.value,
    required this.onChanged,
    this.isLive = true,
  });

  List<Quality> get _availableQualities => Quality.values;

  /// Effective quality shown in the chip.
  Quality get _displayQuality => value;

  @override
  Widget build(BuildContext context) {
    return PopupMenuButton<Quality>(
      initialValue: _displayQuality,
      onSelected: onChanged,
      tooltip: 'Video quality',
      child: _SelectorChip(label: _displayQuality.label),
      itemBuilder: (_) => _availableQualities
          .map((q) => PopupMenuItem(
                value: q,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(q.label, style: const TextStyle(fontSize: 13)),
                    Text(q.description,
                        style: const TextStyle(fontSize: 10, color: Color(0xFF737373))),
                  ],
                ),
              ))
          .toList(),
    );
  }
}

class _SelectorChip extends StatelessWidget {
  final String label;
  const _SelectorChip({required this.label});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
      decoration: BoxDecoration(
        color: const Color(0xFF262626),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: const Color(0xFF404040)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            label,
            style: const TextStyle(fontSize: 12, color: Color(0xFFD4D4D4)),
          ),
          const SizedBox(width: 4),
          const Icon(Icons.unfold_more, size: 14, color: Color(0xFF737373)),
        ],
      ),
    );
  }
}
