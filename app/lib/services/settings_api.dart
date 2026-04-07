import 'api_client.dart';

class SettingsApi {
  final ApiClient _client;
  SettingsApi(this._client);

  /// Fetch all settings grouped by category.
  /// Returns: {category: {key: {value: str, requires_restart: bool}}}
  Future<Map<String, Map<String, dynamic>>> fetchSettings() async {
    final resp = await _client.dio.get('/api/settings');
    final data = resp.data as Map<String, dynamic>;
    final result = <String, Map<String, dynamic>>{};
    for (final entry in data.entries) {
      if (entry.value is Map) {
        result[entry.key] = Map<String, dynamic>.from(entry.value as Map);
      }
    }
    return result;
  }

  /// Update settings. Keys are 'category.key' format.
  /// Returns {settings: ..., restart_required: bool}
  Future<Map<String, dynamic>> updateSettings(Map<String, String> updates) async {
    final resp = await _client.dio.put('/api/settings', data: {
      'settings': updates,
    });
    return resp.data as Map<String, dynamic>;
  }

  // --- Service diagnostics API ---

  /// Fetch backend service diagnostic information.
  Future<Map<String, dynamic>> fetchServiceInfo() async {
    final resp = await _client.dio.get('/api/system/service');
    return resp.data as Map<String, dynamic>;
  }

  // --- Storage migration API ---

  /// Validate a target path for recordings storage.
  Future<Map<String, dynamic>> validateStoragePath(String path) async {
    final resp = await _client.dio.post('/api/storage/validate', data: {
      'path': path,
    });
    return resp.data as Map<String, dynamic>;
  }

  /// Start migrating recordings to a new directory.
  /// mode: "move" or "copy"
  Future<Map<String, dynamic>> startMigration(String targetPath, String mode) async {
    final resp = await _client.dio.post('/api/storage/migrate', data: {
      'target_path': targetPath,
      'mode': mode,
    });
    return resp.data as Map<String, dynamic>;
  }

  /// Poll migration progress.
  Future<Map<String, dynamic>> getMigrationProgress(String migrationId) async {
    final resp = await _client.dio.get('/api/storage/migrate/$migrationId/progress');
    return resp.data as Map<String, dynamic>;
  }

  /// Cancel an in-progress migration.
  Future<void> cancelMigration(String migrationId) async {
    await _client.dio.post('/api/storage/migrate/$migrationId/cancel');
  }

  /// Finalize a completed migration (update settings, restart streams).
  Future<Map<String, dynamic>> finalizeMigration(String migrationId) async {
    final resp = await _client.dio.post('/api/storage/migrate/$migrationId/finalize');
    return resp.data as Map<String, dynamic>;
  }

  /// Change recordings path without migrating files.
  Future<Map<String, dynamic>> updatePathOnly(String path) async {
    final resp = await _client.dio.post('/api/storage/update-path', data: {
      'path': path,
    });
    return resp.data as Map<String, dynamic>;
  }

  // --- Data directory API ---

  /// Get the current data directory info.
  Future<Map<String, dynamic>> fetchDataDir() async {
    final resp = await _client.dio.get('/api/system/data-dir');
    return resp.data as Map<String, dynamic>;
  }

  /// Validate a target path for data directory migration.
  Future<Map<String, dynamic>> validateDataDir(String path) async {
    final resp = await _client.dio.post('/api/system/data-dir/validate', data: {
      'path': path,
    });
    return resp.data as Map<String, dynamic>;
  }

  /// Change data directory. mode: "move", "copy", or "path_only".
  /// Returns {updated, data_dir, restart_required}.
  Future<Map<String, dynamic>> updateDataDir(String path, String mode) async {
    final resp = await _client.dio.post('/api/system/data-dir', data: {
      'path': path,
      'mode': mode,
    });
    return resp.data as Map<String, dynamic>;
  }
}
