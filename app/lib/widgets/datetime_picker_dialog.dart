import 'dart:math' as math;
import 'dart:ui';
import 'package:flutter/material.dart';

/// Which field the dial is editing.
enum DialMode { hour, minute, second }

/// Shows a combined date + rotary time picker dialog.
/// Returns `{date: 'YYYY-MM-DD', hour: int, minute: int, second: int}` or null.
Future<Map<String, dynamic>?> showDateTimePickerDialog(
  BuildContext context, {
  required String initialDate,
  int initialHour = 0,
  int initialMinute = 0,
  int initialSecond = 0,
}) {
  return showDialog<Map<String, dynamic>>(
    context: context,
    builder: (_) => DateTimePickerDialog(
      initialDate: initialDate,
      initialHour: initialHour,
      initialMinute: initialMinute,
      initialSecond: initialSecond,
    ),
  );
}

class DateTimePickerDialog extends StatefulWidget {
  final String initialDate;
  final int initialHour;
  final int initialMinute;
  final int initialSecond;

  const DateTimePickerDialog({
    super.key,
    required this.initialDate,
    required this.initialHour,
    required this.initialMinute,
    required this.initialSecond,
  });

  @override
  State<DateTimePickerDialog> createState() => _DateTimePickerDialogState();
}

class _DateTimePickerDialogState extends State<DateTimePickerDialog> {
  late String _date;
  late int _h, _m, _s;
  DialMode _mode = DialMode.hour;

  static const _accent = Color(0xFF3B82F6);
  static const _bg = Color(0xFF191919);
  static const _dialBg = Color(0xFF222222);
  static const _dimText = Color(0xFF525252);

  @override
  void initState() {
    super.initState();
    _date = widget.initialDate;
    _h = widget.initialHour;
    _m = widget.initialMinute;
    _s = widget.initialSecond;
  }

  // --- date helpers ---

  String _fmtDate(String iso) {
    final p = iso.split('-');
    const mo = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    final dt = DateTime(int.parse(p[0]), int.parse(p[1]), int.parse(p[2]));
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    return '${days[dt.weekday - 1]}, ${mo[int.parse(p[1])]} ${int.parse(p[2])}';
  }

  void _stepDate(int delta) {
    final p = _date.split('-');
    final dt = DateTime(int.parse(p[0]), int.parse(p[1]), int.parse(p[2])).add(Duration(days: delta));
    if (dt.isAfter(DateTime.now().add(const Duration(days: 1)))) return;
    setState(() => _date = '${dt.year}-${dt.month.toString().padLeft(2, '0')}-${dt.day.toString().padLeft(2, '0')}');
  }

  Future<void> _pickDate() async {
    final p = _date.split('-');
    final current = DateTime(int.parse(p[0]), int.parse(p[1]), int.parse(p[2]));
    final picked = await showDatePicker(
      context: context,
      initialDate: current,
      firstDate: DateTime(2024),
      lastDate: DateTime.now().add(const Duration(days: 1)),
      builder: (ctx, child) => Theme(
        data: Theme.of(ctx).copyWith(
          colorScheme: const ColorScheme.dark(primary: _accent, surface: Color(0xFF1E1E1E), onSurface: Colors.white),
        ),
        child: child!,
      ),
    );
    if (picked != null) {
      setState(() => _date = '${picked.year}-${picked.month.toString().padLeft(2, '0')}-${picked.day.toString().padLeft(2, '0')}');
    }
  }

  // --- dial interaction ---

  void _onDialSelect(int value) {
    setState(() {
      switch (_mode) {
        case DialMode.hour:   _h = value;
        case DialMode.minute: _m = value;
        case DialMode.second: _s = value;
      }
    });
  }

  void _onDialEnd() {
    // Auto-advance: hour->minute->second
    setState(() {
      if (_mode == DialMode.hour) _mode = DialMode.minute;
      else if (_mode == DialMode.minute) _mode = DialMode.second;
    });
  }

  int get _dialValue => switch (_mode) { DialMode.hour => _h, DialMode.minute => _m, DialMode.second => _s };
  int get _dialMax   => _mode == DialMode.hour ? 24 : 60;

  // --- header digit display ---

  Widget _digitBox(String text, DialMode mode) {
    final active = _mode == mode;
    return GestureDetector(
      onTap: () => setState(() => _mode = mode),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(
          color: active ? _accent.withValues(alpha: 0.15) : Colors.transparent,
          borderRadius: BorderRadius.circular(8),
        ),
        child: Text(
          text,
          style: TextStyle(
            fontSize: 32,
            fontWeight: FontWeight.w400,
            color: active ? _accent : const Color(0xFF999999),
            fontFeatures: const [FontFeature.tabularFigures()],
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Dialog(
      backgroundColor: _bg,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      insetPadding: const EdgeInsets.symmetric(horizontal: 40, vertical: 24),
      child: IntrinsicWidth(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 20, 24, 16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              // Date row
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  InkWell(
                    onTap: () => _stepDate(-1),
                    borderRadius: BorderRadius.circular(4),
                    child: const Padding(padding: EdgeInsets.all(4), child: Icon(Icons.chevron_left, size: 18, color: _dimText)),
                  ),
                  GestureDetector(
                    onTap: _pickDate,
                    child: Container(
                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
                      decoration: BoxDecoration(
                        color: _dialBg,
                        borderRadius: BorderRadius.circular(6),
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const Icon(Icons.calendar_today_rounded, size: 12, color: _accent),
                          const SizedBox(width: 6),
                          Text(_fmtDate(_date), style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w500, color: Color(0xFFD4D4D4))),
                        ],
                      ),
                    ),
                  ),
                  InkWell(
                    onTap: () => _stepDate(1),
                    borderRadius: BorderRadius.circular(4),
                    child: const Padding(padding: EdgeInsets.all(4), child: Icon(Icons.chevron_right, size: 18, color: _dimText)),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              // HH : MM : SS display -- tap to switch dial mode
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  _digitBox(_h.toString().padLeft(2, '0'), DialMode.hour),
                  const Text(':', style: TextStyle(fontSize: 28, color: _dimText)),
                  _digitBox(_m.toString().padLeft(2, '0'), DialMode.minute),
                  const Text(':', style: TextStyle(fontSize: 28, color: _dimText)),
                  _digitBox(_s.toString().padLeft(2, '0'), DialMode.second),
                ],
              ),
              const SizedBox(height: 8),
              // Rotary dial
              SizedBox(
                width: 240,
                height: 240,
                child: RotaryDial(
                  value: _dialValue,
                  maxValue: _dialMax,
                  onChanged: _onDialSelect,
                  onEnd: _onDialEnd,
                ),
              ),
              const SizedBox(height: 14),
              // Buttons
              Row(
                children: [
                  Expanded(
                    child: SizedBox(
                      height: 38,
                      child: OutlinedButton(
                        onPressed: () => Navigator.pop(context),
                        style: OutlinedButton.styleFrom(
                          foregroundColor: const Color(0xFF737373),
                          side: const BorderSide(color: Color(0xFF333333)),
                          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                        ),
                        child: const Text('Cancel'),
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: SizedBox(
                      height: 38,
                      child: ElevatedButton(
                        onPressed: () => Navigator.pop(context, {'date': _date, 'hour': _h, 'minute': _m, 'second': _s}),
                        style: ElevatedButton.styleFrom(
                          backgroundColor: _accent,
                          foregroundColor: Colors.white,
                          elevation: 0,
                          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                        ),
                        child: const Text('Go'),
                      ),
                    ),
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

// ---------------------------------------------------------------------------
// Rotary dial widget -- clock face with draggable hand
// ---------------------------------------------------------------------------

class RotaryDial extends StatelessWidget {
  final int value;
  final int maxValue; // 24 for hours, 60 for min/sec
  final ValueChanged<int> onChanged;
  final VoidCallback onEnd;

  const RotaryDial({super.key, required this.value, required this.maxValue, required this.onChanged, required this.onEnd});

  void _handleInteraction(Offset localPos, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final dx = localPos.dx - center.dx;
    final dy = localPos.dy - center.dy;
    // atan2 gives angle from positive X axis; rotate so 12 o'clock = 0
    var angle = math.atan2(dx, -dy); // 0 at top, clockwise positive
    if (angle < 0) angle += 2 * math.pi;

    final divisions = maxValue == 24 ? 12 : 60;
    final raw = (angle / (2 * math.pi) * divisions).round() % divisions;

    if (maxValue == 24) {
      // Inner ring (13-23, 0) vs outer ring (1-12) based on distance from center
      final dist = math.sqrt(dx * dx + dy * dy);
      final radius = size.width / 2;
      final isInner = dist < radius * 0.62;
      int val;
      if (isInner) {
        val = raw == 0 ? 0 : raw + 12; // inner: 0, 13-23
      } else {
        val = raw == 0 ? 12 : raw; // outer: 1-12
      }
      onChanged(val);
    } else {
      onChanged(raw);
    }
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onPanUpdate: (d) => _handleInteraction(d.localPosition, Size(240, 240)),
      onPanEnd: (_) => onEnd(),
      onTapUp: (d) {
        _handleInteraction(d.localPosition, const Size(240, 240));
        onEnd();
      },
      child: CustomPaint(
        size: const Size(240, 240),
        painter: DialPainter(value: value, maxValue: maxValue),
      ),
    );
  }
}

class DialPainter extends CustomPainter {
  final int value;
  final int maxValue;

  DialPainter({required this.value, required this.maxValue});

  static const _accent = Color(0xFF3B82F6);

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final radius = size.width / 2;

    // Background circle
    canvas.drawCircle(center, radius, Paint()..color = const Color(0xFF222222));

    final isHour = maxValue == 24;
    final divisions = isHour ? 12 : 60;

    if (isHour) {
      _paintHourDial(canvas, center, radius, divisions);
    } else {
      _paintMinSecDial(canvas, center, radius);
    }
  }

  void _paintHourDial(Canvas canvas, Offset center, double radius, int divisions) {
    final outerR = radius - 20;
    final innerR = radius * 0.42;

    // Determine which ring the current value is in
    final bool isInnerValue = value == 0 || value > 12;
    final int dialPos = isInnerValue ? (value == 0 ? 0 : value - 12) : value;

    // Draw hand
    final handAngle = dialPos / 12 * 2 * math.pi - math.pi / 2;
    final handR = isInnerValue ? innerR : outerR;
    final handEnd = Offset(
      center.dx + math.cos(handAngle) * handR,
      center.dy + math.sin(handAngle) * handR,
    );

    // Hand line
    canvas.drawLine(center, handEnd, Paint()..color = _accent..strokeWidth = 1.5);
    // Center dot
    canvas.drawCircle(center, 4, Paint()..color = _accent);
    // End dot
    canvas.drawCircle(handEnd, 18, Paint()..color = _accent);

    // Outer ring: 1-12
    for (int i = 0; i < 12; i++) {
      final angle = (i / 12) * 2 * math.pi - math.pi / 2;
      final pos = Offset(center.dx + math.cos(angle) * outerR, center.dy + math.sin(angle) * outerR);
      final label = i == 0 ? '12' : '$i';
      final isSelected = !isInnerValue && (i == 0 ? value == 12 : value == i);
      _drawLabel(canvas, pos, label, isSelected, 13);
    }

    // Inner ring: 0, 13-23
    for (int i = 0; i < 12; i++) {
      final angle = (i / 12) * 2 * math.pi - math.pi / 2;
      final pos = Offset(center.dx + math.cos(angle) * innerR, center.dy + math.sin(angle) * innerR);
      final label = i == 0 ? '00' : '${i + 12}';
      final isSelected = isInnerValue && (i == 0 ? value == 0 : value == i + 12);
      _drawLabel(canvas, pos, label, isSelected, 11, dim: true);
    }
  }

  void _paintMinSecDial(Canvas canvas, Offset center, double radius) {
    final numberR = radius - 20;

    // Draw hand
    final handAngle = value / 60 * 2 * math.pi - math.pi / 2;
    final handEnd = Offset(
      center.dx + math.cos(handAngle) * numberR,
      center.dy + math.sin(handAngle) * numberR,
    );

    canvas.drawLine(center, handEnd, Paint()..color = _accent..strokeWidth = 1.5);
    canvas.drawCircle(center, 4, Paint()..color = _accent);
    canvas.drawCircle(handEnd, 18, Paint()..color = _accent);

    // Tick marks for all 60 positions
    for (int i = 0; i < 60; i++) {
      final angle = (i / 60) * 2 * math.pi - math.pi / 2;
      if (i % 5 != 0) {
        // Small tick
        final outer = Offset(center.dx + math.cos(angle) * (radius - 6), center.dy + math.sin(angle) * (radius - 6));
        final inner = Offset(center.dx + math.cos(angle) * (radius - 10), center.dy + math.sin(angle) * (radius - 10));
        canvas.drawLine(outer, inner, Paint()..color = const Color(0xFF333333)..strokeWidth = 1);
      }
    }

    // Labels at 5-minute marks
    for (int i = 0; i < 12; i++) {
      final angle = (i / 12) * 2 * math.pi - math.pi / 2;
      final pos = Offset(center.dx + math.cos(angle) * numberR, center.dy + math.sin(angle) * numberR);
      final val = i * 5;
      final isSelected = value == val;
      _drawLabel(canvas, pos, val.toString().padLeft(2, '0'), isSelected, 13);
    }
  }

  void _drawLabel(Canvas canvas, Offset pos, String text, bool selected, double fontSize, {bool dim = false}) {
    final tp = TextPainter(
      text: TextSpan(
        text: text,
        style: TextStyle(
          fontSize: fontSize,
          fontWeight: FontWeight.w400,
          color: selected ? Colors.white : (dim ? const Color(0xFF777777) : const Color(0xFFBBBBBB)),
          fontFeatures: const [FontFeature.tabularFigures()],
        ),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    tp.paint(canvas, Offset(pos.dx - tp.width / 2, pos.dy - tp.height / 2));
  }

  @override
  bool shouldRepaint(DialPainter old) => old.value != value || old.maxValue != maxValue;
}
