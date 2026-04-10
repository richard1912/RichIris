/// Models for the LAN camera scan flow. Mirrors the backend pydantic
/// schemas in `backend/app/routers/cameras.py` (CameraScanRequest/Response,
/// CameraScanHit).
class CameraScanHit {
  final String ip;
  final int port;
  final String? serverHeader;
  final String? brandHint;

  CameraScanHit({
    required this.ip,
    required this.port,
    this.serverHeader,
    this.brandHint,
  });

  factory CameraScanHit.fromJson(Map<String, dynamic> json) {
    return CameraScanHit(
      ip: json['ip'] as String,
      port: (json['port'] as num).toInt(),
      serverHeader: json['server_header'] as String?,
      brandHint: json['brand_hint'] as String?,
    );
  }
}

class CameraScanResponse {
  final List<String> subnetsScanned;
  final int hostsProbed;
  final List<CameraScanHit> hits;
  final int elapsedMs;

  CameraScanResponse({
    required this.subnetsScanned,
    required this.hostsProbed,
    required this.hits,
    required this.elapsedMs,
  });

  factory CameraScanResponse.fromJson(Map<String, dynamic> json) {
    return CameraScanResponse(
      subnetsScanned: (json['subnets_scanned'] as List).cast<String>(),
      hostsProbed: (json['hosts_probed'] as num).toInt(),
      hits: (json['hits'] as List)
          .map((e) => CameraScanHit.fromJson(e as Map<String, dynamic>))
          .toList(),
      elapsedMs: (json['elapsed_ms'] as num).toInt(),
    );
  }
}
