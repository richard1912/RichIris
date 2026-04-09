import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../models/camera.dart';
import '../models/system_status.dart';
import '../services/stream_api.dart';
import 'camera_card.dart';

class CameraGrid extends StatelessWidget {
  final List<Camera> cameras;
  final SystemStatus? systemStatus;
  final StreamApi streamApi;
  final String streamSource;
  final String quality;
  final int? selectedCameraId;
  final ValueChanged<int> onCameraSelected;
  final ValueChanged<Camera> onEditCamera;
  final VoidCallback onAddCamera;
  final Map<int, Player> livePlayers;
  final Map<int, VideoController> liveControllers;
  final int? fullscreenCameraId;
  final Map<int, VideoController> playbackControllers;
  final Set<int> playbackLoading;
  final Set<int> playbackFailed;

  const CameraGrid({
    super.key,
    required this.cameras,
    this.systemStatus,
    required this.streamApi,
    required this.streamSource,
    required this.quality,
    this.selectedCameraId,
    required this.onCameraSelected,
    required this.onEditCamera,
    required this.onAddCamera,
    this.livePlayers = const {},
    this.liveControllers = const {},
    this.fullscreenCameraId,
    this.playbackControllers = const {},
    this.playbackLoading = const {},
    this.playbackFailed = const {},
  });

  StreamStatus? _streamFor(int cameraId) {
    return systemStatus?.streams
        .where((s) => s.cameraId == cameraId)
        .firstOrNull;
  }

  @override
  Widget build(BuildContext context) {
    final width = MediaQuery.of(context).size.width;
    final columns = width > 900 ? 3 : (width > 500 ? 2 : 1);

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
          return _AddCameraCard(onTap: onAddCamera);
        }
        final cam = cameras[index];
        final url = streamApi.liveUrl(cam.id, streamSource, quality, cameraName: cam.name);
        final isFullscreen = cam.id == fullscreenCameraId;
        return CameraCard(
          camera: cam,
          stream: _streamFor(cam.id),
          streamUrl: url,
          selected: selectedCameraId == cam.id,
          onTap: () => onCameraSelected(cam.id),
          onEdit: () => onEditCamera(cam),
          livePlayer: livePlayers[cam.id],
          liveController: liveControllers[cam.id],
          isFullscreen: isFullscreen,
          playbackController: playbackControllers[cam.id],
          playbackLoading: playbackLoading.contains(cam.id),
          playbackFailed: playbackFailed.contains(cam.id),
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
