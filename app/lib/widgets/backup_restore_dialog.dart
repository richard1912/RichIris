import 'dart:async';
import 'package:flutter/material.dart';
import 'package:file_picker/file_picker.dart';
import '../services/backup_api.dart';

// =============================================================================
// Create Backup Dialog
// =============================================================================

class CreateBackupDialog extends StatefulWidget {
  final BackupApi backupApi;

  const CreateBackupDialog({super.key, required this.backupApi});

  @override
  State<CreateBackupDialog> createState() => _CreateBackupDialogState();
}

enum _BackupStep { components, progress, done }

class _CreateBackupDialogState extends State<CreateBackupDialog> {
  _BackupStep _step = _BackupStep.components;

  // Component selection
  Map<String, dynamic>? _preview;
  bool _loadingPreview = true;
  String? _error;
  final Set<String> _selected = {'settings', 'cameras', 'database'};

  // Progress
  String? _backupId;
  Map<String, dynamic>? _progress;
  Timer? _pollTimer;
  String? _targetPath;

  @override
  void initState() {
    super.initState();
    _loadPreview();
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    super.dispose();
  }

  Future<void> _loadPreview() async {
    try {
      final preview = await widget.backupApi.previewSizes();
      if (mounted) {
        setState(() {
          _preview = preview;
          _loadingPreview = false;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _loadingPreview = false;
          _error = 'Failed to load sizes: $e';
        });
      }
    }
  }

  int _totalSelectedSize() {
    if (_preview == null) return 0;
    int total = 0;
    for (final comp in _selected) {
      final data = _preview![comp];
      if (data is Map) {
        total += (data['size'] as num?)?.toInt() ?? 0;
      }
    }
    return total;
  }

  int _totalSelectedFiles() {
    if (_preview == null) return 0;
    int total = 0;
    for (final comp in _selected) {
      final data = _preview![comp];
      if (data is Map) {
        total += (data['files'] as num?)?.toInt() ?? 0;
      }
    }
    return total;
  }

  Future<void> _pickAndStart() async {
    // Pick save location
    final result = await FilePicker.platform.saveFile(
      dialogTitle: 'Save Backup As',
      fileName: 'RichIris_Backup_${DateTime.now().toIso8601String().split('T')[0]}.richiris',
      type: FileType.custom,
      allowedExtensions: ['richiris'],
    );
    if (result == null || !mounted) return;

    _targetPath = result.endsWith('.richiris') ? result : '$result.richiris';

    setState(() {
      _step = _BackupStep.progress;
      _error = null;
    });

    try {
      final resp = await widget.backupApi.createBackup(
        _selected.toList(),
        _targetPath!,
      );
      _backupId = resp['backup_id'] as String?;
      _startPolling();
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = 'Failed to start backup: $e';
          _step = _BackupStep.components;
        });
      }
    }
  }

  void _startPolling() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(const Duration(seconds: 1), (_) => _poll());
  }

  Future<void> _poll() async {
    if (_backupId == null) return;
    try {
      final result = await widget.backupApi.getBackupProgress(_backupId!);
      if (!mounted) return;

      setState(() => _progress = result);

      final status = result['status'] as String? ?? '';
      if (status == 'completed' || status == 'failed' || status == 'cancelled') {
        _pollTimer?.cancel();
        setState(() {
          _step = _BackupStep.done;
          if (status == 'failed') {
            _error = result['error'] as String? ?? 'Backup failed.';
          }
        });
      }
    } catch (_) {}
  }

  Future<void> _cancel() async {
    if (_backupId == null) return;
    try {
      await widget.backupApi.cancelBackup(_backupId!);
    } catch (_) {}
  }

  @override
  Widget build(BuildContext context) {
    return Dialog(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 520, minWidth: 400),
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(
                    _step == _BackupStep.done ? Icons.check_circle : Icons.backup,
                    size: 24,
                    color: _step == _BackupStep.done
                        ? (_error == null ? Colors.green : Colors.red)
                        : null,
                  ),
                  const SizedBox(width: 12),
                  Text(
                    _step == _BackupStep.done
                        ? (_error == null ? 'Backup Complete' : 'Backup Failed')
                        : 'Create Backup',
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
                  ),
                ],
              ),
              const SizedBox(height: 20),
              _buildStepContent(),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildStepContent() {
    return switch (_step) {
      _BackupStep.components => _buildComponents(),
      _BackupStep.progress => _buildProgress(),
      _BackupStep.done => _buildDone(),
    };
  }

  Widget _buildComponents() {
    if (_loadingPreview) {
      return const Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Center(child: CircularProgressIndicator()),
          SizedBox(height: 16),
          Center(child: Text('Calculating sizes...')),
        ],
      );
    }

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Select what to include in the backup:',
            style: TextStyle(fontSize: 14, color: Colors.grey[400])),
        const SizedBox(height: 12),
        _componentCheckbox('settings', 'Settings & Configuration',
            Icons.settings, 'All system settings (retention, logging, etc.)'),
        _componentCheckbox('cameras', 'Camera Configuration',
            Icons.videocam, 'Camera names, URLs, motion/AI settings'),
        _componentCheckbox('database', 'Full Database',
            Icons.storage, 'Recordings metadata, motion events, clip exports'),
        _componentCheckbox('recordings', 'Recordings',
            Icons.video_library, 'All recorded video files (.ts)'),
        _componentCheckbox('thumbnails', 'Thumbnails',
            Icons.grid_view, 'Trickplay and detection thumbnails'),
        const SizedBox(height: 12),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: const Color(0xFF1E1E1E),
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: const Color(0xFF404040)),
          ),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text('Total backup size:',
                  style: TextStyle(color: Colors.grey[400])),
              Text(
                '${_formatBytes(_totalSelectedSize())} (${_totalSelectedFiles()} files)',
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
            ],
          ),
        ),
        if (_error != null) ...[
          const SizedBox(height: 8),
          Text(_error!, style: const TextStyle(color: Colors.red, fontSize: 13)),
        ],
        const SizedBox(height: 16),
        Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('Cancel'),
            ),
            const SizedBox(width: 8),
            ElevatedButton.icon(
              onPressed: _selected.isEmpty ? null : _pickAndStart,
              icon: const Icon(Icons.save, size: 18),
              label: const Text('Create Backup'),
            ),
          ],
        ),
      ],
    );
  }

  Widget _componentCheckbox(
    String key,
    String title,
    IconData icon,
    String description,
  ) {
    final data = _preview?[key];
    final size = (data is Map ? data['size'] as num? : null) ?? 0;
    final files = (data is Map ? data['files'] as num? : null) ?? 0;
    final hasData = size > 0;

    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: InkWell(
        onTap: hasData ? () => setState(() {
          if (_selected.contains(key)) {
            _selected.remove(key);
          } else {
            _selected.add(key);
          }
        }) : null,
        borderRadius: BorderRadius.circular(8),
        child: Opacity(
          opacity: hasData ? 1.0 : 0.4,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 6, horizontal: 4),
            child: Row(
              children: [
                SizedBox(
                  width: 24,
                  height: 24,
                  child: Checkbox(
                    value: _selected.contains(key),
                    onChanged: hasData
                        ? (v) => setState(() {
                              if (v == true) {
                                _selected.add(key);
                              } else {
                                _selected.remove(key);
                              }
                            })
                        : null,
                  ),
                ),
                const SizedBox(width: 12),
                Icon(icon, size: 20, color: Colors.grey[400]),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(title, style: const TextStyle(fontSize: 14)),
                      Text(description,
                          style: TextStyle(fontSize: 11, color: Colors.grey[600])),
                    ],
                  ),
                ),
                Text(
                  hasData
                      ? '${_formatBytes(size)}${files > 1 ? ' ($files files)' : ''}'
                      : 'empty',
                  style: TextStyle(
                    fontSize: 12,
                    color: hasData ? Colors.grey[400] : Colors.grey[700],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildProgress() {
    final status = _progress?['status'] as String? ?? 'starting';
    final filesTotal = (_progress?['files_total'] as num?) ?? 0;
    final filesDone = (_progress?['files_done'] as num?) ?? 0;
    final bytesTotal = (_progress?['bytes_total'] as num?) ?? 0;
    final bytesDone = (_progress?['bytes_done'] as num?) ?? 0;
    final currentFile = _progress?['current_file'] as String? ?? '';
    final fraction = bytesTotal > 0 ? bytesDone / bytesTotal : 0.0;

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Status: ${status[0].toUpperCase()}${status.substring(1)}',
            style: const TextStyle(fontWeight: FontWeight.w500)),
        const SizedBox(height: 12),
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(
            value: fraction.toDouble(),
            minHeight: 8,
            backgroundColor: const Color(0xFF404040),
          ),
        ),
        const SizedBox(height: 8),
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text('${filesDone.toInt()} / ${filesTotal.toInt()} files',
                style: TextStyle(fontSize: 13, color: Colors.grey[400])),
            Text('${_formatBytes(bytesDone)} / ${_formatBytes(bytesTotal)}',
                style: TextStyle(fontSize: 13, color: Colors.grey[400])),
          ],
        ),
        if (currentFile.isNotEmpty) ...[
          const SizedBox(height: 4),
          Text(
            currentFile,
            style: TextStyle(fontSize: 11, color: Colors.grey[600]),
            overflow: TextOverflow.ellipsis,
            maxLines: 1,
          ),
        ],
        const SizedBox(height: 16),
        Align(
          alignment: Alignment.centerRight,
          child: TextButton(
            onPressed: _cancel,
            child: const Text('Cancel', style: TextStyle(color: Colors.orange)),
          ),
        ),
      ],
    );
  }

  Widget _buildDone() {
    final status = _progress?['status'] as String? ?? '';
    final isSuccess = status == 'completed';
    final bytesDone = (_progress?['bytes_done'] as num?) ?? 0;
    final filesDone = (_progress?['files_done'] as num?) ?? 0;

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (isSuccess) ...[
          Row(
            children: [
              const Icon(Icons.check_circle, color: Colors.green, size: 24),
              const SizedBox(width: 12),
              const Expanded(
                child: Text('Backup created successfully.',
                    style: TextStyle(fontSize: 14)),
              ),
            ],
          ),
          const SizedBox(height: 12),
          _infoRow(Icons.folder, Colors.grey, _targetPath ?? ''),
          _infoRow(Icons.storage, Colors.grey,
              '${_formatBytes(bytesDone)} (${filesDone.toInt()} files)'),
        ] else ...[
          Row(
            children: [
              Icon(
                status == 'cancelled' ? Icons.cancel : Icons.error,
                color: status == 'cancelled' ? Colors.orange : Colors.red,
                size: 24,
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Text(
                  status == 'cancelled'
                      ? 'Backup was cancelled.'
                      : 'Backup failed.',
                  style: const TextStyle(fontSize: 14),
                ),
              ),
            ],
          ),
          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(_error!, style: const TextStyle(color: Colors.red, fontSize: 13)),
          ],
        ],
        const SizedBox(height: 20),
        Align(
          alignment: Alignment.centerRight,
          child: ElevatedButton(
            onPressed: () => Navigator.of(context).pop(isSuccess),
            child: const Text('Close'),
          ),
        ),
      ],
    );
  }

  String _formatBytes(num bytes) {
    if (bytes < 1024) return '${bytes.toInt()} B';
    if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
    if (bytes < 1024 * 1024 * 1024) {
      return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
    }
    return '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(2)} GB';
  }

  Widget _infoRow(IconData icon, Color color, String text) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          Icon(icon, size: 16, color: color),
          const SizedBox(width: 8),
          Expanded(child: Text(text, style: const TextStyle(fontSize: 13))),
        ],
      ),
    );
  }
}

// =============================================================================
// Restore Backup Dialog
// =============================================================================

class RestoreBackupDialog extends StatefulWidget {
  final BackupApi backupApi;

  const RestoreBackupDialog({super.key, required this.backupApi});

  @override
  State<RestoreBackupDialog> createState() => _RestoreBackupDialogState();
}

enum _RestoreStep { pickFile, selectComponents, progress, done }

class _RestoreBackupDialogState extends State<RestoreBackupDialog> {
  _RestoreStep _step = _RestoreStep.pickFile;

  // File selection
  String? _filePath;
  Map<String, dynamic>? _manifest;
  bool _inspecting = false;
  String? _error;

  // Component selection
  final Set<String> _selected = {};

  // Progress
  String? _backupId;
  Map<String, dynamic>? _progress;
  Timer? _pollTimer;
  bool _needsRestart = false;

  @override
  void initState() {
    super.initState();
    _pickFile();
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    super.dispose();
  }

  Future<void> _pickFile() async {
    final result = await FilePicker.platform.pickFiles(
      dialogTitle: 'Select Backup File',
      type: FileType.custom,
      allowedExtensions: ['richiris'],
    );
    if (result == null || result.files.isEmpty) {
      if (mounted && _manifest == null) Navigator.of(context).pop();
      return;
    }

    final path = result.files.first.path;
    if (path == null) return;

    setState(() {
      _filePath = path;
      _inspecting = true;
      _error = null;
    });

    try {
      final manifest = await widget.backupApi.inspectBackup(path);
      if (mounted) {
        final available = (manifest['available_components'] as List?)
                ?.cast<String>() ??
            [];
        setState(() {
          _manifest = manifest;
          _inspecting = false;
          _selected.addAll(available); // Select all by default
          _step = _RestoreStep.selectComponents;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _inspecting = false;
          _error = 'Failed to read backup: $e';
        });
      }
    }
  }

  Future<void> _startRestore() async {
    setState(() {
      _step = _RestoreStep.progress;
      _error = null;
    });

    try {
      final resp = await widget.backupApi.startRestore(
        _filePath!,
        _selected.toList(),
      );
      _backupId = resp['backup_id'] as String?;
      _needsRestart = resp['needs_restart'] == true;
      _startPolling();
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = 'Failed to start restore: $e';
          _step = _RestoreStep.selectComponents;
        });
      }
    }
  }

  void _startPolling() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(const Duration(seconds: 1), (_) => _poll());
  }

  Future<void> _poll() async {
    if (_backupId == null) return;
    try {
      final result = await widget.backupApi.getRestoreProgress(_backupId!);
      if (!mounted) return;

      setState(() => _progress = result);

      final status = result['status'] as String? ?? '';
      if (status == 'completed' || status == 'failed' || status == 'cancelled') {
        _pollTimer?.cancel();
        setState(() {
          _step = _RestoreStep.done;
          if (status == 'failed') {
            _error = result['error'] as String? ?? 'Restore failed.';
          }
        });
      }
    } catch (_) {}
  }

  Future<void> _cancel() async {
    if (_backupId == null) return;
    try {
      await widget.backupApi.cancelRestore(_backupId!);
    } catch (_) {}
  }

  @override
  Widget build(BuildContext context) {
    return Dialog(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 520, minWidth: 400),
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(
                    _step == _RestoreStep.done
                        ? (_error == null ? Icons.check_circle : Icons.error)
                        : Icons.restore,
                    size: 24,
                    color: _step == _RestoreStep.done
                        ? (_error == null ? Colors.green : Colors.red)
                        : null,
                  ),
                  const SizedBox(width: 12),
                  Text(
                    _step == _RestoreStep.done
                        ? (_error == null ? 'Restore Complete' : 'Restore Failed')
                        : 'Restore from Backup',
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
                  ),
                ],
              ),
              const SizedBox(height: 20),
              _buildStepContent(),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildStepContent() {
    return switch (_step) {
      _RestoreStep.pickFile => _buildPickFile(),
      _RestoreStep.selectComponents => _buildSelectComponents(),
      _RestoreStep.progress => _buildProgress(),
      _RestoreStep.done => _buildDone(),
    };
  }

  Widget _buildPickFile() {
    if (_inspecting) {
      return const Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Center(child: CircularProgressIndicator()),
          SizedBox(height: 16),
          Center(child: Text('Reading backup file...')),
        ],
      );
    }

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (_error != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Text(_error!,
                style: const TextStyle(color: Colors.red, fontSize: 13)),
          ),
        Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('Cancel'),
            ),
            const SizedBox(width: 8),
            ElevatedButton(
              onPressed: _pickFile,
              child: const Text('Browse...'),
            ),
          ],
        ),
      ],
    );
  }

  Widget _buildSelectComponents() {
    final details =
        _manifest?['component_details'] as Map<String, dynamic>? ?? {};
    final available = (_manifest?['available_components'] as List?)
            ?.cast<String>() ??
        [];
    final createdAt = _manifest?['created_at'] as String? ?? '';
    final fileSize = (_manifest?['file_size'] as num?) ?? 0;

    final hasConfigRestore =
        {'settings', 'cameras', 'database'}.intersection(_selected).isNotEmpty;

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // Backup info header
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: const Color(0xFF1E1E1E),
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: const Color(0xFF404040)),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _infoRow(Icons.folder, Colors.grey,
                  _filePath ?? ''),
              _infoRow(Icons.access_time, Colors.grey,
                  'Created: ${_formatTimestamp(createdAt)}'),
              _infoRow(Icons.storage, Colors.grey,
                  'File size: ${_formatBytes(fileSize)}'),
            ],
          ),
        ),
        const SizedBox(height: 16),
        Text('Select what to restore:',
            style: TextStyle(fontSize: 14, color: Colors.grey[400])),
        const SizedBox(height: 8),
        for (final comp in available)
          _restoreCheckbox(comp, details[comp] as Map<String, dynamic>?),
        if (hasConfigRestore) ...[
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: Colors.orange.withValues(alpha: 0.1),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.orange.withValues(alpha: 0.3)),
            ),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Icon(Icons.warning_amber, color: Colors.orange, size: 18),
                const SizedBox(width: 8),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text(
                        'Restoring settings, cameras, or database will overwrite current configuration.',
                        style: TextStyle(color: Colors.orange, fontSize: 13),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        'Recording will pause during restore and automatically resume.',
                        style: TextStyle(
                            color: Colors.orange.withValues(alpha: 0.8),
                            fontSize: 12),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ],
        if (_selected.contains('recordings') || _selected.contains('thumbnails')) ...[
          SizedBox(height: hasConfigRestore ? 8 : 12),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: Colors.blue.withValues(alpha: 0.1),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.blue.withValues(alpha: 0.3)),
            ),
            child: const Row(
              children: [
                Icon(Icons.info_outline, color: Colors.blue, size: 18),
                SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'Recordings and thumbnails will be merged — existing files are kept, only missing files are added.',
                    style: TextStyle(color: Colors.blue, fontSize: 13),
                  ),
                ),
              ],
            ),
          ),
        ],
        if (_error != null) ...[
          const SizedBox(height: 8),
          Text(_error!, style: const TextStyle(color: Colors.red, fontSize: 13)),
        ],
        const SizedBox(height: 16),
        Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('Cancel'),
            ),
            const SizedBox(width: 8),
            ElevatedButton.icon(
              onPressed: _selected.isEmpty ? null : _startRestore,
              icon: const Icon(Icons.restore, size: 18),
              label: const Text('Restore'),
            ),
          ],
        ),
      ],
    );
  }

  Widget _restoreCheckbox(String key, Map<String, dynamic>? details) {
    final size = (details?['size'] as num?) ?? 0;
    final files = (details?['files'] as num?) ?? 0;

    final labels = {
      'settings': ('Settings & Configuration', Icons.settings),
      'cameras': ('Camera Configuration', Icons.videocam),
      'database': ('Full Database', Icons.storage),
      'recordings': ('Recordings', Icons.video_library),
      'thumbnails': ('Thumbnails', Icons.grid_view),
    };
    final (title, icon) = labels[key] ?? (key, Icons.help);

    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: InkWell(
        onTap: () => setState(() {
          if (_selected.contains(key)) {
            _selected.remove(key);
          } else {
            _selected.add(key);
          }
        }),
        borderRadius: BorderRadius.circular(8),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 6, horizontal: 4),
          child: Row(
            children: [
              SizedBox(
                width: 24,
                height: 24,
                child: Checkbox(
                  value: _selected.contains(key),
                  onChanged: (v) => setState(() {
                    if (v == true) {
                      _selected.add(key);
                    } else {
                      _selected.remove(key);
                    }
                  }),
                ),
              ),
              const SizedBox(width: 12),
              Icon(icon, size: 20, color: Colors.grey[400]),
              const SizedBox(width: 10),
              Expanded(
                child: Text(title, style: const TextStyle(fontSize: 14)),
              ),
              Text(
                '${_formatBytes(size)}${files > 1 ? ' ($files files)' : ''}',
                style: TextStyle(fontSize: 12, color: Colors.grey[400]),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildProgress() {
    final status = _progress?['status'] as String? ?? 'starting';
    final filesTotal = (_progress?['files_total'] as num?) ?? 0;
    final filesDone = (_progress?['files_done'] as num?) ?? 0;
    final bytesTotal = (_progress?['bytes_total'] as num?) ?? 0;
    final bytesDone = (_progress?['bytes_done'] as num?) ?? 0;
    final currentFile = _progress?['current_file'] as String? ?? '';
    final fraction = bytesTotal > 0 ? bytesDone / bytesTotal : 0.0;

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Status: ${status[0].toUpperCase()}${status.substring(1)}',
            style: const TextStyle(fontWeight: FontWeight.w500)),
        const SizedBox(height: 12),
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(
            value: fraction.toDouble(),
            minHeight: 8,
            backgroundColor: const Color(0xFF404040),
          ),
        ),
        const SizedBox(height: 8),
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text('${filesDone.toInt()} / ${filesTotal.toInt()} files',
                style: TextStyle(fontSize: 13, color: Colors.grey[400])),
            Text('${_formatBytes(bytesDone)} / ${_formatBytes(bytesTotal)}',
                style: TextStyle(fontSize: 13, color: Colors.grey[400])),
          ],
        ),
        if (currentFile.isNotEmpty) ...[
          const SizedBox(height: 4),
          Text(
            currentFile,
            style: TextStyle(fontSize: 11, color: Colors.grey[600]),
            overflow: TextOverflow.ellipsis,
            maxLines: 1,
          ),
        ],
        const SizedBox(height: 16),
        Align(
          alignment: Alignment.centerRight,
          child: TextButton(
            onPressed: _cancel,
            child: const Text('Cancel', style: TextStyle(color: Colors.orange)),
          ),
        ),
      ],
    );
  }

  Widget _buildDone() {
    final status = _progress?['status'] as String? ?? '';
    final isSuccess = status == 'completed';
    final filesDone = (_progress?['files_done'] as num?) ?? 0;
    final bytesDone = (_progress?['bytes_done'] as num?) ?? 0;

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (isSuccess) ...[
          Row(
            children: [
              const Icon(Icons.check_circle, color: Colors.green, size: 24),
              const SizedBox(width: 12),
              const Expanded(
                child: Text('Restore completed successfully.',
                    style: TextStyle(fontSize: 14)),
              ),
            ],
          ),
          const SizedBox(height: 12),
          _infoRow(Icons.storage, Colors.grey,
              'Restored ${_formatBytes(bytesDone)} (${filesDone.toInt()} files)'),
          if (_needsRestart) ...[
            const SizedBox(height: 12),
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: Colors.orange.withValues(alpha: 0.1),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.orange.withValues(alpha: 0.3)),
              ),
              child: const Row(
                children: [
                  Icon(Icons.info_outline, color: Colors.orange, size: 18),
                  SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'Settings and services have been reloaded. If anything seems off, restart the RichIris service.',
                      style: TextStyle(color: Colors.orange, fontSize: 13),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ] else ...[
          Row(
            children: [
              Icon(
                status == 'cancelled' ? Icons.cancel : Icons.error,
                color: status == 'cancelled' ? Colors.orange : Colors.red,
                size: 24,
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Text(
                  status == 'cancelled'
                      ? 'Restore was cancelled.'
                      : 'Restore failed.',
                  style: const TextStyle(fontSize: 14),
                ),
              ),
            ],
          ),
          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(_error!, style: const TextStyle(color: Colors.red, fontSize: 13)),
          ],
        ],
        const SizedBox(height: 20),
        Align(
          alignment: Alignment.centerRight,
          child: ElevatedButton(
            onPressed: () => Navigator.of(context).pop(isSuccess),
            child: const Text('Close'),
          ),
        ),
      ],
    );
  }

  String _formatBytes(num bytes) {
    if (bytes < 1024) return '${bytes.toInt()} B';
    if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
    if (bytes < 1024 * 1024 * 1024) {
      return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
    }
    return '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(2)} GB';
  }

  String _formatTimestamp(String iso) {
    try {
      final dt = DateTime.parse(iso).toLocal();
      return '${dt.year}-${dt.month.toString().padLeft(2, '0')}-${dt.day.toString().padLeft(2, '0')} '
          '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
    } catch (_) {
      return iso;
    }
  }

  Widget _infoRow(IconData icon, Color color, String text) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          Icon(icon, size: 16, color: color),
          const SizedBox(width: 8),
          Expanded(
            child: Text(text,
                style: const TextStyle(fontSize: 13),
                overflow: TextOverflow.ellipsis),
          ),
        ],
      ),
    );
  }
}
