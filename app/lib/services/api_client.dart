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
      return resp.statusCode == 200;
    } catch (_) {
      return false;
    }
  }
}
