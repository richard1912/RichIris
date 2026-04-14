import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';

/// Dialog that helps users assemble script commands step-by-step:
/// pick an executable, a script path, and additional arguments,
/// with a live preview of the fully-quoted command.
class ScriptWizardDialog extends StatefulWidget {
  /// If provided, pre-populates the wizard by parsing an existing command.
  final String? initialCommand;

  const ScriptWizardDialog({super.key, this.initialCommand});

  @override
  State<ScriptWizardDialog> createState() => _ScriptWizardDialogState();
}

class _ScriptWizardDialogState extends State<ScriptWizardDialog> {
  final _exeCtrl = TextEditingController();
  final _scriptPathCtrl = TextEditingController();
  final List<TextEditingController> _argCtrls = [];

  @override
  void initState() {
    super.initState();
    if (widget.initialCommand != null && widget.initialCommand!.isNotEmpty) {
      _parseCommand(widget.initialCommand!);
    }
  }

  void _parseCommand(String cmd) {
    // Simple parser: split on spaces respecting double-quoted segments
    final parts = <String>[];
    final buf = StringBuffer();
    var inQuote = false;
    for (var i = 0; i < cmd.length; i++) {
      final ch = cmd[i];
      if (ch == '"') {
        inQuote = !inQuote;
      } else if (ch == ' ' && !inQuote) {
        if (buf.isNotEmpty) {
          parts.add(buf.toString());
          buf.clear();
        }
      } else {
        buf.write(ch);
      }
    }
    if (buf.isNotEmpty) parts.add(buf.toString());

    if (parts.isEmpty) return;
    _exeCtrl.text = parts[0];
    if (parts.length > 1) _scriptPathCtrl.text = parts[1];
    for (var i = 2; i < parts.length; i++) {
      _argCtrls.add(TextEditingController(text: parts[i]));
    }
  }

  @override
  void dispose() {
    _exeCtrl.dispose();
    _scriptPathCtrl.dispose();
    for (final c in _argCtrls) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _pickExe() async {
    final result = await FilePicker.platform.pickFiles(
      dialogTitle: 'Select executable',
      type: FileType.custom,
      allowedExtensions: ['exe', 'bat', 'cmd', 'ps1', 'py', 'sh'],
    );
    if (result != null && result.files.single.path != null) {
      setState(() {
        _exeCtrl.text = result.files.single.path!.replaceAll('\\', '/');
      });
    }
  }

  Future<void> _pickScript() async {
    final result = await FilePicker.platform.pickFiles(
      dialogTitle: 'Select script or file',
    );
    if (result != null && result.files.single.path != null) {
      setState(() {
        _scriptPathCtrl.text = result.files.single.path!.replaceAll('\\', '/');
      });
    }
  }

  String _quoteIfNeeded(String s) {
    if (s.isEmpty) return s;
    return s.contains(' ') ? '"$s"' : s;
  }

  String get _preview {
    final parts = <String>[
      _exeCtrl.text.trim(),
      _scriptPathCtrl.text.trim(),
      ..._argCtrls.map((c) => c.text.trim()),
    ].where((s) => s.isNotEmpty).map(_quoteIfNeeded);
    return parts.join(' ');
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Script Builder'),
      content: SizedBox(
        width: 500,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Executable
              Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _exeCtrl,
                      decoration: const InputDecoration(
                        labelText: 'Executable',
                        hintText: 'e.g. C:/Python313/python.exe',
                        isDense: true,
                      ),
                      style: const TextStyle(fontSize: 13),
                      onChanged: (_) => setState(() {}),
                    ),
                  ),
                  const SizedBox(width: 4),
                  IconButton(
                    icon: const Icon(Icons.folder_open, size: 20),
                    tooltip: 'Browse for executable',
                    onPressed: _pickExe,
                  ),
                ],
              ),
              const SizedBox(height: 12),

              // Script path
              Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _scriptPathCtrl,
                      decoration: const InputDecoration(
                        labelText: 'Script / file path',
                        hintText: 'e.g. C:/scripts/my_script.py',
                        isDense: true,
                      ),
                      style: const TextStyle(fontSize: 13),
                      onChanged: (_) => setState(() {}),
                    ),
                  ),
                  const SizedBox(width: 4),
                  IconButton(
                    icon: const Icon(Icons.folder_open, size: 20),
                    tooltip: 'Browse for script',
                    onPressed: _pickScript,
                  ),
                ],
              ),
              const SizedBox(height: 12),

              // Arguments
              const Text('Arguments', style: TextStyle(fontSize: 12, color: Colors.grey)),
              const SizedBox(height: 4),
              for (int i = 0; i < _argCtrls.length; i++)
                Padding(
                  padding: const EdgeInsets.only(bottom: 6),
                  child: Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _argCtrls[i],
                          decoration: InputDecoration(
                            labelText: 'Arg ${i + 1}',
                            isDense: true,
                          ),
                          style: const TextStyle(fontSize: 13),
                          onChanged: (_) => setState(() {}),
                        ),
                      ),
                      const SizedBox(width: 4),
                      IconButton(
                        icon: Icon(Icons.remove_circle_outline, size: 18, color: Colors.red[400]),
                        padding: EdgeInsets.zero,
                        constraints: const BoxConstraints(),
                        onPressed: () {
                          setState(() {
                            _argCtrls[i].dispose();
                            _argCtrls.removeAt(i);
                          });
                        },
                      ),
                    ],
                  ),
                ),
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(
                  icon: const Icon(Icons.add, size: 16),
                  label: const Text('Add argument', style: TextStyle(fontSize: 12)),
                  onPressed: () {
                    setState(() => _argCtrls.add(TextEditingController()));
                  },
                ),
              ),
              const SizedBox(height: 12),

              // Preview
              const Text('Preview', style: TextStyle(fontSize: 12, color: Colors.grey)),
              const SizedBox(height: 4),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: Colors.black26,
                  borderRadius: BorderRadius.circular(6),
                ),
                child: SelectableText(
                  _preview.isEmpty ? '(empty)' : _preview,
                  style: TextStyle(
                    fontSize: 12,
                    fontFamily: 'monospace',
                    color: _preview.isEmpty ? Colors.grey : Colors.white,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Cancel'),
        ),
        ElevatedButton(
          onPressed: _preview.isEmpty ? null : () => Navigator.pop(context, _preview),
          child: const Text('Use Command'),
        ),
      ],
    );
  }
}
