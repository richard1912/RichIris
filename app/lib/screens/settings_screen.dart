import 'dart:async';
import 'dart:io' show Platform;
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';
import '../config/api_config.dart';
import '../services/api_client.dart';
import '../services/backend_scanner.dart';

class SettingsScreen extends StatefulWidget {
  final ValueChanged<String> onSaved;
  final String? initialUrl;

  const SettingsScreen({super.key, required this.onSaved, this.initialUrl});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _controller;
  bool _testing = false;
  String? _error;
  bool _success = false;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController(text: widget.initialUrl ?? '');
    if (widget.initialUrl == null) {
      _tryLocalBackend();
    }
  }

  Future<void> _tryLocalBackend() async {
    if (!Platform.isWindows) return;
    const localUrl = 'http://localhost:8700';
    final client = ApiClient(localUrl);
    final ok = await client.testConnection();
    if (!mounted) return;
    if (ok) {
      await saveServerUrl(localUrl);
      widget.onSaved(localUrl);
    } else {
      setState(() => _controller.text = localUrl);
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  String _normalizeUrl(String url) {
    url = url.trim().replaceAll(RegExp(r'/+$'), '');
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      url = 'http://$url';
    }
    return url;
  }

  Future<void> _test() async {
    final url = _normalizeUrl(_controller.text);
    if (url.isEmpty) {
      setState(() => _error = 'Enter a server URL');
      return;
    }
    setState(() {
      _testing = true;
      _error = null;
      _success = false;
    });
    final client = ApiClient(url);
    final ok = await client.testConnection();
    if (!mounted) return;
    setState(() {
      _testing = false;
      if (ok) {
        _success = true;
      } else {
        _error = 'Could not connect to server';
      }
    });
  }

  Future<void> _openScanSheet() async {
    final picked = await showModalBottomSheet<DiscoveredBackend>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => const _ScanBackendsSheet(),
    );
    if (picked == null || !mounted) return;
    setState(() {
      _controller.text = picked.url;
      _error = null;
      _success = false;
    });
    await _test();
  }

  Future<void> _save() async {
    final url = _normalizeUrl(_controller.text);
    if (url.isEmpty) {
      setState(() => _error = 'Enter a server URL');
      return;
    }
    await saveServerUrl(url);
    widget.onSaved(url);
    if (mounted && widget.initialUrl != null) {
      Navigator.of(context).pop();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Server Settings'),
      ),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Image.asset('assets/logo.png', height: 48, filterQuality: FilterQuality.medium),
                const SizedBox(width: 12),
                const Text(
                  'NVR Server',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.w500),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              'Enter the URL of your RichIris server',
              style: TextStyle(color: Colors.grey[500], fontSize: 14),
            ),
            const SizedBox(height: 20),
            TextField(
              controller: _controller,
              decoration: InputDecoration(
                hintText: Platform.isWindows ? 'http://localhost:8700' : 'http://192.168.1.100:8700',
                labelText: 'Server URL',
              ),
              keyboardType: TextInputType.url,
              onSubmitted: (_) => _test(),
            ),
            const SizedBox(height: 12),
            Align(
              alignment: Alignment.centerLeft,
              child: OutlinedButton.icon(
                onPressed: _openScanSheet,
                icon: const Icon(Icons.wifi_find, size: 18),
                label: const Text('Scan network'),
              ),
            ),
            const SizedBox(height: 16),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: Text(_error!, style: const TextStyle(color: Colors.red)),
              ),
            if (_success)
              const Padding(
                padding: EdgeInsets.only(bottom: 12),
                child: Text('Connected successfully!',
                    style: TextStyle(color: Colors.green)),
              ),
            Row(
              children: [
                ElevatedButton(
                  onPressed: _testing ? null : _test,
                  child: _testing
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Text('Test Connection'),
                ),
                const SizedBox(width: 12),
                ElevatedButton(
                  onPressed: _save,
                  child: const Text('Save & Connect'),
                ),
              ],
            ),
            const Spacer(),
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
            const SizedBox(height: 16),
          ],
        ),
      ),
    );
  }
}

class _ScanBackendsSheet extends StatefulWidget {
  const _ScanBackendsSheet();

  @override
  State<_ScanBackendsSheet> createState() => _ScanBackendsSheetState();
}

class _ScanBackendsSheetState extends State<_ScanBackendsSheet> {
  final BackendScanner _scanner = BackendScanner();
  final List<DiscoveredBackend> _results = [];
  StreamSubscription<DiscoveredBackend>? _sub;
  bool _scanning = true;

  @override
  void initState() {
    super.initState();
    _startScan();
  }

  void _startScan() {
    setState(() {
      _scanning = true;
      _results.clear();
    });
    _sub = _scanner.scan().listen(
      (backend) {
        if (!mounted) return;
        // Dedupe by URL in case multiple interfaces reach the same host.
        if (_results.any((r) => r.url == backend.url)) return;
        setState(() => _results.add(backend));
      },
      onDone: () {
        if (!mounted) return;
        setState(() => _scanning = false);
      },
      onError: (_) {
        if (!mounted) return;
        setState(() => _scanning = false);
      },
    );
  }

  @override
  void dispose() {
    _scanner.cancel();
    _sub?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: EdgeInsets.only(
          left: 16,
          right: 16,
          top: 4,
          bottom: MediaQuery.of(context).viewInsets.bottom + 16,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.wifi_find, size: 22),
                const SizedBox(width: 8),
                Text(
                  'Scan for RichIris servers',
                  style: Theme.of(context).textTheme.titleMedium,
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              _scanning
                  ? 'Scanning your network…'
                  : _results.isEmpty
                      ? 'No servers found'
                      : 'Tap a server to connect',
              style: TextStyle(color: Colors.grey[500], fontSize: 13),
            ),
            const SizedBox(height: 12),
            if (_scanning) const LinearProgressIndicator(minHeight: 2),
            const SizedBox(height: 12),
            ConstrainedBox(
              constraints: BoxConstraints(
                maxHeight: MediaQuery.of(context).size.height * 0.45,
              ),
              child: _results.isEmpty && !_scanning
                  ? Padding(
                      padding: const EdgeInsets.symmetric(vertical: 24),
                      child: Center(
                        child: Column(
                          children: [
                            Icon(Icons.search_off,
                                size: 40, color: Colors.grey[600]),
                            const SizedBox(height: 8),
                            Text(
                              'No RichIris backends found.\nMake sure the server is running on the same network.',
                              textAlign: TextAlign.center,
                              style: TextStyle(color: Colors.grey[500]),
                            ),
                          ],
                        ),
                      ),
                    )
                  : ListView.builder(
                      shrinkWrap: true,
                      itemCount: _results.length,
                      itemBuilder: (ctx, i) {
                        final r = _results[i];
                        return ListTile(
                          leading: const Icon(Icons.videocam_outlined),
                          title: Text(r.ip),
                          subtitle: Text(
                            r.version != null
                                ? '${r.url}  ·  v${r.version}'
                                : r.url,
                            style: const TextStyle(fontSize: 12),
                          ),
                          trailing: const Icon(Icons.chevron_right),
                          onTap: () => Navigator.of(context).pop(r),
                        );
                      },
                    ),
            ),
            const SizedBox(height: 8),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                if (!_scanning)
                  TextButton.icon(
                    onPressed: _startScan,
                    icon: const Icon(Icons.refresh, size: 18),
                    label: const Text('Rescan'),
                  ),
                TextButton(
                  onPressed: () => Navigator.of(context).pop(),
                  child: const Text('Close'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
