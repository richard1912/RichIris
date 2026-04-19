import 'package:flutter/material.dart';

/// Renders a polygon zone on top of a snapshot image. Coordinates arrive in
/// display-space (pixels relative to the image widget), not normalized —
/// the editor converts back and forth so points drawn here map 1:1 to what
/// the user sees.
class ZonePainter extends CustomPainter {
  final List<Offset> points;
  final int? selectedIndex;
  final Color fillColor;
  final Color strokeColor;

  ZonePainter({
    required this.points,
    this.selectedIndex,
    this.fillColor = const Color(0x553B82F6),
    this.strokeColor = const Color(0xFF3B82F6),
  });

  @override
  void paint(Canvas canvas, Size size) {
    if (points.isEmpty) return;

    final path = Path()..moveTo(points.first.dx, points.first.dy);
    for (final p in points.skip(1)) {
      path.lineTo(p.dx, p.dy);
    }
    if (points.length >= 3) path.close();

    if (points.length >= 3) {
      canvas.drawPath(
        path,
        Paint()
          ..color = fillColor
          ..style = PaintingStyle.fill,
      );
    }

    canvas.drawPath(
      path,
      Paint()
        ..color = strokeColor
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.0,
    );

    for (var i = 0; i < points.length; i++) {
      final isSelected = i == selectedIndex;
      final p = points[i];
      canvas.drawCircle(
        p,
        isSelected ? 9.0 : 6.0,
        Paint()
          ..color = isSelected ? Colors.white : strokeColor
          ..style = PaintingStyle.fill,
      );
      canvas.drawCircle(
        p,
        isSelected ? 9.0 : 6.0,
        Paint()
          ..color = Colors.white
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1.5,
      );
    }
  }

  @override
  bool shouldRepaint(covariant ZonePainter old) =>
      old.points != points || old.selectedIndex != selectedIndex;
}
