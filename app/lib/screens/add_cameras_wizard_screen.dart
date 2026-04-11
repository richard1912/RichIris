import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:dio/dio.dart';

import '../models/camera_scan.dart';
import '../services/api_client.dart';
import '../services/camera_api.dart';
import '../widgets/rtsp_wizard_dialog.dart' show RtspDiscoverResult;

/// Multi-step wizard: scan the LAN for IP cameras, let the user pick which
/// ones to add, auto-resolve their RTSP URLs, then create them all at once.
///
/// Steps:
///   0. Credentials + scan trigger
///   1. Scan results (checklist)
///   2. RTSP URL resolution (pattern picker + manual fallback)
///   3. Name + confirm
///   4. Sequential create with per-camera progress
class AddCamerasWizardScreen extends StatefulWidget {
  final CameraApi cameraApi;
  final ApiClient apiClient;
  final int existingCameraCount;

  const AddCamerasWizardScreen({
    super.key,
    required this.cameraApi,
    required this.apiClient,
    this.existingCameraCount = 0,
  });

  @override
  State<AddCamerasWizardScreen> createState() => _AddCamerasWizardScreenState();
}

class _PendingCamera {
  final String ip;
  final TextEditingController nameCtrl;
  String mainUrl;
  String? subUrl;
  String brand;
  String? resolution;
  String? codec;
  bool include = true;
  bool manual; // user entered URLs themselves

  // Thumbnail preview state (populated async on step 3 entry).
  Uint8List? thumbnailBytes;
  bool thumbnailLoading = false;
  String? thumbnailError;

  _PendingCamera({
    required this.ip,
    required this.nameCtrl,
    required this.mainUrl,
    this.subUrl,
    required this.brand,
    this.resolution,
    this.codec,
    this.manual = false,
  });
}

class _CreateOutcome {
  final String ip;
  final String name;
  final bool success;
  final String? error;
  _CreateOutcome({required this.ip, required this.name, required this.success, this.error});
}

class _AddCamerasWizardScreenState extends State<AddCamerasWizardScreen> {
  int _step = 0;

  // Step 0: credentials + optional subnet override
  final _userCtrl = TextEditingController();
  final _passCtrl = TextEditingController();
  final _subnetsCtrl = TextEditingController();
  bool _obscurePassword = true;

  // Step 1: scan
  bool _scanning = false;
  CameraScanResponse? _scanResponse;
  String? _scanError;
  final Set<String> _selectedIps = {};

  // Step 2: resolve
  bool _discovering = false;
  String? _discoverError;
  Map<String, List<RtspDiscoverResult>> _discoveredByIp = {};
  // For each IP, the index into _discoveredByIp[ip] of the chosen pattern.
  // -1 means "manual override" (user entered URLs below).
  final Map<String, int> _chosenIdx = {};
  // Manual URLs keyed by ip (used when _chosenIdx[ip] == -1).
  final Map<String, TextEditingController> _manualMainCtrls = {};
  final Map<String, TextEditingController> _manualSubCtrls = {};
  final Set<String> _skippedIps = {};

  // Step 3: confirm / name
  late List<_PendingCamera> _pending;

  // Step 4: create
  bool _creating = false;
  int _createdCount = 0;
  final List<_CreateOutcome> _createOutcomes = [];

  @override
  void dispose() {
    _userCtrl.dispose();
    _passCtrl.dispose();
    _subnetsCtrl.dispose();
    for (final c in _manualMainCtrls.values) {
      c.dispose();
    }
    for (final c in _manualSubCtrls.values) {
      c.dispose();
    }
    if (_step >= 3) {
      for (final p in _pending) {
        p.nameCtrl.dispose();
      }
    }
    super.dispose();
  }

  // --- Step 0 → 1: trigger scan -------------------------------------------

  Future<void> _startScan() async {
    setState(() {
      _step = 1;
      _scanning = true;
      _scanError = null;
      _scanResponse = null;
      _selectedIps.clear();
    });
    try {
      final resp = await widget.cameraApi.scan(subnets: _parseSubnetOverrides());
      if (!mounted) return;
      setState(() {
        _scanResponse = resp;
        // Pre-select everything — user can uncheck.
        _selectedIps.addAll(resp.hits.map((h) => h.ip));
      });
    } on DioException catch (e) {
      if (!mounted) return;
      setState(() {
        _scanError = e.response?.data?['detail']?.toString() ?? e.message ?? 'Scan failed';
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _scanError = e.toString());
    } finally {
      if (mounted) setState(() => _scanning = false);
    }
  }

  // --- Step 1 → 2: resolve RTSP URLs --------------------------------------

  Future<void> _startResolve() async {
    if (_selectedIps.isEmpty) return;
    final targets = _selectedIps
        .map((ip) => DiscoverBatchTarget(
              ip: ip,
              username: _userCtrl.text.trim(),
              password: _passCtrl.text.trim(),
            ))
        .toList();

    setState(() {
      _step = 2;
      _discovering = true;
      _discoverError = null;
      _discoveredByIp = {};
      _chosenIdx.clear();
      _skippedIps.clear();
    });

    try {
      final results = await widget.cameraApi.discoverBatch(targets);
      if (!mounted) return;
      // Auto-pick the best match per host: highest resolution (width * height).
      results.forEach((ip, matches) {
        if (matches.isEmpty) {
          _chosenIdx[ip] = -1;
          _manualMainCtrls[ip] = TextEditingController();
          _manualSubCtrls[ip] = TextEditingController();
          return;
        }
        int bestIdx = 0;
        int bestPixels = _pixels(matches[0].resolution);
        for (var i = 1; i < matches.length; i++) {
          final p = _pixels(matches[i].resolution);
          if (p > bestPixels) {
            bestPixels = p;
            bestIdx = i;
          }
        }
        _chosenIdx[ip] = bestIdx;
      });
      setState(() => _discoveredByIp = results);
    } on DioException catch (e) {
      if (!mounted) return;
      setState(() => _discoverError =
          e.response?.data?['detail']?.toString() ?? e.message ?? 'Discovery failed');
    } catch (e) {
      if (!mounted) return;
      setState(() => _discoverError = e.toString());
    } finally {
      if (mounted) setState(() => _discovering = false);
    }
  }

  /// Parse the comma/space/newline separated subnet override text into a
  /// list of `"A.B.C"` /24 prefixes. Accepts either raw prefixes
  /// (`192.168.8`) or full addresses with any octet (`192.168.8.42`,
  /// `192.168.8.0/24`) — we just strip the 4th octet + mask. Returns `null`
  /// when the field is empty so the backend falls back to auto-detection.
  List<String>? _parseSubnetOverrides() {
    final raw = _subnetsCtrl.text.trim();
    if (raw.isEmpty) return null;
    final out = <String>{};
    for (final tok in raw.split(RegExp(r'[\s,]+'))) {
      if (tok.isEmpty) continue;
      final stripped = tok.split('/').first;
      final parts = stripped.split('.');
      if (parts.length < 3) continue;
      final prefix = '${parts[0]}.${parts[1]}.${parts[2]}';
      // Basic sanity check: each octet is 0-255.
      final ok = prefix.split('.').every((p) {
        final n = int.tryParse(p);
        return n != null && n >= 0 && n <= 255;
      });
      if (ok) out.add(prefix);
    }
    return out.isEmpty ? null : out.toList();
  }

  int _pixels(String? resolution) {
    if (resolution == null) return 0;
    final parts = resolution.split('x');
    if (parts.length != 2) return 0;
    final w = int.tryParse(parts[0]) ?? 0;
    final h = int.tryParse(parts[1]) ?? 0;
    return w * h;
  }

  // --- Step 2 → 3: build pending list -------------------------------------

  bool _resolveReady() {
    // Allow proceeding when: every non-skipped IP has either a chosen pattern
    // OR valid manual URLs. All IPs with no matches must be either skipped or
    // filled manually.
    for (final ip in _selectedIps) {
      if (_skippedIps.contains(ip)) continue;
      final idx = _chosenIdx[ip];
      if (idx == null) return false;
      if (idx == -1) {
        final main = _manualMainCtrls[ip]?.text.trim() ?? '';
        if (main.isEmpty) return false;
      }
    }
    // At least one camera must remain.
    return _selectedIps.any((ip) => !_skippedIps.contains(ip));
  }

  void _goToConfirm() {
    final pending = <_PendingCamera>[];
    var counter = widget.existingCameraCount + 1;
    for (final ip in _selectedIps) {
      if (_skippedIps.contains(ip)) continue;
      final idx = _chosenIdx[ip] ?? -1;
      String mainUrl;
      String? subUrl;
      String brand = 'Manual';
      String? resolution;
      String? codec;
      bool manual = false;
      if (idx == -1) {
        mainUrl = _manualMainCtrls[ip]!.text.trim();
        final sub = _manualSubCtrls[ip]!.text.trim();
        subUrl = sub.isEmpty ? null : sub;
        manual = true;
      } else {
        final match = _discoveredByIp[ip]![idx];
        mainUrl = match.mainUrl;
        subUrl = match.subUrl;
        brand = match.brand;
        resolution = match.resolution;
        codec = match.codec;
      }
      pending.add(_PendingCamera(
        ip: ip,
        nameCtrl: TextEditingController(text: 'Camera ${counter++}'),
        mainUrl: mainUrl,
        subUrl: subUrl,
        brand: brand,
        resolution: resolution,
        codec: codec,
        manual: manual,
      ));
    }
    setState(() {
      _pending = pending;
      _step = 3;
    });
    _fetchAllThumbnails();
  }

  /// Kick off a snapshot request for every pending camera. Each completes
  /// independently and the tile re-renders as its bytes arrive. A small pool
  /// of workers caps concurrency so we don't spawn 10+ simultaneous ffmpeg
  /// snapshot processes on the backend.
  Future<void> _fetchAllThumbnails() async {
    const maxConcurrent = 3;
    int nextIdx = 0;

    Future<void> runOne(_PendingCamera p) async {
      if (!mounted) return;
      setState(() {
        p.thumbnailLoading = true;
        p.thumbnailError = null;
      });
      try {
        final bytes = await widget.cameraApi.snapshot(rtspUrl: p.mainUrl, width: 320);
        if (!mounted) return;
        setState(() {
          p.thumbnailBytes = bytes;
          p.thumbnailLoading = false;
        });
      } on DioException catch (e) {
        if (!mounted) return;
        final msg = e.response?.data is Map
            ? (e.response!.data['detail']?.toString() ?? 'Failed')
            : e.message ?? 'Failed';
        setState(() {
          p.thumbnailError = msg;
          p.thumbnailLoading = false;
        });
      } catch (e) {
        if (!mounted) return;
        setState(() {
          p.thumbnailError = e.toString();
          p.thumbnailLoading = false;
        });
      }
    }

    Future<void> worker() async {
      while (mounted && nextIdx < _pending.length) {
        final idx = nextIdx++;
        await runOne(_pending[idx]);
      }
    }

    await Future.wait(List.generate(maxConcurrent, (_) => worker()));
  }

  // --- Step 3 → 4: create cameras -----------------------------------------

  Future<void> _startCreate() async {
    final toCreate = _pending.where((p) => p.include).toList();
    setState(() {
      _step = 4;
      _creating = true;
      _createdCount = 0;
      _createOutcomes.clear();
    });
    for (final p in toCreate) {
      try {
        await widget.cameraApi.create(
          name: p.nameCtrl.text.trim().isEmpty ? 'Camera ${p.ip}' : p.nameCtrl.text.trim(),
          rtspUrl: p.mainUrl,
          subStreamUrl: p.subUrl,
        );
        _createOutcomes.add(_CreateOutcome(ip: p.ip, name: p.nameCtrl.text.trim(), success: true));
      } on DioException catch (e) {
        final msg = e.response?.data?['detail']?.toString() ?? e.message ?? 'Create failed';
        _createOutcomes.add(_CreateOutcome(
          ip: p.ip,
          name: p.nameCtrl.text.trim(),
          success: false,
          error: msg,
        ));
      } catch (e) {
        _createOutcomes.add(_CreateOutcome(
          ip: p.ip,
          name: p.nameCtrl.text.trim(),
          success: false,
          error: e.toString(),
        ));
      }
      if (!mounted) return;
      setState(() => _createdCount = _createOutcomes.length);
    }
    if (!mounted) return;
    setState(() => _creating = false);
  }

  // --- Build ---------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    final body = switch (_step) {
      0 => _buildStep0Credentials(),
      1 => _buildStep1Scan(),
      2 => _buildStep2Resolve(),
      3 => _buildStep3Confirm(),
      _ => _buildStep4Create(),
    };
    return Scaffold(
      appBar: AppBar(
        title: const Text('Scan & Add Cameras'),
        leading: IconButton(
          icon: const Icon(Icons.close),
          onPressed: () => Navigator.of(context).maybePop(),
          tooltip: 'Close',
        ),
      ),
      body: Column(
        children: [
          _buildStepIndicator(),
          const Divider(height: 1),
          Expanded(child: body),
        ],
      ),
    );
  }

  Widget _buildStepIndicator() {
    const labels = ['Credentials', 'Scan', 'Resolve', 'Name', 'Create'];
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
      child: Row(
        children: [
          for (var i = 0; i < labels.length; i++) ...[
            CircleAvatar(
              radius: 14,
              backgroundColor: i <= _step
                  ? const Color(0xFF3B82F6)
                  : const Color(0xFF374151),
              child: Text('${i + 1}',
                  style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600)),
            ),
            const SizedBox(width: 6),
            Text(
              labels[i],
              style: TextStyle(
                fontSize: 12,
                color: i <= _step ? Colors.white : Colors.grey[500],
                fontWeight: i == _step ? FontWeight.w600 : FontWeight.w400,
              ),
            ),
            if (i < labels.length - 1)
              Expanded(
                child: Container(
                  height: 1,
                  margin: const EdgeInsets.symmetric(horizontal: 8),
                  color: i < _step ? const Color(0xFF3B82F6) : const Color(0xFF374151),
                ),
              ),
          ],
        ],
      ),
    );
  }

  // Step 0: credentials -----------------------------------------------------

  Widget _buildStep0Credentials() {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Camera Credentials',
              style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600)),
          const SizedBox(height: 8),
          Text(
            'Most IP cameras share the same login on a deployment. Enter the '
            'credentials used by your cameras — they will be applied to every '
            'camera discovered in the next step. Leave blank if your cameras '
            'allow anonymous RTSP access.',
            style: TextStyle(fontSize: 13, color: Colors.grey[400], height: 1.4),
          ),
          const SizedBox(height: 24),
          ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 420),
            child: Column(
              children: [
                TextField(
                  controller: _userCtrl,
                  decoration: const InputDecoration(
                    labelText: 'Username',
                    isDense: true,
                  ),
                  autofocus: true,
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _passCtrl,
                  obscureText: _obscurePassword,
                  decoration: InputDecoration(
                    labelText: 'Password',
                    isDense: true,
                    suffixIcon: IconButton(
                      icon: Icon(_obscurePassword ? Icons.visibility_off : Icons.visibility,
                          size: 18),
                      onPressed: () => setState(() => _obscurePassword = !_obscurePassword),
                    ),
                  ),
                ),
                const SizedBox(height: 18),
                TextField(
                  controller: _subnetsCtrl,
                  decoration: const InputDecoration(
                    labelText: 'Subnets to scan (optional)',
                    hintText: '192.168.8, 192.168.1',
                    helperText:
                        'Leave blank to auto-detect. Comma or space separated.',
                    isDense: true,
                  ),
                  style: const TextStyle(fontFamily: 'monospace'),
                ),
              ],
            ),
          ),
          const SizedBox(height: 24),
          Row(
            children: [
              ElevatedButton.icon(
                onPressed: _startScan,
                icon: const Icon(Icons.radar, size: 18),
                label: const Text('Scan network'),
              ),
            ],
          ),
          const SizedBox(height: 16),
          Text(
            'By default, the scan auto-detects every private /24 subnet reachable '
            'from the RichIris server and probes port 554 on each host. Override '
            'the subnet list above if your cameras live on a different network '
            'from the server (useful when the backend runs in a NAT\'d VM or '
            'container, or when cameras are on a dedicated VLAN). Cameras '
            'already added to RichIris are automatically filtered out.',
            style: TextStyle(fontSize: 12, color: Colors.grey[500], height: 1.4),
          ),
        ],
      ),
    );
  }

  // Step 1: scan results ----------------------------------------------------

  Widget _buildStep1Scan() {
    return Column(
      children: [
        Expanded(
          child: _scanning
              ? const Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      CircularProgressIndicator(),
                      SizedBox(height: 16),
                      Text('Scanning LAN for cameras…'),
                    ],
                  ),
                )
              : _scanError != null
                  ? Center(
                      child: Padding(
                        padding: const EdgeInsets.all(24),
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            const Icon(Icons.error_outline, size: 40, color: Colors.red),
                            const SizedBox(height: 12),
                            Text(_scanError!,
                                textAlign: TextAlign.center,
                                style: const TextStyle(color: Colors.red)),
                            const SizedBox(height: 16),
                            ElevatedButton(
                              onPressed: _startScan,
                              child: const Text('Retry'),
                            ),
                          ],
                        ),
                      ),
                    )
                  : _buildScanResults(),
        ),
        _buildNavBar(
          leftLabel: 'Back',
          onLeft: _scanning ? null : () => setState(() => _step = 0),
          rightLabel: 'Resolve ${_selectedIps.length} cameras',
          onRight: (_scanning || _selectedIps.isEmpty) ? null : _startResolve,
        ),
      ],
    );
  }

  Widget _buildScanResults() {
    final resp = _scanResponse;
    if (resp == null) return const SizedBox.shrink();
    if (resp.hits.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.search_off, size: 48, color: Colors.grey[600]),
              const SizedBox(height: 12),
              Text(
                'No cameras found.',
                style: TextStyle(fontSize: 16, color: Colors.grey[300]),
              ),
              const SizedBox(height: 4),
              Text(
                resp.subnetsScanned.isEmpty
                    ? 'No private subnets were detected on this server.'
                    : 'Scanned ${resp.hostsProbed} hosts across ${resp.subnetsScanned.join(", ")} in ${(resp.elapsedMs / 1000).toStringAsFixed(1)}s.',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 12, color: Colors.grey[500]),
              ),
              const SizedBox(height: 8),
              Text(
                'Check that your cameras are powered on and reachable from the '
                'RichIris server. Cameras already added are filtered out.',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 12, color: Colors.grey[500]),
              ),
              const SizedBox(height: 16),
              ElevatedButton(onPressed: _startScan, child: const Text('Rescan')),
            ],
          ),
        ),
      );
    }
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 8),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  'Found ${resp.hits.length} camera${resp.hits.length == 1 ? '' : 's'} '
                  'on ${resp.subnetsScanned.join(", ")} (${(resp.elapsedMs / 1000).toStringAsFixed(1)}s)',
                  style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w500),
                ),
              ),
              TextButton(
                onPressed: () => setState(() => _selectedIps.addAll(resp.hits.map((h) => h.ip))),
                child: const Text('Select all'),
              ),
              TextButton(
                onPressed: () => setState(_selectedIps.clear),
                child: const Text('Select none'),
              ),
            ],
          ),
        ),
        Expanded(
          child: ListView.builder(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            itemCount: resp.hits.length,
            itemBuilder: (ctx, i) {
              final hit = resp.hits[i];
              final checked = _selectedIps.contains(hit.ip);
              return CheckboxListTile(
                dense: true,
                value: checked,
                onChanged: (v) => setState(() {
                  if (v == true) {
                    _selectedIps.add(hit.ip);
                  } else {
                    _selectedIps.remove(hit.ip);
                  }
                }),
                title: Text(hit.ip, style: const TextStyle(fontFamily: 'monospace')),
                subtitle: Text(
                  hit.brandHint ?? hit.serverHeader ?? 'RTSP port open',
                  style: TextStyle(fontSize: 11, color: Colors.grey[500]),
                ),
                secondary: hit.brandHint != null
                    ? Container(
                        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                        decoration: BoxDecoration(
                          color: const Color(0xFF3B82F6).withValues(alpha: 0.2),
                          borderRadius: BorderRadius.circular(4),
                        ),
                        child: Text(
                          hit.brandHint!,
                          style: const TextStyle(
                              fontSize: 11,
                              color: Color(0xFF3B82F6),
                              fontWeight: FontWeight.w600),
                        ),
                      )
                    : null,
              );
            },
          ),
        ),
      ],
    );
  }

  // Step 2: resolve ---------------------------------------------------------

  Widget _buildStep2Resolve() {
    return Column(
      children: [
        Expanded(
          child: _discovering
              ? Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const CircularProgressIndicator(),
                      const SizedBox(height: 16),
                      Text('Probing ${_selectedIps.length} camera${_selectedIps.length == 1 ? '' : 's'} for RTSP URLs…'),
                      const SizedBox(height: 4),
                      Text(
                        'This can take up to 30 seconds per camera.',
                        style: TextStyle(fontSize: 12, color: Colors.grey[500]),
                      ),
                    ],
                  ),
                )
              : _discoverError != null
                  ? Center(
                      child: Padding(
                        padding: const EdgeInsets.all(24),
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            const Icon(Icons.error_outline, size: 40, color: Colors.red),
                            const SizedBox(height: 12),
                            Text(_discoverError!,
                                textAlign: TextAlign.center,
                                style: const TextStyle(color: Colors.red)),
                            const SizedBox(height: 16),
                            ElevatedButton(onPressed: _startResolve, child: const Text('Retry')),
                          ],
                        ),
                      ),
                    )
                  : _buildResolveList(),
        ),
        _buildNavBar(
          leftLabel: 'Back',
          onLeft: _discovering ? null : () => setState(() => _step = 1),
          rightLabel: 'Continue',
          onRight: (_discovering || !_resolveReady()) ? null : _goToConfirm,
        ),
      ],
    );
  }

  Widget _buildResolveList() {
    final ips = _selectedIps.toList();
    return ListView.builder(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
      itemCount: ips.length,
      itemBuilder: (ctx, i) {
        final ip = ips[i];
        final matches = _discoveredByIp[ip] ?? const <RtspDiscoverResult>[];
        final skipped = _skippedIps.contains(ip);
        return Card(
          margin: const EdgeInsets.only(bottom: 10),
          color: skipped ? const Color(0xFF1F1F23) : null,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        ip,
                        style: TextStyle(
                          fontFamily: 'monospace',
                          fontWeight: FontWeight.w600,
                          decoration: skipped ? TextDecoration.lineThrough : null,
                          color: skipped ? Colors.grey : null,
                        ),
                      ),
                    ),
                    if (!skipped)
                      TextButton.icon(
                        onPressed: () => setState(() => _skippedIps.add(ip)),
                        icon: const Icon(Icons.block, size: 14),
                        label: const Text('Skip'),
                        style: TextButton.styleFrom(padding: EdgeInsets.zero),
                      )
                    else
                      TextButton.icon(
                        onPressed: () => setState(() => _skippedIps.remove(ip)),
                        icon: const Icon(Icons.undo, size: 14),
                        label: const Text('Include'),
                        style: TextButton.styleFrom(padding: EdgeInsets.zero),
                      ),
                  ],
                ),
                if (skipped)
                  const SizedBox.shrink()
                else if (matches.isEmpty) ...[
                  const SizedBox(height: 6),
                  Text(
                    'No RTSP URL found via auto-probe. Enter it manually or skip.',
                    style: TextStyle(fontSize: 11, color: Colors.orange[300]),
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    controller: _manualMainCtrls[ip],
                    decoration: const InputDecoration(
                      labelText: 'Main RTSP URL',
                      hintText: 'rtsp://user:pass@ip:554/stream1',
                      isDense: true,
                    ),
                    style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
                    onChanged: (_) => setState(() {}),
                  ),
                  const SizedBox(height: 6),
                  TextField(
                    controller: _manualSubCtrls[ip],
                    decoration: const InputDecoration(
                      labelText: 'Sub RTSP URL (optional)',
                      isDense: true,
                    ),
                    style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
                  ),
                ] else ...[
                  const SizedBox(height: 8),
                  for (var j = 0; j < matches.length; j++)
                    RadioListTile<int>(
                      dense: true,
                      contentPadding: EdgeInsets.zero,
                      value: j,
                      groupValue: _chosenIdx[ip],
                      onChanged: (v) => setState(() => _chosenIdx[ip] = v!),
                      title: Row(
                        children: [
                          Container(
                            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                            decoration: BoxDecoration(
                              color: const Color(0xFF3B82F6).withValues(alpha: 0.2),
                              borderRadius: BorderRadius.circular(4),
                            ),
                            child: Text(matches[j].brand,
                                style: const TextStyle(
                                    fontSize: 11,
                                    color: Color(0xFF3B82F6),
                                    fontWeight: FontWeight.w600)),
                          ),
                          if (matches[j].resolution != null) ...[
                            const SizedBox(width: 6),
                            Text(matches[j].resolution!,
                                style: TextStyle(fontSize: 11, color: Colors.grey[400])),
                          ],
                          if (matches[j].codec != null) ...[
                            const SizedBox(width: 6),
                            Text(matches[j].codec!.toUpperCase(),
                                style: TextStyle(fontSize: 11, color: Colors.grey[400])),
                          ],
                        ],
                      ),
                      subtitle: Text(
                        _stripCreds(matches[j].mainUrl),
                        style: const TextStyle(fontFamily: 'monospace', fontSize: 11),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                ],
              ],
            ),
          ),
        );
      },
    );
  }

  String _stripCreds(String url) {
    final uri = Uri.tryParse(url);
    if (uri == null || uri.userInfo.isEmpty) return url;
    return url.replaceFirst('${uri.userInfo}@', '');
  }

  // Step 3: name + confirm --------------------------------------------------

  Widget _buildThumbnailPreview(_PendingCamera p) {
    const width = 160.0;
    const height = 90.0;
    Widget child;
    if (p.thumbnailBytes != null) {
      child = Image.memory(
        p.thumbnailBytes!,
        width: width,
        height: height,
        fit: BoxFit.cover,
        gaplessPlayback: true,
      );
    } else if (p.thumbnailLoading) {
      child = const Center(
        child: SizedBox(
          width: 20,
          height: 20,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      );
    } else if (p.thumbnailError != null) {
      child = Tooltip(
        message: p.thumbnailError!,
        child: const Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.broken_image_outlined, size: 22, color: Colors.orange),
              SizedBox(height: 2),
              Text('no preview',
                  style: TextStyle(fontSize: 10, color: Colors.orange)),
            ],
          ),
        ),
      );
    } else {
      child = Center(
        child: Icon(Icons.videocam_outlined, size: 22, color: Colors.grey[600]),
      );
    }
    return Container(
      width: width,
      height: height,
      decoration: BoxDecoration(
        color: const Color(0xFF0B0B0F),
        border: Border.all(color: const Color(0xFF2A2A2E)),
        borderRadius: BorderRadius.circular(4),
      ),
      clipBehavior: Clip.antiAlias,
      child: child,
    );
  }

  Widget _buildStep3Confirm() {
    final includedCount = _pending.where((p) => p.include).length;
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 8),
          child: Text(
            'Review and name your cameras. Uncheck any you don\'t want to add.',
            style: TextStyle(fontSize: 13, color: Colors.grey[300]),
          ),
        ),
        Expanded(
          child: ListView.builder(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            itemCount: _pending.length,
            itemBuilder: (ctx, i) {
              final p = _pending[i];
              return Card(
                margin: const EdgeInsets.only(bottom: 8),
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Checkbox(
                        value: p.include,
                        onChanged: (v) => setState(() => p.include = v ?? false),
                      ),
                      _buildThumbnailPreview(p),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            TextField(
                              controller: p.nameCtrl,
                              decoration: const InputDecoration(
                                labelText: 'Name',
                                isDense: true,
                              ),
                              enabled: p.include,
                            ),
                            const SizedBox(height: 4),
                            Row(
                              children: [
                                Text(p.ip,
                                    style: const TextStyle(
                                        fontFamily: 'monospace', fontSize: 12)),
                                const SizedBox(width: 8),
                                Container(
                                  padding:
                                      const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                                  decoration: BoxDecoration(
                                    color: const Color(0xFF3B82F6).withValues(alpha: 0.2),
                                    borderRadius: BorderRadius.circular(4),
                                  ),
                                  child: Text(p.brand,
                                      style: const TextStyle(
                                          fontSize: 11,
                                          color: Color(0xFF3B82F6),
                                          fontWeight: FontWeight.w600)),
                                ),
                                if (p.resolution != null) ...[
                                  const SizedBox(width: 6),
                                  Text(p.resolution!,
                                      style: TextStyle(
                                          fontSize: 11, color: Colors.grey[400])),
                                ],
                                if (p.codec != null) ...[
                                  const SizedBox(width: 6),
                                  Text(p.codec!.toUpperCase(),
                                      style: TextStyle(
                                          fontSize: 11, color: Colors.grey[400])),
                                ],
                              ],
                            ),
                            const SizedBox(height: 2),
                            Text(
                              _stripCreds(p.mainUrl),
                              style: TextStyle(
                                fontFamily: 'monospace',
                                fontSize: 11,
                                color: Colors.grey[500],
                              ),
                              overflow: TextOverflow.ellipsis,
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
        ),
        _buildNavBar(
          leftLabel: 'Back',
          onLeft: () => setState(() => _step = 2),
          rightLabel: 'Create $includedCount camera${includedCount == 1 ? '' : 's'}',
          onRight: includedCount == 0 ? null : _startCreate,
        ),
      ],
    );
  }

  // Step 4: create ----------------------------------------------------------

  Widget _buildStep4Create() {
    final total = _pending.where((p) => p.include).length;
    final successCount = _createOutcomes.where((o) => o.success).length;
    final failCount = _createOutcomes.where((o) => !o.success).length;
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 20, 20, 8),
          child: Row(
            children: [
              if (_creating) ...[
                const SizedBox(
                  width: 20,
                  height: 20,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
                const SizedBox(width: 12),
              ] else
                Icon(
                  failCount == 0 ? Icons.check_circle : Icons.warning,
                  color: failCount == 0 ? Colors.green : Colors.orange,
                ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  _creating
                      ? 'Creating camera $_createdCount of $total…'
                      : failCount == 0
                          ? 'Added $successCount camera${successCount == 1 ? '' : 's'} successfully.'
                          : 'Added $successCount of $total. $failCount failed.',
                  style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w500),
                ),
              ),
            ],
          ),
        ),
        Expanded(
          child: ListView.builder(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            itemCount: _createOutcomes.length,
            itemBuilder: (ctx, i) {
              final o = _createOutcomes[i];
              return ListTile(
                dense: true,
                leading: Icon(
                  o.success ? Icons.check_circle : Icons.error,
                  color: o.success ? Colors.green : Colors.red,
                  size: 20,
                ),
                title: Text(
                  o.name.isEmpty ? o.ip : '${o.name} (${o.ip})',
                  style: const TextStyle(fontSize: 13),
                ),
                subtitle: o.success
                    ? null
                    : Text(o.error ?? 'Failed',
                        style: const TextStyle(fontSize: 11, color: Colors.red)),
              );
            },
          ),
        ),
        Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              ElevatedButton(
                onPressed: _creating ? null : () => Navigator.of(context).pop(successCount),
                child: const Text('Done'),
              ),
            ],
          ),
        ),
      ],
    );
  }

  // --- Shared nav bar ------------------------------------------------------

  Widget _buildNavBar({
    required String leftLabel,
    required VoidCallback? onLeft,
    required String rightLabel,
    required VoidCallback? onRight,
  }) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: const BoxDecoration(
        border: Border(top: BorderSide(color: Color(0xFF27272A))),
      ),
      child: Row(
        children: [
          TextButton(onPressed: onLeft, child: Text(leftLabel)),
          const Spacer(),
          ElevatedButton(onPressed: onRight, child: Text(rightLabel)),
        ],
      ),
    );
  }
}
