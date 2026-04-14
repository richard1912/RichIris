import 'package:flutter/material.dart';
import '../models/grid_layout.dart';

class LayoutPickerButton extends StatelessWidget {
  final String currentLayoutId;
  final ValueChanged<String> onLayoutChanged;

  const LayoutPickerButton({
    super.key,
    required this.currentLayoutId,
    required this.onLayoutChanged,
  });

  Future<void> _openPicker(BuildContext context) async {
    final selected = await showDialog<String>(
      context: context,
      builder: (ctx) => _LayoutPickerDialog(currentLayoutId: currentLayoutId),
    );
    if (selected != null && selected != currentLayoutId) {
      onLayoutChanged(selected);
    }
  }

  @override
  Widget build(BuildContext context) {
    return IconButton(
      icon: const Icon(Icons.grid_view, size: 20),
      tooltip: 'Grid layout',
      onPressed: () => _openPicker(context),
    );
  }
}

class _LayoutPickerDialog extends StatelessWidget {
  final String currentLayoutId;
  const _LayoutPickerDialog({required this.currentLayoutId});

  @override
  Widget build(BuildContext context) {
    return Dialog(
      backgroundColor: const Color(0xFF171717),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 480),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Padding(
                padding: EdgeInsets.only(bottom: 12),
                child: Text(
                  'Grid Layout',
                  style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
                ),
              ),
              Wrap(
                spacing: 12,
                runSpacing: 12,
                children: [
                  for (final layout in kGridLayouts)
                    _LayoutThumb(
                      layout: layout,
                      selected: layout.id == currentLayoutId,
                      onTap: () => Navigator.pop(context, layout.id),
                    ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _LayoutThumb extends StatelessWidget {
  final GridLayout layout;
  final bool selected;
  final VoidCallback onTap;

  const _LayoutThumb({
    required this.layout,
    required this.selected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    const thumbW = 80.0;
    const thumbH = 60.0;
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(6),
      child: Container(
        padding: const EdgeInsets.all(6),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(6),
          border: Border.all(
            color: selected ? const Color(0xFF3B82F6) : const Color(0xFF333333),
            width: selected ? 2 : 1,
          ),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            SizedBox(
              width: thumbW,
              height: thumbH,
              child: CustomPaint(
                painter: _LayoutThumbPainter(
                  layout: layout,
                  color: selected
                      ? const Color(0xFF3B82F6)
                      : const Color(0xFFA3A3A3),
                ),
              ),
            ),
            const SizedBox(height: 4),
            Text(
              layout.label,
              style: TextStyle(
                fontSize: 11,
                color: selected ? const Color(0xFF3B82F6) : const Color(0xFFD4D4D4),
                fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _LayoutThumbPainter extends CustomPainter {
  final GridLayout layout;
  final Color color;

  _LayoutThumbPainter({required this.layout, required this.color});

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.2;
    const gap = 1.5;
    for (final slot in layout.slots) {
      final rect = Rect.fromLTWH(
        slot.x * size.width + gap,
        slot.y * size.height + gap,
        slot.w * size.width - gap * 2,
        slot.h * size.height - gap * 2,
      );
      canvas.drawRRect(
        RRect.fromRectAndRadius(rect, const Radius.circular(2)),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _LayoutThumbPainter old) =>
      old.layout.id != layout.id || old.color != color;
}
