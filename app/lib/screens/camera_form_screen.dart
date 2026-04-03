import 'package:flutter/material.dart';
import '../models/camera.dart';
import '../services/camera_api.dart';
import '../utils/detection_colors.dart';

class _ScriptEntry {
  final TextEditingController onCtrl;
  final TextEditingController offCtrl;
  bool persons;
  bool vehicles;
  bool animals;
  bool motionOnly;

  _ScriptEntry({
    required this.onCtrl,
    required this.offCtrl,
    this.persons = true,
    this.vehicles = true,
    this.animals = true,
    this.motionOnly = true,
  });
}

class CameraFormScreen extends StatefulWidget {
  final CameraApi cameraApi;
  final Camera? camera;

  const CameraFormScreen({super.key, required this.cameraApi, this.camera});

  @override
  State<CameraFormScreen> createState() => _CameraFormScreenState();
}

class _CameraFormScreenState extends State<CameraFormScreen> {
  final _formKey = GlobalKey<FormState>();
  late final TextEditingController _nameCtrl;
  late final TextEditingController _rtspCtrl;
  late final TextEditingController _subStreamCtrl;
  late final TextEditingController _usernameCtrl;
  late final TextEditingController _passwordCtrl;
  late bool _enabled;
  late int _rotation;
  late int _motionSensitivity;
  late List<_ScriptEntry> _scriptEntries;
  late bool _aiDetection;
  late bool _aiDetectPersons;
  late bool _aiDetectVehicles;
  late bool _aiDetectAnimals;
  late int _aiConfidenceThreshold;
  bool _saving = false;
  String? _error;
  bool _obscurePassword = true;

  bool get isEditing => widget.camera != null;

  /// Parse credentials from an RTSP URL like rtsp://user:pass@host/path
  /// Returns (username, password, urlWithoutCreds).
  static (String, String, String) _parseRtspCreds(String url) {
    final uri = Uri.tryParse(url);
    if (uri == null || uri.userInfo.isEmpty) return ('', '', url);
    final parts = uri.userInfo.split(':');
    final user = parts.first;
    final pass = parts.length > 1 ? parts.sublist(1).join(':') : '';
    final clean = url.replaceFirst('${uri.userInfo}@', '');
    return (user, pass, clean);
  }

  /// Inject credentials into an RTSP URL.
  static String _injectCreds(String url, String user, String pass) {
    if (user.isEmpty) return url;
    final creds = pass.isEmpty ? user : '$user:$pass';
    final re = RegExp(r'^(rtsp://)(.*)$', caseSensitive: false);
    final m = re.firstMatch(url);
    if (m == null) return url;
    return '${m.group(1)}$creds@${m.group(2)}';
  }

  @override
  void initState() {
    super.initState();
    final (user, pass, mainUrl) =
        _parseRtspCreds(widget.camera?.rtspUrl ?? '');
    final (_, _, subUrl) =
        _parseRtspCreds(widget.camera?.subStreamUrl ?? '');
    _nameCtrl = TextEditingController(text: widget.camera?.name ?? '');
    _rtspCtrl = TextEditingController(text: mainUrl);
    _subStreamCtrl = TextEditingController(text: subUrl);
    _usernameCtrl = TextEditingController(text: user);
    _passwordCtrl = TextEditingController(text: pass);
    _enabled = widget.camera?.enabled ?? true;
    _rotation = widget.camera?.rotation ?? 0;
    _motionSensitivity = widget.camera?.motionSensitivity ?? 0;
    // Build script entries from motionScripts list
    final scripts = widget.camera?.motionScripts ?? [];
    _scriptEntries = scripts.map((s) => _ScriptEntry(
      onCtrl: TextEditingController(text: s.on ?? ''),
      offCtrl: TextEditingController(text: s.off ?? ''),
      persons: s.persons,
      vehicles: s.vehicles,
      animals: s.animals,
      motionOnly: s.motionOnly,
    )).toList();
    _aiDetection = widget.camera?.aiDetection ?? false;
    _aiDetectPersons = widget.camera?.aiDetectPersons ?? true;
    _aiDetectVehicles = widget.camera?.aiDetectVehicles ?? false;
    _aiDetectAnimals = widget.camera?.aiDetectAnimals ?? false;
    _aiConfidenceThreshold = widget.camera?.aiConfidenceThreshold ?? 50;
  }

  @override
  void dispose() {
    _nameCtrl.dispose();
    _rtspCtrl.dispose();
    _subStreamCtrl.dispose();
    _usernameCtrl.dispose();
    _passwordCtrl.dispose();
    for (final e in _scriptEntries) {
      e.onCtrl.dispose();
      e.offCtrl.dispose();
    }
    super.dispose();
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      final user = _usernameCtrl.text.trim();
      final pass = _passwordCtrl.text.trim();
      final mainUrl = _injectCreds(_rtspCtrl.text.trim(), user, pass);
      final sub = _subStreamCtrl.text.trim();
      final subUrl = sub.isNotEmpty ? _injectCreds(sub, user, pass) : null;

      if (isEditing) {
        // Build motion_scripts from entries (filter out empty ones)
        final scriptsList = _scriptEntries
            .where((e) => e.onCtrl.text.trim().isNotEmpty || e.offCtrl.text.trim().isNotEmpty)
            .map((e) {
              return <String, dynamic>{
                'on': e.onCtrl.text.trim().isEmpty ? null : e.onCtrl.text.trim(),
                'off': e.offCtrl.text.trim().isEmpty ? null : e.offCtrl.text.trim(),
                'persons': e.persons,
                'vehicles': e.vehicles,
                'animals': e.animals,
                'motion_only': e.motionOnly,
              };
            })
            .toList();
        final data = <String, dynamic>{
          'name': _nameCtrl.text.trim(),
          'rtsp_url': mainUrl,
          'sub_stream_url': subUrl,
          'enabled': _enabled,
          'rotation': _rotation,
          'motion_sensitivity': _motionSensitivity,
          'motion_scripts': scriptsList,
          'ai_detection': _aiDetection,
          'ai_detect_persons': _aiDetectPersons,
          'ai_detect_vehicles': _aiDetectVehicles,
          'ai_detect_animals': _aiDetectAnimals,
          'ai_confidence_threshold': _aiConfidenceThreshold,
        };
        await widget.cameraApi.update(widget.camera!.id, data);
      } else {
        await widget.cameraApi.create(
          name: _nameCtrl.text.trim(),
          rtspUrl: mainUrl,
          subStreamUrl: subUrl,
          enabled: _enabled,
          rotation: _rotation,
        );
      }
      if (mounted) Navigator.of(context).pop();
    } catch (e) {
      setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _delete() async {
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete Camera?'),
        content: Text('Delete "${widget.camera!.name}"? Recording files will be preserved.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: TextButton.styleFrom(foregroundColor: Colors.red),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirm != true) return;
    try {
      await widget.cameraApi.delete(widget.camera!.id);
      if (mounted) Navigator.of(context).pop();
    } catch (e) {
      setState(() => _error = e.toString());
    }
  }

  Widget _buildCategoryToggle(String label, Color color, bool value, ValueChanged<bool> onChanged) {
    return Padding(
      padding: const EdgeInsets.only(left: 16),
      child: SwitchListTile(
        title: Row(
          children: [
            Container(
              width: 12,
              height: 12,
              decoration: BoxDecoration(
                color: color,
                borderRadius: BorderRadius.circular(3),
              ),
            ),
            const SizedBox(width: 8),
            Text(label),
          ],
        ),
        value: value,
        onChanged: onChanged,
        contentPadding: EdgeInsets.zero,
        activeColor: color,
      ),
    );
  }

  Widget _buildScriptsList() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const Text('Scripts', style: TextStyle(fontSize: 14, fontWeight: FontWeight.w500)),
            const Spacer(),
            TextButton.icon(
              icon: const Icon(Icons.add, size: 18),
              label: const Text('Add Script'),
              onPressed: () {
                setState(() {
                  _scriptEntries.add(_ScriptEntry(
                    onCtrl: TextEditingController(),
                    offCtrl: TextEditingController(),
                  ));
                });
              },
            ),
          ],
        ),
        if (_scriptEntries.isEmpty)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text(
              'No scripts configured. Add a script to run on motion/detection events.',
              style: TextStyle(fontSize: 12, color: Colors.grey[500]),
            ),
          ),
        for (int i = 0; i < _scriptEntries.length; i++)
          _buildScriptEntry(i),
      ],
    );
  }

  Widget _buildScriptEntry(int index) {
    final entry = _scriptEntries[index];
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Text('Script ${index + 1}',
                    style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w500)),
                const Spacer(),
                IconButton(
                  icon: const Icon(Icons.delete_outline, size: 20),
                  padding: EdgeInsets.zero,
                  constraints: const BoxConstraints(),
                  color: Colors.red[400],
                  onPressed: () {
                    setState(() {
                      _scriptEntries[index].onCtrl.dispose();
                      _scriptEntries[index].offCtrl.dispose();
                      _scriptEntries.removeAt(index);
                    });
                  },
                ),
              ],
            ),
            const SizedBox(height: 8),
            TextFormField(
              controller: entry.onCtrl,
              decoration: InputDecoration(
                labelText: 'On Script',
                hintText: r'C:\scripts\alert.bat',
                isDense: true,
                contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
              ),
              style: const TextStyle(fontSize: 13),
            ),
            const SizedBox(height: 8),
            TextFormField(
              controller: entry.offCtrl,
              decoration: InputDecoration(
                labelText: 'Off Script',
                hintText: r'C:\scripts\end.bat',
                isDense: true,
                contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
              ),
              style: const TextStyle(fontSize: 13),
            ),
            const SizedBox(height: 8),
            const Text('Trigger for:', style: TextStyle(fontSize: 12, color: Colors.grey)),
            const SizedBox(height: 4),
            Wrap(
              spacing: 4,
              runSpacing: 0,
              children: [
                _buildScriptCategoryChip('Persons', DetectionColors.person, entry.persons,
                    (v) => setState(() => entry.persons = v),
                    active: !_aiDetection || _aiDetectPersons),
                _buildScriptCategoryChip('Vehicles', DetectionColors.vehicle, entry.vehicles,
                    (v) => setState(() => entry.vehicles = v),
                    active: !_aiDetection || _aiDetectVehicles),
                _buildScriptCategoryChip('Animals', DetectionColors.animal, entry.animals,
                    (v) => setState(() => entry.animals = v),
                    active: !_aiDetection || _aiDetectAnimals),
                _buildScriptCategoryChip('Any motion', Colors.grey, entry.motionOnly,
                    (v) => setState(() => entry.motionOnly = v),
                    active: true),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildScriptCategoryChip(String label, Color color, bool value, ValueChanged<bool> onChanged,
      {bool active = true}) {
    final showWarning = value && !active;
    return FilterChip(
      avatar: showWarning ? Icon(Icons.warning_amber_rounded, size: 14, color: Colors.orange[300]) : null,
      label: Text(label, style: TextStyle(
        fontSize: 11,
        color: value ? (showWarning ? Colors.orange[200] : Colors.white) : Colors.grey[400],
      )),
      selected: value,
      onSelected: onChanged,
      selectedColor: showWarning ? Colors.orange.withValues(alpha: 0.3) : color.withValues(alpha: 0.8),
      checkmarkColor: showWarning ? Colors.orange[300] : Colors.white,
      materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
      visualDensity: VisualDensity.compact,
      padding: const EdgeInsets.symmetric(horizontal: 4),
      tooltip: showWarning ? '$label detection not enabled above' : null,
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(isEditing ? 'Edit Camera' : 'Add Camera'),
        actions: [
          if (isEditing)
            IconButton(
              icon: const Icon(Icons.delete, color: Colors.red),
              onPressed: _delete,
            ),
        ],
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(20),
        child: Form(
          key: _formKey,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Text(_error!, style: const TextStyle(color: Colors.red)),
                ),
              TextFormField(
                controller: _nameCtrl,
                decoration: const InputDecoration(labelText: 'Camera Name'),
                validator: (v) => v == null || v.trim().isEmpty ? 'Required' : null,
              ),
              const SizedBox(height: 14),
              TextFormField(
                controller: _rtspCtrl,
                decoration: const InputDecoration(
                  labelText: 'RTSP URL',
                  hintText: 'rtsp://192.168.8.41/stream1',
                ),
                validator: (v) => v == null || v.trim().isEmpty ? 'Required' : null,
              ),
              const SizedBox(height: 14),
              TextFormField(
                controller: _subStreamCtrl,
                decoration: const InputDecoration(
                  labelText: 'Sub-Stream URL (optional)',
                  hintText: 'rtsp://192.168.8.41/stream2',
                ),
              ),
              const SizedBox(height: 14),
              Row(
                children: [
                  Expanded(
                    child: TextFormField(
                      controller: _usernameCtrl,
                      decoration: const InputDecoration(
                        labelText: 'Username (optional)',
                        hintText: 'admin',
                      ),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: TextFormField(
                      controller: _passwordCtrl,
                      obscureText: _obscurePassword,
                      decoration: InputDecoration(
                        labelText: 'Password (optional)',
                        suffixIcon: IconButton(
                          icon: Icon(_obscurePassword
                              ? Icons.visibility_off
                              : Icons.visibility),
                          onPressed: () => setState(
                              () => _obscurePassword = !_obscurePassword),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              DropdownButtonFormField<int>(
                initialValue: _rotation,
                decoration: const InputDecoration(labelText: 'Rotation'),
                items: const [
                  DropdownMenuItem(value: 0, child: Text('0')),
                  DropdownMenuItem(value: 90, child: Text('90')),
                  DropdownMenuItem(value: 180, child: Text('180')),
                  DropdownMenuItem(value: 270, child: Text('270')),
                ],
                onChanged: (v) => setState(() => _rotation = v ?? 0),
              ),
              const SizedBox(height: 14),
              SwitchListTile(
                title: const Text('Enabled'),
                value: _enabled,
                onChanged: (v) => setState(() => _enabled = v),
                contentPadding: EdgeInsets.zero,
              ),
              const SizedBox(height: 14),
              Row(
                children: [
                  const Text('Motion Detection', style: TextStyle(fontSize: 14)),
                  const Spacer(),
                  Text(
                    _motionSensitivity == 0 ? 'Off' : '$_motionSensitivity',
                    style: TextStyle(
                      fontSize: 13,
                      color: _motionSensitivity == 0
                          ? const Color(0xFF737373)
                          : const Color(0xFFF59E0B),
                    ),
                  ),
                ],
              ),
              Slider(
                value: _motionSensitivity.toDouble(),
                min: 0,
                max: 100,
                divisions: 20,
                activeColor: const Color(0xFFF59E0B),
                label: _motionSensitivity == 0 ? 'Off' : '$_motionSensitivity',
                onChanged: (v) => setState(() => _motionSensitivity = v.round()),
              ),
              if (_motionSensitivity > 0) ...[
                const SizedBox(height: 6),
                SwitchListTile(
                  title: const Text('AI Object Detection'),
                  subtitle: const Text('Use YOLO to classify detected motion'),
                  value: _aiDetection,
                  onChanged: (v) => setState(() {
                    _aiDetection = v;
                    // Ensure at least persons enabled when turning on
                    if (v && !_aiDetectPersons && !_aiDetectVehicles && !_aiDetectAnimals) {
                      _aiDetectPersons = true;
                    }
                  }),
                  contentPadding: EdgeInsets.zero,
                  activeColor: const Color(0xFF3B82F6),
                ),
                if (_aiDetection) ...[
                  _buildCategoryToggle(
                    'Persons', DetectionColors.person, _aiDetectPersons,
                    (v) => setState(() {
                      _aiDetectPersons = v;
                      if (!_aiDetectPersons && !_aiDetectVehicles && !_aiDetectAnimals) {
                        _aiDetectPersons = true; // keep at least one enabled
                      }
                    }),
                  ),
                  _buildCategoryToggle(
                    'Vehicles', DetectionColors.vehicle, _aiDetectVehicles,
                    (v) => setState(() {
                      _aiDetectVehicles = v;
                      if (!_aiDetectPersons && !_aiDetectVehicles && !_aiDetectAnimals) {
                        _aiDetectPersons = true;
                      }
                    }),
                  ),
                  _buildCategoryToggle(
                    'Animals', DetectionColors.animal, _aiDetectAnimals,
                    (v) => setState(() {
                      _aiDetectAnimals = v;
                      if (!_aiDetectPersons && !_aiDetectVehicles && !_aiDetectAnimals) {
                        _aiDetectPersons = true;
                      }
                    }),
                  ),
                  const SizedBox(height: 4),
                  Row(
                    children: [
                      const Text('Confidence Threshold', style: TextStyle(fontSize: 14)),
                      const Spacer(),
                      Text(
                        '$_aiConfidenceThreshold%',
                        style: const TextStyle(fontSize: 13, color: Color(0xFF3B82F6)),
                      ),
                    ],
                  ),
                  Slider(
                    value: _aiConfidenceThreshold.toDouble(),
                    min: 10,
                    max: 95,
                    divisions: 17,
                    activeColor: const Color(0xFF3B82F6),
                    label: '$_aiConfidenceThreshold%',
                    onChanged: (v) => setState(() => _aiConfidenceThreshold = v.round()),
                  ),
                ],
                const SizedBox(height: 6),
                _buildScriptsList(),
              ],
              const SizedBox(height: 20),
              SizedBox(
                width: double.infinity,
                child: ElevatedButton(
                  onPressed: _saving ? null : _save,
                  child: _saving
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : Text(isEditing ? 'Save Changes' : 'Add Camera'),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
