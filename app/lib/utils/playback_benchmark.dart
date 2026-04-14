import 'package:flutter/foundation.dart';

/// Per-gesture playback benchmark trace. Tracks phases from user tap through
/// to first decoded frame. Correlates with backend logs via [id], which is
/// sent as the `X-Bench-Id` header on the playback POST.
class PlaybackBenchmark {
  static PlaybackBenchmark? current;

  final String id;
  final int startMs;
  final List<_Phase> phases = [];

  PlaybackBenchmark._(this.id, this.startMs);

  static PlaybackBenchmark start({String? quality, List<int>? cameraIds}) {
    final now = DateTime.now().millisecondsSinceEpoch;
    final id =
        '${now.toRadixString(36)}-${(now & 0xffff).toRadixString(36).padLeft(4, '0')}';
    final b = PlaybackBenchmark._(id, now);
    current = b;
    final cams = cameraIds == null ? '' : ' cameras=${cameraIds.join(',')}';
    debugPrint(
        '[BENCH:$id] tap quality=${quality ?? "?"}$cams t=${DateTime.now().toIso8601String()}');
    return b;
  }

  void mark(String phase, {Map<String, Object?>? extra}) {
    final now = DateTime.now().millisecondsSinceEpoch;
    final total = now - startMs;
    final delta =
        phases.isEmpty ? total : total - phases.last.totalMs;
    phases.add(_Phase(phase, total));
    final extraStr = extra == null || extra.isEmpty
        ? ''
        : ' ${extra.entries.map((e) => '${e.key}=${e.value}').join(' ')}';
    debugPrint('[BENCH:$id] $phase +${delta}ms total=${total}ms$extraStr');
  }

  /// Marks the final phase and dumps an aligned summary table.
  void summary({String finalPhase = 'first_frame'}) {
    mark(finalPhase);
    final width = phases.fold<int>(0, (m, p) => p.name.length > m ? p.name.length : m);
    debugPrint('[BENCH:$id] === SUMMARY ===');
    int prev = 0;
    for (final p in phases) {
      final delta = p.totalMs - prev;
      debugPrint(
          '[BENCH:$id]   ${p.name.padRight(width)}  +${delta.toString().padLeft(5)}ms  total=${p.totalMs}ms');
      prev = p.totalMs;
    }
    if (identical(current, this)) current = null;
  }
}

class _Phase {
  final String name;
  final int totalMs;
  const _Phase(this.name, this.totalMs);
}
