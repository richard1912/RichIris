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
  late bool _enabled;
  late int _rotation;
  bool _saving = false;
  String? _error;

  bool get isEditing => widget.camera != null;

  @override
  void initState() {
    super.initState();
    _nameCtrl = TextEditingController(text: widget.camera?.name ?? '');
    _rtspCtrl = TextEditingController(text: widget.camera?.rtspUrl ?? '');
    _subStreamCtrl =
        TextEditingController(text: widget.camera?.subStreamUrl ?? '');
    _enabled = widget.camera?.enabled ?? true;
    _rotation = widget.camera?.rotation ?? 0;
  }

  @override
  void dispose() {
    _nameCtrl.dispose();
    _rtspCtrl.dispose();
    _subStreamCtrl.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      if (isEditing) {
        final data = <String, dynamic>{
          'name': _nameCtrl.text.trim(),
          'rtsp_url': _rtspCtrl.text.trim(),
          'enabled': _enabled,
          'rotation': _rotation,
        };
        final sub = _subStreamCtrl.text.trim();
        if (sub.isNotEmpty) {
          data['sub_stream_url'] = sub;
        } else {
          data['sub_stream_url'] = null;
        }
        await widget.cameraApi.update(widget.camera!.id, data);
      } else {
        await widget.cameraApi.create(
          name: _nameCtrl.text.trim(),
          rtspUrl: _rtspCtrl.text.trim(),
          subStreamUrl:
              _subStreamCtrl.text.trim().isNotEmpty ? _subStreamCtrl.text.trim() : null,
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
                  hintText: 'rtsp://192.168.8.41:554/stream',
                ),
                validator: (v) => v == null || v.trim().isEmpty ? 'Required' : null,
              ),
              const SizedBox(height: 14),
              TextFormField(
                controller: _subStreamCtrl,
                decoration: const InputDecoration(
                  labelText: 'Sub-Stream URL (optional)',
                  hintText: 'rtsp://192.168.8.41:554/sub_stream',
                ),
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
