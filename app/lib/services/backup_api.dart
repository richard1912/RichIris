import 'api_client.dart';

class BackupApi {
  final ApiClient _client;
  BackupApi(this._client);

  /// Get size estimates for each backup component.
  Future<Map<String, dynamic>> previewSizes() async {
    final resp = await _client.dio.get('/api/backup/preview');
    return resp.data as Map<String, dynamic>;
  }

  /// Start creating a backup archive.
  Future<Map<String, dynamic>> createBackup(
    List<String> components,
    String targetPath,
  ) async {
    final resp = await _client.dio.post('/api/backup/create', data: {
      'components': components,
      'target_path': targetPath,
    });
    return resp.data as Map<String, dynamic>;
  }

  /// Poll backup progress.
  Future<Map<String, dynamic>> getBackupProgress(String backupId) async {
    final resp = await _client.dio.get('/api/backup/$backupId/progress');
    return resp.data as Map<String, dynamic>;
  }

  /// Cancel an in-progress backup.
  Future<void> cancelBackup(String backupId) async {
    await _client.dio.post('/api/backup/$backupId/cancel');
  }

  /// Inspect a .richiris backup file — returns manifest with available components.
  Future<Map<String, dynamic>> inspectBackup(String filePath) async {
    final resp = await _client.dio.post('/api/backup/inspect', data: {
      'file_path': filePath,
    });
    return resp.data as Map<String, dynamic>;
  }

  /// Start restoring from a .richiris backup file.
  Future<Map<String, dynamic>> startRestore(
    String filePath,
    List<String> components,
  ) async {
    final resp = await _client.dio.post('/api/backup/restore', data: {
      'file_path': filePath,
      'components': components,
    });
    return resp.data as Map<String, dynamic>;
  }

  /// Poll restore progress.
  Future<Map<String, dynamic>> getRestoreProgress(String backupId) async {
    final resp = await _client.dio.get('/api/backup/restore/$backupId/progress');
    return resp.data as Map<String, dynamic>;
  }

  /// Cancel an in-progress restore.
  Future<void> cancelRestore(String backupId) async {
    await _client.dio.post('/api/backup/restore/$backupId/cancel');
  }
}
