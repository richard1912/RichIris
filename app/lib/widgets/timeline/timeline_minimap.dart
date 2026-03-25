import 'package:flutter/material.dart';
import 'timeline_controller.dart';

class TimelineMinimap extends StatelessWidget {
  final TimelineController controller;
  final ValueChanged<double> onPan;

  const TimelineMinimap({
    super.key,
    required this.controller,
    required this.onPan,
  });

  @override
  Widget build(BuildContext context) {
    if (controller.zoomLevel <= 1) return const SizedBox.shrink();

    return GestureDetector(
      onHorizontalDragUpdate: (details) {
        final box = context.findRenderObject() as RenderBox;
        final pct = details.localPosition.dx / box.size.width;
        final targetHour = pct * 24.0 - controller.visibleHours / 2;
        onPan(targetHour.clamp(0.0, 24.0 - controller.visibleHours));
      },
      onTapDown: (details) {
        final box = context.findRenderObject() as RenderBox;
        final pct = details.localPosition.dx / box.size.width;
        final targetHour = pct * 24.0 - controller.visibleHours / 2;
        onPan(targetHour.clamp(0.0, 24.0 - controller.visibleHours));
      },
      child: Container(
        height: 16,
        decoration: BoxDecoration(
          color: const Color(0xFF0A0A0A),
          borderRadius: BorderRadius.circular(2),
        ),
        child: CustomPaint(
          painter: _MinimapPainter(controller),
          size: Size.infinite,
        ),
      ),
    );
  }
}

class _MinimapPainter extends CustomPainter {
  final TimelineController controller;
  _MinimapPainter(this.controller);

  @override
  void paint(Canvas canvas, Size size) {
    // Segments
    final segPaint = Paint()..color = const Color(0xFF3B82F6).withValues(alpha: 0.4);
    for (final seg in controller.merged) {
      final x1 = seg.startHour / 24.0 * size.width;
      final x2 = seg.endHour / 24.0 * size.width;
      canvas.drawRect(Rect.fromLTRB(x1, 2, x2, size.height - 2), segPaint);
    }

    // Viewport indicator
    final vpX1 = controller.viewportStart / 24.0 * size.width;
    final vpX2 = (controller.viewportStart + controller.visibleHours) / 24.0 * size.width;
    final vpPaint = Paint()
      ..color = Colors.white.withValues(alpha: 0.2)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1;
    canvas.drawRect(Rect.fromLTRB(vpX1, 0, vpX2, size.height), vpPaint);

    // Playhead
    if (controller.playheadHour != null) {
      final px = controller.playheadHour! / 24.0 * size.width;
      canvas.drawLine(
        Offset(px, 0),
        Offset(px, size.height),
        Paint()
          ..color = const Color(0xFFEF4444)
          ..strokeWidth = 1,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _MinimapPainter oldDelegate) => true;
}
