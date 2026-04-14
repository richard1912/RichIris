import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
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

  Size _feedbackSize(GridLayout layout, double gridW, double gridH) {
    // Use the smallest slot as the drag-feedback size so the preview looks
    // reasonable regardless of which tile the user picked up.
    var w = gridW;
    var h = gridH;
    for (final s in layout.slots) {
      final sw = s.w * gridW;
      final sh = s.h * gridH;
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
    final left = slot.x * gridW + gap / 2;
    final top = slot.y * gridH + gap / 2;
    final width = (slot.w * gridW - gap).clamp(0.0, double.infinity);
    final height = (slot.h * gridH - gap).clamp(0.0, double.infinity);

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
