import 'package:dio/dio.dart';
import '../config/api_config.dart';

class ApiClient {
  late final Dio dio;
  String _baseUrl;

  String get baseUrl => _baseUrl;

  ApiClient(String baseUrl) : _baseUrl = _normalizeUrl(baseUrl) {
    dio = Dio(BaseOptions(
      baseUrl: _baseUrl,
      connectTimeout: kDefaultTimeout,
      receiveTimeout: const Duration(seconds: 30),
      headers: {'Accept': 'application/json'},
    ));
  }

  static String _normalizeUrl(String url) {
    url = url.trim().replaceAll(RegExp(r'/+$'), '');
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      url = 'http://$url';
    }
    return url;
  }

  void updateBaseUrl(String url) {
    _baseUrl = _normalizeUrl(url);
    dio.options.baseUrl = _baseUrl;
  }

  Future<bool> testConnection() async {
    try {
      final resp = await dio.get('/api/health');
      if (resp.statusCode != 200) return false;
      // Verify this is actually a RichIris backend — `/api/health` returns
      // `{"status":"ok","app":"richiris","version":"..."}`. Older backends
      // without the `app` field are still accepted (return true on 200) so
      // users on older versions can still connect after upgrading the client.
      final data = resp.data;
      if (data is Map && data['app'] != null) {
        return data['app'] == 'richiris';
      }
      return true;
    } catch (_) {
      return false;
    }
  }
}
