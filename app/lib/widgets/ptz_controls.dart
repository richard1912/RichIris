import 'dart:async';

import 'package:flutter/material.dart';

import '../models/ptz.dart';
import '../services/ptz_api.dart';

const _kAccent = Color(0xFF3B82F6);
const _kMuted = Color(0xFFA3A3A3);
const _kDim = Color(0xFF737373);
const _kError = Color(0xFFEF4444);

/// PTZ panel floated over the fullscreen live view: a direction pad and zoom
/// buttons that move while held and stop on release, a speed slider and the
/// camera's presets.
///
/// Holding a button re-sends `move` once a second. The server (and the camera)
/// stop the move by themselves a few seconds after the last one, so a lost
/// release, a closed app or a dropped connection cannot leave it turning.
class PtzControls extends StatefulWidget {
  final int cameraId;
  final PtzApi ptzApi;

  const PtzControls({super.key, required this.cameraId, required this.ptzApi});

  @override
  State<PtzControls> createState() => _PtzControlsState();
}

class _PtzControlsState extends State<PtzControls> {
  static const _heartbeat = Duration(seconds: 1);
  static const _btn =
      60.0; // finger-sized: smaller pads were hard to hit on a phone
  // A tap shorter than this still moves for this long, so it is a visible
  // nudge. The zoom motor needs about half a second before it moves at all.
  static const _minMove = Duration(milliseconds: 350);
  static const _minZoom = Duration(milliseconds: 700);

  int _speed = 3;
  String? _held;
  Timer? _heartbeatTimer;
  // Requests go out one at a time, in order, so a `stop` can never overtake
  // the `move` sent just before it.
  Future<void> _queue = Future.value();

  PtzStatus? _status;
  List<PtzPreset> _presets = const [];
  bool _presetsOpen = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _refreshStatus();
    _loadPresets();
  }

  @override
  void dispose() {
    _heartbeatTimer?.cancel();
    if (_held != null || _sent != null) {
      // Leaving mid-move (camera switched, panel closed): stop, best effort.
      widget.ptzApi.stop(widget.cameraId).catchError((_) {});
    }
    super.dispose();
  }

  void _enqueue(Future<void> Function() request) {
    _queue = _queue.then((_) async {
      try {
        await request();
        if (mounted && _error != null) setState(() => _error = null);
      } catch (e) {
        if (mounted) setState(() => _error = PtzApi.describeError(e));
      }
    });
  }

  // -- hold-to-move --------------------------------------------------------
  // Latest intent wins. `_want` is what the user is holding now (null = stop),
  // `_sent` what the camera was last told; one pump loop closes the gap, one
  // request at a time. Presses made while a request is in flight are NOT
  // queued behind it: a FIFO queue replayed every stale move and stop in
  // order, so on a slow link the camera lagged seconds behind the fingers and
  // a zoom press played out pans tapped earlier.
  String? _want;
  String? _sent;
  bool _keepAlive = false;
  bool _pumping = false;
  DateTime _moveStart = DateTime.now();

  void _startHold(String direction) {
    _heartbeatTimer?.cancel();
    setState(() => _held = direction);
    _want = direction;
    _pump();
    _heartbeatTimer = Timer.periodic(_heartbeat, (_) {
      _keepAlive = true;
      _pump();
    });
  }

  void _endHold() {
    if (_held == null) return;
    _heartbeatTimer?.cancel();
    _heartbeatTimer = null;
    setState(() => _held = null);
    _want = null;
    _pump();
  }

  void _stop() {
    _heartbeatTimer?.cancel();
    _heartbeatTimer = null;
    if (_held != null) setState(() => _held = null);
    _want = null;
    // Send a stop even if we believe nothing is moving.
    _sent ??= 'stop';
    _pump();
  }

  Future<void> _pump() async {
    if (_pumping) return;
    _pumping = true;
    try {
      while (mounted) {
        final want = _want;
        if (want != null && (want != _sent || _keepAlive)) {
          _keepAlive = false;
          if (want != _sent) _moveStart = DateTime.now();
          _sent = want;
          await _guard(
            () => widget.ptzApi.move(widget.cameraId, want, speed: _speed),
          );
        } else if (want == null && _sent != null) {
          final least = _sent!.startsWith('zoom') ? _minZoom : _minMove;
          final left = least - DateTime.now().difference(_moveStart);
          if (_sent != 'stop' && left > Duration.zero) {
            await Future.delayed(left);
            if (_want != null) continue; // pressed again meanwhile
          }
          _sent = null;
          await _guard(() => widget.ptzApi.stop(widget.cameraId));
          if (_want == null && mounted) await _guard(_fetchStatus);
        } else {
          break;
        }
      }
    } finally {
      _pumping = false;
    }
  }

  Future<void> _guard(Future<void> Function() request) async {
    try {
      await request();
      if (mounted && _error != null) setState(() => _error = null);
    } catch (e) {
      if (mounted) setState(() => _error = PtzApi.describeError(e));
    }
  }

  Future<void> _fetchStatus() async {
    final status = await widget.ptzApi.status(widget.cameraId);
    if (mounted) setState(() => _status = status);
  }

  void _refreshStatus() => _enqueue(_fetchStatus);

  Future<void> _loadPresets() async {
    try {
      final presets = await widget.ptzApi.presets(widget.cameraId);
      if (mounted) setState(() => _presets = presets);
    } catch (e) {
      if (mounted) setState(() => _error = PtzApi.describeError(e));
    }
  }

  void _gotoPreset(PtzPreset preset) {
    _enqueue(() => widget.ptzApi.gotoPreset(widget.cameraId, preset.id));
    // The move takes a moment; read the position once it has had time to land.
    Future.delayed(const Duration(seconds: 3), () {
      if (mounted) _refreshStatus();
    });
  }

  Future<void> _savePreset() async {
    final controller = TextEditingController();
    final name = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Save Preset'),
        content: TextField(
          controller: controller,
          autofocus: true,
          maxLength: 40,
          decoration: const InputDecoration(
            hintText: 'Preset name',
            border: OutlineInputBorder(),
          ),
          onSubmitted: (v) => Navigator.pop(ctx, v),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, controller.text),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    if (name == null || !mounted) return;
    _enqueue(() async {
      await widget.ptzApi.savePreset(widget.cameraId, name);
      await _loadPresets();
      if (mounted) setState(() => _presetsOpen = true);
    });
  }

  Future<void> _deletePreset(PtzPreset preset) async {
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete Preset'),
        content: Text('Delete "${preset.label}"?'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Delete', style: TextStyle(color: _kError)),
          ),
        ],
      ),
    );
    if (confirm != true || !mounted) return;
    _enqueue(() async {
      await widget.ptzApi.deletePreset(widget.cameraId, preset.id);
      await _loadPresets();
    });
  }

  // -- widgets --------------------------------------------------------------

  /// One tap = one autofocus pass. Lit while the request is out.
  bool _focusing = false;

  Future<void> _focus() async {
    if (_focusing) return;
    setState(() => _focusing = true);
    await _guard(() => widget.ptzApi.focus(widget.cameraId));
    // The pass itself takes a second or two after the camera accepts it.
    await Future.delayed(const Duration(milliseconds: 1500));
    if (mounted) setState(() => _focusing = false);
  }

  Widget _focusButton() {
    return Tooltip(
      message: 'Autofocus',
      child: Padding(
        padding: const EdgeInsets.all(1),
        child: Material(
          color: _focusing ? const Color(0xFF3B82F6) : const Color(0xFF262626),
          borderRadius: BorderRadius.circular(6),
          clipBehavior: Clip.antiAlias,
          child: InkWell(
            onTap: _focus,
            child: SizedBox(
              width: _btn,
              height: _btn,
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(
                    Icons.center_focus_strong,
                    size: 26,
                    color: _focusing ? Colors.white : _kMuted,
                  ),
                  Text(
                    'Focus',
                    style: TextStyle(
                      fontSize: 10,
                      color: _focusing ? Colors.white : _kMuted,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  /// A button that moves the camera for as long as it is pressed.
  Widget _holdButton(String direction, IconData icon, String tooltip) {
    final active = _held == direction;
    return Tooltip(
      message: tooltip,
      child: Listener(
        onPointerDown: (_) => _startHold(direction),
        onPointerUp: (_) => _endHold(),
        onPointerCancel: (_) => _endHold(),
        child: Container(
          width: _btn,
          height: _btn,
          margin: const EdgeInsets.all(1),
          decoration: BoxDecoration(
            color: active ? _kAccent : const Color(0xFF262626),
            borderRadius: BorderRadius.circular(6),
          ),
          child: Icon(icon, size: 32, color: active ? Colors.white : _kMuted),
        ),
      ),
    );
  }

  Widget _stopButton() {
    return Tooltip(
      message: 'Stop',
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: _stop,
          borderRadius: BorderRadius.circular(6),
          child: const SizedBox(
            width: _btn + 2,
            height: _btn + 2,
            child: Icon(Icons.stop, size: 30, color: _kDim),
          ),
        ),
      ),
    );
  }

  Widget _buildPad() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            _holdButton('up_left', Icons.north_west, 'Up left'),
            _holdButton('up', Icons.north, 'Up'),
            _holdButton('up_right', Icons.north_east, 'Up right'),
          ],
        ),
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            _holdButton('left', Icons.west, 'Left'),
            _stopButton(),
            _holdButton('right', Icons.east, 'Right'),
          ],
        ),
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            _holdButton('down_left', Icons.south_west, 'Down left'),
            _holdButton('down', Icons.south, 'Down'),
            _holdButton('down_right', Icons.south_east, 'Down right'),
          ],
        ),
      ],
    );
  }

  Widget _buildSpeed() {
    return Row(
      children: [
        const Text('Speed', style: TextStyle(fontSize: 11, color: _kDim)),
        Expanded(
          child: SliderTheme(
            data: SliderTheme.of(context).copyWith(
              trackHeight: 2,
              thumbShape: const RoundSliderThumbShape(enabledThumbRadius: 6),
              overlayShape: const RoundSliderOverlayShape(overlayRadius: 12),
              activeTrackColor: _kAccent,
              thumbColor: _kAccent,
              inactiveTrackColor: const Color(0xFF404040),
            ),
            child: Slider(
              value: _speed.toDouble(),
              min: 1,
              max: 8,
              divisions: 7,
              onChanged: (v) => setState(() => _speed = v.round()),
            ),
          ),
        ),
        SizedBox(
          width: 10,
          child: Text(
            '$_speed',
            style: const TextStyle(fontSize: 11, color: _kMuted),
          ),
        ),
      ],
    );
  }

  Widget _buildPresets() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: InkWell(
                onTap: () {
                  setState(() => _presetsOpen = !_presetsOpen);
                  // Presets can be saved elsewhere (another client, the
                  // camera's own page), so re-read them on every expand
                  // instead of trusting the list from when the panel opened.
                  if (_presetsOpen) _loadPresets();
                },
                borderRadius: BorderRadius.circular(4),
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 12),
                  child: Row(
                    children: [
                      Icon(
                        _presetsOpen ? Icons.expand_more : Icons.chevron_right,
                        size: 16,
                        color: _kMuted,
                      ),
                      const SizedBox(width: 2),
                      Text(
                        'Presets (${_presets.length})',
                        style: const TextStyle(fontSize: 13, color: _kMuted),
                      ),
                    ],
                  ),
                ),
              ),
            ),
            Tooltip(
              message: 'Save current position as a preset',
              child: InkWell(
                onTap: _savePreset,
                borderRadius: BorderRadius.circular(4),
                child: const Padding(
                  padding: EdgeInsets.all(12),
                  child: Icon(
                    Icons.add_location_alt_outlined,
                    size: 20,
                    color: _kMuted,
                  ),
                ),
              ),
            ),
          ],
        ),
        if (_presetsOpen)
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 176),
            child: _presets.isEmpty
                ? const Padding(
                    padding: EdgeInsets.symmetric(vertical: 4),
                    child: Text(
                      'No presets saved',
                      style: TextStyle(fontSize: 11, color: _kDim),
                    ),
                  )
                : ListView(
                    shrinkWrap: true,
                    padding: EdgeInsets.zero,
                    children: [for (final p in _presets) _presetRow(p)],
                  ),
          ),
      ],
    );
  }

  Widget _presetRow(PtzPreset preset) {
    return Row(
      children: [
        Expanded(
          child: Tooltip(
            message: 'Go to ${preset.label}',
            child: InkWell(
              onTap: () => _gotoPreset(preset),
              borderRadius: BorderRadius.circular(4),
              child: Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 4,
                  vertical: 13,
                ),
                child: Text(
                  preset.label,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontSize: 14, color: Colors.white),
                ),
              ),
            ),
          ),
        ),
        Tooltip(
          message: 'Delete preset',
          child: InkWell(
            onTap: () => _deletePreset(preset),
            borderRadius: BorderRadius.circular(4),
            child: const Padding(
              padding: EdgeInsets.all(12),
              child: Icon(Icons.delete_outline, size: 20, color: _kDim),
            ),
          ),
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final s = _status;
    return Material(
      color: Colors.transparent,
      child: Container(
        width: (_btn + 2) * 4 + 8 + 16,
        padding: const EdgeInsets.all(8),
        decoration: BoxDecoration(
          color: const Color(0xFF171717).withValues(alpha: 0.85),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                _buildPad(),
                Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    _holdButton('zoom_in', Icons.zoom_in, 'Zoom in'),
                    _focusButton(),
                    _holdButton('zoom_out', Icons.zoom_out, 'Zoom out'),
                  ],
                ),
              ],
            ),
            const SizedBox(height: 4),
            _buildSpeed(),
            if (s != null)
              Text(
                'Pan ${s.pan.toStringAsFixed(1)}  Tilt ${s.tilt.toStringAsFixed(1)}  ${s.zoom.toStringAsFixed(1)}x',
                style: const TextStyle(
                  fontSize: 10,
                  color: _kDim,
                  fontFamily: 'monospace',
                ),
              ),
            _buildPresets(),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  _error!,
                  style: const TextStyle(fontSize: 11, color: _kError),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
