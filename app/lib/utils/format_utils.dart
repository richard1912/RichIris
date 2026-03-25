String formatBytes(int bytes) {
  if (bytes < 1024) return '$bytes B';
  if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
  if (bytes < 1024 * 1024 * 1024) {
    return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
  }
  if (bytes < 1024 * 1024 * 1024 * 1024) {
    return '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(1)} GB';
  }
  return '${(bytes / (1024 * 1024 * 1024 * 1024)).toStringAsFixed(2)} TB';
}

String formatUptime(double seconds) {
  final h = (seconds / 3600).floor();
  final m = ((seconds % 3600) / 60).floor();
  if (h > 0) return '${h}h ${m}m';
  return '${m}m';
}

String formatDuration(double seconds) {
  final h = (seconds / 3600).floor();
  final m = ((seconds % 3600) / 60).floor();
  final s = (seconds % 60).floor();
  if (h > 0) return '${h}h ${m}m ${s}s';
  if (m > 0) return '${m}m ${s}s';
  return '${s}s';
}
