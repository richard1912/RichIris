import '../models/storage_stats.dart';
import '../models/system_status.dart';
import 'api_client.dart';

class SystemApi {
  final ApiClient _client;
  SystemApi(this._client);

  Future<SystemStatus> fetchStatus() async {
    final resp = await _client.dio.get('/api/system/status');
    return SystemStatus.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<StorageStats> fetchStorage() async {
    final resp = await _client.dio.get('/api/system/storage');
    return StorageStats.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<int> fetchTzOffsetMs() async {
    try {
      final resp = await _client.dio.get('/api/system/time');
      final serverOffsetMin = resp.data['utc_offset_min'] as int;
      final clientOffsetMin = DateTime.now().timeZoneOffset.inMinutes;
      return (serverOffsetMin - clientOffsetMin) * 60 * 1000;
    } catch (_) {
      return 0;
    }
  }

  Future<RetentionResult> runRetention() async {
    final resp =
        await _client.dio.post('/api/system/retention/run');
    return RetentionResult.fromJson(resp.data as Map<String, dynamic>);
  }

  Future<String> fetchRecentLogs({int minutes = 10}) async {
    final resp = await _client.dio.get(
      '/api/system/logs',
      queryParameters: {'minutes': minutes},
    );
    return resp.data as String;
  }
}
