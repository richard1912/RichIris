/// Format a DateTime as local ISO string without timezone (matches server DB format).
String formatLocalISO(DateTime dt) {
  return '${dt.year}-${_pad(dt.month)}-${_pad(dt.day)}T'
      '${_pad(dt.hour)}:${_pad(dt.minute)}:${_pad(dt.second)}';
}

/// Format milliseconds since epoch as local ISO string.
String formatLocalISOFromMs(int ms) {
  return formatLocalISO(DateTime.fromMillisecondsSinceEpoch(ms));
}

/// Convert an ISO datetime string to fractional hours (0-24).
double isoToHour(String iso) {
  final dt = DateTime.parse(iso);
  return dt.hour + dt.minute / 60.0 + dt.second / 3600.0;
}

/// Convert fractional hours to an ISO datetime string for a given date.
String hourToISO(String date, double hour) {
  final h = hour.floor();
  final minuteFrac = (hour - h) * 60;
  final m = minuteFrac.floor();
  final s = ((minuteFrac - m) * 60).floor();
  return '${date}T${_pad(h)}:${_pad(m)}:${_pad(s)}';
}

/// Convert fractional hours to HH:MM:SS display string.
String hourToTimeString(double hour) {
  final h = hour.floor().clamp(0, 23);
  final minuteFrac = (hour - h) * 60;
  final m = minuteFrac.floor().clamp(0, 59);
  final s = ((minuteFrac - m) * 60).floor().clamp(0, 59);
  return '${_pad(h)}:${_pad(m)}:${_pad(s)}';
}

/// Get today's date as YYYY-MM-DD string.
String todayDate({int tzOffsetMs = 0}) {
  final now = DateTime.now().add(Duration(milliseconds: tzOffsetMs));
  return '${now.year}-${_pad(now.month)}-${_pad(now.day)}';
}

/// Get the current fractional hour (0-24).
double nowHour({int tzOffsetMs = 0}) {
  final now = DateTime.now().add(Duration(milliseconds: tzOffsetMs));
  return now.hour + now.minute / 60.0 + now.second / 3600.0;
}

String _pad(int n) => n.toString().padLeft(2, '0');
