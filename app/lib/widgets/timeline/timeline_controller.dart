import 'package:flutter/foundation.dart';
import '../../config/constants.dart';
import '../../models/recording_segment.dart';
import '../../utils/time_utils.dart';

class MergedSegment {
  final double startHour;
  final double endHour;
  final List<int> ids;
  MergedSegment(this.startHour, this.endHour, this.ids);
}

class TimelineController extends ChangeNotifier {
  String selectedDate;
  double zoomLevel;
  double viewportStart;
  double? playheadHour;
  bool draggingPlayhead = false;
  bool exportMode = false;
  double? exportStartHour;
  double? exportEndHour;
  List<RecordingSegment> _segments = [];
  List<MergedSegment> merged = [];

  TimelineController({required this.selectedDate})
      : zoomLevel = 1.0,
        viewportStart = 0.0;

  double get visibleHours => 24.0 / zoomLevel;

  void setSegments(List<RecordingSegment> segments) {
    _segments = segments;
    _mergeSegments();
    notifyListeners();
  }

  void _mergeSegments() {
    if (_segments.isEmpty) {
      merged = [];
      return;
    }

    final sorted = [..._segments]
      ..sort((a, b) => a.startTime.compareTo(b.startTime));

    final result = <MergedSegment>[];
    double curStart = isoToHour(sorted.first.startTime);
    double curEnd = sorted.first.endTime != null
        ? isoToHour(sorted.first.endTime!)
        : curStart + (sorted.first.duration ?? 900) / 3600.0;
    List<int> curIds = [sorted.first.id];

    for (int i = 1; i < sorted.length; i++) {
      final s = sorted[i];
      final sStart = isoToHour(s.startTime);
      final sEnd = s.endTime != null
          ? isoToHour(s.endTime!)
          : sStart + (s.duration ?? 900) / 3600.0;

      if (sStart - curEnd <= kSegmentMergeGapSeconds / 3600.0) {
        curEnd = sEnd > curEnd ? sEnd : curEnd;
        curIds.add(s.id);
      } else {
        result.add(MergedSegment(curStart, curEnd, curIds));
        curStart = sStart;
        curEnd = sEnd;
        curIds = [s.id];
      }
    }
    result.add(MergedSegment(curStart, curEnd, curIds));
    merged = result;
  }

  void setPlayhead(double? hour) {
    playheadHour = hour;
    notifyListeners();
  }

  double? scrubHour;

  void setScrubHour(double? hour) {
    scrubHour = hour;
    notifyListeners();
  }

  void setDate(String date) {
    selectedDate = date;
    _segments = [];
    merged = [];
    exportMode = false;
    exportStartHour = null;
    exportEndHour = null;
    notifyListeners();
  }

  void zoom(double delta, double anchorPct) {
    final anchorHour = viewportStart + anchorPct * visibleHours;
    zoomLevel = (zoomLevel + delta).clamp(kMinZoom, kMaxZoom);
    viewportStart = (anchorHour - anchorPct * visibleHours).clamp(0.0, 24.0 - visibleHours);
    notifyListeners();
  }

  void pan(double deltaHours) {
    viewportStart = (viewportStart + deltaHours).clamp(0.0, 24.0 - visibleHours);
    notifyListeners();
  }

  void panToPlayhead() {
    if (playheadHour == null) return;
    final center = playheadHour! - visibleHours / 2;
    viewportStart = center.clamp(0.0, 24.0 - visibleHours);
    notifyListeners();
  }

  double hourToViewportPct(double hour) {
    return (hour - viewportStart) / visibleHours;
  }

  double viewportPctToHour(double pct) {
    return viewportStart + pct * visibleHours;
  }

  void toggleExportMode() {
    exportMode = !exportMode;
    if (!exportMode) {
      exportStartHour = null;
      exportEndHour = null;
    }
    notifyListeners();
  }

  void setViewportStart(double start) {
    viewportStart = start.clamp(0.0, 24.0 - visibleHours);
    notifyListeners();
  }

  void setExportPoint(double hour) {
    if (exportStartHour == null || exportEndHour != null) {
      exportStartHour = hour;
      exportEndHour = null;
    } else {
      if (hour < exportStartHour!) {
        exportEndHour = exportStartHour;
        exportStartHour = hour;
      } else {
        exportEndHour = hour;
      }
    }
    notifyListeners();
  }
}
