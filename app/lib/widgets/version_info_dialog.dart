import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../services/update_service.dart';
import 'update_dialog.dart';

const _releasesPageUrl = 'https://github.com/richard1912/RichIris/releases';

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

enum _CheckState { checking, upToDate, error }

class _VersionInfoDialogState extends State<VersionInfoDialog> {
  _CheckState _state = _CheckState.checking;
  UpdateCheckResult? _result;
  String? _lastChecked;
  String? _error;

  @override
  void initState() {
    super.initState();
    // Auto-check for updates when dialog opens
    _checkForUpdates();
  }

  Future<void> _checkForUpdates() async {
    setState(() {
      _state = _CheckState.checking;
      _error = null;
    });

    try {
      final result = await widget.updateService.checkNow();
      if (!mounted) return;
      _result = result;
      if (result.appUpdate != null) {
        // App update available — jump straight to the update dialog
        _showUpdateDialog(result);
        return;
      }
      setState(() {
        _lastChecked = result.lastChecked;
        _state = _CheckState.upToDate;
      });
    } catch (e) {
      if (mounted) {
        final msg = e.toString();
        setState(() {
          _state = _CheckState.error;
          _error = msg.contains('DioException')
              ? 'Could not reach the server'
              : 'Could not check for updates: $msg';
        });
      }
    }
  }

  void _showUpdateDialog(UpdateCheckResult result) {
    Navigator.of(context).pop();
    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (_) => UpdateDialog(
        update: result.appUpdate!,
        updateService: widget.updateService,
        backendVersion: result.backendVersion,
        backendUpdateAvailable: result.backendUpdateAvailable,
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
                'Last checked: ${_formatLastChecked(_lastChecked ?? _result?.lastChecked)}',
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
                if (_result != null && _result!.backendUpdateAvailable && _result!.backendVersion != null) ...[
                  const SizedBox(height: 12),
                  Container(
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: Colors.orange.withValues(alpha: 0.1),
                      borderRadius: BorderRadius.circular(6),
                      border: Border.all(color: Colors.orange.withValues(alpha: 0.3)),
                    ),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(Icons.dns, color: Colors.orange[400], size: 18),
                        const SizedBox(width: 8),
                        Flexible(
                          child: Text(
                            'Server is running v${_result!.backendVersion}. Run the latest installer on the host machine to update.',
                            style: TextStyle(color: Colors.orange[300], fontSize: 12),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
                const SizedBox(height: 20),
              ] else if (_state == _CheckState.error) ...[
                Icon(Icons.error_outline, color: Colors.red[400], size: 32),
                const SizedBox(height: 8),
                Text(_error ?? 'Check failed', style: TextStyle(color: Colors.red[400], fontSize: 13)),
                const SizedBox(height: 12),
              ],

              // Actions
              if (_state != _CheckState.checking) ...[
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    TextButton(
                      onPressed: () => Navigator.of(context).pop(),
                      child: const Text('Close'),
                    ),
                    const SizedBox(width: 8),
                    OutlinedButton.icon(
                      onPressed: _checkForUpdates,
                      icon: const Icon(Icons.refresh, size: 18),
                      label: const Text('Check again'),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                TextButton.icon(
                  onPressed: () => launchUrl(
                    Uri.parse(_releasesPageUrl),
                    mode: LaunchMode.externalApplication,
                  ),
                  icon: Icon(Icons.open_in_new, size: 14, color: Colors.grey[500]),
                  label: Text(
                    'View all releases on GitHub',
                    style: TextStyle(color: Colors.grey[500], fontSize: 12),
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
