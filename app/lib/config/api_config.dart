import 'package:shared_preferences/shared_preferences.dart';

const kDefaultTimeout = Duration(seconds: 15);
const kServerUrlKey = 'richiris_server_url';
const kQualityKey = 'richiris-quality';
const kStreamSourceKey = 'richiris-stream-source';

Future<String?> getSavedServerUrl() async {
  final prefs = await SharedPreferences.getInstance();
  return prefs.getString(kServerUrlKey);
}

Future<void> saveServerUrl(String url) async {
  final prefs = await SharedPreferences.getInstance();
  await prefs.setString(kServerUrlKey, url);
}
