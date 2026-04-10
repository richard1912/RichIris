import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// A RichIris backend discovered on the LAN.
class DiscoveredBackend {
  final String ip;
  final int port;
  final String? version;

  DiscoveredBackend({required this.ip, required this.port, this.version});

  String get url => 'http://$ip:$port';
}

/// Scans the local /24 subnet(s) for running RichIris backends by hitting
/// `GET /api/health` on every candidate IP and matching `{"app": "richiris"}`.
///
/// Uses `dart:io` `NetworkInterface.list` + `HttpClient` — no external packages.
/// Emits results onto a stream as they arrive so the UI can populate a list
/// incrementally while the scan is still running.
class BackendScanner {
  bool _cancelled = false;

  /// Cancel an in-flight scan. Subsequent probes won't start and in-flight
  /// probes that hit the cancel check will abort immediately.
  void cancel() {
    _cancelled = true;
  }

  /// Scan all private /24 subnets reachable via local network interfaces.
  ///
  /// - [port]: target port on each candidate (default 8700, RichIris default).
  /// - [probeTimeout]: per-request timeout for each probe.
  /// - [concurrency]: max in-flight probes at any time.
  Stream<DiscoveredBackend> scan({
    int port = 8700,
    Duration probeTimeout = const Duration(milliseconds: 600),
    int concurrency = 32,
  }) async* {
    _cancelled = false;

    final subnets = await _findPrivateSubnets();
    if (subnets.isEmpty) return;

    // Build the full list of candidate IPs across all subnets, skipping each
    // interface's own address to avoid probing ourselves.
    final candidates = <_Candidate>[];
    for (final subnet in subnets) {
      for (var host = 1; host <= 254; host++) {
        if (host == subnet.selfHost) continue;
        candidates.add(_Candidate('${subnet.prefix}.$host', port));
      }
    }

    // Fan out probes with a bounded concurrency window. Results stream back
    // via the controller so the UI sees hits as they arrive.
    final controller = StreamController<DiscoveredBackend>();
    var index = 0;
    var inFlight = 0;

    void launchNext() {
      while (!_cancelled && inFlight < concurrency && index < candidates.length) {
        final c = candidates[index++];
        inFlight++;
        _probe(c.ip, c.port, probeTimeout).then((result) {
          if (result != null && !_cancelled) {
            controller.add(result);
          }
        }).whenComplete(() {
          inFlight--;
          if (_cancelled) {
            if (inFlight == 0 && !controller.isClosed) controller.close();
            return;
          }
          if (index >= candidates.length && inFlight == 0) {
            if (!controller.isClosed) controller.close();
          } else {
            launchNext();
          }
        });
      }
    }

    launchNext();

    yield* controller.stream;
  }

  /// Enumerate private-range IPv4 interfaces and derive each unique /24.
  Future<List<_Subnet>> _findPrivateSubnets() async {
    final seen = <String, _Subnet>{};
    try {
      final interfaces = await NetworkInterface.list(
        type: InternetAddressType.IPv4,
        includeLoopback: false,
        includeLinkLocal: false,
      );
      for (final iface in interfaces) {
        for (final addr in iface.addresses) {
          final parts = addr.address.split('.');
          if (parts.length != 4) continue;
          final a = int.tryParse(parts[0]);
          final b = int.tryParse(parts[1]);
          final d = int.tryParse(parts[3]);
          if (a == null || b == null || d == null) continue;
          if (!_isPrivate(a, b)) continue;
          final prefix = '${parts[0]}.${parts[1]}.${parts[2]}';
          seen.putIfAbsent(prefix, () => _Subnet(prefix: prefix, selfHost: d));
        }
      }
    } catch (_) {
      // NetworkInterface enumeration can fail on restricted platforms — just
      // return what we've got (possibly empty).
    }
    return seen.values.toList();
  }

  bool _isPrivate(int a, int b) {
    // 10.0.0.0/8
    if (a == 10) return true;
    // 172.16.0.0/12
    if (a == 172 && b >= 16 && b <= 31) return true;
    // 192.168.0.0/16
    if (a == 192 && b == 168) return true;
    return false;
  }

  /// Probe a single candidate. Returns a [DiscoveredBackend] if the target
  /// responds on `/api/health` with `{"app": "richiris"}`, otherwise null.
  Future<DiscoveredBackend?> _probe(String ip, int port, Duration timeout) async {
    if (_cancelled) return null;
    HttpClient? client;
    try {
      client = HttpClient()
        ..connectionTimeout = timeout
        ..idleTimeout = timeout;
      final req = await client
          .getUrl(Uri.parse('http://$ip:$port/api/health'))
          .timeout(timeout);
      final resp = await req.close().timeout(timeout);
      if (resp.statusCode != 200) return null;
      final body = await resp
          .transform(utf8.decoder)
          .join()
          .timeout(timeout);
      final data = jsonDecode(body);
      if (data is! Map) return null;
      if (data['app'] != 'richiris') return null;
      final version = data['version'] as String?;
      return DiscoveredBackend(ip: ip, port: port, version: version);
    } catch (_) {
      return null;
    } finally {
      try {
        client?.close(force: true);
      } catch (_) {}
    }
  }
}

class _Subnet {
  final String prefix; // "192.168.1"
  final int selfHost; // 42 for 192.168.1.42
  _Subnet({required this.prefix, required this.selfHost});
}

class _Candidate {
  final String ip;
  final int port;
  _Candidate(this.ip, this.port);
}
