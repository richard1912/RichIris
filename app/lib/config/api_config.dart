import 'package:shared_preferences/shared_preferences.dart';

import 'install_flavor.dart';

const kDefaultTimeout = Duration(seconds: 15);

/// SharedPreferences on Windows is keyed per Windows user, not per install
/// directory. A client-only install on the same user profile as a full
/// install would otherwise inherit the full install's saved `localhost:8700`
/// URL and skip the first-run setup flow entirely. Give each flavor its own
/// key so the two can coexist without stepping on each other.
const kServerUrlKey = 'richiris_server_url';
const kServerUrlKeyClient = 'richiris_server_url_client';

const kQualityKey = 'richiris-quality';
const kPlaybackQualityKey = 'richiris-playback-quality';
const kStreamSourceKey = 'richiris-stream-source';
const kSkippedVersionKey = 'richiris-skipped-version';

String _serverUrlKey() =>
    isClientOnlyInstall() ? kServerUrlKeyClient : kServerUrlKey;

Future<String?> getSavedServerUrl() async {
  final prefs = await SharedPreferences.getInstance();
  return prefs.getString(_serverUrlKey());
}

Future<void> saveServerUrl(String url) async {
  final prefs = await SharedPreferences.getInstance();
  await prefs.setString(_serverUrlKey(), url);
}
