import 'dart:async';
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';
import '../models/camera.dart';
import '../models/clip_export.dart';
import '../services/clip_api.dart';
import 'datetime_picker_dialog.dart';

class ExportClipWizardDialog extends StatefulWidget {
  final List<Camera> cameras;
  final ClipApi clipApi;
  final int? initialCameraId;

  const ExportClipWizardDialog({
    super.key,
    required this.cameras,
    required this.clipApi,
    this.initialCameraId,
  });

  @override
  State<ExportClipWizardDialog> createState() => _ExportClipWizardDialogState();
}

class _ExportClipWizardDialogState extends State<ExportClipWizardDialog> {
  final Set<int> _selectedCameraIds = {};
  bool _join = false;
  late String _startDate;
  int _startH = 0, _startM = 0, _startS = 0;
  late String _endDate;
  late int _endH, _endM, _endS;
  bool _exporting = false;
  String? _error;

  // Clips list state
  bool _exported = false;
  final Set<int> _exportedClipIds = {};
  List<ClipExport> _clips = [];
  Timer? _clipPollTimer;

  static const _accent = Color(0xFF22C55E);
  static const _blue = Color(0xFF3B82F6);
  static const _bg = Color(0xFF191919);
  static const _cardBg = Color(0xFF222222);
  static const _dimText = Color(0xFF737373);

  @override
  void initState() {
    super.initState();
    final enabledCameras = widget.cameras.where((c) => c.enabled).toList();
    final initial = widget.initialCameraId ??
        (enabledCameras.isNotEmpty ? enabledCameras.first.id : widget.cameras.first.id);
    _selectedCameraIds.add(initial);

    final now = DateTime.now();
    _startDate = _fmtDateISO(now);
    _endDate = _startDate;
    _endH = now.hour;
    _endM = now.minute;
    _endS = now.second;
  }

  @override
  void dispose() {
    _clipPollTimer?.cancel();
    super.dispose();
  }

  String _cameraName(int id) =>
      widget.cameras.firstWhere((c) => c.id == id, orElse: () => widget.cameras.first).name;

  String _fmtDateISO(DateTime dt) =>
      '${dt.year}-${dt.month.toString().padLeft(2, '0')}-${dt.day.toString().padLeft(2, '0')}';

  String _fmtDateDisplay(String iso) {
    final p = iso.split('-');
    const mo = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return '${mo[int.parse(p[1])]} ${int.parse(p[2])}, ${p[0]}';
  }

  String _fmtTime(int h, int m, int s) =>
      '${h.toString().padLeft(2, '0')}:${m.toString().padLeft(2, '0')}:${s.toString().padLeft(2, '0')}';

  String _fmtClipTime(DateTime dt) =>
      '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';

  String _buildISO(String date, int h, int m, int s) =>
      '${date}T${h.toString().padLeft(2, '0')}:${m.toString().padLeft(2, '0')}:${s.toString().padLeft(2, '0')}';

  Future<void> _pickDate({required bool isStart}) async {
    final current = isStart ? _startDate : _endDate;
    final p = current.split('-');
    final dt = DateTime(int.parse(p[0]), int.parse(p[1]), int.parse(p[2]));
    final picked = await showDatePicker(
      context: context,
      initialDate: dt,
      firstDate: DateTime(2024),
      lastDate: DateTime.now().add(const Duration(days: 1)),
      builder: (ctx, child) => Theme(
        data: Theme.of(ctx).copyWith(
          colorScheme: const ColorScheme.dark(primary: _blue, surface: Color(0xFF1E1E1E), onSurface: Colors.white),
        ),
        child: child!,
      ),
    );
    if (picked != null && mounted) {
      setState(() {
        final dateStr = _fmtDateISO(picked);
        if (isStart) {
          _startDate = dateStr;
        } else {
          _endDate = dateStr;
        }
        _error = null;
      });
    }
  }

  Future<void> _pickTime({required bool isStart}) async {
    final result = await showDateTimePickerDialog(
      context,
      initialDate: isStart ? _startDate : _endDate,
      initialHour: isStart ? _startH : _endH,
      initialMinute: isStart ? _startM : _endM,
      initialSecond: isStart ? _startS : _endS,
    );
    if (result != null && mounted) {
      setState(() {
        if (isStart) {
          _startDate = result['date'] as String;
          _startH = result['hour'] as int;
          _startM = result['minute'] as int;
          _startS = result['second'] as int;
        } else {
          _endDate = result['date'] as String;
          _endH = result['hour'] as int;
          _endM = result['minute'] as int;
          _endS = result['second'] as int;
        }
        _error = null;
      });
    }
  }

  Future<void> _export() async {
    if (_selectedCameraIds.isEmpty) {
      setState(() => _error = 'Select at least one camera');
      return;
    }

    final startISO = _buildISO(_startDate, _startH, _startM, _startS);
    final endISO = _buildISO(_endDate, _endH, _endM, _endS);

    final startDt = DateTime.parse(startISO);
    final endDt = DateTime.parse(endISO);
    if (!endDt.isAfter(startDt)) {
      setState(() => _error = 'End time must be after start time');
      return;
    }

    // Preserve camera display order when sending IDs
    final ids = widget.cameras
        .where((c) => _selectedCameraIds.contains(c.id))
        .map((c) => c.id)
        .toList();
    final join = _join && ids.length > 1;

    setState(() { _exporting = true; _error = null; });
    try {
      final created = await widget.clipApi.createComposite(ids, startISO, endISO, join: join);
      if (mounted) {
        _exportedClipIds
          ..clear()
          ..addAll(created.map((c) => c.id));
        setState(() {
          _exporting = false;
          _exported = true;
        });
        _fetchClips();
        _startClipPolling();
      }
    } catch (err) {
      if (mounted) setState(() { _error = 'Export failed: $err'; _exporting = false; });
    }
  }

  // --- Clips list ---

  Future<void> _fetchClips() async {
    try {
      final all = await widget.clipApi.fetchAll();
      final clips = all.where((c) => _exportedClipIds.contains(c.id)).toList();
      if (mounted) setState(() => _clips = clips);
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
      _exportedClipIds.remove(clip.id);
      _fetchClips();
    } catch (err) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Delete failed: $err')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final enabledCameras = widget.cameras.where((c) => c.enabled).toList();
    final multi = _selectedCameraIds.length > 1;

    return Dialog(
      backgroundColor: _bg,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      insetPadding: const EdgeInsets.symmetric(horizontal: 32, vertical: 24),
      child: IntrinsicWidth(
        child: ConstrainedBox(
          constraints: const BoxConstraints(minWidth: 320, maxWidth: 400),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(24, 20, 24, 16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                // Title
                Row(
                  children: [
                    const Icon(Icons.content_cut, size: 18, color: _accent),
                    const SizedBox(width: 8),
                    const Text('Export Clip', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600, color: Colors.white)),
                  ],
                ),
                const SizedBox(height: 20),

                // Camera multi-selector
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    const Text('Cameras', style: TextStyle(fontSize: 11, color: _dimText)),
                    if (!_exported && enabledCameras.length > 1)
                      GestureDetector(
                        onTap: () => setState(() {
                          if (_selectedCameraIds.length == enabledCameras.length) {
                            _selectedCameraIds
                              ..clear()
                              ..add(enabledCameras.first.id);
                          } else {
                            _selectedCameraIds
                              ..clear()
                              ..addAll(enabledCameras.map((c) => c.id));
                          }
                          _error = null;
                        }),
                        child: Text(
                          _selectedCameraIds.length == enabledCameras.length ? 'Clear' : 'Select all',
                          style: const TextStyle(fontSize: 11, color: _blue),
                        ),
                      ),
                  ],
                ),
                const SizedBox(height: 6),
                Container(
                  decoration: BoxDecoration(
                    color: _cardBg,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxHeight: 168),
                    child: ListView(
                      shrinkWrap: true,
                      padding: const EdgeInsets.symmetric(vertical: 4),
                      children: enabledCameras.map(_buildCameraCheckRow).toList(),
                    ),
                  ),
                ),
                const SizedBox(height: 12),

                // Join toggle (only meaningful with >1 camera)
                if (multi) ...[
                  GestureDetector(
                    onTap: _exported ? null : () => setState(() => _join = !_join),
                    child: Container(
                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                      decoration: BoxDecoration(
                        color: _cardBg,
                        borderRadius: BorderRadius.circular(8),
                      ),
                      child: Row(
                        children: [
                          Icon(Icons.grid_view_rounded, size: 16, color: _join ? _accent : _dimText),
                          const SizedBox(width: 10),
                          const Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text('Join into one video', style: TextStyle(fontSize: 13, color: Color(0xFFD4D4D4))),
                                SizedBox(height: 2),
                                Text('Synced side-by-side grid', style: TextStyle(fontSize: 10, color: _dimText)),
                              ],
                            ),
                          ),
                          Switch(
                            value: _join,
                            onChanged: _exported ? null : (v) => setState(() => _join = v),
                            activeThumbColor: _accent,
                            activeTrackColor: _accent.withValues(alpha: 0.4),
                            inactiveThumbColor: _dimText,
                            inactiveTrackColor: const Color(0xFF333333),
                            materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                          ),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 12),
                ],

                // Start time
                const Text('Start', style: TextStyle(fontSize: 11, color: _dimText)),
                const SizedBox(height: 6),
                _buildDateTimeRow(
                  date: _startDate,
                  time: _fmtTime(_startH, _startM, _startS),
                  onPickDate: _exported ? null : () => _pickDate(isStart: true),
                  onPickTime: _exported ? null : () => _pickTime(isStart: true),
                ),
                const SizedBox(height: 12),

                // End time
                const Text('End', style: TextStyle(fontSize: 11, color: _dimText)),
                const SizedBox(height: 6),
                _buildDateTimeRow(
                  date: _endDate,
                  time: _fmtTime(_endH, _endM, _endS),
                  onPickDate: _exported ? null : () => _pickDate(isStart: false),
                  onPickTime: _exported ? null : () => _pickTime(isStart: false),
                ),

                // Error
                if (_error != null) ...[
                  const SizedBox(height: 12),
                  Text(_error!, style: const TextStyle(fontSize: 11, color: Color(0xFFEF4444))),
                ],

                // Clips list (shown after export)
                if (_exported && _clips.isNotEmpty) ...[
                  const SizedBox(height: 16),
                  const Text('Clips', style: TextStyle(fontSize: 11, color: _dimText)),
                  const SizedBox(height: 6),
                  ConstrainedBox(
                    constraints: const BoxConstraints(maxHeight: 160),
                    child: ListView.builder(
                      shrinkWrap: true,
                      itemCount: _clips.length,
                      itemBuilder: (_, i) => _buildClipRow(_clips[i]),
                    ),
                  ),
                ],

                const SizedBox(height: 20),

                // Buttons
                if (!_exported)
                  Row(
                    children: [
                      Expanded(
                        child: SizedBox(
                          height: 38,
                          child: OutlinedButton(
                            onPressed: _exporting ? null : () => Navigator.pop(context),
                            style: OutlinedButton.styleFrom(
                              foregroundColor: _dimText,
                              side: const BorderSide(color: Color(0xFF333333)),
                              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                            ),
                            child: const Text('Cancel'),
                          ),
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: SizedBox(
                          height: 38,
                          child: ElevatedButton(
                            onPressed: _exporting ? null : _export,
                            style: ElevatedButton.styleFrom(
                              backgroundColor: _accent,
                              disabledBackgroundColor: _accent.withValues(alpha: 0.5),
                              foregroundColor: Colors.white,
                              elevation: 0,
                              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                            ),
                            child: _exporting
                                ? const SizedBox(
                                    width: 16, height: 16,
                                    child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white),
                                  )
                                : Text(_exportButtonLabel(multi), style: const TextStyle(fontWeight: FontWeight.w600)),
                          ),
                        ),
                      ),
                    ],
                  ),
                if (_exported)
                  Row(
                    children: [
                      Expanded(
                        child: SizedBox(
                          height: 38,
                          child: OutlinedButton(
                            onPressed: () {
                              _stopClipPolling();
                              setState(() {
                                _exported = false;
                                _clips = [];
                                _exportedClipIds.clear();
                                _error = null;
                              });
                            },
                            style: OutlinedButton.styleFrom(
                              foregroundColor: _blue,
                              side: const BorderSide(color: Color(0xFF333333)),
                              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                            ),
                            child: const Text('New Export'),
                          ),
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: SizedBox(
                          height: 38,
                          child: ElevatedButton(
                            onPressed: () {
                              _stopClipPolling();
                              Navigator.pop(context, true);
                            },
                            style: ElevatedButton.styleFrom(
                              backgroundColor: _accent,
                              foregroundColor: Colors.white,
                              elevation: 0,
                              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                            ),
                            child: const Text('Done', style: TextStyle(fontWeight: FontWeight.w600)),
                          ),
                        ),
                      ),
                    ],
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  String _exportButtonLabel(bool multi) {
    if (!multi) return 'Export';
    return _join ? 'Export Grid' : 'Export ${_selectedCameraIds.length}';
  }

  Widget _buildCameraCheckRow(Camera cam) {
    final selected = _selectedCameraIds.contains(cam.id);
    return InkWell(
      onTap: _exported
          ? null
          : () => setState(() {
                if (selected) {
                  // Keep at least one selected
                  if (_selectedCameraIds.length > 1) _selectedCameraIds.remove(cam.id);
                } else {
                  _selectedCameraIds.add(cam.id);
                }
                _error = null;
              }),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
        child: Row(
          children: [
            Container(
              width: 16,
              height: 16,
              decoration: BoxDecoration(
                color: selected ? _accent : Colors.transparent,
                borderRadius: BorderRadius.circular(4),
                border: Border.all(color: selected ? _accent : const Color(0xFF555555), width: 1.5),
              ),
              child: selected ? const Icon(Icons.check, size: 12, color: Colors.white) : null,
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                cam.name,
                style: TextStyle(fontSize: 13, color: selected ? Colors.white : const Color(0xFFA3A3A3)),
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildClipRow(ClipExport clip) {
    final startDt = DateTime.parse(clip.startTime);
    final endDt = DateTime.parse(clip.endTime);
    final timeStr = '${_fmtClipTime(startDt)} - ${_fmtClipTime(endDt)}';
    final dateStr = '${startDt.month}/${startDt.day}';
    final label = clip.isGrid
        ? 'Grid · ${clip.cameraIds?.length ?? 0} cam'
        : _cameraName(clip.cameraId);

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
        statusText = clip.isGrid ? 'Composing' : 'Processing';
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
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
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
          if (clip.isGrid) ...[
            const Icon(Icons.grid_view_rounded, size: 11, color: Color(0xFF22C55E)),
            const SizedBox(width: 4),
          ],
          // Label + time range
          Expanded(
            child: Text(
              '$label  ·  $dateStr $timeStr',
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

  Widget _buildDateTimeRow({
    required String date,
    required String time,
    required VoidCallback? onPickDate,
    required VoidCallback? onPickTime,
  }) {
    final dimmed = onPickDate == null;
    return Row(
      children: [
        Expanded(
          child: GestureDetector(
            onTap: onPickDate,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              decoration: BoxDecoration(
                color: _cardBg,
                borderRadius: BorderRadius.circular(8),
              ),
              child: Row(
                children: [
                  Icon(Icons.calendar_today_rounded, size: 14, color: dimmed ? _dimText : _blue),
                  const SizedBox(width: 8),
                  Text(_fmtDateDisplay(date), style: TextStyle(fontSize: 13, color: dimmed ? _dimText : const Color(0xFFD4D4D4))),
                ],
              ),
            ),
          ),
        ),
        const SizedBox(width: 8),
        GestureDetector(
          onTap: onPickTime,
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            decoration: BoxDecoration(
              color: _cardBg,
              borderRadius: BorderRadius.circular(8),
            ),
            child: Row(
              children: [
                Icon(Icons.access_time, size: 14, color: dimmed ? _dimText : _blue),
                const SizedBox(width: 8),
                Text(time, style: TextStyle(fontSize: 13, color: dimmed ? _dimText : const Color(0xFFD4D4D4))),
              ],
            ),
          ),
        ),
      ],
    );
  }
}
