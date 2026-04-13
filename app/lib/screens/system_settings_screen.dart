import 'dart:async';
import 'dart:io' show Platform, Process;
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';
import '../services/backup_api.dart';
import '../services/settings_api.dart';
import '../widgets/backup_restore_dialog.dart';

class SystemSettingsScreen extends StatefulWidget {
  final SettingsApi settingsApi;
  final BackupApi? backupApi;

  const SystemSettingsScreen({super.key, required this.settingsApi, this.backupApi});

  @override
  State<SystemSettingsScreen> createState() => _SystemSettingsScreenState();
}

class _SystemSettingsScreenState extends State<SystemSettingsScreen> {
  Map<String, Map<String, dynamic>>? _settings;
  final Map<String, String> _edits = {};
  bool _backendLoading = true;
  bool _saving = false;
  String? _error; // save errors only
  String? _backendError; // load/connectivity errors
  bool _restartRequired = false;
  bool _saved = false;

  // Data directory state
  Map<String, dynamic>? _dataDirInfo;


  // Service diagnostics state
  Map<String, dynamic>? _serviceInfo;
  String? _serviceStatus; // "Running", "Stopped", "Not Installed", or null
  bool _serviceLoading = false;
  String? _serviceError;
  Timer? _serviceRefreshTimer;

  @override
  void initState() {
    super.initState();
    _load();
    if (Platform.isWindows) {
      _loadServiceInfo();
      _serviceRefreshTimer = Timer.periodic(
        const Duration(seconds: 10),
        (_) => _loadServiceInfo(),
      );
    }
  }

  @override
  void dispose() {
    _serviceRefreshTimer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    // If the cache was pre-warmed at app startup, hydrate immediately so the
    // user never sees a loading spinner on first open.
    final api = widget.settingsApi;
    if (_settings == null && api.cachedSettings != null) {
      _settings = api.cachedSettings;
      _dataDirInfo = api.cachedDataDir;
      _backendLoading = false;
    }
    setState(() {
      _backendLoading = _settings == null;
      _backendError = null;
    });
    // Always refresh from the backend so the data is authoritative.
    try {
      final settings = await api.fetchSettings();
      Map<String, dynamic>? dataDirInfo;
      try {
        dataDirInfo = await api.fetchDataDir();
      } catch (_) {}
      if (mounted) {
        setState(() {
          _settings = settings;
          // Only update data dir if the fetch actually succeeded — don't
          // overwrite a previously cached value with null on failure.
          if (dataDirInfo != null) _dataDirInfo = dataDirInfo;
          _backendLoading = false;
          _backendError = null;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _backendLoading = false;
          // Only show error if we have no cached fallback at all.
          if (_settings == null) {
            _backendError = 'Failed to load settings: $e';
          }
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

  Future<void> _loadServiceInfo() async {
    // Query Windows service status via sc query
    try {
      final scResult = await Process.run('sc', ['query', 'RichIris']);
      final output = scResult.stdout.toString();
      if (scResult.exitCode != 0) {
        if (mounted) setState(() => _serviceStatus = 'Not Installed');
      } else if (output.contains('RUNNING')) {
        if (mounted) setState(() => _serviceStatus = 'Running');
      } else if (output.contains('STOPPED')) {
        if (mounted) setState(() => _serviceStatus = 'Stopped');
      } else if (output.contains('STOP_PENDING')) {
        if (mounted) setState(() => _serviceStatus = 'Stopping...');
      } else if (output.contains('START_PENDING')) {
        if (mounted) setState(() => _serviceStatus = 'Starting...');
      } else {
        if (mounted) setState(() => _serviceStatus = 'Unknown');
      }
    } catch (_) {
      if (mounted) setState(() => _serviceStatus = null);
    }

    // Fetch diagnostics from backend API (only if service is running)
    if (_serviceStatus == 'Running') {
      try {
        final info = await widget.settingsApi.fetchServiceInfo();
        if (mounted) setState(() {
          _serviceInfo = info;
          _serviceError = null;
        });
      } catch (_) {
        // Backend not reachable — keep stale info
      }
    }
  }

  Future<void> _runServiceCommand(String action, {List<String>? args}) async {
    setState(() {
      _serviceLoading = true;
      _serviceError = null;
    });

    try {
      String command;

      if (action == 'install' || action == 'uninstall') {
        // nssm required for install/uninstall — find it
        final nssmPath = await _findNssm();
        if (nssmPath == null) {
          setState(() {
            _serviceLoading = false;
            _serviceError = 'nssm.exe not found. Cannot $action service.';
          });
          return;
        }
        if (action == 'uninstall') {
          command = 'Start-Process -FilePath "$nssmPath" -ArgumentList "stop RichIris" -Verb RunAs -Wait -ErrorAction SilentlyContinue; '
              'Start-Process -FilePath "$nssmPath" -ArgumentList "remove RichIris confirm" -Verb RunAs -Wait';
          command = command.replaceAll(r'$nssmPath', nssmPath);
        } else {
          final installArgs = args?.join(' ') ?? '';
          command = 'Start-Process -FilePath "$nssmPath" -ArgumentList "install RichIris $installArgs" -Verb RunAs -Wait';
          command = command.replaceAll(r'$nssmPath', nssmPath).replaceAll(r'$installArgs', installArgs);
        }
      } else {
        // start/stop/restart use sc.exe (built-in, no nssm needed)
        if (action == 'restart') {
          command = 'Start-Process -FilePath "sc.exe" -ArgumentList "stop RichIris" -Verb RunAs -Wait; '
              'Start-Process -FilePath "sc.exe" -ArgumentList "start RichIris" -Verb RunAs -Wait';
        } else {
          command = 'Start-Process -FilePath "sc.exe" -ArgumentList "$action RichIris" -Verb RunAs -Wait';
          command = command.replaceAll(r'$action', action);
        }
      }

      await Process.run('powershell', ['-Command', command]);
      // Wait a moment for service state to change
      await Future.delayed(const Duration(seconds: 2));
      if (mounted) {
        await _loadServiceInfo();
        // Reload settings if service was just started (backend now available)
        if (_serviceStatus == 'Running' && _settings == null) {
          await _load();
        }
      }
    } catch (e) {
      if (mounted) setState(() => _serviceError = 'Failed: $e');
    } finally {
      if (mounted) setState(() => _serviceLoading = false);
    }
  }

  Future<String?> _findNssm() async {
    // Check relative to the app executable
    final exePath = Platform.resolvedExecutable;
    final exeDir = exePath.substring(0, exePath.lastIndexOf(Platform.pathSeparator));
    final candidates = [
      '$exeDir${Platform.pathSeparator}dependencies${Platform.pathSeparator}nssm.exe',
      '$exeDir${Platform.pathSeparator}nssm.exe',
    ];

    for (final path in candidates) {
      try {
        final result = await Process.run('cmd', ['/c', 'if', 'exist', path, 'echo', 'found']);
        if (result.stdout.toString().contains('found')) return path;
      } catch (_) {}
    }

    // Try on PATH
    try {
      final result = await Process.run('where', ['nssm']);
      final path = result.stdout.toString().trim().split('\n').first.trim();
      if (path.isNotEmpty && result.exitCode == 0) return path;
    } catch (_) {}

    return null;
  }

  Future<bool> _confirmServiceAction(String action) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('${action[0].toUpperCase()}${action.substring(1)} Service?'),
        content: Text(
          action == 'stop'
              ? 'Stopping the service will stop all recording and make the backend unavailable. Continue?'
              : action == 'uninstall'
                  ? 'This will stop and remove the RichIris Windows service. You can reinstall it later. Continue?'
                  : action == 'restart'
                      ? 'This will briefly stop all recording while the service restarts. Continue?'
                      : 'Continue with $action?',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          ElevatedButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: action == 'uninstall' || action == 'stop'
                ? ElevatedButton.styleFrom(backgroundColor: Colors.red[700])
                : null,
            child: Text(action[0].toUpperCase() + action.substring(1)),
          ),
        ],
      ),
    );
    return result == true;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Settings'),
        actions: [
          if (_edits.isNotEmpty && _settings != null)
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
      body: _buildContent(),
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
        if (_settings == null && !_backendLoading)
          Container(
            margin: const EdgeInsets.only(bottom: 16),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
            decoration: BoxDecoration(
              color: Colors.orange.withValues(alpha: 0.15),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.orange.withValues(alpha: 0.4)),
            ),
            child: Row(
              children: [
                const Icon(Icons.cloud_off, color: Colors.orange, size: 20),
                const SizedBox(width: 12),
                const Expanded(
                  child: Text(
                    'Backend is unreachable. Local settings are still available.',
                    style: TextStyle(color: Colors.orange),
                  ),
                ),
                TextButton(
                  onPressed: _load,
                  child: const Text('Retry'),
                ),
              ],
            ),
          ),
        if (Platform.isWindows)
          _buildSection('Backend Service', Icons.miscellaneous_services, [
            _serviceField(),
          ]),
        _buildBackendSection('General', Icons.settings, () => [
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
        _buildBackendSection('Storage', Icons.storage, () => [
          _dataDirField(),
        ]),
        _buildBackendSection('Retention', Icons.auto_delete, () => [
          _numberField('retention', 'max_age_days', 'Max Age (days)'),
          _numberField('retention', 'max_storage_gb', 'Max Storage (GB)'),
        ]),
        _buildBackendSection('Trickplay Thumbnails', Icons.grid_view, () => [
          _toggleField('trickplay', 'enabled', 'Enable Trickplay'),
        ]),
        _buildBackendSection('Logging', Icons.article, () => [
          _dropdownField('logging', 'level', 'Log Level', [
            'DEBUG',
            'INFO',
            'WARNING',
            'ERROR',
          ]),
        ]),
        if (Platform.isWindows && widget.backupApi != null)
          _buildBackendSection('Backup & Restore', Icons.backup, () => [
            _backupRestoreField(),
          ]),
        Center(
          child: TextButton.icon(
            onPressed: () => launchUrl(
              Uri.parse('https://ko-fi.com/richard1912'),
              mode: LaunchMode.externalApplication,
            ),
            icon: const Icon(Icons.favorite, color: Colors.redAccent, size: 18),
            label: Text(
              'Support RichIris on Ko-fi',
              style: TextStyle(color: Colors.grey[400]),
            ),
          ),
        ),
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

  Widget _buildBackendSection(String title, IconData icon, List<Widget> Function() childrenBuilder) {
    if (_backendLoading && _settings == null) {
      return _buildSection(title, icon, [
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: Row(
            children: [
              const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)),
              const SizedBox(width: 12),
              Text('Loading...', style: TextStyle(color: Colors.grey[500])),
            ],
          ),
        ),
      ]);
    }
    if (_settings == null) {
      return _buildSection(title, icon, [
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: Row(
            children: [
              Icon(Icons.cloud_off, size: 16, color: Colors.grey[600]),
              const SizedBox(width: 8),
              Text('Backend unavailable', style: TextStyle(color: Colors.grey[600])),
            ],
          ),
        ),
      ]);
    }
    return _buildSection(title, icon, childrenBuilder());
  }

  String _formatUptime(int seconds) {
    final days = seconds ~/ 86400;
    final hours = (seconds % 86400) ~/ 3600;
    final mins = (seconds % 3600) ~/ 60;
    if (days > 0) return '${days}d ${hours}h ${mins}m';
    if (hours > 0) return '${hours}h ${mins}m';
    return '${mins}m';
  }

  Widget _serviceField() {
    final isRunning = _serviceStatus == 'Running';
    final isStopped = _serviceStatus == 'Stopped';
    final isNotInstalled = _serviceStatus == 'Not Installed';
    final info = _serviceInfo;

    Color statusColor;
    IconData statusIcon;
    if (isRunning) {
      statusColor = Colors.green;
      statusIcon = Icons.check_circle;
    } else if (isStopped) {
      statusColor = Colors.orange;
      statusIcon = Icons.pause_circle;
    } else if (isNotInstalled) {
      statusColor = Colors.red;
      statusIcon = Icons.cancel;
    } else {
      statusColor = Colors.grey;
      statusIcon = Icons.help_outline;
    }

    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Status row
          Row(
            children: [
              Icon(statusIcon, size: 18, color: statusColor),
              const SizedBox(width: 8),
              Text(
                'Status: ${_serviceStatus ?? 'Checking...'}',
                style: TextStyle(fontSize: 14, fontWeight: FontWeight.w500, color: statusColor),
              ),
              const Spacer(),
              if (_serviceLoading)
                const SizedBox(
                  width: 16, height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              else
                IconButton(
                  onPressed: _loadServiceInfo,
                  icon: const Icon(Icons.refresh, size: 18),
                  tooltip: 'Refresh',
                  constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
                  padding: EdgeInsets.zero,
                ),
            ],
          ),

          // Diagnostics (only shown when running and we have info)
          if (isRunning && info != null) ...[
            const SizedBox(height: 12),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: const Color(0xFF1A1A1A),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: const Color(0xFF333333)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _diagRow('PID', '${info['pid']}'),
                  _diagRow('Uptime', _formatUptime((info['uptime_seconds'] as num?)?.toInt() ?? 0)),
                  _diagRow('Memory', '${info['memory_mb']} MB'),
                  _diagRow('Python', '${info['python_version']}'),
                  const Divider(height: 16, color: Color(0xFF333333)),
                  _diagRow('Cameras', '${info['active_streams']} / ${info['total_cameras']} streaming'),
                  _diagRow('FFmpeg processes', '${info['ffmpeg_processes']}'),
                  _diagRow('go2rtc', info['go2rtc_running'] == true
                      ? 'Running (PID ${info['go2rtc_pid'] ?? '?'})'
                      : 'Not running'),
                  const Divider(height: 16, color: Color(0xFF333333)),
                  _diagRow('Port', '${info['port']}'),
                  _diagRow('Log file', '${info['log_file_size_mb']} MB'),
                  _diagRow('Started', _formatStartupTime(info['startup_time'] as String?)),
                ],
              ),
            ),
          ],

          if (_serviceError != null) ...[
            const SizedBox(height: 8),
            Text(_serviceError!, style: const TextStyle(color: Colors.red, fontSize: 13)),
          ],

          // Control buttons
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              if (isStopped || _serviceStatus == 'Starting...')
                _serviceButton('Start', Icons.play_arrow, Colors.green, () async {
                  await _runServiceCommand('start');
                }),
              if (isRunning || _serviceStatus == 'Stopping...')
                _serviceButton('Stop', Icons.stop, Colors.red, () async {
                  if (await _confirmServiceAction('stop')) {
                    await _runServiceCommand('stop');
                  }
                }),
              if (isRunning)
                _serviceButton('Restart', Icons.restart_alt, Colors.orange, () async {
                  if (await _confirmServiceAction('restart')) {
                    await _runServiceCommand('restart');
                  }
                }),
              if (isNotInstalled)
                _serviceButton('Install', Icons.install_desktop, Colors.blue, () async {
                  await _runServiceCommand('install');
                }),
              if (!isNotInstalled && _serviceStatus != null)
                _serviceButton('Uninstall', Icons.delete_forever, Colors.red[300]!, () async {
                  if (await _confirmServiceAction('uninstall')) {
                    await _runServiceCommand('uninstall');
                    setState(() => _serviceInfo = null);
                  }
                }),
            ],
          ),

          const SizedBox(height: 8),
          Text(
            'Service controls require administrator privileges (UAC prompt).',
            style: TextStyle(fontSize: 11, color: Colors.grey[600]),
          ),
        ],
      ),
    );
  }

  Widget _serviceButton(String label, IconData icon, Color color, Future<void> Function() onPressed) {
    return OutlinedButton.icon(
      onPressed: _serviceLoading ? null : onPressed,
      icon: Icon(icon, size: 16, color: _serviceLoading ? Colors.grey : color),
      label: Text(label),
      style: OutlinedButton.styleFrom(
        foregroundColor: color,
        side: BorderSide(color: color.withValues(alpha: 0.4)),
      ),
    );
  }

  Widget _diagRow(String label, String value) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          SizedBox(
            width: 130,
            child: Text(label, style: TextStyle(fontSize: 12, color: Colors.grey[500])),
          ),
          Expanded(
            child: Text(value, style: const TextStyle(fontSize: 12)),
          ),
        ],
      ),
    );
  }

  String _formatStartupTime(String? iso) {
    if (iso == null) return '—';
    try {
      final dt = DateTime.parse(iso);
      return '${dt.year}-${dt.month.toString().padLeft(2, '0')}-${dt.day.toString().padLeft(2, '0')} '
          '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}:${dt.second.toString().padLeft(2, '0')}';
    } catch (_) {
      return iso;
    }
  }

  Widget _dataDirField() {
    final dataDir = _dataDirInfo?['data_dir'] as String? ?? '';
    final freeGb = _dataDirInfo?['free_space_gb'];
    final totalGb = _dataDirInfo?['total_size_gb'];

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
              Text(
                totalGb != null ? 'Data size: $totalGb GB' : 'Data size: calculating...',
                style: TextStyle(fontSize: 12, color: Colors.grey[500]),
              ),
              const SizedBox(width: 16),
              Text(
                freeGb != null ? 'Free space: $freeGb GB' : 'Free space: ...',
                style: TextStyle(fontSize: 12, color: Colors.grey[500]),
              ),
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

  Widget _backupRestoreField() {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Create a backup of your settings, cameras, database, recordings, and thumbnails, or restore from a previous backup.',
            style: TextStyle(fontSize: 13, color: Colors.grey[500]),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              ElevatedButton.icon(
                onPressed: () => _openBackupDialog(),
                icon: const Icon(Icons.backup, size: 16),
                label: const Text('Create Backup'),
              ),
              const SizedBox(width: 8),
              OutlinedButton.icon(
                onPressed: () => _openRestoreDialog(),
                icon: const Icon(Icons.restore, size: 16),
                label: const Text('Restore from Backup'),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Future<void> _openBackupDialog() async {
    await showDialog(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => CreateBackupDialog(backupApi: widget.backupApi!),
    );
  }

  Future<void> _openRestoreDialog() async {
    final result = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => RestoreBackupDialog(backupApi: widget.backupApi!),
    );
    if (result == true && mounted) {
      // Reload settings after restore
      await _load();
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
