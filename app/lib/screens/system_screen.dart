import 'package:flutter/material.dart';
import '../models/camera.dart';
import '../models/system_status.dart';
import '../models/storage_stats.dart';
import '../services/system_api.dart';
import '../utils/format_utils.dart';
import 'dart:async';

class SystemScreen extends StatefulWidget {
  final SystemApi systemApi;
  final List<Camera> cameras;
  final SystemStatus? systemStatus;
  final VoidCallback onBack;

  const SystemScreen({
    super.key,
    required this.systemApi,
    required this.cameras,
    this.systemStatus,
    required this.onBack,
  });

  @override
  State<SystemScreen> createState() => _SystemScreenState();
}

class _SystemScreenState extends State<SystemScreen> {
  StorageStats? _storage;
  bool _runningRetention = false;
  String? _retentionResult;
  Timer? _pollTimer;

  @override
  void initState() {
    super.initState();
    _fetchStorage();
    _pollTimer = Timer.periodic(const Duration(seconds: 10), (_) {
      if (mounted) _fetchStorage();
    });
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    super.dispose();
  }

  Future<void> _fetchStorage() async {
    try {
      final s = await widget.systemApi.fetchStorage();
      if (mounted) setState(() => _storage = s);
    } catch (_) {}
  }

  Future<void> _runRetention() async {
    setState(() {
      _runningRetention = true;
      _retentionResult = null;
    });
    try {
      final result = await widget.systemApi.runRetention();
      setState(() {
        _retentionResult =
            'Deleted ${result.deleted} files, freed ${formatBytes(result.freedBytes)}';
        _runningRetention = false;
      });
      _fetchStorage();
    } catch (e) {
      setState(() {
        _retentionResult = 'Error: $e';
        _runningRetention = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: widget.onBack,
        ),
        title: const Text('System', style: TextStyle(fontSize: 16)),
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          // Storage cards
          if (_storage != null) ..._buildStorageCards(),
          const SizedBox(height: 16),

          // Stream health
          _buildStreamHealth(),
          const SizedBox(height: 16),

          // Per-camera storage
          if (_storage != null) _buildCameraStorage(),
          const SizedBox(height: 16),

          // Retention
          _buildRetentionSection(),
        ],
      ),
    );
  }

  List<Widget> _buildStorageCards() {
    final s = _storage!;
    final diskPct = s.diskTotalBytes > 0 ? s.diskUsedBytes / s.diskTotalBytes : 0.0;
    final recPct = s.maxStorageBytes > 0 ? s.recordingsTotalBytes / s.maxStorageBytes : 0.0;

    return [
      Row(
        children: [
          Expanded(child: _StatCard(
            title: 'Disk Usage',
            value: '${formatBytes(s.diskUsedBytes)} / ${formatBytes(s.diskTotalBytes)}',
            subtitle: '${formatBytes(s.diskFreeBytes)} free',
            progress: diskPct,
          )),
          const SizedBox(width: 8),
          Expanded(child: _StatCard(
            title: 'Recordings',
            value: formatBytes(s.recordingsTotalBytes),
            subtitle: 'Limit: ${formatBytes(s.maxStorageBytes)}',
            progress: recPct,
          )),
          const SizedBox(width: 8),
          Expanded(child: _StatCard(
            title: 'Retention',
            value: '${s.maxAgeDays} days',
            subtitle: 'Max age',
            progress: null,
          )),
        ],
      ),
    ];
  }

  Widget _buildStreamHealth() {
    final status = widget.systemStatus;
    if (status == null) {
      return const Card(child: Padding(
        padding: EdgeInsets.all(16),
        child: Text('Loading status...'),
      ));
    }

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Stream Health (${status.activeStreams}/${status.totalCameras} active)',
                style: const TextStyle(fontWeight: FontWeight.w500)),
            const SizedBox(height: 8),
            ...status.streams.map((s) => Padding(
              padding: const EdgeInsets.symmetric(vertical: 3),
              child: Row(
                children: [
                  Container(
                    width: 8,
                    height: 8,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: s.running ? const Color(0xFF22C55E) : const Color(0xFFEF4444),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(child: Text(s.cameraName, style: const TextStyle(fontSize: 13))),
                  if (s.uptimeSeconds != null)
                    Text(formatUptime(s.uptimeSeconds!),
                        style: const TextStyle(fontSize: 12, color: Color(0xFF737373))),
                  if (s.pid != null)
                    Padding(
                      padding: const EdgeInsets.only(left: 12),
                      child: Text('PID ${s.pid}',
                          style: const TextStyle(fontSize: 11, color: Color(0xFF525252))),
                    ),
                  if (s.error != null)
                    Padding(
                      padding: const EdgeInsets.only(left: 8),
                      child: Text(s.error!,
                          style: const TextStyle(fontSize: 11, color: Color(0xFFEF4444))),
                    ),
                ],
              ),
            )),
          ],
        ),
      ),
    );
  }

  Widget _buildCameraStorage() {
    final stats = _storage!.cameraStats;
    final cameraMap = {for (final c in widget.cameras) c.id: c.name};

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Per-Camera Storage', style: TextStyle(fontWeight: FontWeight.w500)),
            const SizedBox(height: 8),
            ...stats.map((s) => Padding(
              padding: const EdgeInsets.symmetric(vertical: 3),
              child: Row(
                children: [
                  Expanded(
                    child: Text(cameraMap[s.cameraId] ?? 'Camera ${s.cameraId}',
                        style: const TextStyle(fontSize: 13)),
                  ),
                  Text('${s.segmentCount} files',
                      style: const TextStyle(fontSize: 12, color: Color(0xFF737373))),
                  const SizedBox(width: 12),
                  Text(formatBytes(s.totalSizeBytes),
                      style: const TextStyle(fontSize: 12)),
                ],
              ),
            )),
          ],
        ),
      ),
    );
  }

  Widget _buildRetentionSection() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Retention', style: TextStyle(fontWeight: FontWeight.w500)),
            const SizedBox(height: 8),
            Row(
              children: [
                ElevatedButton(
                  onPressed: _runningRetention ? null : _runRetention,
                  child: _runningRetention
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Text('Run Retention Now'),
                ),
                if (_retentionResult != null)
                  Padding(
                    padding: const EdgeInsets.only(left: 12),
                    child: Text(_retentionResult!,
                        style: const TextStyle(fontSize: 12, color: Color(0xFFA3A3A3))),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _StatCard extends StatelessWidget {
  final String title;
  final String value;
  final String subtitle;
  final double? progress;

  const _StatCard({
    required this.title,
    required this.value,
    required this.subtitle,
    this.progress,
  });

  @override
  Widget build(BuildContext context) {
    Color progressColor = const Color(0xFF3B82F6);
    if (progress != null) {
      if (progress! > 0.9) {
        progressColor = const Color(0xFFEF4444);
      } else if (progress! > 0.7) {
        progressColor = const Color(0xFFEAB308);
      }
    }

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title,
                style: const TextStyle(fontSize: 11, color: Color(0xFF737373))),
            const SizedBox(height: 4),
            Text(value, style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w500)),
            const SizedBox(height: 2),
            Text(subtitle,
                style: const TextStyle(fontSize: 11, color: Color(0xFF525252))),
            if (progress != null) ...[
              const SizedBox(height: 6),
              ClipRRect(
                borderRadius: BorderRadius.circular(2),
                child: LinearProgressIndicator(
                  value: progress!.clamp(0, 1),
                  backgroundColor: const Color(0xFF333333),
                  valueColor: AlwaysStoppedAnimation(progressColor),
                  minHeight: 4,
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
