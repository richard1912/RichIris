import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';
import '../services/system_api.dart';

/// Shared "Report a Bug" dialog used from both grid and fullscreen headers.
/// Fetches recent backend logs and exposes basic filtering: a dropdown of
/// logger names (services/modules like `app.services.motion_detector`) and
/// a free-text contains filter. Both apply as AND.
Future<void> showBugReportDialog(
  BuildContext context, {
  required SystemApi systemApi,
  int minutes = 10,
}) async {
  // Fetch logs before opening the dialog so we can parse and build the
  // module list up-front.
  String? rawLogs;
  try {
    rawLogs = await systemApi.fetchRecentLogs(minutes: minutes);
  } catch (e) {
    rawLogs = 'Failed to fetch logs: $e';
  }

  if (!context.mounted) return;

  await showDialog<void>(
    context: context,
    builder: (ctx) => _BugReportDialog(
      rawLogs: rawLogs ?? '',
      minutes: minutes,
    ),
  );
}

class _BugReportDialog extends StatefulWidget {
  final String rawLogs;
  final int minutes;

  const _BugReportDialog({
    required this.rawLogs,
    required this.minutes,
  });

  @override
  State<_BugReportDialog> createState() => _BugReportDialogState();
}

class _BugReportDialogState extends State<_BugReportDialog> {
  static final RegExp _ansi = RegExp(r'\x1B\[[0-9;]*[A-Za-z]');
  static final RegExp _loggerRe = RegExp(r'\[(app\.[\w.]+)\]');

  late final List<String> _cleanLines;
  late final List<String> _allModules; // sorted, unique
  String? _moduleFilter; // null = All
  String _textFilter = '';
  bool _copied = false;

  @override
  void initState() {
    super.initState();
    final stripped = widget.rawLogs.replaceAll(_ansi, '');
    _cleanLines = stripped.split('\n');
    final seen = <String>{};
    for (final line in _cleanLines) {
      final m = _loggerRe.firstMatch(line);
      if (m != null) seen.add(m.group(1)!);
    }
    _allModules = seen.toList()..sort();
  }

  List<String> _filteredLines() {
    final text = _textFilter.toLowerCase();
    final mod = _moduleFilter;
    return _cleanLines.where((line) {
      if (mod != null) {
        final m = _loggerRe.firstMatch(line);
        if (m == null || m.group(1) != mod) return false;
      }
      if (text.isNotEmpty && !line.toLowerCase().contains(text)) {
        return false;
      }
      return true;
    }).toList();
  }

  @override
  Widget build(BuildContext context) {
    final filtered = _filteredLines();
    final filteredText = filtered.join('\n');
    return AlertDialog(
      title: const Text('Report a Bug'),
      content: SizedBox(
        width: 720,
        height: 520,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Logs from the last ${widget.minutes} minutes '
              '(${filtered.length}/${_cleanLines.length} lines):',
              style: TextStyle(color: Colors.grey[400], fontSize: 13),
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Expanded(
                  flex: 2,
                  child: DropdownButtonFormField<String?>(
                    initialValue: _moduleFilter,
                    isDense: true,
                    decoration: const InputDecoration(
                      labelText: 'Service / module',
                      border: OutlineInputBorder(),
                      contentPadding:
                          EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                    ),
                    items: [
                      const DropdownMenuItem<String?>(
                        value: null,
                        child: Text('All',
                            style: TextStyle(fontFamily: 'monospace')),
                      ),
                      ..._allModules.map((m) => DropdownMenuItem<String?>(
                            value: m,
                            child: Text(m,
                                style:
                                    const TextStyle(fontFamily: 'monospace')),
                          )),
                    ],
                    onChanged: (v) => setState(() => _moduleFilter = v),
                  ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  flex: 3,
                  child: TextField(
                    decoration: const InputDecoration(
                      labelText: 'Contains',
                      border: OutlineInputBorder(),
                      isDense: true,
                      contentPadding:
                          EdgeInsets.symmetric(horizontal: 10, vertical: 12),
                    ),
                    onChanged: (v) => setState(() => _textFilter = v),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Expanded(
              child: Container(
                padding: const EdgeInsets.all(8),
                decoration: BoxDecoration(
                  color: Colors.black,
                  borderRadius: BorderRadius.circular(6),
                ),
                child: SelectionArea(
                  child: SingleChildScrollView(
                    child: Text(
                      filtered.isEmpty
                          ? 'No log lines match the current filter.'
                          : filteredText,
                      style: const TextStyle(
                        fontFamily: 'monospace',
                        fontSize: 11,
                        color: Color(0xFFCCCCCC),
                      ),
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                ElevatedButton.icon(
                  onPressed: () {
                    Clipboard.setData(ClipboardData(text: filteredText));
                    setState(() => _copied = true);
                  },
                  icon: Icon(_copied ? Icons.check : Icons.copy, size: 16),
                  label: Text(_copied ? 'Copied!' : 'Copy filtered'),
                ),
                const SizedBox(width: 12),
                ElevatedButton.icon(
                  onPressed: () => launchUrl(
                    Uri.parse(
                        'https://github.com/richard1912/RichIris/issues/new'),
                    mode: LaunchMode.externalApplication,
                  ),
                  icon: const Icon(Icons.open_in_new, size: 16),
                  label: const Text('Open GitHub Issues'),
                ),
              ],
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Close'),
        ),
      ],
    );
  }
}
