import 'package:flutter/material.dart';
import '../models/camera.dart';
import '../services/camera_api.dart';

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
  late final TextEditingController _motionScriptCtrl;
  late final TextEditingController _motionScriptOffCtrl;
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
    _motionScriptCtrl = TextEditingController(text: widget.camera?.motionScript ?? '');
    _motionScriptOffCtrl = TextEditingController(text: widget.camera?.motionScriptOff ?? '');
  }

  @override
  void dispose() {
    _nameCtrl.dispose();
    _rtspCtrl.dispose();
    _subStreamCtrl.dispose();
    _usernameCtrl.dispose();
    _passwordCtrl.dispose();
    _motionScriptCtrl.dispose();
    _motionScriptOffCtrl.dispose();
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
        final data = <String, dynamic>{
          'name': _nameCtrl.text.trim(),
          'rtsp_url': mainUrl,
          'sub_stream_url': subUrl,
          'enabled': _enabled,
          'rotation': _rotation,
          'motion_sensitivity': _motionSensitivity,
          'motion_script': _motionScriptCtrl.text.trim().isEmpty
              ? null
              : _motionScriptCtrl.text.trim(),
          'motion_script_off': _motionScriptOffCtrl.text.trim().isEmpty
              ? null
              : _motionScriptOffCtrl.text.trim(),
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
                TextFormField(
                  controller: _motionScriptCtrl,
                  decoration: const InputDecoration(
                    labelText: 'Motion Start Script (optional)',
                    hintText: r'C:\scripts\motion_alert.bat',
                    helperText: 'Env vars: MOTION_CAMERA, MOTION_TIME, MOTION_INTENSITY',
                    helperMaxLines: 2,
                  ),
                ),
                const SizedBox(height: 8),
                TextFormField(
                  controller: _motionScriptOffCtrl,
                  decoration: const InputDecoration(
                    labelText: 'Motion End Script (optional)',
                    hintText: r'C:\scripts\motion_end.bat',
                    helperText: 'Runs after motion stops (10s cooldown)',
                    helperMaxLines: 2,
                  ),
                ),
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
