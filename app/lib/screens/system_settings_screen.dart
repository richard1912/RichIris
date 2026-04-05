import 'dart:io' show Platform, Process;
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../services/settings_api.dart';

class SystemSettingsScreen extends StatefulWidget {
  final SettingsApi settingsApi;

  const SystemSettingsScreen({super.key, required this.settingsApi});

  @override
  State<SystemSettingsScreen> createState() => _SystemSettingsScreenState();
}

class _SystemSettingsScreenState extends State<SystemSettingsScreen> {
  Map<String, Map<String, dynamic>>? _settings;
  final Map<String, String> _edits = {};
  bool _loading = true;
  bool _saving = false;
  String? _error;
  bool _restartRequired = false;
  bool _saved = false;

  // Data directory state
  Map<String, dynamic>? _dataDirInfo;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final settings = await widget.settingsApi.fetchSettings();
      Map<String, dynamic>? dataDirInfo;
      try {
        dataDirInfo = await widget.settingsApi.fetchDataDir();
      } catch (_) {}
      if (mounted) {
        setState(() {
          _settings = settings;
          _dataDirInfo = dataDirInfo;
          _loading = false;
          _error = null;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _loading = false;
          _error = 'Failed to load settings: $e';
        });
      }
    }
  }

  String _getValue(String category, String key) {
    final fullKey = '$category.$key';
    if (_edits.containsKey(fullKey)) return _edits[fullKey]!;
    final cat = _settings?[category];
    if (cat == null) return '';
    final setting = cat[key];
    if (setting is Map) return setting['value']?.toString() ?? '';
    return '';
  }

  bool _requiresRestart(String category, String key) {
    final cat = _settings?[category];
    if (cat == null) return false;
    final setting = cat[key];
    if (setting is Map) return setting['requires_restart'] == true;
    return false;
  }

  void _setValue(String category, String key, String value) {
    setState(() {
      _edits['$category.$key'] = value;
      _saved = false;
    });
  }

  Future<void> _save() async {
    if (_edits.isEmpty) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      final result = await widget.settingsApi.updateSettings(_edits);
      if (mounted) {
        final restart = result['restart_required'] == true;
        setState(() {
          _saving = false;
          _edits.clear();
          _saved = true;
          if (restart) _restartRequired = true;
        });
        // Reload to get fresh values
        await _load();
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _saving = false;
          _error = 'Failed to save: $e';
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('System Settings'),
        actions: [
          if (_edits.isNotEmpty)
            TextButton.icon(
              onPressed: _saving ? null : _save,
              icon: _saving
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.save, size: 18),
              label: const Text('Save'),
            ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null && _settings == null
              ? Center(child: Text(_error!, style: const TextStyle(color: Colors.red)))
              : _buildContent(),
    );
  }

  Widget _buildContent() {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        if (_restartRequired)
          Container(
            margin: const EdgeInsets.only(bottom: 16),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
            decoration: BoxDecoration(
              color: Colors.orange.withValues(alpha: 0.15),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.orange.withValues(alpha: 0.4)),
            ),
            child: const Row(
              children: [
                Icon(Icons.warning_amber, color: Colors.orange, size: 20),
                SizedBox(width: 12),
                Expanded(
                  child: Text(
                    'Some changes require a service restart to take effect.',
                    style: TextStyle(color: Colors.orange),
                  ),
                ),
              ],
            ),
          ),
        if (_saved && _edits.isEmpty)
          Container(
            margin: const EdgeInsets.only(bottom: 16),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
            decoration: BoxDecoration(
              color: Colors.green.withValues(alpha: 0.15),
              borderRadius: BorderRadius.circular(8),
            ),
            child: const Row(
              children: [
                Icon(Icons.check_circle, color: Colors.green, size: 20),
                SizedBox(width: 12),
                Text('Settings saved.', style: TextStyle(color: Colors.green)),
              ],
            ),
          ),
        if (_error != null && _settings != null)
          Container(
            margin: const EdgeInsets.only(bottom: 16),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
            decoration: BoxDecoration(
              color: Colors.red.withValues(alpha: 0.15),
              borderRadius: BorderRadius.circular(8),
            ),
            child: Text(_error!, style: const TextStyle(color: Colors.red)),
          ),
        _buildSection('General', Icons.settings, [
          _dropdownField('logging', 'timezone', 'Timezone', [
            'UTC',
            'US/Eastern',
            'US/Central',
            'US/Mountain',
            'US/Pacific',
            'Canada/Eastern',
            'Canada/Central',
            'Canada/Pacific',
            'Europe/London',
            'Europe/Paris',
            'Europe/Berlin',
            'Europe/Amsterdam',
            'Europe/Stockholm',
            'Europe/Moscow',
            'Asia/Dubai',
            'Asia/Kolkata',
            'Asia/Singapore',
            'Asia/Shanghai',
            'Asia/Tokyo',
            'Asia/Seoul',
            'Australia/Perth',
            'Australia/Adelaide',
            'Australia/Sydney',
            'Australia/Brisbane',
            'Pacific/Auckland',
          ]),
        ]),
        if (_dataDirInfo != null)
          _buildSection('Storage', Icons.storage, [
            _dataDirField(),
          ]),
        _buildSection('Retention', Icons.auto_delete, [
          _numberField('retention', 'max_age_days', 'Max Age (days)'),
          _numberField('retention', 'max_storage_gb', 'Max Storage (GB)'),
        ]),
        _buildSection('Trickplay Thumbnails', Icons.grid_view, [
          _toggleField('trickplay', 'enabled', 'Enable Trickplay'),
        ]),
        _buildSection('Logging', Icons.article, [
          _dropdownField('logging', 'level', 'Log Level', [
            'DEBUG',
            'INFO',
            'WARNING',
            'ERROR',
          ]),
        ]),
        const SizedBox(height: 80),
      ],
    );
  }

  Widget _buildSection(String title, IconData icon, List<Widget> children) {
    return Card(
      margin: const EdgeInsets.only(bottom: 16),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, size: 20, color: Colors.grey[400]),
                const SizedBox(width: 8),
                Text(title,
                    style: const TextStyle(
                        fontSize: 16, fontWeight: FontWeight.w600)),
              ],
            ),
            const SizedBox(height: 16),
            ...children,
          ],
        ),
      ),
    );
  }

  Widget _dataDirField() {
    final dataDir = _dataDirInfo?['data_dir'] as String? ?? '';
    final freeGb = _dataDirInfo?['free_space_gb'] ?? 0.0;
    final totalGb = _dataDirInfo?['total_size_gb'] ?? 0.0;

    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Current Data Directory',
              style: TextStyle(fontSize: 12, color: Colors.grey)),
          const SizedBox(height: 6),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            decoration: BoxDecoration(
              color: const Color(0xFF262626),
              borderRadius: BorderRadius.circular(6),
              border: Border.all(color: const Color(0xFF404040)),
            ),
            child: Text(
              dataDir.isEmpty ? '(default)' : dataDir,
              style: TextStyle(color: Colors.grey[300]),
            ),
          ),
          const SizedBox(height: 6),
          Row(
            children: [
              Text('Data size: $totalGb GB',
                  style: TextStyle(fontSize: 12, color: Colors.grey[500])),
              const SizedBox(width: 16),
              Text('Free space: $freeGb GB',
                  style: TextStyle(fontSize: 12, color: Colors.grey[500])),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            'Contains: database, logs, recordings, thumbnails, playback cache',
            style: TextStyle(fontSize: 11, color: Colors.grey[600]),
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              ElevatedButton.icon(
                onPressed: _openDataDirDialog,
                icon: const Icon(Icons.drive_file_move, size: 16),
                label: const Text('Change Data Directory...'),
              ),
              const SizedBox(width: 8),
              OutlinedButton.icon(
                onPressed: dataDir.isNotEmpty
                    ? () => Process.run('explorer.exe', [dataDir])
                    : null,
                icon: const Icon(Icons.folder_open, size: 16),
                label: const Text('Open Folder'),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Future<void> _openDataDirDialog() async {
    final result = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => _DataDirMigrationDialog(
        settingsApi: widget.settingsApi,
        currentPath: _dataDirInfo?['data_dir'] as String? ?? '',
      ),
    );
    if (result == true && mounted) {
      await _load();
      setState(() {
        _restartRequired = true;
      });
    }
  }

  Widget _textField(String category, String key, String label,
      {String? hint}) {
    final restart = _requiresRestart(category, key);
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: TextField(
        controller: TextEditingController(text: _getValue(category, key)),
        decoration: InputDecoration(
          labelText: restart ? '$label *' : label,
          hintText: hint,
          suffixIcon: restart
              ? const Tooltip(
                  message: 'Requires restart',
                  child: Icon(Icons.restart_alt, size: 18, color: Colors.orange),
                )
              : null,
        ),
        onChanged: (v) => _setValue(category, key, v),
      ),
    );
  }

  Widget _numberField(String category, String key, String label) {
    final restart = _requiresRestart(category, key);
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: TextField(
        controller: TextEditingController(text: _getValue(category, key)),
        decoration: InputDecoration(
          labelText: restart ? '$label *' : label,
          suffixIcon: restart
              ? const Tooltip(
                  message: 'Requires restart',
                  child: Icon(Icons.restart_alt, size: 18, color: Colors.orange),
                )
              : null,
        ),
        keyboardType: TextInputType.number,
        inputFormatters: [FilteringTextInputFormatter.digitsOnly],
        onChanged: (v) => _setValue(category, key, v),
      ),
    );
  }

  Widget _dropdownField(
      String category, String key, String label, List<String> options) {
    final value = _getValue(category, key);
    final restart = _requiresRestart(category, key);
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: InputDecorator(
        decoration: InputDecoration(
          labelText: restart ? '$label *' : label,
          suffixIcon: restart
              ? const Tooltip(
                  message: 'Requires restart',
                  child: Icon(Icons.restart_alt, size: 18, color: Colors.orange),
                )
              : null,
        ),
        child: DropdownButtonHideUnderline(
          child: DropdownButton<String>(
            value: options.contains(value) ? value : options.first,
            isExpanded: true,
            isDense: true,
            dropdownColor: const Color(0xFF262626),
            items: options
                .map((o) => DropdownMenuItem(value: o, child: Text(o)))
                .toList(),
            onChanged: (v) {
              if (v != null) _setValue(category, key, v);
            },
          ),
        ),
      ),
    );
  }

  Widget _toggleField(String category, String key, String label) {
    final value = _getValue(category, key).toLowerCase();
    final isOn = value == 'true' || value == '1' || value == 'yes';
    final restart = _requiresRestart(category, key);
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: Text(restart ? '$label *' : label),
        secondary: restart
            ? const Tooltip(
                message: 'Requires restart',
                child: Icon(Icons.restart_alt, size: 18, color: Colors.orange),
              )
            : null,
        value: isOn,
        onChanged: (v) => _setValue(category, key, v.toString()),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Data Directory Migration Dialog
// ---------------------------------------------------------------------------

class _DataDirMigrationDialog extends StatefulWidget {
  final SettingsApi settingsApi;
  final String currentPath;

  const _DataDirMigrationDialog({
    required this.settingsApi,
    required this.currentPath,
  });

  @override
  State<_DataDirMigrationDialog> createState() => _DataDirMigrationDialogState();
}

enum _DataDirStep { pathInput, options, migrating, done }

class _DataDirMigrationDialogState extends State<_DataDirMigrationDialog> {
  late TextEditingController _pathController;
  _DataDirStep _step = _DataDirStep.pathInput;

  bool _validating = false;
  Map<String, dynamic>? _validation;
  String? _error;
  bool _success = false;

  @override
  void initState() {
    super.initState();
    _pathController = TextEditingController();
  }

  @override
  void dispose() {
    _pathController.dispose();
    super.dispose();
  }

  Future<void> _browse() async {
    final result = await FilePicker.platform.getDirectoryPath(
      dialogTitle: 'Select data directory',
    );
    if (result != null && mounted) {
      _pathController.text = result;
    }
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
      final result = await widget.settingsApi.validateDataDir(path);
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

  Future<void> _applyChange(String mode) async {
    setState(() {
      _step = _DataDirStep.migrating;
      _error = null;
    });

    try {
      final result = await widget.settingsApi.updateDataDir(
        _pathController.text.trim(),
        mode,
      );
      if (mounted) {
        setState(() {
          _success = result['updated'] == true;
          _step = _DataDirStep.done;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = 'Failed: $e';
          _step = _DataDirStep.done;
        });
      }
    }
  }

  String _formatGb(dynamic gb) {
    if (gb is num) return '${gb.toStringAsFixed(2)} GB';
    return '$gb GB';
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
                  const Icon(Icons.folder_special, size: 24),
                  const SizedBox(width: 12),
                  Text(
                    _step == _DataDirStep.done
                        ? 'Data Directory Updated'
                        : 'Change Data Directory',
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
      _DataDirStep.pathInput => _buildPathInput(),
      _DataDirStep.options => _buildOptions(),
      _DataDirStep.migrating => _buildMigrating(),
      _DataDirStep.done => _buildDone(),
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
                  hintText: 'Enter new data directory path...',
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
            child: Text(_error!,
                style: const TextStyle(color: Colors.red, fontSize: 13)),
          ),
        if (_validation != null) ...[
          if (_validation!['valid'] == true) ...[
            _infoRow(Icons.check_circle, Colors.green, 'Path is valid and writable'),
            _infoRow(Icons.storage, Colors.grey,
                'Free space: ${_formatGb(_validation!['free_space_gb'])}'),
            _infoRow(Icons.folder, Colors.grey,
                'Current data size: ${_formatGb(_validation!['source_size_gb'])}'),
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
                onPressed: () => setState(() => _step = _DataDirStep.options),
                child: const Text('Next'),
              )
            else
              ElevatedButton(
                onPressed: _validating ? null : _validate,
                child: _validating
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Validate'),
              ),
          ],
        ),
      ],
    );
  }

  Widget _buildOptions() {
    final sourceGb = (_validation?['source_size_gb'] as num?) ?? 0.0;
    final estimateMin = (sourceGb / 0.1).ceil();

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('How would you like to handle existing data?',
            style: TextStyle(fontSize: 14)),
        const SizedBox(height: 16),
        _optionTile(
          icon: Icons.drive_file_move,
          title: 'Move data',
          subtitle: 'Move all data to the new location (frees space on old drive).'
              '${sourceGb > 0 ? " Estimated: ~$estimateMin min." : ""}',
          onTap: () => _applyChange('move'),
        ),
        const SizedBox(height: 8),
        _optionTile(
          icon: Icons.file_copy,
          title: 'Copy data',
          subtitle: 'Copy data to the new location (keeps originals as backup).'
              '${sourceGb > 0 ? " Estimated: ~$estimateMin min." : ""}',
          onTap: () => _applyChange('copy'),
        ),
        const SizedBox(height: 8),
        _optionTile(
          icon: Icons.link,
          title: 'Change path only',
          subtitle:
              'Just update the setting. Use if you already moved files manually or want to start fresh.',
          onTap: () => _applyChange('path_only'),
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
                  'A service restart is required after changing the data directory.',
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
              _step = _DataDirStep.pathInput;
              _error = null;
            }),
            child: const Text('Back'),
          ),
        ),
      ],
    );
  }

  Widget _buildMigrating() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        const Center(child: CircularProgressIndicator()),
        const SizedBox(height: 16),
        const Center(child: Text('Migrating data and updating configuration...')),
        const SizedBox(height: 8),
        Text(
          'Please wait. Do not close the application.',
          style: TextStyle(fontSize: 12, color: Colors.grey[500]),
        ),
      ],
    );
  }

  Widget _buildDone() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Icon(
              _success ? Icons.check_circle : Icons.error,
              color: _success ? Colors.green : Colors.red,
              size: 24,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                _success
                    ? 'Data directory updated. Restart the RichIris service for changes to take effect.'
                    : 'Failed to update data directory.',
                style: const TextStyle(fontSize: 14),
              ),
            ),
          ],
        ),
        if (_error != null) ...[
          const SizedBox(height: 8),
          Text(_error!, style: const TextStyle(color: Colors.red, fontSize: 13)),
        ],
        const SizedBox(height: 20),
        Align(
          alignment: Alignment.centerRight,
          child: ElevatedButton(
            onPressed: () => Navigator.of(context).pop(_success),
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
