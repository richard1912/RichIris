import 'dart:io' show Platform;
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// Wraps a video widget with zoom + pan controls.
/// - Windows: Ctrl+scroll to zoom, drag to pan, zoom icon → slider, minimap
/// - Android: Pinch to zoom, touch to pan
class ZoomableVideo extends StatefulWidget {
  final Widget child;
  final double minScale;
  final double maxScale;

  const ZoomableVideo({
    super.key,
    required this.child,
    this.minScale = 1.0,
    this.maxScale = 8.0,
  });

  @override
  State<ZoomableVideo> createState() => _ZoomableVideoState();
}

class _ZoomableVideoState extends State<ZoomableVideo> {
  final TransformationController _transformCtrl = TransformationController();
  bool _showSlider = false;
  double _currentScale = 1.0;

  @override
  void dispose() {
    _transformCtrl.dispose();
    super.dispose();
  }

  void _updateScale(double newScale) {
    newScale = newScale.clamp(widget.minScale, widget.maxScale);
    final matrix = _transformCtrl.value;
    final oldScale = matrix.getMaxScaleOnAxis();
    if ((newScale - oldScale).abs() < 0.001) return;

    // Scale around center of viewport
    final ratio = newScale / oldScale;
    final focalPoint = _viewportCenter;
    _scaleAroundPoint(ratio, focalPoint);
  }

  Offset get _viewportCenter {
    final ctx = context;
    final box = ctx.findRenderObject() as RenderBox?;
    if (box == null) return Offset.zero;
    return Offset(box.size.width / 2, box.size.height / 2);
  }

  void _scaleAroundPoint(double ratio, Offset focalPoint) {
    final matrix = _transformCtrl.value.clone();
    // Translate to focal point, scale, translate back
    matrix.translate(focalPoint.dx, focalPoint.dy);
    matrix.scale(ratio, ratio);
    matrix.translate(-focalPoint.dx, -focalPoint.dy);

    // Clamp scale
    final newScale = matrix.getMaxScaleOnAxis();
    if (newScale < widget.minScale || newScale > widget.maxScale) return;

    _transformCtrl.value = matrix;
    _syncScale();
  }

  void _syncScale() {
    setState(() => _currentScale = _transformCtrl.value.getMaxScaleOnAxis());
  }

  void _resetZoom() {
    _transformCtrl.value = Matrix4.identity();
    setState(() {
      _currentScale = 1.0;
      _showSlider = false;
    });
  }

  void _onInteractionUpdate(ScaleUpdateDetails details) {
    _syncScale();
  }

  void _onInteractionEnd(ScaleEndDetails details) {
    final scale = _transformCtrl.value.getMaxScaleOnAxis();
    if (scale <= 1.01) {
      _transformCtrl.value = Matrix4.identity();
    }
    _syncScale();
  }

  void _handlePointerSignal(PointerSignalEvent event) {
    if (event is PointerScrollEvent && _isCtrlPressed(event)) {
      final delta = event.scrollDelta.dy;
      final scale = _transformCtrl.value.getMaxScaleOnAxis();
      // Zoom step proportional to current scale
      final step = scale * 0.1;
      final newScale = delta < 0 ? scale + step : scale - step;
      final clamped = newScale.clamp(widget.minScale, widget.maxScale);

      if (clamped <= 1.01) {
        _resetZoom();
        return;
      }

      final ratio = clamped / scale;
      // Zoom toward cursor position
      final box = context.findRenderObject() as RenderBox?;
      if (box != null) {
        final localPos = box.globalToLocal(event.position);
        _scaleAroundPoint(ratio, localPos);
      }
    }
  }

  bool _isCtrlPressed(PointerScrollEvent event) {
    // Check for Ctrl key via HardwareKeyboard
    final kb = HardwareKeyboard.instance;
    return kb.isControlPressed;
  }

  @override
  Widget build(BuildContext context) {
    final isAndroid = Platform.isAndroid;
    final isZoomed = _currentScale > 1.01;

    return Stack(
      children: [
        // Video with zoom/pan
        Positioned.fill(
          child: GestureDetector(
            onDoubleTap: isZoomed ? _resetZoom : null,
            child: Listener(
              onPointerSignal: isAndroid ? null : _handlePointerSignal,
              child: InteractiveViewer(
                transformationController: _transformCtrl,
                minScale: widget.minScale,
                maxScale: widget.maxScale,
                boundaryMargin: const EdgeInsets.all(double.infinity),
                constrained: true,
                panEnabled: isZoomed,
                scaleEnabled: isAndroid, // Android uses pinch; Windows uses Ctrl+scroll
                onInteractionUpdate: _onInteractionUpdate,
                onInteractionEnd: _onInteractionEnd,
                child: widget.child,
              ),
            ),
          ),
        ),

        // Minimap (top-left) — shown when zoomed
        if (isZoomed) _buildMinimap(),

        // Zoom controls (Windows only) — bottom-right
        if (!isAndroid) _buildZoomControls(),
      ],
    );
  }

  Widget _buildMinimap() {
    return Positioned(
      top: 8,
      left: 8,
      child: IgnorePointer(
        child: ListenableBuilder(
          listenable: _transformCtrl,
          builder: (context, child) {
            final matrix = _transformCtrl.value;
            final scale = matrix.getMaxScaleOnAxis();
            if (scale <= 1.01) return const SizedBox.shrink();

            const mapW = 120.0;
            const mapH = 67.5; // 16:9

            final tx = matrix.getTranslation().x;
            final ty = matrix.getTranslation().y;

            final parentBox = this.context.findRenderObject() as RenderBox?;
            final vw = parentBox?.size.width ?? 400;
            final vh = parentBox?.size.height ?? 225;

            // Viewport rect in content coordinates
            final viewW = vw / scale;
            final viewH = vh / scale;
            final viewX = (vw / 2 - tx) / scale - viewW / 2;
            final viewY = (vh / 2 - ty) / scale - viewH / 2;

            // Map to minimap coordinates
            final rx = (viewX / vw) * mapW;
            final ry = (viewY / vh) * mapH;
            final rw = (viewW / vw) * mapW;
            final rh = (viewH / vh) * mapH;

            return Container(
              width: mapW,
              height: mapH,
              decoration: BoxDecoration(
                color: Colors.black54,
                border: Border.all(color: const Color(0xFF404040), width: 1),
                borderRadius: BorderRadius.circular(4),
              ),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(3),
                child: Stack(
                  children: [
                    // Dark fill representing the full video
                    Container(color: const Color(0xFF1A1A1A)),
                    // Viewport indicator
                    Positioned(
                      left: rx.clamp(0, mapW - 4),
                      top: ry.clamp(0, mapH - 4),
                      child: Container(
                        width: rw.clamp(4, mapW),
                        height: rh.clamp(4, mapH),
                        decoration: BoxDecoration(
                          border: Border.all(
                            color: const Color(0xFF3B82F6),
                            width: 1.5,
                          ),
                          color: const Color(0xFF3B82F6).withValues(alpha: 0.15),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            );
          },
        ),
      ),
    );
  }

  Widget _buildZoomControls() {
    return Positioned(
      bottom: 12,
      right: 12,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          // Slider popup
          if (_showSlider)
            Container(
              margin: const EdgeInsets.only(bottom: 8),
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 12),
              decoration: BoxDecoration(
                color: const Color(0xFF262626).withValues(alpha: 0.95),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: const Color(0xFF404040)),
              ),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    '${_currentScale.toStringAsFixed(1)}x',
                    style: const TextStyle(
                      fontSize: 11,
                      color: Color(0xFFD4D4D4),
                      fontFamily: 'monospace',
                    ),
                  ),
                  const SizedBox(height: 4),
                  SizedBox(
                    height: 140,
                    child: RotatedBox(
                      quarterTurns: -1,
                      child: SliderTheme(
                        data: SliderThemeData(
                          trackHeight: 3,
                          thumbShape: const RoundSliderThumbShape(
                            enabledThumbRadius: 6,
                          ),
                          activeTrackColor: const Color(0xFF3B82F6),
                          inactiveTrackColor: const Color(0xFF404040),
                          thumbColor: const Color(0xFF3B82F6),
                          overlayColor: const Color(0xFF3B82F6).withValues(alpha: 0.2),
                        ),
                        child: Slider(
                          value: _currentScale,
                          min: widget.minScale,
                          max: widget.maxScale,
                          onChanged: _updateScale,
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(height: 4),
                  if (_currentScale > 1.01)
                    GestureDetector(
                      onTap: _resetZoom,
                      child: const Text(
                        'Reset',
                        style: TextStyle(
                          fontSize: 10,
                          color: Color(0xFF3B82F6),
                        ),
                      ),
                    ),
                ],
              ),
            ),

          // Zoom button
          Material(
            color: _currentScale > 1.01
                ? const Color(0xFF3B82F6).withValues(alpha: 0.9)
                : const Color(0xFF262626).withValues(alpha: 0.8),
            borderRadius: BorderRadius.circular(8),
            child: InkWell(
              borderRadius: BorderRadius.circular(8),
              onTap: () => setState(() => _showSlider = !_showSlider),
              child: Container(
                padding: const EdgeInsets.all(8),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: const Color(0xFF404040)),
                ),
                child: Icon(
                  _currentScale > 1.01 ? Icons.zoom_in : Icons.search,
                  size: 20,
                  color: const Color(0xFFD4D4D4),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
