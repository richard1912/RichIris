import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../models/camera.dart';
import '../models/zone.dart';
import '../services/camera_api.dart';
import '../services/zone_api.dart';
import '../widgets/zone_painter.dart';

/// Polygon zone editor. Tap to add a vertex; drag a handle to move it;
/// long-press a handle to delete it (requires ≥3 remaining).
class ZoneEditorScreen extends StatefulWidget {
  final CameraApi cameraApi;
  final ZoneApi zoneApi;
  final Camera camera;
  final Zone? zone; // null = creating a new zone

  const ZoneEditorScreen({
    super.key,
    required this.cameraApi,
    required this.zoneApi,
    required this.camera,
    this.zone,
  });

  @override
  State<ZoneEditorScreen> createState() => _ZoneEditorScreenState();
}

class _ZoneEditorScreenState extends State<ZoneEditorScreen> {
  static const double _handleHitRadius = 22.0;

  late final TextEditingController _nameCtrl;
  final GlobalKey _canvasKey = GlobalKey();
  Uint8List? _snapshot;
  bool _loadingSnapshot = true;
  bool _saving = false;
  String? _error;
  int? _draggingIndex;
  Size? _canvasSize;

  /// Points in normalized [0..1] coords. Stored normalized so resolution
  /// changes between view and backend don't distort the polygon.
  List<Offset> _pointsNorm = [];

  @override
  void initState() {
    super.initState();
    _nameCtrl = TextEditingController(text: widget.zone?.name ?? '');
    _pointsNorm = widget.zone?.points.toList() ?? <Offset>[];
    _loadSnapshot();
  }

  @override
  void dispose() {
    _nameCtrl.dispose();
    super.dispose();
  }

  Future<void> _loadSnapshot() async {
    try {
      final bytes = await widget.cameraApi.snapshot(
        rtspUrl: widget.camera.rtspUrl,
        width: 800,
        timeoutS: 10,
      );
      if (mounted) setState(() { _snapshot = bytes; _loadingSnapshot = false; });
    } catch (e) {
      if (mounted) {
        setState(() {
          _loadingSnapshot = false;
          _error = 'Couldn\'t load snapshot — the backend needs to reach the camera. ($e)';
        });
      }
    }
  }

  Offset _toDisplay(Offset norm) {
    final s = _canvasSize;
    if (s == null) return Offset.zero;
    return Offset(norm.dx * s.width, norm.dy * s.height);
  }

  Offset _toNormalized(Offset display) {
    final s = _canvasSize;
    if (s == null || s.width == 0 || s.height == 0) return Offset.zero;
    final x = (display.dx / s.width).clamp(0.0, 1.0);
    final y = (display.dy / s.height).clamp(0.0, 1.0);
    return Offset(x.toDouble(), y.toDouble());
  }

  int? _hitTestHandle(Offset localPos) {
    for (var i = 0; i < _pointsNorm.length; i++) {
      final d = (_toDisplay(_pointsNorm[i]) - localPos).distance;
      if (d <= _handleHitRadius) return i;
    }
    return null;
  }

  void _onTapUp(TapUpDetails details) {
    final hit = _hitTestHandle(details.localPosition);
    if (hit != null) return; // tapping a handle shouldn't append a vertex
    setState(() {
      _pointsNorm = [..._pointsNorm, _toNormalized(details.localPosition)];
    });
  }

  void _onPanStart(DragStartDetails details) {
    final hit = _hitTestHandle(details.localPosition);
    if (hit != null) setState(() => _draggingIndex = hit);
  }

  void _onPanUpdate(DragUpdateDetails details) {
    final idx = _draggingIndex;
    if (idx == null) return;
    setState(() {
      final updated = [..._pointsNorm];
      updated[idx] = _toNormalized(details.localPosition);
      _pointsNorm = updated;
    });
  }

  void _onPanEnd(DragEndDetails _) {
    if (_draggingIndex != null) setState(() => _draggingIndex = null);
  }

  void _onLongPressStart(LongPressStartDetails details) {
    final hit = _hitTestHandle(details.localPosition);
    if (hit == null) return;
    if (_pointsNorm.length <= 3) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('A zone needs at least 3 points')),
      );
      return;
    }
    setState(() {
      final updated = [..._pointsNorm]..removeAt(hit);
      _pointsNorm = updated;
    });
  }

  Future<void> _save() async {
    final name = _nameCtrl.text.trim();
    if (name.isEmpty) {
      setState(() => _error = 'Name is required');
      return;
    }
    if (_pointsNorm.length < 3) {
      setState(() => _error = 'A zone needs at least 3 points');
      return;
    }
    setState(() { _saving = true; _error = null; });
    try {
      if (widget.zone == null) {
        await widget.zoneApi.create(
          cameraId: widget.camera.id,
          name: name,
          points: _pointsNorm,
        );
      } else {
        await widget.zoneApi.update(
          cameraId: widget.camera.id,
          zoneId: widget.zone!.id,
          name: name,
          points: _pointsNorm,
        );
      }
      if (mounted) Navigator.of(context).pop(true);
    } catch (e) {
      setState(() => _error = 'Save failed: $e');
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.zone == null ? 'New Zone' : 'Edit Zone'),
        actions: [
          if (_pointsNorm.isNotEmpty)
            IconButton(
              tooltip: 'Clear all points',
              icon: const Icon(Icons.clear),
              onPressed: () => setState(() => _pointsNorm = <Offset>[]),
            ),
        ],
      ),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
            child: TextField(
              controller: _nameCtrl,
              decoration: const InputDecoration(
                labelText: 'Zone Name',
                isDense: true,
                border: OutlineInputBorder(),
              ),
            ),
          ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
              child: Text(_error!, style: const TextStyle(color: Colors.red)),
            ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
            child: Text(
              'Tap to add a point · drag a handle to move · long-press to remove',
              style: TextStyle(fontSize: 12, color: Colors.grey[500]),
            ),
          ),
          Expanded(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: _buildCanvas(),
            ),
          ),
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
              child: Row(
                children: [
                  Expanded(
                    child: OutlinedButton(
                      onPressed: _saving ? null : () => Navigator.of(context).pop(false),
                      child: const Text('Cancel'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: ElevatedButton(
                      onPressed: _saving ? null : _save,
                      child: _saving
                          ? const SizedBox(
                              width: 18, height: 18,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Text('Save Zone'),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildCanvas() {
    if (_loadingSnapshot) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_snapshot == null) {
      return const Center(child: Text('Snapshot unavailable'));
    }
    return LayoutBuilder(
      builder: (context, constraints) {
        return Center(
          child: AspectRatio(
            aspectRatio: 16 / 9,
            child: LayoutBuilder(
              builder: (ctx, inner) {
                _canvasSize = Size(inner.maxWidth, inner.maxHeight);
                return ClipRect(
                  child: Stack(
                    fit: StackFit.expand,
                    children: [
                      Image.memory(
                        _snapshot!,
                        fit: BoxFit.cover,
                        gaplessPlayback: true,
                      ),
                      GestureDetector(
                        key: _canvasKey,
                        behavior: HitTestBehavior.opaque,
                        onTapUp: _onTapUp,
                        onPanStart: _onPanStart,
                        onPanUpdate: _onPanUpdate,
                        onPanEnd: _onPanEnd,
                        onLongPressStart: _onLongPressStart,
                        child: CustomPaint(
                          painter: ZonePainter(
                            points: [
                              for (final p in _pointsNorm) _toDisplay(p),
                            ],
                            selectedIndex: _draggingIndex,
                          ),
                          child: const SizedBox.expand(),
                        ),
                      ),
                    ],
                  ),
                );
              },
            ),
          ),
        );
      },
    );
  }
}
