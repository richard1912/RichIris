import 'package:flutter/material.dart';
import 'timeline_controller.dart';
import '../../utils/time_utils.dart';

class TimelinePainter extends CustomPainter {
  final TimelineController controller;

  TimelinePainter(this.controller);

  @override
  void paint(Canvas canvas, Size size) {
    final vpStart = controller.viewportStart;
    final vpHours = controller.visibleHours;
    final vpEnd = vpStart + vpHours;

    // Background
    canvas.drawRect(
      Rect.fromLTWH(0, 0, size.width, size.height),
      Paint()..color = const Color(0xFF1A1A1A),
    );

    // Hour markers
    _drawMarkers(canvas, size, vpStart, vpEnd, vpHours);

    // Segments (blue bars)
    _drawSegments(canvas, size, vpStart, vpHours);

    // Export range overlay (green)
    _drawExportRange(canvas, size, vpStart, vpHours);

    // Playhead (red line)
    _drawPlayhead(canvas, size, vpStart, vpHours);

    // Scrub indicator with time label
    _drawScrubIndicator(canvas, size, vpStart, vpHours);
  }

  void _drawMarkers(Canvas canvas, Size size, double vpStart, double vpEnd, double vpHours) {
    final markerPaint = Paint()..color = const Color(0xFF333333);
    final textStyle = const TextStyle(color: Color(0xFF737373), fontSize: 9);

    // Adaptive interval based on zoom
    double interval;
    if (vpHours <= 2) {
      interval = 5 / 60; // 5 min
    } else if (vpHours <= 4) {
      interval = 15 / 60; // 15 min
    } else if (vpHours <= 8) {
      interval = 0.5; // 30 min
    } else if (vpHours <= 16) {
      interval = 1; // 1 hour
    } else {
      interval = 2; // 2 hours
    }

    double h = (vpStart / interval).ceil() * interval;
    while (h <= vpEnd) {
      final x = (h - vpStart) / vpHours * size.width;
      canvas.drawLine(
        Offset(x, 0),
        Offset(x, size.height),
        markerPaint,
      );

      final hours = h.floor();
      final minutes = ((h - hours) * 60).round();
      final label = '${hours.toString().padLeft(2, '0')}:${minutes.toString().padLeft(2, '0')}';
      final tp = TextPainter(
        text: TextSpan(text: label, style: textStyle),
        textDirection: TextDirection.ltr,
      )..layout();
      tp.paint(canvas, Offset(x + 2, 2));

      h += interval;
    }
  }

  void _drawSegments(Canvas canvas, Size size, double vpStart, double vpHours) {
    final segPaint = Paint()..color = const Color(0xFF3B82F6).withValues(alpha: 0.6);
    final barTop = size.height * 0.3;
    final barHeight = size.height * 0.5;

    for (final seg in controller.merged) {
      final x1 = (seg.startHour - vpStart) / vpHours * size.width;
      final x2 = (seg.endHour - vpStart) / vpHours * size.width;
      if (x2 < 0 || x1 > size.width) continue;
      canvas.drawRRect(
        RRect.fromRectAndRadius(
          Rect.fromLTRB(
            x1.clamp(0, size.width),
            barTop,
            x2.clamp(0, size.width),
            barTop + barHeight,
          ),
          const Radius.circular(2),
        ),
        segPaint,
      );
    }
  }

  void _drawExportRange(Canvas canvas, Size size, double vpStart, double vpHours) {
    if (!controller.exportMode) return;
    final s = controller.exportStartHour;
    final e = controller.exportEndHour;
    if (s == null) return;

    final end = e ?? s;
    final x1 = (s - vpStart) / vpHours * size.width;
    final x2 = (end - vpStart) / vpHours * size.width;

    // Green range fill
    if (e != null) {
      final exportPaint = Paint()..color = const Color(0xFF22C55E).withValues(alpha: 0.2);
      canvas.drawRect(Rect.fromLTRB(x1, 0, x2, size.height), exportPaint);
    }

    // Start marker (green line + label)
    final markerPaint = Paint()..color = const Color(0xFF22C55E)..strokeWidth = 2;
    if (x1 >= -5 && x1 <= size.width + 5) {
      canvas.drawLine(Offset(x1, 0), Offset(x1, size.height), markerPaint);
      _drawExportLabel(canvas, size, x1, s, true);
    }

    // End marker
    if (e != null && x2 >= -5 && x2 <= size.width + 5) {
      canvas.drawLine(Offset(x2, 0), Offset(x2, size.height), markerPaint);
      _drawExportLabel(canvas, size, x2, e, false);
    }
  }

  void _drawExportLabel(Canvas canvas, Size size, double x, double hour, bool isStart) {
    final timeStr = hourToTimeString(hour);
    final tp = TextPainter(
      text: TextSpan(
        text: timeStr,
        style: const TextStyle(color: Color(0xFF22C55E), fontSize: 9, fontWeight: FontWeight.w600),
      ),
      textDirection: TextDirection.ltr,
    )..layout();

    final bw = tp.width + 8;
    final bh = tp.height + 4;
    // Position label: start goes right, end goes left
    final bx = isStart
        ? (x + 2).clamp(0.0, size.width - bw)
        : (x - bw - 2).clamp(0.0, size.width - bw);

    canvas.drawRRect(
      RRect.fromRectAndRadius(
        Rect.fromLTWH(bx, 2, bw, bh),
        const Radius.circular(3),
      ),
      Paint()..color = const Color(0xE0000000),
    );
    tp.paint(canvas, Offset(bx + 4, 4));
  }

  void _drawPlayhead(Canvas canvas, Size size, double vpStart, double vpHours) {
    final ph = controller.playheadHour;
    if (ph == null) return;
    final x = (ph - vpStart) / vpHours * size.width;
    if (x < -5 || x > size.width + 5) return;

    final paint = Paint()
      ..color = const Color(0xFFEF4444)
      ..strokeWidth = 2;
    canvas.drawLine(Offset(x, 0), Offset(x, size.height), paint);
    canvas.drawCircle(Offset(x, 8), 5, paint);
  }

  void _drawScrubIndicator(Canvas canvas, Size size, double vpStart, double vpHours) {
    final sh = controller.scrubHour;
    if (sh == null) return;
    final x = (sh - vpStart) / vpHours * size.width;
    if (x < -5 || x > size.width + 5) return;

    canvas.drawLine(
      Offset(x, 0), Offset(x, size.height),
      Paint()..color = const Color(0xCCFFFFFF)..strokeWidth = 1.5,
    );

    final timeStr = hourToTimeString(sh);
    final tp = TextPainter(
      text: TextSpan(
        text: timeStr,
        style: const TextStyle(
          color: Colors.white, fontSize: 10, fontWeight: FontWeight.w600,
        ),
      ),
      textDirection: TextDirection.ltr,
    )..layout();

    final bw = tp.width + 10;
    final bh = tp.height + 6;
    final bx = (x - bw / 2).clamp(0.0, size.width - bw);

    canvas.drawRRect(
      RRect.fromRectAndRadius(
        Rect.fromLTWH(bx, size.height - bh - 2, bw, bh),
        const Radius.circular(4),
      ),
      Paint()..color = const Color(0xE0000000),
    );
    tp.paint(canvas, Offset(bx + 5, size.height - bh + 1));
  }

  @override
  bool shouldRepaint(covariant TimelinePainter oldDelegate) => true;
}
