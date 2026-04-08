import 'dart:async';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';
import '../../utils/detection_colors.dart';
import '../../services/recording_api.dart';
import '../../services/clip_api.dart';
import '../../services/motion_api.dart';
import '../../utils/time_utils.dart';
import '../../config/constants.dart';
import '../../models/thumbnail_info.dart';
import '../../models/clip_export.dart';
import '../../models/motion_event.dart';
import 'timeline_controller.dart';
import 'timeline_painter.dart';
import 'timeline_minimap.dart';

class TimelineWidget extends StatefulWidget {
  final int cameraId;
  final RecordingApi recordingApi;
  final ClipApi clipApi;
  final MotionApi motionApi;
  final int tzOffsetMs;
  final bool isLive;
  final bool isPaused;
  final bool compact;
  final ValueChanged<String> onPlayback;
  final VoidCallback onLive;
  final int? speed;
  final ValueChanged<int>? onSpeedChanged;
  /// Called periodically to get the current NVR time in ms.
  /// Returns null if unknown.
  final int Function()? getNvrTime;
  /// If set, the timeline starts on this date instead of today.
  final String? initialDate;

  const TimelineWidget({
    super.key,
    required this.cameraId,
    required this.recordingApi,
    required this.clipApi,
    required this.motionApi,
    required this.tzOffsetMs,
    required this.isLive,
    this.isPaused = false,
    this.compact = false,
    required this.onPlayback,
    required this.onLive,
    this.speed,
    this.onSpeedChanged,
    this.getNvrTime,
    this.initialDate,
  });

  @override
  State<TimelineWidget> createState() => _TimelineWidgetState();
}

class _TimelineWidgetState extends State<TimelineWidget> {
  late TimelineController _ctrl;
  Timer? _playheadTimer;
  Timer? _segmentPollTimer;
  Timer? _clipPollTimer;
  bool _manualPan = false;
  /// After a timeline tap, hold the playhead position for a short time so that
  /// the player has time to load before getNvrTime starts driving it.
  int _playheadHoldUntilMs = 0;
  String? _hoverTime;
  List<ThumbnailInfo> _thumbnails = [];
  double _lastScale = 1.0;
  bool _wasPinch = false;
  double? _scaleStartPct;
  double? _scaleStartYFrac;
  Timer? _longPressTimer;
  Offset? _pointerDownPos;
  bool _longPressActive = false;
  final GlobalKey _barKey = GlobalKey();
  OverlayEntry? _thumbOverlay;
  double _thumbLeft = 0;
  double _thumbTop = 0;
  String _thumbSrc = '';
  MotionEvent? _hoverMotionEvent;

  // Clip export state
  List<ClipExport> _clips = [];
  bool _showClips = false;
  bool _exporting = false;

  @override
  void initState() {
    super.initState();
    _ctrl = TimelineController(selectedDate: widget.initialDate ?? todayDate(tzOffsetMs: widget.tzOffsetMs));
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
      _clips = [];
      _showClips = false;
      _stopClipPolling();
    }
    if (widget.initialDate != null && widget.initialDate != oldWidget.initialDate) {
      _ctrl.setDate(widget.initialDate!);
      _fetchSegments();
    }
    if (widget.isLive && !oldWidget.isLive) {
      _manualPan = false;
      _playheadHoldUntilMs = 0;
    }
  }

  @override
  void dispose() {
    _thumbOverlay?.remove();
    _thumbOverlay = null;
    _longPressTimer?.cancel();
    _playheadTimer?.cancel();
    _segmentPollTimer?.cancel();
    _clipPollTimer?.cancel();
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
    _fetchMotionEvents();
  }

  Future<void> _fetchMotionEvents() async {
    try {
      final events = await widget.motionApi.fetchEvents(
        widget.cameraId,
        _ctrl.selectedDate,
      );
      _ctrl.setMotionEvents(events);
    } catch (_) {}
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
      final tSecs = int.parse(parts[0]) * 3600.0 +
          int.parse(parts[1]) * 60.0 +
          (parts.length > 2 ? int.parse(parts[2]) : 0);
      // Only consider thumbnails at or before the hovered time
      if (tSecs > targetSecs) continue;
      final dist = targetSecs - tSecs;
      if (dist < bestDist) {
        bestDist = dist;
        best = t;
      }
    }
    if (best == null) return null;
    return widget.recordingApi.getThumbnailUrl(best.url);
  }

  void _updateThumbOverlay() {
    final scrubHour = _ctrl.scrubHour;
    if (scrubHour == null) {
      _thumbOverlay?.remove();
      _thumbOverlay = null;
      return;
    }

    // Use detection thumbnail if hovering over a detection event that has one
    String? thumbUrl;
    if (_hoverMotionEvent != null && _hoverMotionEvent!.hasThumbnail) {
      thumbUrl = widget.motionApi.getEventThumbnailUrl(
        _hoverMotionEvent!.cameraId,
        _hoverMotionEvent!.id,
      );
    } else {
      thumbUrl = _findNearestThumbUrl(scrubHour);
    }

    if (thumbUrl == null) return;
    final box = _barKey.currentContext?.findRenderObject() as RenderBox?;
    if (box == null || !box.attached) return;
    final barPos = box.localToGlobal(Offset.zero);
    final barWidth = box.size.width;
    final pct = _ctrl.hourToViewportPct(scrubHour);
    final scrubX = pct * barWidth;
    const tw = 320.0;
    const th = 180.0;
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
    final isMotion = _hoverMotionEvent != null;
    final motionColor = DetectionColors.forLabel(_hoverMotionEvent?.detectionLabel);
    final borderColor = isMotion ? motionColor : const Color(0xFF404040);

    return Positioned(
      left: _thumbLeft,
      top: _thumbTop,
      child: IgnorePointer(
        child: Container(
          width: 320,
          height: 180,
          decoration: BoxDecoration(
            color: Colors.black,
            border: Border.all(color: borderColor, width: isMotion ? 2 : 1),
            borderRadius: BorderRadius.circular(4),
          ),
          clipBehavior: Clip.antiAlias,
          child: Stack(
            children: [
              Image.network(
                _thumbSrc,
                fit: BoxFit.cover,
                width: 320,
                height: 180,
                gaplessPlayback: true,
                errorBuilder: (_, __, ___) => const SizedBox.shrink(),
              ),
              if (isMotion)
                Positioned(
                  bottom: 4,
                  left: 4,
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(
                      color: const Color(0xCC000000),
                      borderRadius: BorderRadius.circular(3),
                      border: Border.all(
                        color: motionColor,
                        width: 0.5,
                      ),
                    ),
                    child: Text(
                      () {
                        final e = _hoverMotionEvent!;
                        final minSens = (101 - e.peakIntensity / 0.05).clamp(0, 100).round();
                        final ai = e.detectionLabel != null
                            ? '${e.detectionLabel}${e.detectionConfidence != null ? ' ${(e.detectionConfidence! * 100).round()}%' : ''} | '
                            : '';
                        // Parse startTime to show HH:MM:SS
                        final dt = DateTime.tryParse(e.startTime);
                        final ts = dt != null
                            ? '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}:${dt.second.toString().padLeft(2, '0')}'
                            : '';
                        return '${ts.isNotEmpty ? '$ts | ' : ''}${ai}min sens $minSens';
                      }(),
                      style: TextStyle(
                        color: motionColor,
                        fontSize: 9,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }

  void _startPlayheadTimer() {
    _playheadTimer = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (!mounted) return;
      if (_manualPan && widget.isLive) return;
      // After a timeline tap, hold the playhead in place until the player loads
      if (DateTime.now().millisecondsSinceEpoch < _playheadHoldUntilMs) return;
      double? hour;
      if (widget.getNvrTime != null) {
        // Use actual player position — stops advancing when stream
        // freezes, buffers, or fails to load.
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

  String _formatPlayheadTime(double hour) {
    final h = hour.floor();
    final minutesFrac = (hour - h) * 60;
    final m = minutesFrac.floor();
    final s = ((minutesFrac - m) * 60).floor();
    return '${h.toString().padLeft(2, '0')}:${m.toString().padLeft(2, '0')}:${s.toString().padLeft(2, '0')}';
  }

  Future<List<int>?> _showHmsPickerDialog(BuildContext ctx, int initH, int initM, int initS) {
    final hCtrl = FixedExtentScrollController(initialItem: initH);
    final mCtrl = FixedExtentScrollController(initialItem: initM);
    final sCtrl = FixedExtentScrollController(initialItem: initS);
    int h = initH, m = initM, s = initS;

    Widget spinner(FixedExtentScrollController c, int count, ValueChanged<int> onChanged) {
      return SizedBox(
        width: 52,
        height: 150,
        child: ListWheelScrollView.useDelegate(
          controller: c,
          itemExtent: 36,
          physics: const FixedExtentScrollPhysics(),
          diameterRatio: 1.2,
          onSelectedItemChanged: onChanged,
          childDelegate: ListWheelChildBuilderDelegate(
            childCount: count,
            builder: (_, i) => Center(
              child: Text(i.toString().padLeft(2, '0'),
                  style: const TextStyle(fontSize: 20, color: Colors.white)),
            ),
          ),
        ),
      );
    }

    Widget label(String text) => Padding(
      padding: const EdgeInsets.symmetric(horizontal: 2),
      child: Text(text, style: const TextStyle(fontSize: 20, color: Colors.white54)),
    );

    return showDialog<List<int>>(
      context: ctx,
      builder: (_) => AlertDialog(
        backgroundColor: const Color(0xFF1E1E1E),
        title: const Text('Go to time', style: TextStyle(fontSize: 16, color: Colors.white)),
        content: Row(
          mainAxisSize: MainAxisSize.min,
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            spinner(hCtrl, 24, (v) => h = v),
            label(':'),
            spinner(mCtrl, 60, (v) => m = v),
            label(':'),
            spinner(sCtrl, 60, (v) => s = v),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          TextButton(onPressed: () => Navigator.pop(ctx, [h, m, s]), child: const Text('Go')),
        ],
      ),
    );
  }

  void _changeDate(int delta) {
    final parts = _ctrl.selectedDate.split('-');
    final dt = DateTime(int.parse(parts[0]), int.parse(parts[1]), int.parse(parts[2]));
    final newDt = dt.add(Duration(days: delta));
    final newDate = '${newDt.year}-${newDt.month.toString().padLeft(2, '0')}-${newDt.day.toString().padLeft(2, '0')}';
    _ctrl.setDate(newDate);
    _fetchSegments();
  }

  void _onTimelineTap(double pct, {double? yFraction}) {
    final hour = _ctrl.viewportPctToHour(pct);
    if (_ctrl.exportMode) {
      _ctrl.setExportPoint(hour);
      return;
    }

    double targetHour = hour;

    // Snap to motion event start if tapping in a detection row — category-aware
    final tappedCat = yFraction != null ? _ctrl.categoryAtYFraction(yFraction) : null;
    if (tappedCat != null) {
      final box = _barKey.currentContext?.findRenderObject() as RenderBox?;
      final barWidth = box?.size.width ?? 400;
      final padHours = 8.0 / barWidth * _ctrl.visibleHours;
      final hitEvent = _ctrl.motionEventAtHour(hour, padHours: padHours, category: tappedCat);
      if (hitEvent != null) {
        targetHour = isoToHour(hitEvent.startTime);
      }
    }

    // Ignore taps in the future
    final now = DateTime.now();
    final todayStr = '${now.year}-${now.month.toString().padLeft(2, '0')}-${now.day.toString().padLeft(2, '0')}';
    if (_ctrl.selectedDate == todayStr) {
      final nowHour = now.hour + now.minute / 60.0 + now.second / 3600.0;
      if (targetHour > nowHour) return;
    } else if (_ctrl.selectedDate.compareTo(todayStr) > 0) {
      return;
    }

    _manualPan = true;
    _ctrl.setPlayhead(targetHour);
    // Hold the playhead at the tapped position for 3s while the player loads
    _playheadHoldUntilMs = DateTime.now().millisecondsSinceEpoch + 3000;
    final iso = hourToISO(_ctrl.selectedDate, targetHour);
    widget.onPlayback(iso);
  }

  // --- Clip export ---

  void _enterExportMode() {
    _ctrl.toggleExportMode();
    if (_ctrl.exportMode) {
      _fetchClips();
      setState(() => _showClips = true);
    } else {
      _stopClipPolling();
      setState(() => _showClips = false);
    }
  }

  Future<void> _exportClip() async {
    final s = _ctrl.exportStartHour;
    final e = _ctrl.exportEndHour;
    if (s == null || e == null) return;
    final start = hourToISO(_ctrl.selectedDate, s);
    final end = hourToISO(_ctrl.selectedDate, e);

    setState(() => _exporting = true);
    try {
      await widget.clipApi.create(widget.cameraId, start, end);
      _ctrl.clearExportPoints();
      _fetchClips();
      _startClipPolling();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('Clip export started'),
            backgroundColor: Color(0xFF22C55E),
            duration: Duration(seconds: 2),
          ),
        );
      }
    } catch (err) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Export failed: $err')),
        );
      }
    } finally {
      if (mounted) setState(() => _exporting = false);
    }
  }

  Future<void> _fetchClips() async {
    try {
      final clips = await widget.clipApi.fetchAll(cameraId: widget.cameraId);
      if (mounted) setState(() => _clips = clips);
      // If any clip is still processing, keep polling
      if (clips.any((c) => c.status == 'pending' || c.status == 'processing')) {
        _startClipPolling();
      } else {
        _stopClipPolling();
      }
    } catch (_) {}
  }

  void _startClipPolling() {
    _clipPollTimer?.cancel();
    _clipPollTimer = Timer.periodic(const Duration(seconds: 2), (_) {
      if (!mounted) return;
      _fetchClips();
    });
  }

  void _stopClipPolling() {
    _clipPollTimer?.cancel();
    _clipPollTimer = null;
  }

  Future<void> _downloadClip(ClipExport clip) async {
    final url = widget.clipApi.downloadUrl(clip.id);
    final uri = Uri.parse(url);
    if (await canLaunchUrl(uri)) {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    }
  }

  Future<void> _deleteClip(ClipExport clip) async {
    try {
      await widget.clipApi.delete(clip.id);
      _fetchClips();
    } catch (err) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Delete failed: $err')),
        );
      }
    }
  }

  String _exportInstruction() {
    if (_ctrl.exportStartHour == null) return 'Tap timeline to set start';
    if (_ctrl.exportEndHour == null) return 'Tap timeline to set end';
    final dur = ((_ctrl.exportEndHour! - _ctrl.exportStartHour!) * 3600).round();
    final m = dur ~/ 60;
    final s = dur % 60;
    return '${hourToTimeString(_ctrl.exportStartHour!)} - ${hourToTimeString(_ctrl.exportEndHour!)}  (${m}m ${s}s)';
  }

  // --- Build ---

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
          // Export mode instructions
          if (_ctrl.exportMode) _buildExportBar(),
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
          // Clips list
          if (_showClips && _clips.isNotEmpty) _buildClipsList(),
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
        if (_ctrl.selectedDate != todayDate(tzOffsetMs: widget.tzOffsetMs))
          Padding(
            padding: const EdgeInsets.only(left: 4),
            child: SizedBox(
              height: 26,
              child: TextButton(
                onPressed: () {
                  _ctrl.setDate(todayDate(tzOffsetMs: widget.tzOffsetMs));
                  _fetchSegments();
                },
                style: TextButton.styleFrom(
                  padding: const EdgeInsets.symmetric(horizontal: 8),
                  minimumSize: Size.zero,
                  foregroundColor: const Color(0xFFA3A3A3),
                ),
                child: const Text('Today', style: TextStyle(fontSize: 11)),
              ),
            ),
          ),
        // Playhead timestamp (tap to pick time)
        if (_ctrl.playheadHour != null)
          Padding(
            padding: const EdgeInsets.only(left: 8),
            child: GestureDetector(
              onTap: () async {
                final h = _ctrl.playheadHour!.floor();
                final minutesFrac = (_ctrl.playheadHour! - h) * 60;
                final m = minutesFrac.floor();
                final s = ((minutesFrac - m) * 60).floor();
                final result = await _showHmsPickerDialog(context, h, m, s);
                if (result == null) return;
                final targetHour = result[0] + result[1] / 60.0 + result[2] / 3600.0;
                // Ignore future times on today
                final now = DateTime.now();
                final todayStr = '${now.year}-${now.month.toString().padLeft(2, '0')}-${now.day.toString().padLeft(2, '0')}';
                if (_ctrl.selectedDate == todayStr) {
                  final nowHour = now.hour + now.minute / 60.0 + now.second / 3600.0;
                  if (targetHour > nowHour) return;
                }
                _manualPan = true;
                _ctrl.setPlayhead(targetHour);
                _playheadHoldUntilMs = DateTime.now().millisecondsSinceEpoch + 3000;
                final iso = hourToISO(_ctrl.selectedDate, targetHour);
                widget.onPlayback(iso);
              },
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    _formatPlayheadTime(_ctrl.playheadHour!),
                    style: const TextStyle(fontSize: 12, color: Color(0xFFA3A3A3), decoration: TextDecoration.underline, decorationColor: Color(0x80A3A3A3), fontFeatures: [FontFeature.tabularFigures()]),
                  ),
                  const SizedBox(width: 2),
                  const Icon(Icons.access_time, size: 11, color: Color(0x80A3A3A3)),
                ],
              ),
            ),
          ),
        // LIVE button
        Padding(
          padding: const EdgeInsets.only(left: 6),
          child: SizedBox(
            height: 26,
            child: ElevatedButton(
              onPressed: () {
                _manualPan = false;
                final today = todayDate(tzOffsetMs: widget.tzOffsetMs);
                if (_ctrl.selectedDate != today) {
                  _ctrl.setDate(today);
                  _fetchSegments();
                }
                widget.onLive();
              },
              style: ElevatedButton.styleFrom(
                backgroundColor: widget.isLive
                    ? (widget.isPaused ? const Color(0xFFEA580C) : const Color(0xFFEF4444))
                    : const Color(0xFF404040),
                padding: const EdgeInsets.symmetric(horizontal: 10),
                minimumSize: Size.zero,
              ),
              child: const Text('LIVE', style: TextStyle(fontSize: 11, fontWeight: FontWeight.w600)),
            ),
          ),
        ),
        const Spacer(),
        // Hover time
        if (_hoverTime != null && !_ctrl.exportMode)
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: Text(_hoverTime!, style: const TextStyle(fontSize: 11, color: Color(0xFF737373))),
          ),
        // Export Clip / Cancel
        SizedBox(
          height: 26,
          child: TextButton(
            onPressed: _enterExportMode,
            style: TextButton.styleFrom(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              minimumSize: Size.zero,
              foregroundColor: _ctrl.exportMode ? const Color(0xFF22C55E) : const Color(0xFF737373),
            ),
            child: Text(
              _ctrl.exportMode ? 'Cancel' : 'Export Clip',
              style: const TextStyle(fontSize: 11),
            ),
          ),
        ),
      ],
    );
  }

  Widget _buildExportBar() {
    final hasRange = _ctrl.exportStartHour != null && _ctrl.exportEndHour != null;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      margin: const EdgeInsets.only(bottom: 4),
      decoration: BoxDecoration(
        color: const Color(0xFF22C55E).withValues(alpha: 0.1),
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: const Color(0xFF22C55E).withValues(alpha: 0.3)),
      ),
      child: Row(
        children: [
          const Icon(Icons.content_cut, size: 14, color: Color(0xFF22C55E)),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              _exportInstruction(),
              style: const TextStyle(fontSize: 11, color: Color(0xFF22C55E)),
            ),
          ),
          if (hasRange)
            SizedBox(
              height: 24,
              child: ElevatedButton(
                onPressed: _exporting ? null : _exportClip,
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF22C55E),
                  disabledBackgroundColor: const Color(0xFF22C55E).withValues(alpha: 0.5),
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  minimumSize: Size.zero,
                ),
                child: _exporting
                    ? const SizedBox(
                        width: 12, height: 12,
                        child: CircularProgressIndicator(
                          strokeWidth: 2, color: Colors.white),
                      )
                    : const Text('Export', style: TextStyle(fontSize: 11, fontWeight: FontWeight.w600)),
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildClipsList() {
    return Container(
      margin: const EdgeInsets.only(top: 4),
      constraints: const BoxConstraints(maxHeight: 120),
      child: ListView.builder(
        shrinkWrap: true,
        itemCount: _clips.length,
        itemBuilder: (context, index) {
          final clip = _clips[index];
          return _buildClipRow(clip);
        },
      ),
    );
  }

  Widget _buildClipRow(ClipExport clip) {
    final startDt = DateTime.parse(clip.startTime);
    final endDt = DateTime.parse(clip.endTime);
    final timeStr = '${_fmtTime(startDt)} - ${_fmtTime(endDt)}';
    final dateStr = '${startDt.month}/${startDt.day}';

    Color statusColor;
    String statusText;
    IconData? actionIcon;

    switch (clip.status) {
      case 'pending':
        statusColor = const Color(0xFFEAB308);
        statusText = 'Queued';
        break;
      case 'processing':
        statusColor = const Color(0xFF3B82F6);
        statusText = 'Processing';
        break;
      case 'done':
        statusColor = const Color(0xFF22C55E);
        statusText = 'Ready';
        actionIcon = Icons.download;
        break;
      case 'failed':
        statusColor = const Color(0xFFEF4444);
        statusText = 'Failed';
        break;
      default:
        statusColor = const Color(0xFF737373);
        statusText = clip.status;
    }

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      margin: const EdgeInsets.only(bottom: 2),
      decoration: BoxDecoration(
        color: const Color(0xFF1A1A1A),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Row(
        children: [
          // Status dot
          Container(
            width: 6, height: 6,
            decoration: BoxDecoration(shape: BoxShape.circle, color: statusColor),
          ),
          const SizedBox(width: 6),
          // Spinner for processing
          if (clip.status == 'pending' || clip.status == 'processing')
            const Padding(
              padding: EdgeInsets.only(right: 6),
              child: SizedBox(
                width: 10, height: 10,
                child: CircularProgressIndicator(strokeWidth: 1.5, color: Color(0xFF3B82F6)),
              ),
            ),
          // Time range
          Expanded(
            child: Text(
              '$dateStr  $timeStr',
              style: const TextStyle(fontSize: 10, color: Color(0xFFA3A3A3)),
              overflow: TextOverflow.ellipsis,
            ),
          ),
          // Status label
          Text(statusText, style: TextStyle(fontSize: 10, color: statusColor)),
          const SizedBox(width: 8),
          // Download button
          if (actionIcon != null)
            InkWell(
              onTap: () => _downloadClip(clip),
              borderRadius: BorderRadius.circular(4),
              child: Padding(
                padding: const EdgeInsets.all(2),
                child: Icon(actionIcon, size: 16, color: const Color(0xFF22C55E)),
              ),
            ),
          // Delete button
          InkWell(
            onTap: () => _deleteClip(clip),
            borderRadius: BorderRadius.circular(4),
            child: const Padding(
              padding: EdgeInsets.all(2),
              child: Icon(Icons.close, size: 14, color: Color(0xFF525252)),
            ),
          ),
        ],
      ),
    );
  }

  String _fmtTime(DateTime dt) =>
      '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';

  Widget _buildTimelineBar() {
    final barHeight = widget.compact ? 60.0 : 120.0;

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
              onPointerDown: (event) {
                _pointerDownPos = event.localPosition;
                _longPressActive = false;
                _longPressTimer?.cancel();
                _longPressTimer = Timer(const Duration(milliseconds: 400), () {
                  if (!mounted) return;
                  _longPressActive = true;
                  final pos = _pointerDownPos!;
                  final pct = (pos.dx / width).clamp(0.0, 1.0);
                  final yFrac = (pos.dy / barHeight).clamp(0.0, 1.0);
                  final hour = _ctrl.viewportPctToHour(pct);
                  _ctrl.setScrubHour(hour);
                  setState(() => _hoverTime = hourToTimeString(hour));
                  // Check for detection event at hold position
                  final hoveredCat = _ctrl.categoryAtYFraction(yFrac);
                  if (hoveredCat != null) {
                    final padHours = 8.0 / width * _ctrl.visibleHours;
                    _hoverMotionEvent = _ctrl.motionEventAtHour(hour, padHours: padHours, category: hoveredCat);
                  } else {
                    _hoverMotionEvent = null;
                  }
                  _updateThumbOverlay();
                });
              },
              onPointerMove: (event) {
                if (_pointerDownPos != null && !_longPressActive) {
                  final delta = (event.localPosition - _pointerDownPos!).distance;
                  if (delta > 20) {
                    // Finger moved too far — cancel long press, let scale gesture handle it
                    _longPressTimer?.cancel();
                  }
                }
              },
              onPointerUp: (_) {
                _longPressTimer?.cancel();
                _pointerDownPos = null;
                if (_longPressActive) {
                  _longPressActive = false;
                  _hoverMotionEvent = null;
                  _ctrl.setScrubHour(null);
                  setState(() => _hoverTime = null);
                  _updateThumbOverlay();
                }
              },
              child: GestureDetector(
                onTapUp: (details) {
                  final pct = details.localPosition.dx / width;
                  final yFrac = details.localPosition.dy / barHeight;
                  _onTimelineTap(pct, yFraction: yFrac);
                },
                onScaleStart: (details) {
                  _lastScale = 1.0;
                  _wasPinch = false;
                  _scaleStartPct = (details.localFocalPoint.dx / width).clamp(0.0, 1.0);
                  _scaleStartYFrac = (details.localFocalPoint.dy / barHeight).clamp(0.0, 1.0);
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
                    final yFrac = (details.localFocalPoint.dy / barHeight).clamp(0.0, 1.0);
                    final hour = _ctrl.viewportPctToHour(pct);
                    _ctrl.setScrubHour(hour);
                    _scaleStartYFrac = yFrac;
                    setState(() => _hoverTime = hourToTimeString(hour));

                    // Set hover motion event for touch scrub (Android)
                    final hoveredCat = _ctrl.categoryAtYFraction(yFrac);
                    if (hoveredCat != null) {
                      final padHours = 8.0 / width * _ctrl.visibleHours;
                      _hoverMotionEvent = _ctrl.motionEventAtHour(hour, padHours: padHours, category: hoveredCat);
                    } else {
                      _hoverMotionEvent = null;
                    }

                    _updateThumbOverlay();
                  }
                },
                onScaleEnd: (_) {
                  if (!_wasPinch && !_longPressActive) {
                    final scrub = _ctrl.scrubHour;
                    if (scrub != null) {
                      final pct = _ctrl.hourToViewportPct(scrub);
                      _onTimelineTap(pct, yFraction: _scaleStartYFrac);
                    } else if (_scaleStartPct != null) {
                      // Quick tap with no movement — scrubHour never set
                      _onTimelineTap(_scaleStartPct!, yFraction: _scaleStartYFrac);
                    }
                  }
                  _scaleStartPct = null;
                  _scaleStartYFrac = null;
                  _hoverMotionEvent = null;
                  _ctrl.setScrubHour(null);
                  setState(() => _hoverTime = null);
                  _updateThumbOverlay();
                },
                child: MouseRegion(
                  cursor: _hoverMotionEvent != null
                      ? SystemMouseCursors.click
                      : SystemMouseCursors.basic,
                  onHover: (event) {
                    final pct = event.localPosition.dx / width;
                    final hour = _ctrl.viewportPctToHour(pct);
                    _ctrl.setScrubHour(hour);
                    setState(() => _hoverTime = hourToTimeString(hour));

                    // Detect hover over detection rows — category-aware
                    final yFrac = event.localPosition.dy / barHeight;
                    final hoveredCat = _ctrl.categoryAtYFraction(yFrac);
                    if (hoveredCat != null) {
                      final padHours = 8.0 / width * _ctrl.visibleHours;
                      _hoverMotionEvent = _ctrl.motionEventAtHour(hour, padHours: padHours, category: hoveredCat);
                    } else {
                      _hoverMotionEvent = null;
                    }

                    _updateThumbOverlay();
                  },
                  onExit: (_) {
                    _ctrl.setScrubHour(null);
                    _hoverMotionEvent = null;
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
