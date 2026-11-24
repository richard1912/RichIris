import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../models/camera.dart';
import '../models/grid_layout.dart';
import '../models/system_status.dart';
import '../services/stream_api.dart';
import 'camera_card.dart';

class CameraGrid extends StatefulWidget {
  final List<Camera> cameras;
  final List<Camera> allCameras;
  final SystemStatus? systemStatus;
  final StreamApi streamApi;
  final String streamSource;
  final String quality;
  final GridLayout layout;

  /// Identifies whose divider positions these are (the selected group). The
  /// positions are stored per group AND per layout.
  final String resizeScope;
  final int? selectedCameraId;
  final ValueChanged<int> onCameraSelected;
  final ValueChanged<Camera> onEditCamera;
  final ValueChanged<Camera>? onAddToGroup;
  final Future<void> Function(List<int>) onReorder;
  final ValueChanged<bool> onDragStateChanged;
  final Map<int, Player> livePlayers;
  final Map<int, VideoController> liveControllers;
  final int? fullscreenCameraId;
  final Map<int, VideoController> playbackControllers;
  final Set<int> playbackLoading;
  final Set<int> playbackFailed;

  const CameraGrid({
    super.key,
    required this.cameras,
    required this.allCameras,
    this.systemStatus,
    required this.streamApi,
    required this.streamSource,
    required this.quality,
    required this.layout,
    this.resizeScope = 'all',
    this.selectedCameraId,
    required this.onCameraSelected,
    required this.onEditCamera,
    this.onAddToGroup,
    required this.onReorder,
    required this.onDragStateChanged,
    this.livePlayers = const {},
    this.liveControllers = const {},
    this.fullscreenCameraId,
    this.playbackControllers = const {},
    this.playbackLoading = const {},
    this.playbackFailed = const {},
  });

  @override
  State<CameraGrid> createState() => _CameraGridState();
}

class _CameraGridState extends State<CameraGrid> {
  List<Camera>? _localOrder;
  int? _draggingId;
  int? _hoverIndex;
  int _pageIdx = 0;

  /// Captured once per grid so every poster URL stays stable across rebuilds
  /// (Flutter's image cache is keyed on the URL — a per-build timestamp would
  /// re-download the poster on every status poll).
  final int _posterBust = DateTime.now().millisecondsSinceEpoch;

  // Draggable dividers. A layout's slot edges fall on a few distinct x and y
  // lines ("cuts"); dragging a divider moves one cut, and every slot edge on
  // that line follows. Positions are fractions of the grid, kept in the same
  // order as the layout's own cuts, and saved per group + layout.
  static const _minTile = 0.08;
  List<double> _defX = const [], _defY = const [];
  List<double> _cutX = const [], _cutY = const [];
  int _cutsGen = 0;

  String get _cutsKey =>
      'richiris-grid-cuts-${widget.resizeScope}-${widget.layout.id}';

  static List<double> _interiorEdges(Iterable<double> values) {
    final out = <double>[];
    for (final v in values) {
      if (v < 1e-6 || v > 1 - 1e-6) continue;
      if (!out.any((o) => (o - v).abs() < 1e-6)) out.add(v);
    }
    out.sort();
    return out;
  }

  void _resetCutsToLayout() {
    final slots = widget.layout.slots;
    _defX = _interiorEdges(slots.expand((s) => [s.x, s.x + s.w]));
    _defY = _interiorEdges(slots.expand((s) => [s.y, s.y + s.h]));
    _cutX = List.of(_defX);
    _cutY = List.of(_defY);
  }

  Future<void> _loadCuts() async {
    final gen = ++_cutsGen;
    _resetCutsToLayout();
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(_cutsKey);
      if (raw == null || !mounted || gen != _cutsGen) return;
      final data = jsonDecode(raw) as Map<String, dynamic>;
      final x = (data['x'] as List).map((e) => (e as num).toDouble()).toList();
      final y = (data['y'] as List).map((e) => (e as num).toDouble()).toList();
      if (x.length != _defX.length || y.length != _defY.length) return;
      if (!_validCuts(x) || !_validCuts(y)) return;
      setState(() {
        _cutX = x;
        _cutY = y;
      });
    } catch (_) {
      // Unreadable saved positions: keep the layout's own.
    }
  }

  static bool _validCuts(List<double> cuts) {
    var prev = 0.0;
    for (final c in cuts) {
      if (c - prev < _minTile - 1e-6) return false;
      prev = c;
    }
    return 1.0 - prev >= _minTile - 1e-6;
  }

  Future<void> _saveCuts() async {
    final prefs = await SharedPreferences.getInstance();
    final isDefault = _same(_cutX, _defX) && _same(_cutY, _defY);
    if (isDefault) {
      await prefs.remove(_cutsKey);
    } else {
      await prefs.setString(_cutsKey, jsonEncode({'x': _cutX, 'y': _cutY}));
    }
  }

  static bool _same(List<double> a, List<double> b) {
    if (a.length != b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if ((a[i] - b[i]).abs() > 1e-6) return false;
    }
    return true;
  }

  /// Where a layout edge sits now: the outer edges never move, an interior
  /// edge follows its cut.
  double _mapEdge(double v, List<double> def, List<double> cur) {
    if (v < 1e-6) return 0;
    if (v > 1 - 1e-6) return 1;
    final i = _cutIndex(v, def);
    return i < 0 ? v : cur[i];
  }

  static int _cutIndex(double v, List<double> def) {
    for (var i = 0; i < def.length; i++) {
      if ((def[i] - v).abs() < 1e-6) return i;
    }
    return -1;
  }

  void _dragCut(bool vertical, int idx, double deltaFraction) {
    final cuts = vertical ? _cutX : _cutY;
    final lo = (idx > 0 ? cuts[idx - 1] : 0.0) + _minTile;
    final hi = (idx < cuts.length - 1 ? cuts[idx + 1] : 1.0) - _minTile;
    if (hi <= lo) return;
    setState(() => cuts[idx] = (cuts[idx] + deltaFraction).clamp(lo, hi));
  }

  void _resetCut(bool vertical, int idx) {
    setState(() {
      // One divider back to its place if that fits between its neighbours,
      // otherwise the whole axis.
      final cuts = vertical ? _cutX : _cutY;
      final def = vertical ? _defX : _defY;
      final trial = List.of(cuts)..[idx] = def[idx];
      if (_validCuts(trial)) {
        cuts[idx] = def[idx];
      } else if (vertical) {
        _cutX = List.of(_defX);
      } else {
        _cutY = List.of(_defY);
      }
    });
    _saveCuts();
  }

  @override
  void initState() {
    super.initState();
    _loadCuts();
  }

  List<Camera> get _cameras => _localOrder ?? widget.cameras;

  StreamStatus? _streamFor(int cameraId) {
    return widget.systemStatus?.streams
        .where((s) => s.cameraId == cameraId)
        .firstOrNull;
  }

  @override
  void didUpdateWidget(covariant CameraGrid oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (_draggingId == null && _localOrder != null) {
      _localOrder = null;
    }
    // Reset page on layout change so users see page 1 of the new layout.
    if (oldWidget.layout.id != widget.layout.id) {
      _pageIdx = 0;
    }
    if (oldWidget.layout.id != widget.layout.id ||
        oldWidget.resizeScope != widget.resizeScope) {
      _loadCuts();
    }
    // Clamp pageIdx if camera count dropped.
    final totalPages = _totalPages();
    if (_pageIdx >= totalPages) {
      _pageIdx = (totalPages - 1).clamp(0, totalPages);
    }
  }

  int _totalPages() {
    final slotsPerPage = widget.layout.slotCount;
    if (slotsPerPage <= 0) return 1;
    final count = _cameras.length;
    if (count == 0) return 1;
    return ((count + slotsPerPage - 1) ~/ slotsPerPage);
  }

  void _onDragStarted(int cameraId) {
    setState(() => _draggingId = cameraId);
    widget.onDragStateChanged(true);
  }

  void _onDragEnd() {
    setState(() {
      _draggingId = null;
      _hoverIndex = null;
    });
    widget.onDragStateChanged(false);
  }

  void _applyReorder(int draggedId, int targetIndex) {
    final cameras = List<Camera>.from(_cameras);
    final fromIdx = cameras.indexWhere((c) => c.id == draggedId);
    if (fromIdx < 0 || fromIdx == targetIndex) return;

    final cam = cameras.removeAt(fromIdx);
    // After removal, indices >= fromIdx shift down by 1
    final insertAt = fromIdx < targetIndex ? targetIndex - 1 : targetIndex;
    cameras.insert(insertAt.clamp(0, cameras.length), cam);

    setState(() => _localOrder = cameras);

    final orderedIds = cameras.map((c) => c.id).toList();

    // Include cameras not currently displayed (other groups)
    final allIds = <int>[];
    final displayedSet = orderedIds.toSet();
    var insertIdx = 0;
    for (final c in widget.allCameras) {
      if (displayedSet.contains(c.id)) {
        while (insertIdx < orderedIds.length && allIds.contains(orderedIds[insertIdx])) {
          insertIdx++;
        }
        if (insertIdx < orderedIds.length) {
          allIds.add(orderedIds[insertIdx]);
          insertIdx++;
        }
      } else {
        allIds.add(c.id);
      }
    }
    for (final id in orderedIds) {
      if (!allIds.contains(id)) allIds.add(id);
    }

    widget.onReorder(allIds);
  }

  @override
  Widget build(BuildContext context) {
    final layout = widget.layout;
    final slotsPerPage = layout.slotCount;
    final cameras = _cameras;
    final totalPages = _totalPages();
    final pageIdx = _pageIdx.clamp(0, totalPages - 1);
    final pageStart = pageIdx * slotsPerPage;
    final showPager = totalPages > 1;
    const gap = 4.0;
    const pagerHeight = 28.0;

    return Padding(
      padding: const EdgeInsets.all(8),
      child: LayoutBuilder(
        builder: (context, constraints) {
          final gridW = constraints.maxWidth;
          final gridH = (constraints.maxHeight - (showPager ? pagerHeight : 0))
              .clamp(0.0, double.infinity);
          final feedbackSize = _feedbackSize(layout, gridW, gridH);

          return Column(
            children: [
              SizedBox(
                width: gridW,
                height: gridH,
                child: Stack(
                  children: [
                    for (var i = 0; i < slotsPerPage; i++)
                      _buildSlot(
                        slotIdx: i,
                        globalIdx: pageStart + i,
                        slot: layout.slots[i],
                        gridW: gridW,
                        gridH: gridH,
                        gap: gap,
                        cameras: cameras,
                        feedbackSize: feedbackSize,
                      ),
                    ..._buildDividers(layout, gridW, gridH),
                  ],
                ),
              ),
              if (showPager)
                SizedBox(
                  height: pagerHeight,
                  child: _PagerBar(
                    pageIdx: pageIdx,
                    totalPages: totalPages,
                    onChanged: (i) => setState(() => _pageIdx = i),
                  ),
                ),
            ],
          );
        },
      ),
    );
  }

  /// One drag strip per tile edge that is not on the outside of the grid.
  /// Strips on the same line overlap and all move the same cut. Drawn over
  /// the tiles; double-click puts a divider back.
  List<Widget> _buildDividers(GridLayout layout, double gridW, double gridH) {
    const grab = 10.0;
    final out = <Widget>[];
    for (final s in layout.slots) {
      final x0 = _mapEdge(s.x, _defX, _cutX) * gridW;
      final x1 = _mapEdge(s.x + s.w, _defX, _cutX) * gridW;
      final y0 = _mapEdge(s.y, _defY, _cutY) * gridH;
      final y1 = _mapEdge(s.y + s.h, _defY, _cutY) * gridH;
      final xi = _cutIndex(s.x + s.w, _defX);
      if (xi >= 0 && gridW > 0) {
        out.add(Positioned(
          left: x1 - grab / 2,
          top: y0,
          width: grab,
          height: y1 - y0,
          child: _DividerHandle(
            vertical: true,
            onDrag: (d) => _dragCut(true, xi, d / gridW),
            onDragEnd: _saveCuts,
            onReset: () => _resetCut(true, xi),
          ),
        ));
      }
      final yi = _cutIndex(s.y + s.h, _defY);
      if (yi >= 0 && gridH > 0) {
        out.add(Positioned(
          left: x0,
          top: y1 - grab / 2,
          width: x1 - x0,
          height: grab,
          child: _DividerHandle(
            vertical: false,
            onDrag: (d) => _dragCut(false, yi, d / gridH),
            onDragEnd: _saveCuts,
            onReset: () => _resetCut(false, yi),
          ),
        ));
      }
    }
    return out;
  }

  Size _feedbackSize(GridLayout layout, double gridW, double gridH) {
    // Use the smallest slot as the drag-feedback size so the preview looks
    // reasonable regardless of which tile the user picked up.
    var w = gridW;
    var h = gridH;
    for (final s in layout.slots) {
      final sw = (_mapEdge(s.x + s.w, _defX, _cutX) -
              _mapEdge(s.x, _defX, _cutX)) *
          gridW;
      final sh = (_mapEdge(s.y + s.h, _defY, _cutY) -
              _mapEdge(s.y, _defY, _cutY)) *
          gridH;
      if (sw < w) w = sw;
      if (sh < h) h = sh;
    }
    return Size(w, h);
  }

  Widget _buildSlot({
    required int slotIdx,
    required int globalIdx,
    required GridSlot slot,
    required double gridW,
    required double gridH,
    required double gap,
    required List<Camera> cameras,
    required Size feedbackSize,
  }) {
    final x0 = _mapEdge(slot.x, _defX, _cutX);
    final x1 = _mapEdge(slot.x + slot.w, _defX, _cutX);
    final y0 = _mapEdge(slot.y, _defY, _cutY);
    final y1 = _mapEdge(slot.y + slot.h, _defY, _cutY);
    final left = x0 * gridW + gap / 2;
    final top = y0 * gridH + gap / 2;
    final width = ((x1 - x0) * gridW - gap).clamp(0.0, double.infinity);
    final height = ((y1 - y0) * gridH - gap).clamp(0.0, double.infinity);

    Widget child;
    if (globalIdx < cameras.length) {
      final cam = cameras[globalIdx];
      final url = widget.streamApi.liveUrl(
        cam.id,
        widget.streamSource,
        widget.quality,
        cameraName: cam.name,
      );
      final isFullscreen = cam.id == widget.fullscreenCameraId;
      final isDragged = cam.id == _draggingId;
      final isHoverTarget = _hoverIndex == globalIdx &&
          _draggingId != null &&
          _draggingId != cam.id;

      final card = CameraCard(
        camera: cam,
        stream: _streamFor(cam.id),
        streamUrl: url,
        posterUrl: widget.streamApi.posterUrl(cam.id,
            cacheBust: _posterBust, stream: widget.streamSource),
        selected: widget.selectedCameraId == cam.id,
        onTap: () => widget.onCameraSelected(cam.id),
        onEdit: () => widget.onEditCamera(cam),
        onAddToGroup:
            widget.onAddToGroup != null ? () => widget.onAddToGroup!(cam) : null,
        livePlayer: widget.livePlayers[cam.id],
        liveController: widget.liveControllers[cam.id],
        isFullscreen: isFullscreen,
        playbackController: widget.playbackControllers[cam.id],
        playbackLoading: widget.playbackLoading.contains(cam.id),
        playbackFailed: widget.playbackFailed.contains(cam.id),
        showDragHint: widget.selectedCameraId == cam.id,
        dragFeedbackSize: feedbackSize,
        onDragStarted: () => _onDragStarted(cam.id),
        onDragEnd: _onDragEnd,
      );

      child = DragTarget<int>(
        key: ValueKey(cam.id),
        onWillAcceptWithDetails: (details) => details.data != cam.id,
        onAcceptWithDetails: (details) {
          _applyReorder(details.data, globalIdx);
          setState(() {
            _draggingId = null;
            _hoverIndex = null;
          });
          widget.onDragStateChanged(false);
        },
        onMove: (_) {
          if (_hoverIndex != globalIdx) {
            setState(() => _hoverIndex = globalIdx);
          }
        },
        onLeave: (_) {
          if (_hoverIndex == globalIdx) {
            setState(() => _hoverIndex = null);
          }
        },
        builder: (context, candidateData, rejectedData) {
          return AnimatedContainer(
            duration: const Duration(milliseconds: 150),
            decoration: isHoverTarget
                ? BoxDecoration(
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(
                      color: const Color(0xFF3B82F6).withValues(alpha: 0.5),
                      width: 2,
                    ),
                  )
                : null,
            child: isDragged ? Opacity(opacity: 0.3, child: card) : card,
          );
        },
      );
    } else {
      // Empty slot — accepts drags so users can drop a camera into an
      // unfilled slot (list reorders, camera lands at this index).
      final isHoverTarget = _hoverIndex == globalIdx && _draggingId != null;
      child = DragTarget<int>(
        onWillAcceptWithDetails: (_) => true,
        onAcceptWithDetails: (details) {
          _applyReorder(details.data, globalIdx);
          setState(() {
            _draggingId = null;
            _hoverIndex = null;
          });
          widget.onDragStateChanged(false);
        },
        onMove: (_) {
          if (_hoverIndex != globalIdx) {
            setState(() => _hoverIndex = globalIdx);
          }
        },
        onLeave: (_) {
          if (_hoverIndex == globalIdx) {
            setState(() => _hoverIndex = null);
          }
        },
        builder: (context, candidateData, rejectedData) {
          return AnimatedContainer(
            duration: const Duration(milliseconds: 150),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(8),
              border: Border.all(
                color: isHoverTarget
                    ? const Color(0xFF3B82F6).withValues(alpha: 0.5)
                    : const Color(0xFF262626),
                width: isHoverTarget ? 2 : 1,
              ),
              color: const Color(0xFF111111),
            ),
            child: const Center(
              child: Icon(Icons.videocam_outlined,
                  color: Color(0xFF404040), size: 24),
            ),
          );
        },
      );
    }

    return Positioned(
      left: left,
      top: top,
      width: width,
      height: height,
      child: child,
    );
  }
}

/// A thin strip over the gap between tiles: drag to move the boundary,
/// double-click to put it back. Shows a blue line while hovered or dragged.
class _DividerHandle extends StatefulWidget {
  final bool vertical;
  final ValueChanged<double> onDrag;
  final VoidCallback onDragEnd;
  final VoidCallback onReset;

  const _DividerHandle({
    required this.vertical,
    required this.onDrag,
    required this.onDragEnd,
    required this.onReset,
  });

  @override
  State<_DividerHandle> createState() => _DividerHandleState();
}

class _DividerHandleState extends State<_DividerHandle> {
  bool _hover = false;
  bool _dragging = false;

  @override
  Widget build(BuildContext context) {
    final v = widget.vertical;
    final active = _hover || _dragging;
    return MouseRegion(
      cursor: v
          ? SystemMouseCursors.resizeColumn
          : SystemMouseCursors.resizeRow,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onDoubleTap: widget.onReset,
        onHorizontalDragStart:
            v ? (_) => setState(() => _dragging = true) : null,
        onHorizontalDragUpdate: v ? (d) => widget.onDrag(d.delta.dx) : null,
        onHorizontalDragEnd: v ? (_) => _end() : null,
        onHorizontalDragCancel: v ? _end : null,
        onVerticalDragStart:
            v ? null : (_) => setState(() => _dragging = true),
        onVerticalDragUpdate: v ? null : (d) => widget.onDrag(d.delta.dy),
        onVerticalDragEnd: v ? null : (_) => _end(),
        onVerticalDragCancel: v ? null : _end,
        child: Center(
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 120),
            width: v ? 2 : double.infinity,
            height: v ? double.infinity : 2,
            color: active
                ? const Color(0xFF3B82F6)
                : const Color(0x00000000),
          ),
        ),
      ),
    );
  }

  void _end() {
    if (mounted) setState(() => _dragging = false);
    widget.onDragEnd();
  }
}

class _PagerBar extends StatelessWidget {
  final int pageIdx;
  final int totalPages;
  final ValueChanged<int> onChanged;

  const _PagerBar({
    required this.pageIdx,
    required this.totalPages,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        IconButton(
          icon: const Icon(Icons.chevron_left, size: 20),
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
          onPressed: pageIdx > 0 ? () => onChanged(pageIdx - 1) : null,
        ),
        for (var i = 0; i < totalPages; i++)
          GestureDetector(
            onTap: () => onChanged(i),
            child: Container(
              margin: const EdgeInsets.symmetric(horizontal: 3),
              width: 8,
              height: 8,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: i == pageIdx
                    ? const Color(0xFF3B82F6)
                    : const Color(0xFF404040),
              ),
            ),
          ),
        IconButton(
          icon: const Icon(Icons.chevron_right, size: 20),
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
          onPressed:
              pageIdx < totalPages - 1 ? () => onChanged(pageIdx + 1) : null,
        ),
      ],
    );
  }
}
