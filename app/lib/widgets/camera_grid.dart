import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../models/camera.dart';
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
  final int? selectedCameraId;
  final ValueChanged<int> onCameraSelected;
  final ValueChanged<Camera> onEditCamera;
  final VoidCallback onAddCamera;
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
    this.selectedCameraId,
    required this.onCameraSelected,
    required this.onEditCamera,
    required this.onAddCamera,
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
    final width = MediaQuery.of(context).size.width;
    final columns = width > 900 ? 3 : (width > 500 ? 2 : 1);
    final cameras = _cameras;

    final gridWidth = width - 16;
    final cardWidth = (gridWidth - (columns - 1) * 8) / columns;
    final cardHeight = cardWidth / (16 / 9);

    return GridView.builder(
      padding: const EdgeInsets.all(8),
      gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
        crossAxisCount: columns,
        crossAxisSpacing: 8,
        mainAxisSpacing: 8,
        childAspectRatio: 16 / 9,
      ),
      itemCount: cameras.length + 1,
      itemBuilder: (context, index) {
        if (index == cameras.length) {
          return _AddCameraCard(onTap: widget.onAddCamera);
        }
        final cam = cameras[index];
        final url = widget.streamApi.liveUrl(cam.id, widget.streamSource, widget.quality, cameraName: cam.name);
        final isFullscreen = cam.id == widget.fullscreenCameraId;
        final isDragged = cam.id == _draggingId;
        final isHoverTarget = _hoverIndex == index && _draggingId != null && _draggingId != cam.id;

        final card = CameraCard(
          camera: cam,
          stream: _streamFor(cam.id),
          streamUrl: url,
          selected: widget.selectedCameraId == cam.id,
          onTap: () => widget.onCameraSelected(cam.id),
          onEdit: () => widget.onEditCamera(cam),
          livePlayer: widget.livePlayers[cam.id],
          liveController: widget.liveControllers[cam.id],
          isFullscreen: isFullscreen,
          playbackController: widget.playbackControllers[cam.id],
          playbackLoading: widget.playbackLoading.contains(cam.id),
          playbackFailed: widget.playbackFailed.contains(cam.id),
          showDragHint: widget.selectedCameraId == cam.id,
          dragFeedbackSize: Size(cardWidth, cardHeight),
          onDragStarted: () => _onDragStarted(cam.id),
          onDragEnd: _onDragEnd,
        );

        return DragTarget<int>(
          key: ValueKey(cam.id),
          onWillAcceptWithDetails: (details) => details.data != cam.id,
          onAcceptWithDetails: (details) {
            _applyReorder(details.data, index);
            // Clear drag state immediately — Draggable.onDragEnd may not fire
            // reliably after a reorder rebuilds the widget tree
            setState(() {
              _draggingId = null;
              _hoverIndex = null;
            });
            widget.onDragStateChanged(false);
          },
          onMove: (_) {
            if (_hoverIndex != index) {
              setState(() => _hoverIndex = index);
            }
          },
          onLeave: (_) {
            if (_hoverIndex == index) {
              setState(() => _hoverIndex = null);
            }
          },
          builder: (context, candidateData, rejectedData) {
            return AnimatedContainer(
              duration: const Duration(milliseconds: 150),
              decoration: isHoverTarget
                  ? BoxDecoration(
                      borderRadius: BorderRadius.circular(10),
                      border: Border.all(color: const Color(0xFF3B82F6).withValues(alpha: 0.5), width: 2),
                    )
                  : null,
              child: isDragged ? Opacity(opacity: 0.3, child: card) : card,
            );
          },
        );
      },
    );
  }
}

class _AddCameraCard extends StatelessWidget {
  final VoidCallback onTap;
  const _AddCameraCard({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: const Color(0xFF333333)),
          color: const Color(0xFF1A1A1A),
        ),
        child: const Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.add, color: Color(0xFF525252), size: 28),
              SizedBox(height: 4),
              Text('Add Camera',
                  style: TextStyle(color: Color(0xFF525252), fontSize: 12)),
            ],
          ),
        ),
      ),
    );
  }
}
