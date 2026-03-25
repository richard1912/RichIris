import 'package:flutter/material.dart';
import '../config/constants.dart';
import '../models/camera.dart';
import '../models/system_status.dart';
import '../services/stream_api.dart';
import '../services/recording_api.dart';
import '../services/clip_api.dart';
import '../services/camera_api.dart';
import '../widgets/camera_grid.dart';
import '../widgets/quality_selector.dart';
import '../widgets/timeline/timeline_widget.dart';

class HomeScreen extends StatelessWidget {
  final List<Camera> cameras;
  final SystemStatus? systemStatus;
  final Quality quality;
  final StreamSource streamSource;
  final StreamApi streamApi;
  final RecordingApi recordingApi;
  final ClipApi clipApi;
  final CameraApi cameraApi;
  final int tzOffsetMs;
  final int? selectedCameraId;
  final ValueChanged<int> onCameraSelected;
  final ValueChanged<Quality> onQualityChanged;
  final ValueChanged<StreamSource> onStreamSourceChanged;
  final VoidCallback onOpenSystem;
  final VoidCallback onOpenSettings;
  final VoidCallback onAddCamera;
  final ValueChanged<Camera> onEditCamera;

  const HomeScreen({
    super.key,
    required this.cameras,
    this.systemStatus,
    required this.quality,
    required this.streamSource,
    required this.streamApi,
    required this.recordingApi,
    required this.clipApi,
    required this.cameraApi,
    required this.tzOffsetMs,
    this.selectedCameraId,
    required this.onCameraSelected,
    required this.onQualityChanged,
    required this.onStreamSourceChanged,
    required this.onOpenSystem,
    required this.onOpenSettings,
    required this.onAddCamera,
    required this.onEditCamera,
  });

  @override
  Widget build(BuildContext context) {
    final activeCount = systemStatus?.activeStreams ?? 0;
    final totalCount = systemStatus?.totalCameras ?? cameras.length;

    return Scaffold(
      appBar: AppBar(
        title: const Text('RichIris', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
        actions: [
          Center(
            child: Padding(
              padding: const EdgeInsets.only(right: 8),
              child: Text(
                '$activeCount/$totalCount active',
                style: const TextStyle(fontSize: 12, color: Color(0xFF737373)),
              ),
            ),
          ),
          StreamSourceSelector(value: streamSource, onChanged: onStreamSourceChanged),
          const SizedBox(width: 4),
          QualitySelector(value: quality, onChanged: onQualityChanged),
          const SizedBox(width: 8),
          IconButton(
            icon: const Icon(Icons.storage, size: 20),
            tooltip: 'System',
            onPressed: onOpenSystem,
          ),
          IconButton(
            icon: const Icon(Icons.settings, size: 20),
            tooltip: 'Settings',
            onPressed: onOpenSettings,
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: CameraGrid(
              cameras: cameras,
              systemStatus: systemStatus,
              streamApi: streamApi,
              streamSource: streamSource.param,
              quality: quality.param,
              selectedCameraId: selectedCameraId,
              onCameraSelected: onCameraSelected,
              onEditCamera: onEditCamera,
              onAddCamera: onAddCamera,
            ),
          ),
          if (selectedCameraId != null)
            SizedBox(
              height: 120,
              child: TimelineWidget(
                cameraId: selectedCameraId!,
                recordingApi: recordingApi,
                clipApi: clipApi,
                tzOffsetMs: tzOffsetMs,
                isLive: true,
                compact: true,
                onPlayback: (_) {},
                onLive: () {},
              ),
            ),
        ],
      ),
    );
  }
}
