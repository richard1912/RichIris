import 'dart:async';
import 'package:flutter/material.dart';
import 'package:file_picker/file_picker.dart';
import '../services/settings_api.dart';

/// Multi-step dialog for changing the recordings storage location.
///
/// Steps: path input -> validate -> choose mode -> progress -> done
class StorageMigrationDialog extends StatefulWidget {
  final SettingsApi settingsApi;
  final String currentPath;

  const StorageMigrationDialog({
    super.key,
    required this.settingsApi,
    required this.currentPath,
  });

  @override
  State<StorageMigrationDialog> createState() => _StorageMigrationDialogState();
}

enum _Step { pathInput, migrationOptions, progress, done }

class _StorageMigrationDialogState extends State<StorageMigrationDialog> {
  late TextEditingController _pathController;
  _Step _step = _Step.pathInput;

  // Validation
  bool _validating = false;
  Map<String, dynamic>? _validation;

  // Migration
  String? _migrationId;
  Map<String, dynamic>? _progress;
  Timer? _pollTimer;
  bool _finalizing = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _pathController = TextEditingController();
  }

  @override
  void dispose() {
    _pathController.dispose();
    _pollTimer?.cancel();
    super.dispose();
  }

  Future<void> _validate() async {
    final path = _pathController.text.trim();
    if (path.isEmpty) return;

    setState(() {
      _validating = true;
      _validation = null;
      _error = null;
    });

    try {
      final result = await widget.settingsApi.validateStoragePath(path);
      if (mounted) {
        setState(() {
          _validating = false;
          _validation = result;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _validating = false;
          _error = 'Validation failed: $e';
        });
      }
    }
  }

  Future<void> _startMigration(String mode) async {
    setState(() {
      _step = _Step.progress;
      _error = null;
    });

    try {
      if (mode == 'path_only') {
        // Just update the path, no file migration
        setState(() => _finalizing = true);
        await widget.settingsApi.updatePathOnly(_pathController.text.trim());
        if (mounted) {
          setState(() {
            _finalizing = false;
            _step = _Step.done;
          });
        }
        return;
      }

      final result = await widget.settingsApi.startMigration(
        _pathController.text.trim(),
        mode,
      );
      _migrationId = result['migration_id'] as String?;
      _startPolling();
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = 'Failed to start migration: $e';
          _step = _Step.migrationOptions;
        });
      }
    }
  }

  void _startPolling() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(const Duration(seconds: 1), (_) => _pollProgress());
  }

  Future<void> _pollProgress() async {
    if (_migrationId == null) return;
    try {
      final result = await widget.settingsApi.getMigrationProgress(_migrationId!);
      if (!mounted) return;

      setState(() => _progress = result);

      final status = result['status'] as String? ?? '';
      if (status == 'completed' || status == 'cancelled' || status == 'failed') {
        _pollTimer?.cancel();
        if (status == 'completed') {
          _finalize();
        } else if (status == 'failed') {
          setState(() {
            _error = result['error'] as String? ?? 'Migration failed.';
            _step = _Step.done;
          });
          // Still finalize to restart streams even on failure
          _finalizeQuietly();
        } else {
          // Cancelled
          setState(() => _step = _Step.done);
          _finalizeQuietly();
        }
      }
    } catch (e) {
      // Polling error — keep trying
    }
  }

  Future<void> _finalize() async {
    if (_migrationId == null) return;
    setState(() => _finalizing = true);
    try {
      await widget.settingsApi.finalizeMigration(_migrationId!);
    } catch (e) {
      if (mounted) {
        setState(() => _error = 'Finalization warning: $e');
      }
    }
    if (mounted) {
      setState(() {
        _finalizing = false;
        _step = _Step.done;
      });
    }
  }

  Future<void> _finalizeQuietly() async {
    if (_migrationId == null) return;
    try {
      await widget.settingsApi.finalizeMigration(_migrationId!);
    } catch (_) {}
  }

  Future<void> _cancel() async {
    if (_migrationId == null) return;
    try {
      await widget.settingsApi.cancelMigration(_migrationId!);
    } catch (_) {}
  }

  Future<void> _browse() async {
    final result = await FilePicker.platform.getDirectoryPath(
      dialogTitle: 'Select recordings directory',
    );
    if (result != null && mounted) {
      _pathController.text = result;
    }
  }

  String _formatBytes(num bytes) {
    if (bytes < 1024) return '${bytes.toInt()} B';
    if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
    if (bytes < 1024 * 1024 * 1024) return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
    return '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(2)} GB';
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
                  const Icon(Icons.drive_file_move, size: 24),
                  const SizedBox(width: 12),
                  Text(
                    _step == _Step.done ? 'Migration Complete' : 'Change Recordings Location',
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              Text(
                'Current: ${widget.currentPath}',
                style: TextStyle(fontSize: 12, color: Colors.grey[500]),
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
      _Step.pathInput => _buildPathInput(),
      _Step.migrationOptions => _buildMigrationOptions(),
      _Step.progress => _buildProgress(),
      _Step.done => _buildDone(),
    };
  }

  Widget _buildPathInput() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: TextField(
                controller: _pathController,
                decoration: const InputDecoration(
                  hintText: 'Enter new recordings path...',
                  isDense: true,
                ),
              ),
            ),
            const SizedBox(width: 8),
            IconButton(
              onPressed: _browse,
              icon: const Icon(Icons.folder_open),
              tooltip: 'Browse...',
            ),
          ],
        ),
        const SizedBox(height: 12),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Text(_error!, style: const TextStyle(color: Colors.red, fontSize: 13)),
          ),
        if (_validation != null) ...[
          if (_validation!['valid'] == true) ...[
            _infoRow(Icons.check_circle, Colors.green,
                'Path is valid and writable'),
            _infoRow(Icons.storage, Colors.grey,
                'Free space: ${_validation!['free_space_gb']} GB'),
            _infoRow(Icons.folder, Colors.grey,
                'Current recordings size: ${_validation!['source_size_gb']} GB'),
          ] else
            _infoRow(Icons.error, Colors.red,
                _validation!['error']?.toString() ?? 'Invalid path'),
          const SizedBox(height: 12),
        ],
        Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(false),
              child: const Text('Cancel'),
            ),
            const SizedBox(width: 8),
            if (_validation != null && _validation!['valid'] == true)
              ElevatedButton(
                onPressed: () => setState(() => _step = _Step.migrationOptions),
                child: const Text('Next'),
              )
            else
              ElevatedButton(
                onPressed: _validating ? null : _validate,
                child: _validating
                    ? const SizedBox(
                        width: 16, height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Validate'),
              ),
          ],
        ),
      ],
    );
  }

  Widget _buildMigrationOptions() {
    final sourceGb = _validation?['source_size_gb'] ?? 0.0;
    final estimateMin = ((sourceGb as num) / 0.1).ceil(); // ~100 MB/s estimate

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('How would you like to handle existing recordings?',
            style: TextStyle(fontSize: 14)),
        const SizedBox(height: 16),
        _optionTile(
          icon: Icons.drive_file_move,
          title: 'Move recordings',
          subtitle: 'Move all files to the new location (frees space on old drive).'
              '${sourceGb > 0 ? " Estimated time: ~$estimateMin min." : ""}',
          onTap: () => _startMigration('move'),
        ),
        const SizedBox(height: 8),
        _optionTile(
          icon: Icons.file_copy,
          title: 'Copy recordings',
          subtitle: 'Copy files to the new location (keeps originals as backup).'
              '${sourceGb > 0 ? " Estimated time: ~$estimateMin min." : ""}',
          onTap: () => _startMigration('copy'),
        ),
        const SizedBox(height: 8),
        _optionTile(
          icon: Icons.link,
          title: 'Change path only',
          subtitle: 'Just update the setting. Use if you already moved files manually or want to start fresh.',
          onTap: () => _startMigration('path_only'),
        ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.only(top: 12),
            child: Text(_error!, style: const TextStyle(color: Colors.red, fontSize: 13)),
          ),
        const SizedBox(height: 16),
        Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: Colors.orange.withValues(alpha: 0.1),
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: Colors.orange.withValues(alpha: 0.3)),
          ),
          child: const Row(
            children: [
              Icon(Icons.warning_amber, color: Colors.orange, size: 18),
              SizedBox(width: 8),
              Expanded(
                child: Text(
                  'Recording will pause during migration.',
                  style: TextStyle(color: Colors.orange, fontSize: 13),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 12),
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton(
            onPressed: () => setState(() {
              _step = _Step.pathInput;
              _error = null;
            }),
            child: const Text('Back'),
          ),
        ),
      ],
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
        if (_finalizing) ...[
          const Center(child: CircularProgressIndicator()),
          const SizedBox(height: 16),
          const Center(child: Text('Finalizing and restarting streams...')),
        ] else ...[
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
      ],
    );
  }

  Widget _buildDone() {
    final status = _progress?['status'] as String? ?? 'completed';
    final isSuccess = status == 'completed' || _migrationId == null; // path_only has no migration id

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Icon(
              isSuccess ? Icons.check_circle : Icons.warning_amber,
              color: isSuccess ? Colors.green : Colors.orange,
              size: 24,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                isSuccess
                    ? 'Recordings location updated successfully.'
                    : status == 'cancelled'
                        ? 'Migration was cancelled. Streams have been restarted.'
                        : 'Migration failed. Streams have been restarted.',
                style: const TextStyle(fontSize: 14),
              ),
            ),
          ],
        ),
        if (_error != null) ...[
          const SizedBox(height: 8),
          Text(_error!, style: const TextStyle(color: Colors.orange, fontSize: 13)),
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

  Widget _optionTile({
    required IconData icon,
    required String title,
    required String subtitle,
    required VoidCallback onTap,
  }) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(8),
      child: Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          border: Border.all(color: const Color(0xFF404040)),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Row(
          children: [
            Icon(icon, size: 24, color: Colors.grey[400]),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(title, style: const TextStyle(fontWeight: FontWeight.w500)),
                  const SizedBox(height: 2),
                  Text(subtitle,
                      style: TextStyle(fontSize: 12, color: Colors.grey[500])),
                ],
              ),
            ),
            Icon(Icons.chevron_right, color: Colors.grey[600]),
          ],
        ),
      ),
    );
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
