import 'package:flutter/material.dart';
import '../models/camera.dart';
import '../models/camera_group.dart';
import '../models/face.dart';
import '../models/zone.dart';
import '../services/api_client.dart';
import '../services/camera_api.dart';
import '../services/face_api.dart';
import '../services/group_api.dart';
import '../services/zone_api.dart';
import '../utils/detection_colors.dart';
import '../widgets/rtsp_wizard_dialog.dart';
import '../widgets/script_wizard_dialog.dart';
import 'zone_editor_screen.dart';

class _ScriptEntry {
  String? name;
  bool editingName;
  final TextEditingController nameCtrl;
  final FocusNode nameFocus;
  final TextEditingController onCtrl;
  final TextEditingController offCtrl;
  bool persons;
  bool vehicles;
  bool animals;
  bool motionOnly;
  int offDelay;
  List<int> faces;
  bool faceUnknown;
  List<int> zoneIds;

  _ScriptEntry({
    this.name,
    required this.onCtrl,
    required this.offCtrl,
    this.persons = true,
    this.vehicles = true,
    this.animals = true,
    this.motionOnly = true,
    this.offDelay = 10,
    List<int>? faces,
    this.faceUnknown = false,
    List<int>? zoneIds,
  })  : editingName = false,
        nameCtrl = TextEditingController(text: name ?? ''),
        nameFocus = FocusNode(),
        faces = faces ?? [],
        zoneIds = zoneIds ?? [];
}

class CameraFormScreen extends StatefulWidget {
  final CameraApi cameraApi;
  final ApiClient apiClient;
  final Camera? camera;
  final List<CameraGroup> groups;
  final GroupApi? groupApi;
  final FaceApi? faceApi;
  final int? initialGroupId;

  const CameraFormScreen({
    super.key,
    required this.cameraApi,
    required this.apiClient,
    this.camera,
    this.groups = const [],
    this.groupApi,
    this.faceApi,
    this.initialGroupId,
  });

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
  late bool _faceRecognition;
  late int _faceMatchThreshold;
  List<Face> _knownFaces = [];
  List<Zone> _zones = [];
  late final ZoneApi _zoneApi;
  int? _groupId;
  bool _saving = false;
  String? _error;
  bool _obscurePassword = true;
  String? _testingScript; // tracks which script field is currently being tested

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
    _motionSensitivity = widget.camera?.motionSensitivity ?? 100;
    // Build script entries from motionScripts list
    final scripts = widget.camera?.motionScripts ?? [];
    _scriptEntries = scripts.map((s) => _ScriptEntry(
      name: s.name,
      onCtrl: TextEditingController(text: s.on ?? ''),
      offCtrl: TextEditingController(text: s.off ?? ''),
      persons: s.persons,
      vehicles: s.vehicles,
      animals: s.animals,
      motionOnly: s.motionOnly,
      offDelay: s.offDelay,
      faces: List<int>.from(s.faces),
      faceUnknown: s.faceUnknown,
      zoneIds: List<int>.from(s.zoneIds),
    )).toList();
    _zoneApi = ZoneApi(widget.apiClient);
    _aiDetection = widget.camera?.aiDetection ?? true;
    _aiDetectPersons = widget.camera?.aiDetectPersons ?? true;
    _aiDetectVehicles = widget.camera?.aiDetectVehicles ?? true;
    _aiDetectAnimals = widget.camera?.aiDetectAnimals ?? true;
    _aiConfidenceThreshold = widget.camera?.aiConfidenceThreshold ?? 50;
    _faceRecognition = widget.camera?.faceRecognition ?? false;
    _faceMatchThreshold = widget.camera?.faceMatchThreshold ?? 60;
    _groupId = widget.camera?.groupId ?? widget.initialGroupId;
    _loadFaces();
    _loadZones();
  }

  Future<void> _loadFaces() async {
    if (widget.faceApi == null) return;
    try {
      final faces = await widget.faceApi!.fetchAll();
      if (mounted) setState(() => _knownFaces = faces);
    } catch (_) {
      // Non-fatal: form still works without face filter UI
    }
  }

  Future<void> _loadZones() async {
    if (widget.camera == null) return; // zones require a persisted camera id
    try {
      final zones = await _zoneApi.listForCamera(widget.camera!.id);
      if (!mounted) return;
      setState(() {
        _zones = zones;
        // Drop any stale zone references from scripts (zone may have been
        // deleted server-side since the camera was last loaded).
        final validIds = zones.map((z) => z.id).toSet();
        for (final e in _scriptEntries) {
          e.zoneIds = e.zoneIds.where(validIds.contains).toList();
        }
      });
    } catch (_) {
      // Non-fatal: form still works without zones
    }
  }

  Future<void> _openZoneEditor({Zone? zone}) async {
    if (widget.camera == null) return;
    final saved = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => ZoneEditorScreen(
          cameraApi: widget.cameraApi,
          zoneApi: _zoneApi,
          camera: widget.camera!,
          zone: zone,
        ),
      ),
    );
    if (saved == true) await _loadZones();
  }

  Future<void> _deleteZone(Zone zone) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Delete "${zone.name}"?'),
        content: const Text('Any scripts restricted to this zone will have the restriction removed.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Delete')),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      await _zoneApi.delete(cameraId: widget.camera!.id, zoneId: zone.id);
      if (mounted) {
        setState(() {
          _zones = _zones.where((z) => z.id != zone.id).toList();
          for (final e in _scriptEntries) {
            e.zoneIds = e.zoneIds.where((id) => id != zone.id).toList();
          }
        });
      }
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Delete failed: $e')),
      );
    }
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
      e.nameCtrl.dispose();
      e.nameFocus.dispose();
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
              final trimmedName = e.name?.trim();
              return <String, dynamic>{
                'name': (trimmedName == null || trimmedName.isEmpty) ? null : trimmedName,
                'on': e.onCtrl.text.trim().isEmpty ? null : e.onCtrl.text.trim(),
                'off': e.offCtrl.text.trim().isEmpty ? null : e.offCtrl.text.trim(),
                'persons': e.persons,
                'vehicles': e.vehicles,
                'animals': e.animals,
                'motion_only': e.motionOnly,
                'off_delay': e.offDelay,
                'faces': e.faces,
                'face_unknown': e.faceUnknown,
                'zone_ids': e.zoneIds,
              };
            })
            .toList();
        final data = <String, dynamic>{
          'name': _nameCtrl.text.trim(),
          'rtsp_url': mainUrl,
          'sub_stream_url': subUrl,
          'enabled': _enabled,
          'rotation': _rotation,
          'group_id': _groupId,
          'motion_sensitivity': _motionSensitivity,
          'motion_scripts': scriptsList,
          'ai_detection': _aiDetection,
          'ai_detect_persons': _aiDetectPersons,
          'ai_detect_vehicles': _aiDetectVehicles,
          'ai_detect_animals': _aiDetectAnimals,
          'ai_confidence_threshold': _aiConfidenceThreshold,
          'face_recognition': _faceRecognition,
          'face_match_threshold': _faceMatchThreshold,
        };
        await widget.cameraApi.update(widget.camera!.id, data);
      } else {
        await widget.cameraApi.create(
          name: _nameCtrl.text.trim(),
          rtspUrl: mainUrl,
          subStreamUrl: subUrl,
          enabled: _enabled,
          rotation: _rotation,
          groupId: _groupId,
        );
      }
      if (mounted) Navigator.of(context).pop();
    } catch (e) {
      setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _openRtspWizard() async {
    // Try to extract IP from existing RTSP URL for pre-fill
    String initialIp = '';
    final existingUrl = _rtspCtrl.text.trim();
    if (existingUrl.isNotEmpty) {
      final uri = Uri.tryParse(existingUrl);
      if (uri != null && uri.host.isNotEmpty) {
        initialIp = uri.host;
      }
    }

    final result = await showDialog<RtspWizardResult>(
      context: context,
      builder: (ctx) => RtspWizardDialog(
        apiClient: widget.apiClient,
        initialIp: initialIp,
        initialUsername: _usernameCtrl.text.trim(),
        initialPassword: _passwordCtrl.text.trim(),
      ),
    );
    if (result == null || !mounted) return;

    setState(() {
      _rtspCtrl.text = result.mainUrl;
      if (result.subUrl != null && result.subUrl!.isNotEmpty) {
        _subStreamCtrl.text = result.subUrl!;
      }
      if (result.username.isNotEmpty) {
        _usernameCtrl.text = result.username;
      }
      if (result.password.isNotEmpty) {
        _passwordCtrl.text = result.password;
      }
    });
  }

  Future<void> _delete() async {
    final result = await showDialog<Map<String, bool>>(
      context: context,
      builder: (ctx) => _DeleteCameraDialog(cameraName: widget.camera!.name),
    );
    if (result == null) return;
    final purge = result['purge'] ?? false;
    try {
      await widget.cameraApi.delete(widget.camera!.id, purgeData: purge);
      if (mounted) Navigator.of(context).pop();
    } catch (e) {
      setState(() => _error = e.toString());
    }
  }

  Future<void> _testScript(String command, String fieldKey) async {
    if (command.trim().isEmpty) return;
    setState(() => _testingScript = fieldKey);
    try {
      final result = await widget.cameraApi.testScript(command.trim());
      if (!mounted) return;
      final exitCode = result['exit_code'] as int;
      final timedOut = result['timed_out'] as bool? ?? false;
      final stdout = (result['stdout'] as String? ?? '').trim();
      final stderr = (result['stderr'] as String? ?? '').trim();

      if (timedOut) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Script timed out (15s limit)'), backgroundColor: Colors.orange),
        );
      } else if (exitCode == 0) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(stdout.isNotEmpty ? 'OK: $stdout' : 'Script ran successfully (exit code 0)'),
            backgroundColor: Colors.green[700],
          ),
        );
      } else {
        showDialog(
          context: context,
          builder: (ctx) => AlertDialog(
            title: Text('Script failed (exit code $exitCode)'),
            content: SingleChildScrollView(
              child: SelectableText(
                stderr.isNotEmpty ? stderr : (stdout.isNotEmpty ? stdout : 'No output'),
                style: const TextStyle(fontSize: 12, fontFamily: 'monospace'),
              ),
            ),
            actions: [TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('OK'))],
          ),
        );
      }
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Error: $e'), backgroundColor: Colors.red),
      );
    } finally {
      if (mounted) setState(() => _testingScript = null);
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

  Widget _buildZonesSection() {
    if (!isEditing) {
      return Padding(
        padding: const EdgeInsets.only(bottom: 4),
        child: Text(
          'Save the camera once to draw detection zones.',
          style: TextStyle(fontSize: 12, color: Colors.grey[500]),
        ),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const Text('Detection Zones',
                style: TextStyle(fontSize: 14, fontWeight: FontWeight.w500)),
            const Spacer(),
            TextButton.icon(
              icon: const Icon(Icons.add_location_alt, size: 18),
              label: const Text('New Zone'),
              onPressed: () => _openZoneEditor(),
            ),
          ],
        ),
        if (_zones.isEmpty)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text(
              'No zones drawn. A script with no zone restriction fires on the whole frame.',
              style: TextStyle(fontSize: 12, color: Colors.grey[500]),
            ),
          )
        else
          for (final z in _zones)
            Card(
              margin: const EdgeInsets.only(bottom: 6),
              child: ListTile(
                dense: true,
                leading: const Icon(Icons.hexagon_outlined, size: 20),
                title: Text(z.name, style: const TextStyle(fontSize: 13)),
                subtitle: Text('${z.points.length} points',
                    style: const TextStyle(fontSize: 11)),
                trailing: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    IconButton(
                      icon: const Icon(Icons.edit_outlined, size: 18),
                      tooltip: 'Edit zone',
                      onPressed: () => _openZoneEditor(zone: z),
                    ),
                    IconButton(
                      icon: Icon(Icons.delete_outline, size: 18, color: Colors.red[400]),
                      tooltip: 'Delete zone',
                      onPressed: () => _deleteZone(z),
                    ),
                  ],
                ),
              ),
            ),
      ],
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

  Widget _buildScriptActions(TextEditingController ctrl, String fieldKey) {
    return ValueListenableBuilder<TextEditingValue>(
      valueListenable: ctrl,
      builder: (context, value, _) {
        final hasText = value.text.trim().isNotEmpty;
        final isTesting = _testingScript == fieldKey;
        return Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            IconButton(
              icon: Icon(Icons.auto_fix_high, size: 18, color: Colors.blue[300]),
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints(),
              tooltip: 'Script builder wizard',
              onPressed: () async {
                final result = await showDialog<String>(
                  context: context,
                  builder: (_) => ScriptWizardDialog(
                    initialCommand: ctrl.text.trim().isNotEmpty ? ctrl.text.trim() : null,
                  ),
                );
                if (result != null && mounted) {
                  setState(() => ctrl.text = result);
                }
              },
            ),
            const SizedBox(width: 4),
            if (isTesting)
              const Padding(
                padding: EdgeInsets.only(right: 8),
                child: SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)),
              )
            else
              IconButton(
                icon: Icon(Icons.play_arrow, size: 20,
                    color: hasText ? Colors.green[400] : Colors.grey[700]),
                padding: EdgeInsets.zero,
                constraints: const BoxConstraints(),
                tooltip: 'Test run this script',
                onPressed: hasText ? () => _testScript(ctrl.text, fieldKey) : null,
              ),
          ],
        );
      },
    );
  }

  void _commitScriptName(int index) {
    final entry = _scriptEntries[index];
    final typed = entry.nameCtrl.text.trim();
    final defaultName = 'Script ${index + 1}';
    setState(() {
      entry.name = (typed.isEmpty || typed == defaultName) ? null : typed;
      entry.editingName = false;
    });
  }

  Widget _buildScriptNameEditor(int index) {
    final entry = _scriptEntries[index];
    final displayName = entry.name ?? 'Script ${index + 1}';
    const style = TextStyle(fontSize: 13, fontWeight: FontWeight.w500);

    if (entry.editingName) {
      return TextField(
        controller: entry.nameCtrl,
        focusNode: entry.nameFocus,
        autofocus: true,
        style: style,
        decoration: const InputDecoration(
          isDense: true,
          contentPadding: EdgeInsets.symmetric(horizontal: 6, vertical: 6),
          border: OutlineInputBorder(),
        ),
        onSubmitted: (_) => _commitScriptName(index),
        onTapOutside: (_) => _commitScriptName(index),
      );
    }

    return InkWell(
      onTap: () {
        setState(() {
          entry.nameCtrl.text = entry.name ?? '';
          entry.editingName = true;
        });
        WidgetsBinding.instance.addPostFrameCallback((_) {
          entry.nameFocus.requestFocus();
          entry.nameCtrl.selection = TextSelection(
            baseOffset: 0,
            extentOffset: entry.nameCtrl.text.length,
          );
        });
      },
      borderRadius: BorderRadius.circular(4),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Flexible(child: Text(displayName, style: style, overflow: TextOverflow.ellipsis)),
            const SizedBox(width: 4),
            Icon(Icons.edit, size: 12, color: Colors.grey[500]),
          ],
        ),
      ),
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
                Expanded(child: _buildScriptNameEditor(index)),
                IconButton(
                  icon: const Icon(Icons.delete_outline, size: 20),
                  padding: EdgeInsets.zero,
                  constraints: const BoxConstraints(),
                  color: Colors.red[400],
                  onPressed: () {
                    setState(() {
                      final e = _scriptEntries[index];
                      e.onCtrl.dispose();
                      e.offCtrl.dispose();
                      e.nameCtrl.dispose();
                      e.nameFocus.dispose();
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
                isDense: true,
                contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
                suffixIcon: _buildScriptActions(entry.onCtrl, 'on_$index'),
                suffixIconConstraints: const BoxConstraints(maxHeight: 30),
              ),
              style: const TextStyle(fontSize: 13),
            ),
            const SizedBox(height: 8),
            ValueListenableBuilder<TextEditingValue>(
              valueListenable: entry.offCtrl,
              builder: (context, value, _) {
                if (value.text.trim().isEmpty) return const SizedBox.shrink();
                return Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Row(
                    children: [
                      const Text('Off delay:', style: TextStyle(fontSize: 12, color: Colors.grey)),
                      Expanded(
                        child: Slider(
                          value: entry.offDelay.toDouble(),
                          min: 10,
                          max: 300,
                          divisions: 58,
                          activeColor: const Color(0xFFF59E0B),
                          label: '${entry.offDelay}s',
                          onChanged: (v) => setState(() => entry.offDelay = v.round()),
                        ),
                      ),
                      SizedBox(
                        width: 40,
                        child: Text('${entry.offDelay}s',
                            style: const TextStyle(fontSize: 12, color: Color(0xFFF59E0B))),
                      ),
                    ],
                  ),
                );
              },
            ),
            TextFormField(
              controller: entry.offCtrl,
              decoration: InputDecoration(
                labelText: 'Off Script',
                isDense: true,
                contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
                suffixIcon: _buildScriptActions(entry.offCtrl, 'off_$index'),
                suffixIconConstraints: const BoxConstraints(maxHeight: 30),
              ),
              style: const TextStyle(fontSize: 13),
            ),
            const SizedBox(height: 8),
            const Text('Execute for:', style: TextStyle(fontSize: 12, color: Colors.grey)),
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
            if (_faceRecognition && _aiDetectPersons) ...[
              const SizedBox(height: 8),
              const Text('Face filter (optional):',
                  style: TextStyle(fontSize: 12, color: Colors.grey)),
              const SizedBox(height: 4),
              Wrap(
                spacing: 4,
                runSpacing: 0,
                children: [
                  for (final f in _knownFaces)
                    FilterChip(
                      label: Text(f.displayName, style: const TextStyle(fontSize: 11)),
                      selected: entry.faces.contains(f.id),
                      onSelected: (v) => setState(() {
                        if (v) {
                          if (!entry.faces.contains(f.id)) entry.faces.add(f.id);
                        } else {
                          entry.faces.remove(f.id);
                        }
                      }),
                      selectedColor: const Color(0xFF06B6D4).withValues(alpha: 0.7),
                      checkmarkColor: Colors.white,
                      materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                      visualDensity: VisualDensity.compact,
                      padding: const EdgeInsets.symmetric(horizontal: 4),
                    ),
                  FilterChip(
                    label: const Text('Unknown', style: TextStyle(fontSize: 11)),
                    selected: entry.faceUnknown,
                    onSelected: (v) => setState(() => entry.faceUnknown = v),
                    selectedColor: const Color(0xFFE11D48).withValues(alpha: 0.6),
                    checkmarkColor: Colors.white,
                    materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    visualDensity: VisualDensity.compact,
                    padding: const EdgeInsets.symmetric(horizontal: 4),
                  ),
                ],
              ),
              if (entry.faces.isEmpty && !entry.faceUnknown)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    _knownFaces.isEmpty
                        ? 'No faces enrolled yet — script fires for all persons.'
                        : 'No filter selected — script fires for all persons.',
                    style: TextStyle(fontSize: 11, color: Colors.grey[500]),
                  ),
                ),
            ],
            if (_zones.isNotEmpty) ...[
              const SizedBox(height: 8),
              const Text('Restrict to zones (optional):',
                  style: TextStyle(fontSize: 12, color: Colors.grey)),
              const SizedBox(height: 4),
              Wrap(
                spacing: 4,
                runSpacing: 0,
                children: [
                  for (final z in _zones)
                    FilterChip(
                      label: Text(z.name, style: const TextStyle(fontSize: 11)),
                      selected: entry.zoneIds.contains(z.id),
                      onSelected: (v) => setState(() {
                        if (v) {
                          if (!entry.zoneIds.contains(z.id)) entry.zoneIds.add(z.id);
                        } else {
                          entry.zoneIds.remove(z.id);
                        }
                      }),
                      selectedColor: const Color(0xFF3B82F6).withValues(alpha: 0.7),
                      checkmarkColor: Colors.white,
                      materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                      visualDensity: VisualDensity.compact,
                      padding: const EdgeInsets.symmetric(horizontal: 4),
                    ),
                ],
              ),
              if (entry.zoneIds.isEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    'No zone selected — script fires anywhere in the frame.',
                    style: TextStyle(fontSize: 11, color: Colors.grey[500]),
                  ),
                ),
            ],
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
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(
                    child: TextFormField(
                      controller: _rtspCtrl,
                      decoration: const InputDecoration(
                        labelText: 'Main-Stream RTSP URL',
                      ),
                      validator: (v) => v == null || v.trim().isEmpty ? 'Required' : null,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Tooltip(
                      message: 'Auto-discover RTSP URL',
                      child: ElevatedButton.icon(
                        onPressed: _openRtspWizard,
                        icon: const Icon(Icons.wifi_find, size: 18),
                        label: const Text('Find'),
                        style: ElevatedButton.styleFrom(
                          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 14),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              TextFormField(
                controller: _subStreamCtrl,
                decoration: const InputDecoration(
                  labelText: 'Sub-Stream RTSP URL (optional)',
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
              if (widget.groups.isNotEmpty) ...[
                const SizedBox(height: 14),
                DropdownButtonFormField<int?>(
                  value: _groupId,
                  decoration: const InputDecoration(labelText: 'Group'),
                  items: [
                    const DropdownMenuItem<int?>(value: null, child: Text('Ungrouped')),
                    ...widget.groups.map((g) =>
                        DropdownMenuItem<int?>(value: g.id, child: Text(g.name))),
                  ],
                  onChanged: (v) => setState(() => _groupId = v),
                ),
              ],
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
                  // Face recognition (only meaningful when Persons detection is on)
                  SwitchListTile(
                    title: const Text('Face Recognition'),
                    subtitle: Text(
                      _aiDetectPersons
                          ? 'Identify enrolled people in person detections'
                          : 'Enable Persons detection above to use',
                      style: const TextStyle(fontSize: 12),
                    ),
                    value: _faceRecognition && _aiDetectPersons,
                    onChanged: _aiDetectPersons
                        ? (v) => setState(() => _faceRecognition = v)
                        : null,
                    contentPadding: EdgeInsets.zero,
                    activeColor: const Color(0xFF06B6D4),
                  ),
                  if (_faceRecognition && _aiDetectPersons) ...[
                    Row(
                      children: [
                        const Text('Match Threshold', style: TextStyle(fontSize: 14)),
                        const Spacer(),
                        Text('$_faceMatchThreshold%',
                            style: const TextStyle(fontSize: 13, color: Color(0xFF06B6D4))),
                      ],
                    ),
                    Slider(
                      value: _faceMatchThreshold.toDouble(),
                      min: 30,
                      max: 90,
                      divisions: 12,
                      activeColor: const Color(0xFF06B6D4),
                      label: '$_faceMatchThreshold%',
                      onChanged: (v) => setState(() => _faceMatchThreshold = v.round()),
                    ),
                  ],
                ],
                const SizedBox(height: 6),
                _buildZonesSection(),
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

/// Confirmation dialog for camera deletion with optional data purge.
class _DeleteCameraDialog extends StatefulWidget {
  final String cameraName;
  const _DeleteCameraDialog({required this.cameraName});

  @override
  State<_DeleteCameraDialog> createState() => _DeleteCameraDialogState();
}

class _DeleteCameraDialogState extends State<_DeleteCameraDialog> {
  bool _purge = false;

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Delete Camera?'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Delete "${widget.cameraName}"?'),
          const SizedBox(height: 12),
          CheckboxListTile(
            value: _purge,
            onChanged: (v) => setState(() => _purge = v ?? false),
            contentPadding: EdgeInsets.zero,
            controlAffinity: ListTileControlAffinity.leading,
            title: const Text(
              'Also delete all recordings, thumbnails, and detection data',
              style: TextStyle(fontSize: 13),
            ),
            subtitle: _purge
                ? const Text(
                    'This cannot be undone.',
                    style: TextStyle(color: Colors.redAccent, fontSize: 12),
                  )
                : const Text(
                    'Files on disk will be preserved.',
                    style: TextStyle(fontSize: 12),
                  ),
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Cancel'),
        ),
        TextButton(
          onPressed: () =>
              Navigator.pop(context, {'purge': _purge}),
          style: TextButton.styleFrom(
            foregroundColor: _purge ? Colors.red : null,
          ),
          child: Text(_purge ? 'Delete Everything' : 'Delete'),
        ),
      ],
    );
  }
}
