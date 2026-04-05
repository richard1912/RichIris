import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';

import '../services/update_service.dart';

enum _Step { info, downloading, installing, error }

class UpdateDialog extends StatefulWidget {
  final UpdateInfo update;
  final UpdateService updateService;
  final bool updateOnly;

  const UpdateDialog({
    super.key,
    required this.update,
    required this.updateService,
    this.updateOnly = false,
  });

  @override
  State<UpdateDialog> createState() => _UpdateDialogState();
}

class _UpdateDialogState extends State<UpdateDialog> {
  _Step _step = _Step.info;
  int _received = 0;
  int _total = 0;
  String? _error;
  CancelToken? _cancelToken;

  void _dismiss() {
    Navigator.of(context).pop();
    if (widget.updateOnly) exit(0);
  }

  Future<void> _skipVersion() async {
    await widget.updateService.skipVersion(widget.update.version);
    if (mounted) _dismiss();
  }

  Future<void> _startDownload() async {
    setState(() {
      _step = _Step.downloading;
      _received = 0;
      _total = 0;
    });
    _cancelToken = CancelToken();

    try {
      final path = await widget.updateService.downloadUpdate(
        widget.update,
        (received, total) {
          if (mounted) setState(() { _received = received; _total = total; });
        },
        cancelToken: _cancelToken,
      );

      if (!mounted) return;
      setState(() => _step = _Step.installing);

      if (Platform.isWindows) {
        await widget.updateService.installUpdate(path);
      } else if (Platform.isAndroid) {
        // On Android, open the APK via intent
        // The install intent is handled by the OS
        await _installApkAndroid(path);
      }
    } on DioException catch (e) {
      if (e.type == DioExceptionType.cancel) {
        if (mounted) setState(() => _step = _Step.info);
        return;
      }
      if (mounted) {
        setState(() {
          _step = _Step.error;
          _error = 'Download failed: ${e.message}';
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _step = _Step.error;
          _error = e.toString();
        });
      }
    }
  }

  Future<void> _installApkAndroid(String path) async {
    try {
      // Use Android intent to install APK
      final result = await Process.run('am', [
        'start',
        '-a', 'android.intent.action.VIEW',
        '-t', 'application/vnd.android.package-archive',
        '-d', 'file://$path',
        '--grant-read-uri-permission',
      ]);
      if (result.exitCode != 0) {
        throw Exception('Failed to open APK installer');
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _step = _Step.error;
          _error = 'Could not open APK: $e\n\nThe file was downloaded to:\n$path';
        });
      }
    }
  }

  void _cancelDownload() {
    _cancelToken?.cancel();
  }

  String _formatBytes(int bytes) {
    if (bytes < 1024) return '$bytes B';
    if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
    return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
  }

  String _formatDate(String iso) {
    try {
      final dt = DateTime.parse(iso);
      return '${dt.year}-${dt.month.toString().padLeft(2, '0')}-${dt.day.toString().padLeft(2, '0')}';
    } catch (_) {
      return iso;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Dialog(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 560, minWidth: 400),
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _buildHeader(),
              const SizedBox(height: 16),
              switch (_step) {
                _Step.info => _buildInfoStep(),
                _Step.downloading => _buildDownloadingStep(),
                _Step.installing => _buildInstallingStep(),
                _Step.error => _buildErrorStep(),
              },
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildHeader() {
    return Row(
      children: [
        Icon(Icons.system_update, color: Colors.blue[400], size: 28),
        const SizedBox(width: 12),
        Text(
          _step == _Step.error ? 'Update Error' : 'Update Available',
          style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
        ),
      ],
    );
  }

  Widget _buildInfoStep() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // Version badge
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          decoration: BoxDecoration(
            color: Colors.blue.withValues(alpha: 0.1),
            borderRadius: BorderRadius.circular(6),
            border: Border.all(color: Colors.blue.withValues(alpha: 0.3)),
          ),
          child: Text(
            'v${widget.update.version}',
            style: TextStyle(color: Colors.blue[300], fontWeight: FontWeight.w600, fontSize: 14),
          ),
        ),
        if (widget.update.publishedAt.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            'Released ${_formatDate(widget.update.publishedAt)}',
            style: TextStyle(color: Colors.grey[500], fontSize: 12),
          ),
        ],
        const SizedBox(height: 16),

        // Changelog
        if (widget.update.changelog.isNotEmpty) ...[
          Text('Changelog', style: TextStyle(color: Colors.grey[400], fontSize: 13, fontWeight: FontWeight.w500)),
          const SizedBox(height: 8),
          Container(
            constraints: const BoxConstraints(maxHeight: 300),
            decoration: BoxDecoration(
              color: const Color(0xFF1A1A1A),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: const Color(0xFF333333)),
            ),
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(12),
              child: _buildChangelog(widget.update.changelog),
            ),
          ),
          const SizedBox(height: 16),
        ],

        // Buttons
        Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            TextButton(
              onPressed: _skipVersion,
              child: Text('Skip this version', style: TextStyle(color: Colors.grey[400])),
            ),
            const SizedBox(width: 8),
            TextButton(
              onPressed: _dismiss,
              child: const Text('Remind me later'),
            ),
            const SizedBox(width: 8),
            FilledButton.icon(
              onPressed: _startDownload,
              icon: const Icon(Icons.download, size: 18),
              label: const Text('Update now'),
            ),
          ],
        ),
      ],
    );
  }

  Widget _buildChangelog(String markdown) {
    // Simple markdown-like rendering for changelog
    final lines = markdown.split('\n');
    final children = <Widget>[];

    for (final line in lines) {
      final trimmed = line.trimLeft();
      if (trimmed.isEmpty) {
        children.add(const SizedBox(height: 4));
      } else if (trimmed.startsWith('## ')) {
        children.add(Padding(
          padding: EdgeInsets.only(top: children.isEmpty ? 0 : 12, bottom: 4),
          child: Text(
            trimmed.substring(3),
            style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600, color: Colors.grey[200]),
          ),
        ));
      } else if (trimmed.startsWith('# ')) {
        children.add(Padding(
          padding: EdgeInsets.only(top: children.isEmpty ? 0 : 12, bottom: 4),
          child: Text(
            trimmed.substring(2),
            style: TextStyle(fontSize: 15, fontWeight: FontWeight.w700, color: Colors.grey[100]),
          ),
        ));
      } else if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
        children.add(Padding(
          padding: const EdgeInsets.only(left: 8, top: 2),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('  \u2022  ', style: TextStyle(color: Colors.grey[500])),
              Expanded(
                child: Text(
                  trimmed.substring(2),
                  style: TextStyle(fontSize: 13, color: Colors.grey[300], height: 1.4),
                ),
              ),
            ],
          ),
        ));
      } else {
        children.add(Text(
          trimmed,
          style: TextStyle(fontSize: 13, color: Colors.grey[300], height: 1.4),
        ));
      }
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: children,
    );
  }

  Widget _buildDownloadingStep() {
    final progress = _total > 0 ? _received / _total : null;
    final pct = progress != null ? '${(progress * 100).toStringAsFixed(0)}%' : '';

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Downloading update... $pct', style: TextStyle(color: Colors.grey[300])),
        const SizedBox(height: 12),
        LinearProgressIndicator(value: progress),
        const SizedBox(height: 8),
        if (_total > 0)
          Text(
            '${_formatBytes(_received)} / ${_formatBytes(_total)}',
            style: TextStyle(color: Colors.grey[500], fontSize: 12),
          ),
        const SizedBox(height: 16),
        Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            TextButton(
              onPressed: _cancelDownload,
              child: const Text('Cancel'),
            ),
          ],
        ),
      ],
    );
  }

  Widget _buildInstallingStep() {
    final message = Platform.isWindows
        ? 'Launching installer... The app will close shortly.'
        : 'Opening package installer...';

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        const SizedBox(height: 8),
        const CircularProgressIndicator(),
        const SizedBox(height: 16),
        Text(message, style: TextStyle(color: Colors.grey[300])),
        const SizedBox(height: 8),
      ],
    );
  }

  Widget _buildErrorStep() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: Colors.red.withValues(alpha: 0.1),
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: Colors.red.withValues(alpha: 0.3)),
          ),
          child: Text(
            _error ?? 'An unknown error occurred',
            style: TextStyle(color: Colors.red[300], fontSize: 13),
          ),
        ),
        const SizedBox(height: 16),
        Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            TextButton(
              onPressed: _dismiss,
              child: const Text('Close'),
            ),
            const SizedBox(width: 8),
            FilledButton(
              onPressed: _startDownload,
              child: const Text('Retry'),
            ),
          ],
        ),
      ],
    );
  }
}
