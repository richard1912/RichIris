import 'package:flutter/material.dart';
import 'package:dio/dio.dart';
import '../services/api_client.dart';

class RtspDiscoverResult {
  final String brand;
  final String mainUrl;
  final String? subUrl;
  final String? codec;
  final String? resolution;

  RtspDiscoverResult({
    required this.brand,
    required this.mainUrl,
    this.subUrl,
    this.codec,
    this.resolution,
  });

  factory RtspDiscoverResult.fromJson(Map<String, dynamic> json) {
    return RtspDiscoverResult(
      brand: json['brand'] as String,
      mainUrl: json['main_url'] as String,
      subUrl: json['sub_url'] as String?,
      codec: json['codec'] as String?,
      resolution: json['resolution'] as String?,
    );
  }
}

/// Result returned when the user picks a discovered stream.
class RtspWizardResult {
  final String mainUrl;
  final String? subUrl;
  final String username;
  final String password;

  RtspWizardResult({
    required this.mainUrl,
    this.subUrl,
    this.username = '',
    this.password = '',
  });
}

/// Dialog that auto-discovers RTSP URLs on a camera by IP address.
class RtspWizardDialog extends StatefulWidget {
  final ApiClient apiClient;
  final String initialIp;
  final String initialUsername;
  final String initialPassword;

  const RtspWizardDialog({
    super.key,
    required this.apiClient,
    this.initialIp = '',
    this.initialUsername = '',
    this.initialPassword = '',
  });

  @override
  State<RtspWizardDialog> createState() => _RtspWizardDialogState();
}

class _RtspWizardDialogState extends State<RtspWizardDialog> {
  late final TextEditingController _ipCtrl;
  late final TextEditingController _userCtrl;
  late final TextEditingController _passCtrl;
  late final TextEditingController _portCtrl;
  bool _scanning = false;
  bool _scanned = false;
  List<RtspDiscoverResult> _results = [];
  String? _error;
  bool _obscurePassword = true;

  @override
  void initState() {
    super.initState();
    _ipCtrl = TextEditingController(text: widget.initialIp);
    _userCtrl = TextEditingController(text: widget.initialUsername);
    _passCtrl = TextEditingController(text: widget.initialPassword);
    _portCtrl = TextEditingController(text: '554');
  }

  @override
  void dispose() {
    _ipCtrl.dispose();
    _userCtrl.dispose();
    _passCtrl.dispose();
    _portCtrl.dispose();
    super.dispose();
  }

  Future<void> _scan() async {
    final ip = _ipCtrl.text.trim();
    if (ip.isEmpty) {
      setState(() => _error = 'Enter an IP address');
      return;
    }

    setState(() {
      _scanning = true;
      _error = null;
      _results = [];
      _scanned = false;
    });

    try {
      final resp = await widget.apiClient.dio.post('/api/cameras/discover', data: {
        'ip': ip,
        'username': _userCtrl.text.trim(),
        'password': _passCtrl.text.trim(),
        'port': int.tryParse(_portCtrl.text.trim()) ?? 554,
      });
      final list = (resp.data as List)
          .map((e) => RtspDiscoverResult.fromJson(e as Map<String, dynamic>))
          .toList();
      if (mounted) {
        setState(() {
          _results = list;
          _scanned = true;
        });
      }
    } on DioException catch (e) {
      if (mounted) {
        setState(() => _error = e.response?.data?['detail']?.toString() ?? e.message ?? 'Discovery failed');
      }
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _scanning = false);
    }
  }

  /// Strip credentials from a URL for display.
  String _stripCreds(String url) {
    final uri = Uri.tryParse(url);
    if (uri == null || uri.userInfo.isEmpty) return url;
    return url.replaceFirst('${uri.userInfo}@', '');
  }

  void _select(RtspDiscoverResult result) {
    Navigator.of(context).pop(RtspWizardResult(
      mainUrl: _stripCreds(result.mainUrl),
      subUrl: result.subUrl != null ? _stripCreds(result.subUrl!) : null,
      username: _userCtrl.text.trim(),
      password: _passCtrl.text.trim(),
    ));
  }

  @override
  Widget build(BuildContext context) {
    return Dialog(
      insetPadding: const EdgeInsets.all(24),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 520, maxHeight: 600),
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  const Icon(Icons.wifi_find, size: 22),
                  const SizedBox(width: 8),
                  const Text('RTSP Auto-Discover',
                      style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
                  const Spacer(),
                  IconButton(
                    icon: const Icon(Icons.close, size: 20),
                    onPressed: () => Navigator.of(context).pop(),
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(),
                  ),
                ],
              ),
              const SizedBox(height: 4),
              Text(
                'Enter the camera IP address and optional credentials. '
                'RichIris will probe common RTSP paths to find working streams.',
                style: TextStyle(fontSize: 12, color: Colors.grey[400]),
              ),
              const SizedBox(height: 14),
              Row(
                children: [
                  Expanded(
                    flex: 3,
                    child: TextField(
                      controller: _ipCtrl,
                      decoration: const InputDecoration(
                        labelText: 'Camera IP',
                        hintText: '192.168.1.100',
                        isDense: true,
                        contentPadding: EdgeInsets.symmetric(horizontal: 10, vertical: 12),
                      ),
                      onSubmitted: (_) => _scan(),
                    ),
                  ),
                  const SizedBox(width: 8),
                  SizedBox(
                    width: 70,
                    child: TextField(
                      controller: _portCtrl,
                      decoration: const InputDecoration(
                        labelText: 'Port',
                        isDense: true,
                        contentPadding: EdgeInsets.symmetric(horizontal: 10, vertical: 12),
                      ),
                      keyboardType: TextInputType.number,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 10),
              Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _userCtrl,
                      decoration: const InputDecoration(
                        labelText: 'Username',
                        hintText: 'admin',
                        isDense: true,
                        contentPadding: EdgeInsets.symmetric(horizontal: 10, vertical: 12),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: TextField(
                      controller: _passCtrl,
                      obscureText: _obscurePassword,
                      decoration: InputDecoration(
                        labelText: 'Password',
                        isDense: true,
                        contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 12),
                        suffixIcon: IconButton(
                          icon: Icon(_obscurePassword ? Icons.visibility_off : Icons.visibility, size: 18),
                          onPressed: () => setState(() => _obscurePassword = !_obscurePassword),
                          padding: EdgeInsets.zero,
                          constraints: const BoxConstraints(),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              SizedBox(
                width: double.infinity,
                child: ElevatedButton.icon(
                  onPressed: _scanning ? null : _scan,
                  icon: _scanning
                      ? const SizedBox(
                          width: 16, height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.search, size: 18),
                  label: Text(_scanning ? 'Scanning...' : 'Scan'),
                ),
              ),
              if (_error != null) ...[
                const SizedBox(height: 8),
                Text(_error!, style: const TextStyle(color: Colors.red, fontSize: 12)),
              ],
              if (_scanned && _results.isEmpty) ...[
                const SizedBox(height: 16),
                Center(
                  child: Column(
                    children: [
                      Icon(Icons.videocam_off, size: 40, color: Colors.grey[600]),
                      const SizedBox(height: 8),
                      Text(
                        'No RTSP streams found at this address.\n'
                        'Check the IP, port, and credentials.',
                        textAlign: TextAlign.center,
                        style: TextStyle(fontSize: 13, color: Colors.grey[500]),
                      ),
                    ],
                  ),
                ),
              ],
              if (_results.isNotEmpty) ...[
                const SizedBox(height: 12),
                Text('Found ${_results.length} stream${_results.length > 1 ? 's' : ''}:',
                    style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w500)),
                const SizedBox(height: 6),
                Flexible(
                  child: ListView.builder(
                    shrinkWrap: true,
                    itemCount: _results.length,
                    itemBuilder: (ctx, i) {
                      final r = _results[i];
                      return Card(
                        margin: const EdgeInsets.only(bottom: 6),
                        child: InkWell(
                          onTap: () => _select(r),
                          borderRadius: BorderRadius.circular(8),
                          child: Padding(
                            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                            child: Row(
                              children: [
                                Expanded(
                                  child: Column(
                                    crossAxisAlignment: CrossAxisAlignment.start,
                                    children: [
                                      Row(
                                        children: [
                                          Container(
                                            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                                            decoration: BoxDecoration(
                                              color: const Color(0xFF3B82F6).withValues(alpha: 0.2),
                                              borderRadius: BorderRadius.circular(4),
                                            ),
                                            child: Text(r.brand,
                                                style: const TextStyle(fontSize: 11, color: Color(0xFF3B82F6), fontWeight: FontWeight.w600)),
                                          ),
                                          if (r.codec != null) ...[
                                            const SizedBox(width: 6),
                                            Text(r.codec!.toUpperCase(),
                                                style: TextStyle(fontSize: 11, color: Colors.grey[400], fontWeight: FontWeight.w500)),
                                          ],
                                          if (r.resolution != null) ...[
                                            const SizedBox(width: 6),
                                            Text(r.resolution!,
                                                style: TextStyle(fontSize: 11, color: Colors.grey[500])),
                                          ],
                                        ],
                                      ),
                                      const SizedBox(height: 4),
                                      Text(
                                        _stripCreds(r.mainUrl),
                                        style: const TextStyle(fontSize: 12, fontFamily: 'monospace'),
                                        overflow: TextOverflow.ellipsis,
                                      ),
                                      if (r.subUrl != null) ...[
                                        const SizedBox(height: 2),
                                        Text(
                                          'Sub: ${_stripCreds(r.subUrl!)}',
                                          style: TextStyle(fontSize: 11, color: Colors.grey[500], fontFamily: 'monospace'),
                                          overflow: TextOverflow.ellipsis,
                                        ),
                                      ],
                                    ],
                                  ),
                                ),
                                const SizedBox(width: 8),
                                const Icon(Icons.check_circle_outline, size: 22, color: Color(0xFF22C55E)),
                              ],
                            ),
                          ),
                        ),
                      );
                    },
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
