import 'package:flutter/material.dart';

import '../services/update_service.dart';
import 'update_dialog.dart';

class VersionInfoDialog extends StatefulWidget {
  final String currentVersion;
  final UpdateService updateService;

  const VersionInfoDialog({
    super.key,
    required this.currentVersion,
    required this.updateService,
  });

  @override
  State<VersionInfoDialog> createState() => _VersionInfoDialogState();
}

enum _CheckState { idle, checking, upToDate, updateAvailable, error }

class _VersionInfoDialogState extends State<VersionInfoDialog> {
  _CheckState _state = _CheckState.idle;
  UpdateInfo? _update;
  String? _lastChecked;
  String? _error;

  @override
  void initState() {
    super.initState();
    _loadCached();
  }

  Future<void> _loadCached() async {
    try {
      final update = await widget.updateService.getUpdate();
      if (mounted) {
        setState(() {
          _update = update;
          _lastChecked = update?.lastChecked;
        });
      }
    } catch (_) {}
  }

  Future<void> _checkForUpdates() async {
    setState(() {
      _state = _CheckState.checking;
      _error = null;
    });

    try {
      final update = await widget.updateService.checkNow();
      if (!mounted) return;
      setState(() {
        _update = update;
        _state = update != null ? _CheckState.updateAvailable : _CheckState.upToDate;
      });
    } catch (e) {
      if (mounted) {
        setState(() {
          _state = _CheckState.error;
          _error = 'Could not check for updates';
        });
      }
    }
  }

  void _showUpdateDialog() {
    Navigator.of(context).pop();
    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (_) => UpdateDialog(
        update: _update!,
        updateService: widget.updateService,
      ),
    );
  }

  String _formatLastChecked(String? iso) {
    if (iso == null) return 'Never';
    try {
      final dt = DateTime.parse(iso);
      final diff = DateTime.now().toUtc().difference(dt);
      if (diff.inMinutes < 1) return 'Just now';
      if (diff.inMinutes < 60) return '${diff.inMinutes}m ago';
      if (diff.inHours < 24) return '${diff.inHours}h ago';
      return '${diff.inDays}d ago';
    } catch (_) {
      return iso;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Dialog(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 420, minWidth: 340),
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              // App icon / title
              Icon(Icons.videocam, color: Colors.blue[400], size: 40),
              const SizedBox(height: 12),
              const Text('RichIris NVR', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
              const SizedBox(height: 4),
              Text(
                'v${widget.currentVersion}',
                style: TextStyle(fontSize: 14, color: Colors.grey[400]),
              ),
              const SizedBox(height: 4),
              Text(
                'Last checked: ${_formatLastChecked(_lastChecked ?? _update?.lastChecked)}',
                style: TextStyle(fontSize: 11, color: Colors.grey[600]),
              ),
              const SizedBox(height: 20),

              // State-dependent content
              if (_state == _CheckState.checking) ...[
                const SizedBox(height: 8),
                const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2)),
                const SizedBox(height: 12),
                Text('Checking for updates...', style: TextStyle(color: Colors.grey[400], fontSize: 13)),
                const SizedBox(height: 20),
              ] else if (_state == _CheckState.upToDate) ...[
                Icon(Icons.check_circle, color: Colors.green[400], size: 32),
                const SizedBox(height: 8),
                Text("You're running the latest version", style: TextStyle(color: Colors.green[400], fontSize: 13)),
                const SizedBox(height: 20),
              ] else if (_state == _CheckState.updateAvailable && _update != null) ...[
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: Colors.blue.withValues(alpha: 0.1),
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(color: Colors.blue.withValues(alpha: 0.3)),
                  ),
                  child: Column(
                    children: [
                      Text(
                        'v${_update!.version} available',
                        style: TextStyle(color: Colors.blue[300], fontWeight: FontWeight.w600, fontSize: 14),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        'Released ${_update!.publishedAt.split("T").first}',
                        style: TextStyle(color: Colors.grey[500], fontSize: 12),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                FilledButton.icon(
                  onPressed: _showUpdateDialog,
                  icon: const Icon(Icons.download, size: 18),
                  label: const Text('View update details'),
                ),
                const SizedBox(height: 12),
              ] else if (_state == _CheckState.error) ...[
                Icon(Icons.error_outline, color: Colors.red[400], size: 32),
                const SizedBox(height: 8),
                Text(_error ?? 'Check failed', style: TextStyle(color: Colors.red[400], fontSize: 13)),
                const SizedBox(height: 12),
              ],

              // Actions
              if (_state != _CheckState.checking)
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    if (_state != _CheckState.idle && _state != _CheckState.updateAvailable)
                      TextButton(
                        onPressed: () => Navigator.of(context).pop(),
                        child: const Text('Close'),
                      ),
                    if (_state != _CheckState.updateAvailable) ...[
                      const SizedBox(width: 8),
                      OutlinedButton.icon(
                        onPressed: _checkForUpdates,
                        icon: const Icon(Icons.refresh, size: 18),
                        label: Text(_state == _CheckState.idle ? 'Check for updates' : 'Check again'),
                      ),
                    ],
                    if (_state == _CheckState.idle)
                      TextButton(
                        onPressed: () => Navigator.of(context).pop(),
                        child: const Text('Close'),
                      ),
                  ],
                ),
            ],
          ),
        ),
      ),
    );
  }
}
