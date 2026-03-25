import 'dart:async';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import '../../services/recording_api.dart';
import '../../services/clip_api.dart';
import '../../utils/time_utils.dart';
import '../../config/constants.dart';
import '../../models/thumbnail_info.dart';
import 'timeline_controller.dart';
import 'timeline_painter.dart';
import 'timeline_minimap.dart';

class TimelineWidget extends StatefulWidget {
  final int cameraId;
  final RecordingApi recordingApi;
  final ClipApi clipApi;
  final int tzOffsetMs;
  final bool isLive;
  final bool compact;
  final ValueChanged<String> onPlayback;
  final VoidCallback onLive;
  final int? speed;
  final ValueChanged<int>? onSpeedChanged;
  /// Called periodically to get the current NVR time in ms.
  /// Returns null if unknown.
  final int Function()? getNvrTime;

  const TimelineWidget({
    super.key,
    required this.cameraId,
    required this.recordingApi,
    required this.clipApi,
    required this.tzOffsetMs,
    required this.isLive,
    this.compact = false,
    required this.onPlayback,
    required this.onLive,
    this.speed,
    this.onSpeedChanged,
    this.getNvrTime,
  });

  @override
  State<TimelineWidget> createState() => _TimelineWidgetState();
}

class _TimelineWidgetState extends State<TimelineWidget> {
  late TimelineController _ctrl;
  Timer? _playheadTimer;
  Timer? _segmentPollTimer;
  bool _manualPan = false;
  String? _hoverTime;
  List<ThumbnailInfo> _thumbnails = [];
  double _lastScale = 1.0;
  bool _wasPinch = false;
  final GlobalKey _barKey = GlobalKey();
  OverlayEntry? _thumbOverlay;
  double _thumbLeft = 0;
  double _thumbTop = 0;
  String _thumbSrc = '';

  @override
  void initState() {
    super.initState();
    _ctrl = TimelineController(selectedDate: todayDate(tzOffsetMs: widget.tzOffsetMs));
    _ctrl.addListener(_onCtrlChange);
    _fetchSegments();
    _startPlayheadTimer();
    _startSegmentPolling();
  }

  @override
  void didUpdateWidget(TimelineWidget oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.cameraId != widget.cameraId) {
      _ctrl.setDate(todayDate(tzOffsetMs: widget.tzOffsetMs));
      _fetchSegments();
    }
    if (widget.isLive && !oldWidget.isLive) {
      _manualPan = false;
    }
  }

  @override
  void dispose() {
    _thumbOverlay?.remove();
    _thumbOverlay = null;
    _playheadTimer?.cancel();
    _segmentPollTimer?.cancel();
    _ctrl.removeListener(_onCtrlChange);
    _ctrl.dispose();
    super.dispose();
  }

  void _onCtrlChange() {
    if (mounted) setState(() {});
  }

  Future<void> _fetchSegments() async {
    try {
      final segs = await widget.recordingApi.fetchSegments(
        widget.cameraId,
        _ctrl.selectedDate,
      );
      _ctrl.setSegments(segs);
    } catch (_) {}
    _fetchThumbnails();
  }

  Future<void> _fetchThumbnails() async {
    try {
      final thumbs = await widget.recordingApi.fetchThumbnails(
        widget.cameraId, _ctrl.selectedDate);
      if (mounted) setState(() => _thumbnails = thumbs);
    } catch (_) {}
  }

  String? _findNearestThumbUrl(double hour) {
    if (_thumbnails.isEmpty) return null;
    final targetSecs = hour * 3600;
    ThumbnailInfo? best;
    double bestDist = double.infinity;
    for (final t in _thumbnails) {
      final parts = t.timestamp.split(':');
      final tHour = int.parse(parts[0]) +
          int.parse(parts[1]) / 60.0 +
          (parts.length > 2 ? int.parse(parts[2]) / 3600.0 : 0);
      final dist = (tHour * 3600 - targetSecs).abs();
      if (dist < bestDist) {
        bestDist = dist;
        best = t;
      }
    }
    if (best == null || bestDist > best.interval * 2) return null;
    return widget.recordingApi.getThumbnailUrl(best.url);
  }

  void _updateThumbOverlay() {
    final scrubHour = _ctrl.scrubHour;
    if (scrubHour == null) {
      _thumbOverlay?.remove();
      _thumbOverlay = null;
      return;
    }
    final thumbUrl = _findNearestThumbUrl(scrubHour);
    if (thumbUrl == null) {
      _thumbOverlay?.remove();
      _thumbOverlay = null;
      return;
    }
    final box = _barKey.currentContext?.findRenderObject() as RenderBox?;
    if (box == null || !box.attached) return;
    final barPos = box.localToGlobal(Offset.zero);
    final barWidth = box.size.width;
    final pct = _ctrl.hourToViewportPct(scrubHour);
    final scrubX = pct * barWidth;
    const tw = 160.0;
    const th = 90.0;
    _thumbLeft = barPos.dx + (scrubX - tw / 2).clamp(0.0, barWidth - tw);
    _thumbTop = barPos.dy - th - 4;
    _thumbSrc = thumbUrl;
    if (_thumbOverlay == null) {
      _thumbOverlay = OverlayEntry(builder: _buildThumbEntry);
      Overlay.of(context).insert(_thumbOverlay!);
    } else {
      _thumbOverlay!.markNeedsBuild();
    }
  }

  Widget _buildThumbEntry(BuildContext _) {
    return Positioned(
      left: _thumbLeft,
      top: _thumbTop,
      child: IgnorePointer(
        child: Container(
          width: 160,
          height: 90,
          decoration: BoxDecoration(
            color: Colors.black,
            border: Border.all(color: const Color(0xFF404040)),
            borderRadius: BorderRadius.circular(4),
          ),
          clipBehavior: Clip.antiAlias,
          child: Image.network(
            _thumbSrc,
            fit: BoxFit.cover,
            gaplessPlayback: true,
            errorBuilder: (_, __, ___) => const SizedBox.shrink(),
          ),
        ),
      ),
    );
  }

  void _startPlayheadTimer() {
    _playheadTimer = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (!mounted) return;
      if (_manualPan && widget.isLive) return;
      double? hour;
      if (widget.getNvrTime != null) {
        final ms = widget.getNvrTime!();
        final dt = DateTime.fromMillisecondsSinceEpoch(ms);
        hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0;
      } else if (widget.isLive) {
        hour = nowHour(tzOffsetMs: widget.tzOffsetMs);
      }
      if (hour != null) {
        _ctrl.setPlayhead(hour);
        if (!_manualPan) {
          _autoPanToPlayhead();
        }
      }
    });
  }

  void _startSegmentPolling() {
    _segmentPollTimer = Timer.periodic(
      const Duration(milliseconds: kSegmentPollMs),
      (_) {
        if (!mounted) return;
        final today = todayDate(tzOffsetMs: widget.tzOffsetMs);
        if (_ctrl.selectedDate == today) {
          _fetchSegments();
        }
      },
    );
  }

  void _autoPanToPlayhead() {
    final ph = _ctrl.playheadHour;
    if (ph == null) return;
    final pct = _ctrl.hourToViewportPct(ph);
    if (pct < 0.1 || pct > 0.9) {
      _ctrl.panToPlayhead();
    }
  }

  void _changeDate(int delta) {
    final parts = _ctrl.selectedDate.split('-');
    final dt = DateTime(int.parse(parts[0]), int.parse(parts[1]), int.parse(parts[2]));
    final newDt = dt.add(Duration(days: delta));
    final newDate = '${newDt.year}-${newDt.month.toString().padLeft(2, '0')}-${newDt.day.toString().padLeft(2, '0')}';
    _ctrl.setDate(newDate);
    _fetchSegments();
  }

  void _onTimelineTap(double pct) {
    final hour = _ctrl.viewportPctToHour(pct);
    if (_ctrl.exportMode) {
      _ctrl.setExportPoint(hour);
    } else {
      _manualPan = true;
      _ctrl.setPlayhead(hour);
      final iso = hourToISO(_ctrl.selectedDate, hour);
      widget.onPlayback(iso);
    }
  }

  Future<void> _exportClip() async {
    final s = _ctrl.exportStartHour;
    final e = _ctrl.exportEndHour;
    if (s == null || e == null) return;
    final start = hourToISO(_ctrl.selectedDate, s);
    final end = hourToISO(_ctrl.selectedDate, e);
    try {
      await widget.clipApi.create(widget.cameraId, start, end);
      _ctrl.toggleExportMode();
    } catch (err) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Export failed: $err')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      color: const Color(0xFF0F0F0F),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          // Top row: date picker, LIVE button, export controls
          _buildControls(),
          const SizedBox(height: 4),
          // Timeline bar
          _buildTimelineBar(),
          // Minimap
          TimelineMinimap(
            controller: _ctrl,
            onPan: (h) {
              _manualPan = true;
              _ctrl.setViewportStart(h);
            },
          ),
          // Speed controls (only in fullscreen)
          if (!widget.compact && widget.onSpeedChanged != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: kSpeeds.map((speed) {
                  final isActive = widget.speed == speed;
                  return Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 2),
                    child: SizedBox(
                      height: 26,
                      child: TextButton(
                        onPressed: () => widget.onSpeedChanged!(speed),
                        style: TextButton.styleFrom(
                          padding: const EdgeInsets.symmetric(horizontal: 5),
                          minimumSize: Size.zero,
                          backgroundColor: isActive ? const Color(0xFF3B82F6) : Colors.transparent,
                          foregroundColor: isActive ? Colors.white : const Color(0xFF737373),
                          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(4)),
                        ),
                        child: Text('${speed}x', style: const TextStyle(fontSize: 10)),
                      ),
                    ),
                  );
                }).toList(),
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildControls() {
    final dateLabel = _ctrl.selectedDate;
    return Row(
      children: [
        // Date picker
        IconButton(
          icon: const Icon(Icons.chevron_left, size: 18),
          onPressed: () => _changeDate(-1),
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
        ),
        GestureDetector(
          onTap: () async {
            final parts = _ctrl.selectedDate.split('-');
            final current = DateTime(int.parse(parts[0]), int.parse(parts[1]), int.parse(parts[2]));
            final picked = await showDatePicker(
              context: context,
              initialDate: current,
              firstDate: DateTime(2024),
              lastDate: DateTime.now().add(const Duration(days: 1)),
            );
            if (picked != null) {
              final newDate = '${picked.year}-${picked.month.toString().padLeft(2, '0')}-${picked.day.toString().padLeft(2, '0')}';
              _ctrl.setDate(newDate);
              _fetchSegments();
            }
          },
          child: Text(dateLabel, style: const TextStyle(fontSize: 12, color: Color(0xFFA3A3A3))),
        ),
        IconButton(
          icon: const Icon(Icons.chevron_right, size: 18),
          onPressed: () => _changeDate(1),
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
        ),
        const Spacer(),
        // Hover time
        if (_hoverTime != null)
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: Text(_hoverTime!, style: const TextStyle(fontSize: 11, color: Color(0xFF737373))),
          ),
        // Export button
        if (_ctrl.exportMode && _ctrl.exportStartHour != null && _ctrl.exportEndHour != null)
          Padding(
            padding: const EdgeInsets.only(right: 4),
            child: SizedBox(
              height: 26,
              child: ElevatedButton(
                onPressed: _exportClip,
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF22C55E),
                  padding: const EdgeInsets.symmetric(horizontal: 10),
                  minimumSize: Size.zero,
                ),
                child: const Text('Export', style: TextStyle(fontSize: 11)),
              ),
            ),
          ),
        SizedBox(
          height: 26,
          child: TextButton(
            onPressed: () => _ctrl.toggleExportMode(),
            style: TextButton.styleFrom(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              minimumSize: Size.zero,
              foregroundColor: _ctrl.exportMode ? const Color(0xFF22C55E) : const Color(0xFF737373),
            ),
            child: Text(
              _ctrl.exportMode ? 'Cancel Export' : 'Export Clip',
              style: const TextStyle(fontSize: 11),
            ),
          ),
        ),
        const SizedBox(width: 4),
        // LIVE button
        SizedBox(
          height: 26,
          child: ElevatedButton(
            onPressed: () {
              _manualPan = false;
              widget.onLive();
            },
            style: ElevatedButton.styleFrom(
              backgroundColor: widget.isLive ? const Color(0xFFEF4444) : const Color(0xFF404040),
              padding: const EdgeInsets.symmetric(horizontal: 10),
              minimumSize: Size.zero,
            ),
            child: const Text('LIVE', style: TextStyle(fontSize: 11, fontWeight: FontWeight.w600)),
          ),
        ),
      ],
    );
  }

  Widget _buildTimelineBar() {
    final barHeight = widget.compact ? 40.0 : 56.0;

    return LayoutBuilder(
      builder: (context, constraints) {
        final width = constraints.maxWidth;

        return SizedBox(
          key: _barKey,
          height: barHeight,
          child: Listener(
              onPointerSignal: (event) {
                if (event is PointerScrollEvent) {
                  final pct = event.localPosition.dx / width;
                  final delta = event.scrollDelta.dy > 0 ? -1.0 : 1.0;
                  _manualPan = true;
                  _ctrl.zoom(delta, pct);
                }
              },
              child: GestureDetector(
                onTapUp: (details) {
                  final pct = details.localPosition.dx / width;
                  _onTimelineTap(pct);
                },
                onScaleStart: (_) {
                  _lastScale = 1.0;
                  _wasPinch = false;
                },
                onScaleUpdate: (details) {
                  if (details.pointerCount >= 2) {
                    _wasPinch = true;
                    _manualPan = true;
                    final scaleDelta = details.scale - _lastScale;
                    _lastScale = details.scale;
                    final anchorPct = (details.localFocalPoint.dx / width).clamp(0.0, 1.0);
                    _ctrl.zoom(scaleDelta * 4, anchorPct);
                    _ctrl.setScrubHour(null);
                    setState(() => _hoverTime = null);
                    _updateThumbOverlay();
                  } else if (!_wasPinch) {
                    final pct = (details.localFocalPoint.dx / width).clamp(0.0, 1.0);
                    final hour = _ctrl.viewportPctToHour(pct);
                    _ctrl.setScrubHour(hour);
                    setState(() => _hoverTime = hourToTimeString(hour));
                    _updateThumbOverlay();
                  }
                },
                onScaleEnd: (_) {
                  if (!_wasPinch) {
                    final scrub = _ctrl.scrubHour;
                    if (scrub != null) {
                      final pct = _ctrl.hourToViewportPct(scrub);
                      _onTimelineTap(pct);
                    }
                  }
                  _ctrl.setScrubHour(null);
                  setState(() => _hoverTime = null);
                  _updateThumbOverlay();
                },
                child: MouseRegion(
                  onHover: (event) {
                    final pct = event.localPosition.dx / width;
                    final hour = _ctrl.viewportPctToHour(pct);
                    _ctrl.setScrubHour(hour);
                    setState(() => _hoverTime = hourToTimeString(hour));
                    _updateThumbOverlay();
                  },
                  onExit: (_) {
                    _ctrl.setScrubHour(null);
                    setState(() => _hoverTime = null);
                    _updateThumbOverlay();
                  },
                  child: CustomPaint(
                    painter: TimelinePainter(_ctrl),
                    size: Size.infinite,
                  ),
                ),
              ),
          ),
        );
      },
    );
  }
}
