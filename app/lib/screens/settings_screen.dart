import 'package:flutter/material.dart';
import '../config/api_config.dart';
import '../services/api_client.dart';

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
            const Text(
              'RichIris NVR Server',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w500),
            ),
            const SizedBox(height: 8),
            Text(
              'Enter the URL of your RichIris server',
              style: TextStyle(color: Colors.grey[500], fontSize: 14),
            ),
            const SizedBox(height: 20),
            TextField(
              controller: _controller,
              decoration: const InputDecoration(
                hintText: 'http://192.168.8.10:8700',
                labelText: 'Server URL',
              ),
              keyboardType: TextInputType.url,
              onSubmitted: (_) => _test(),
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
          ],
        ),
      ),
    );
  }
}
